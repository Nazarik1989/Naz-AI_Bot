"""Process-isolated worker for the review-only Narrative Normalizer provider.

The protocol is line-delimited canonical JSON over inherited stdin/stdout pipes.
No raw provider output or exception is written to either stream.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
from dataclasses import asdict

import narrative_generation as generation
import narrative_normalizer_evidence as evidence
import narrative_normalizer_provider as provider


BOOTSTRAP_VERSION = "normalizer-provider-worker-bootstrap-v1"
READY_VERSION = "normalizer-provider-worker-ready-v1"
REQUEST_VERSION = "normalizer-provider-ipc-request-v1"
RESPONSE_VERSION = "normalizer-provider-ipc-response-v1"
CONTROL_VERSION = "normalizer-provider-ipc-control-v1"
CONTROL_RESPONSE_VERSION = "normalizer-provider-ipc-control-response-v1"

_REQUEST_KEYS = frozenset({
    "schema_version", "run_id", "request_id", "source_identity", "operation", "payload",
})
_CONTROL_KEYS = frozenset({"schema_version", "action", "payload"})
_BOOTSTRAP_KEYS = frozenset({
    "schema_version", "configuration", "secret", "run", "transport_mode", "scripted_replies",
})
_CONFIG_KEYS = frozenset({
    "adapter_version", "base_url", "generation_model", "adjudication_model", "timeout_seconds",
})
_RUN_KEYS = frozenset({
    "adapter_version", "adapter_spec", "local_execution_authorized", "live_provider_authorized",
    "live_environment_value", "authorized_source_identities", "global_call_budget", "timeout_seconds",
    "content_model", "adjudication_model", "trust_key_id", "review_authority_identity", "run_id",
})


class _UnsupportedPayload:
    __slots__ = ()


def _emit(value: dict[str, object]) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def _safe_error(reason: str) -> dict[str, object]:
    safe = reason if reason in provider.PROVIDER_REASON_CODES else provider.PROVIDER_TRANSPORT_FAILED
    return {"schema_version": RESPONSE_VERSION, "status": "error", "reason_code": safe}


def _exact_mapping(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys:
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    return value


def _decode_value(value: object) -> object:
    if type(value) is dict:
        if value == {"__ipc_unsupported__": True}:
            return _UnsupportedPayload()
        if frozenset(value) == {"__ipc_tuple__"} and type(value["__ipc_tuple__"]) is list:
            return tuple(_decode_value(item) for item in value["__ipc_tuple__"])
        return {key: _decode_value(item) for key, item in value.items()}
    if type(value) is list:
        return [_decode_value(item) for item in value]
    return value


def _decode_request(value: object) -> object:
    payload = _exact_mapping(value, frozenset({"request_type", "fields"}))
    request_type = payload["request_type"]
    fields = payload["fields"]
    if request_type == "narrative":
        fields = _exact_mapping(
            fields,
            frozenset({"request_kind", "model", "system_prompt", "user_prompt", "response_schema"}),
        )
        schema = _decode_value(fields["response_schema"])
        if type(schema) is not dict:
            raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
        return generation.NarrativeModelRequest(
            fields["request_kind"], fields["model"], fields["system_prompt"],
            fields["user_prompt"], schema,
        )
    if request_type == "evidence":
        fields = _exact_mapping(
            fields,
            frozenset({"request_kind", "model", "payload_json", "response_schema_version"}),
        )
        return evidence.EvidenceModelRequest(
            fields["request_kind"], fields["model"], fields["payload_json"],
            fields["response_schema_version"],
        )
    raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)


def _decode_scripted_reply(value: object) -> object:
    item = _exact_mapping(value, frozenset({"kind", "value"}))
    kind = item["kind"]
    payload = item["value"]
    if kind == "value":
        return _decode_value(payload)
    if kind == "bytes":
        if type(payload) is not str:
            raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
        return payload.encode("utf-8")
    if kind == "iterator":
        return iter(("scripted",))
    if kind == "exception":
        if payload == "TimeoutError":
            return TimeoutError("scripted timeout")
        if payload == "CancelledError":
            return asyncio.CancelledError("scripted cancellation")
        if payload == "KeyboardInterrupt":
            return KeyboardInterrupt("scripted cancellation")
        if payload == "SystemExit":
            return SystemExit("scripted cancellation")
        if payload == "GeneratorExit":
            return GeneratorExit("scripted cancellation")
        return RuntimeError("scripted transport failure")
    if kind == "crash":
        return _CrashMarker()
    raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)


class _CrashMarker:
    __slots__ = ()


class _ScriptedTransport:
    __slots__ = ("_replies", "_calls")

    def __init__(self, replies: tuple[object, ...]):
        self._replies = list(replies)
        self._calls: list[dict[str, object]] = []

    def enqueue(self, replies: tuple[object, ...]) -> None:
        self._replies.extend(replies)

    def calls(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(item) for item in self._calls)

    def complete(self, **kwargs: object) -> object:
        self._calls.append({
            "model": kwargs.get("model"),
            "timeout_seconds": kwargs.get("timeout_seconds"),
            "operation_id": kwargs.get("operation_id"),
            "request_id": kwargs.get("request_id"),
        })
        if not self._replies:
            return '{"ok":true}'
        value = self._replies.pop(0)
        if type(value) is _CrashMarker:
            os._exit(70)
        if isinstance(value, BaseException):
            raise value
        return value


def _bootstrap(value: object):
    root = _exact_mapping(value, _BOOTSTRAP_KEYS)
    if root["schema_version"] != BOOTSTRAP_VERSION:
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    config_value = _exact_mapping(root["configuration"], _CONFIG_KEYS)
    run_value = _exact_mapping(root["run"], _RUN_KEYS)
    sources = run_value["authorized_source_identities"]
    if type(sources) is not list:
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    config = provider.ProviderConfiguration(
        config_value["adapter_version"], config_value["base_url"],
        config_value["generation_model"], config_value["adjudication_model"],
        config_value["timeout_seconds"],
    )
    secret = provider._ProviderSecret(root["secret"])
    capsule = provider._RunCapsule(
        run_value["adapter_version"], run_value["adapter_spec"],
        run_value["local_execution_authorized"], run_value["live_provider_authorized"],
        run_value["live_environment_value"], tuple(sources), run_value["global_call_budget"],
        run_value["timeout_seconds"], run_value["content_model"], run_value["adjudication_model"],
        run_value["trust_key_id"], run_value["review_authority_identity"], run_value["run_id"],
    )
    if (
        config.generation_model != capsule.content_model
        or config.adjudication_model != capsule.adjudication_model
        or config.timeout_seconds != capsule.timeout_seconds
    ):
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    mode = root["transport_mode"]
    if mode == "production":
        if root["scripted_replies"] != []:
            raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
        transport = provider._default_transport_factory(config, secret)
    elif mode == "scripted":
        if type(root["scripted_replies"]) is not list:
            raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
        transport = _ScriptedTransport(tuple(
            _decode_scripted_reply(item) for item in root["scripted_replies"]
        ))
    else:
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    return config, secret, capsule, provider._ProviderRunState(capsule, secret), transport


def _expected_model(config: provider.ProviderConfiguration, operation: str) -> str:
    if operation in {"generation", "repair", "evidence_extraction"}:
        return config.generation_model
    if operation in {"adjudication", "evidence_adjudication"}:
        return config.adjudication_model
    raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)


class _ReplayEntry:
    __slots__ = ("request_digest", "status", "terminal_response")

    def __init__(self, request_digest: str):
        self.request_digest = request_digest
        self.status = "reserved"
        self.terminal_response: str | None = None


class _RequestCoordinator:
    """Worker-only request replay registry and atomic transport admission owner."""

    __slots__ = ("_state", "_entries", "_condition", "_message_count")

    def __init__(self, state: provider._ProviderRunState):
        if type(state) is not provider._ProviderRunState:
            raise TypeError("request coordinator")
        self._state = state
        self._entries: dict[str, _ReplayEntry] = {}
        self._condition = threading.Condition(threading.Lock())
        self._message_count = 0

    def observe_message(self) -> None:
        with self._condition:
            self._message_count += 1

    def message_count(self) -> int:
        with self._condition:
            return self._message_count

    @staticmethod
    def _conflict(request_id: str) -> dict[str, object]:
        return {
            "schema_version": RESPONSE_VERSION,
            "request_id": request_id,
            "status": "error",
            "reason_code": provider.PROVIDER_REQUEST_CONFLICT,
        }

    @staticmethod
    def _decode_terminal(entry: _ReplayEntry) -> dict[str, object]:
        if entry.status != "terminal" or type(entry.terminal_response) is not str:
            raise provider.NormalizerProviderError(provider.PROVIDER_TRANSPORT_FAILED)
        value = json.loads(entry.terminal_response)
        if type(value) is not dict:
            raise provider.NormalizerProviderError(provider.PROVIDER_TRANSPORT_FAILED)
        return value

    def _existing_locked(
        self, request_id: str, request_digest: str
    ) -> dict[str, object] | None:
        entry = self._entries.get(request_id)
        if entry is None:
            return None
        if entry.request_digest != request_digest:
            return self._conflict(request_id)
        while entry.status == "reserved":
            self._condition.wait()
        return self._decode_terminal(entry)

    def replay(
        self, request_id: str, request_digest: str
    ) -> dict[str, object] | None:
        with self._condition:
            return self._existing_locked(request_id, request_digest)

    def begin(
        self,
        request_id: str,
        request_digest: str,
        request: provider.AuthorizedProviderRequest,
        *,
        model: str,
        timeout_seconds: int,
        payload_digest: str,
    ) -> tuple[dict[str, object] | None, object | None, object | None]:
        with self._condition:
            existing = self._existing_locked(request_id, request_digest)
            if existing is not None:
                return existing, None, None
            entry = _ReplayEntry(request_digest)
            self._entries[request_id] = entry
            try:
                permit, record = self._state._begin(
                    request,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    request_digest=payload_digest,
                    request_id=request_id,
                )
            except provider.NormalizerProviderError as error:
                response = {
                    "schema_version": RESPONSE_VERSION,
                    "request_id": request_id,
                    "status": "error",
                    "reason_code": error.reason_code,
                }
                self._complete_locked(entry, response)
                return response, None, None
            return None, permit, record

    def finish(
        self, request_id: str, request_digest: str, response: dict[str, object]
    ) -> None:
        with self._condition:
            entry = self._entries.get(request_id)
            if (
                entry is None
                or entry.request_digest != request_digest
                or entry.status != "reserved"
            ):
                raise provider.NormalizerProviderError(provider.PROVIDER_TRANSPORT_FAILED)
            self._complete_locked(entry, response)

    def finish_without_transport(
        self, request_id: str, request_digest: str, response: dict[str, object]
    ) -> dict[str, object]:
        with self._condition:
            existing = self._existing_locked(request_id, request_digest)
            if existing is not None:
                return existing
            entry = _ReplayEntry(request_digest)
            self._entries[request_id] = entry
            self._complete_locked(entry, response)
            return json.loads(entry.terminal_response or "{}")

    def _complete_locked(
        self, entry: _ReplayEntry, response: dict[str, object]
    ) -> None:
        entry.terminal_response = json.dumps(
            response,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        entry.status = "terminal"
        self._condition.notify_all()


def _handle_request(
    message: object,
    config: provider.ProviderConfiguration,
    secret: provider._ProviderSecret,
    capsule: provider._RunCapsule,
    state: provider._ProviderRunState,
    transport: object,
    coordinator: _RequestCoordinator,
) -> dict[str, object]:
    coordinator.observe_message()
    item = _exact_mapping(message, _REQUEST_KEYS)
    request_id = item["request_id"]
    if type(request_id) is not str or provider._HEX24.fullmatch(request_id) is None:
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    request_digest = hashlib.sha256(provider._canonical(item)).hexdigest()
    replay = coordinator.replay(request_id, request_digest)
    if replay is not None:
        return replay
    if (
        item["schema_version"] != REQUEST_VERSION
        or type(item["run_id"]) is not str
        or item["run_id"] != capsule.run_id
        or type(item["source_identity"]) is not str
        or item["source_identity"] not in capsule.authorized_source_identities
        or type(item["operation"]) is not str
        or provider._OPERATION.fullmatch(item["operation"]) is None
    ):
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    request = _decode_request(item["payload"])
    operation, requested_model, messages, response_format, digest_payload = provider._request_payload(request)
    if operation != item["operation"] or type(requested_model) is not str:
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    expected_model = _expected_model(config, operation)
    if requested_model != expected_model:
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    transport_payload = {
        "model": expected_model,
        "messages": messages,
        "response_format": response_format,
        "temperature": 0,
        "stream": False,
        "timeout_seconds": config.timeout_seconds,
    }
    provider._assert_private_payload(transport_payload, secret_values=(secret.reveal(),))
    authorized = provider.AuthorizedProviderRequest(
        capsule.run_id, item["source_identity"], operation, request
    )
    digest = hashlib.sha256(provider._canonical(digest_payload)).hexdigest()
    cached, permit, record = coordinator.begin(
        request_id,
        request_digest,
        authorized,
        model=expected_model,
        timeout_seconds=config.timeout_seconds,
        payload_digest=digest,
    )
    if cached is not None:
        return cached
    if permit is None or record is None:
        raise provider.NormalizerProviderError(provider.PROVIDER_TRANSPORT_FAILED)
    outcome = provider.PROVIDER_TRANSPORT_FAILED
    response_digest: str | None = None
    error: provider.NormalizerProviderError | None = None
    result: object = None
    base_exception: str | None = None
    try:
        result = transport.complete(
            model=expected_model, messages=messages, response_format=response_format,
            timeout_seconds=config.timeout_seconds, operation_id=record.operation_id,
            request_id=record.request_id,
        )
        result = provider._strict_response(result)
        provider._assert_private_payload(result, secret_values=(secret.reveal(),))
        response_digest = hashlib.sha256(provider._canonical(result)).hexdigest()
        outcome = "completed"
    except TimeoutError:
        outcome = provider.PROVIDER_TIMEOUT
        error = provider.NormalizerProviderError(provider.PROVIDER_TIMEOUT)
    except provider.NormalizerProviderError as caught:
        outcome = caught.reason_code
        error = provider.NormalizerProviderError(caught.reason_code)
    except asyncio.CancelledError:
        outcome = provider.PROVIDER_CANCELLED
        base_exception = "CancelledError"
    except KeyboardInterrupt:
        outcome = provider.PROVIDER_CANCELLED
        base_exception = "KeyboardInterrupt"
    except SystemExit:
        outcome = provider.PROVIDER_CANCELLED
        base_exception = "SystemExit"
    except GeneratorExit:
        outcome = provider.PROVIDER_CANCELLED
        base_exception = "GeneratorExit"
    except Exception:
        outcome = provider.PROVIDER_TRANSPORT_FAILED
        error = provider.NormalizerProviderError(provider.PROVIDER_TRANSPORT_FAILED)
    finally:
        state._finish(permit, outcome=outcome, response_digest=response_digest)
    base = {
        "schema_version": RESPONSE_VERSION,
        "request_id": request_id,
    }
    if base_exception is not None:
        response = {**base, "status": "base_exception", "exception_type": base_exception}
    elif error is not None:
        response = {**base, "status": "error", "reason_code": error.reason_code}
    else:
        response = {**base, "status": "ok", "result": result}
    coordinator.finish(request_id, request_digest, response)
    return response


def _record_payload(item: provider._ProviderCallRecord) -> dict[str, object]:
    return asdict(item)


def _handle_control(
    message: object,
    state: provider._ProviderRunState,
    transport: object,
    coordinator: _RequestCoordinator,
):
    item = _exact_mapping(message, _CONTROL_KEYS)
    if item["schema_version"] != CONTROL_VERSION or type(item["action"]) is not str:
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    action = item["action"]
    payload = item["payload"]
    if action == "ledger" and payload is None:
        result: object = [_record_payload(record) for record in state._snapshot()]
    elif action == "calls" and payload is None and type(transport) is _ScriptedTransport:
        result = list(transport.calls())
    elif action == "messages" and payload is None:
        result = coordinator.message_count()
    elif action == "enqueue" and type(payload) is list and type(transport) is _ScriptedTransport:
        transport.enqueue(tuple(_decode_scripted_reply(value) for value in payload))
        result = True
    elif action == "shutdown" and payload is None:
        result = "shutdown"
    elif action == "crash" and payload is None:
        os._exit(71)
    else:
        raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
    return {
        "schema_version": CONTROL_RESPONSE_VERSION,
        "status": "ok",
        "action": action,
        "result": result,
    }


def main() -> int:
    try:
        first = sys.stdin.readline()
        if not first or len(first) > 2_000_000:
            return 2
        config, secret, capsule, state, transport = _bootstrap(json.loads(first))
        coordinator = _RequestCoordinator(state)
        _emit({"schema_version": READY_VERSION, "status": "ready", "run_id": capsule.run_id})
    except BaseException:
        _emit({
            "schema_version": READY_VERSION,
            "status": "error",
            "reason_code": provider.PROVIDER_CONFIGURATION_INVALID,
        })
        return 2
    while True:
        line = sys.stdin.readline()
        if not line:
            return 0
        try:
            if len(line) > 2_000_000:
                raise provider.NormalizerProviderError(provider.PROVIDER_CONFIGURATION_INVALID)
            message = json.loads(line)
            if type(message) is dict and message.get("schema_version") == CONTROL_VERSION:
                response = _handle_control(message, state, transport, coordinator)
                _emit(response)
                if message.get("action") == "shutdown":
                    return 0
            else:
                _emit(_handle_request(
                    message, config, secret, capsule, state, transport, coordinator
                ))
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            return 3
        except provider.NormalizerProviderError as error:
            _emit(_safe_error(error.reason_code))
        except Exception:
            _emit(_safe_error(provider.PROVIDER_CONFIGURATION_INVALID))


if __name__ == "__main__":
    raise SystemExit(main())
