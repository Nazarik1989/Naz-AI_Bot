"""Dormant production provider adapter for the review-only Normalizer.

Importing this module is deliberately inert: environment values are not read,
the OpenAI SDK is not imported, and no client or transport is constructed until
``production_adapter_factory`` is explicitly called after the CLI live gates.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import weakref
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Protocol
from urllib.parse import urlsplit

import narrative_generation as generation
import narrative_normalizer as normalizer
import narrative_normalizer_evidence as evidence
import narrative_normalizer_review_state as review_state
import narrative_normalizer_trust as trust
import narrative_translator as contract


NORMALIZER_PRODUCTION_ADAPTER_VERSION = "normalizer-production-provider-adapter-v1"
PRODUCTION_ADAPTER_SPEC = "narrative_normalizer_provider:production_adapter_factory"
LIVE_ENV = "NARRATIVE_NORMALIZER_LIVE"
ADAPTER_VERSION_ENV = "NARRATIVE_NORMALIZER_ADAPTER_VERSION"
TIMEOUT_ENV = "NARRATIVE_NORMALIZER_MODEL_TIMEOUT_SECONDS"
API_KEY_ENV = "OPENAI_API_KEY"
BASE_URL_ENV = "OPENAI_BASE_URL"
GENERATION_MODEL_ENV = "CONTENT_MODEL_NAME"
ADJUDICATION_MODEL_ENV = "MODEL_NAME"

DEFAULT_TIMEOUT_SECONDS = 120
MIN_TIMEOUT_SECONDS = 10
MAX_TIMEOUT_SECONDS = 300
MAX_PROVIDER_CALLS = 25
FIRST_FIVE_SOURCE_COUNT = 5
WORKER_STARTUP_TIMEOUT_SECONDS = 10
WORKER_BOOTSTRAP_VERSION = "normalizer-provider-worker-bootstrap-v1"
WORKER_READY_VERSION = "normalizer-provider-worker-ready-v1"
IPC_REQUEST_VERSION = "normalizer-provider-ipc-request-v1"
IPC_RESPONSE_VERSION = "normalizer-provider-ipc-response-v1"
IPC_CONTROL_VERSION = "normalizer-provider-ipc-control-v1"
IPC_CONTROL_RESPONSE_VERSION = "normalizer-provider-ipc-control-response-v1"
_IPC_REQUEST_KEYS = frozenset({
    "schema_version", "run_id", "request_id", "source_identity", "operation", "payload",
})
_IPC_CONTROL_KEYS = frozenset({"schema_version", "action", "payload"})
_IPC_CONTROL_ACTIONS = frozenset({"ledger", "calls", "messages", "enqueue", "shutdown", "crash"})

PROVIDER_DISABLED = "normalizer_provider_disabled"
PROVIDER_CONFIGURATION_INVALID = "normalizer_provider_configuration_invalid"
PROVIDER_TIMEOUT = "normalizer_provider_timeout"
PROVIDER_TRANSPORT_FAILED = "normalizer_provider_transport_failed"
PROVIDER_RESPONSE_INVALID = "normalizer_provider_response_invalid"
PROVIDER_CANCELLED = "normalizer_provider_cancelled"
PROVIDER_BUDGET_EXCEEDED = "normalizer_provider_budget_exceeded"
PROVIDER_REQUEST_CONFLICT = "normalizer_provider_request_conflict"
PROVIDER_REASON_CODES = frozenset({
    PROVIDER_DISABLED,
    PROVIDER_CONFIGURATION_INVALID,
    PROVIDER_TIMEOUT,
    PROVIDER_TRANSPORT_FAILED,
    PROVIDER_RESPONSE_INVALID,
    PROVIDER_CANCELLED,
    PROVIDER_BUDGET_EXCEEDED,
    PROVIDER_REQUEST_CONFLICT,
})

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_OPERATION = re.compile(r"(?:evidence_extraction|evidence_adjudication|generation|adjudication|repair)\Z")
_HEX24 = re.compile(r"[0-9a-f]{24}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_CREDENTIAL_MARKER = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer\s|password|credential|"
    r"narrative_normalizer_trust_key|naz_ai_bot\.sqlite3|"
    r"review-authority|registry\.json|sk-[A-Za-z0-9_-]{8,})"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*\S+"
)
_ENV_ASSIGNMENT = re.compile(r"(?m)(?:^|\n)\s*[A-Z][A-Z0-9_]{1,63}\s*=\s*[^\n]+(?:\n|$)")
_ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|\\\\[^\\/\s]+[\\/][^\s]+|file:///(?:[^\s]+)|"
    r"(?<![A-Za-z0-9_:/])/(?!/)[^\s\"'<>\]\)]+|(?<![A-Za-z0-9_])~[\\/])"
)
_LOG = logging.getLogger(__name__)


class NormalizerProviderError(RuntimeError):
    """Privacy-safe provider error containing only a stable reason code."""

    def __init__(self, reason_code: str):
        safe = reason_code if reason_code in PROVIDER_REASON_CODES else PROVIDER_TRANSPORT_FAILED
        self.reason_code = safe
        super().__init__(safe)


def _raise(reason_code: str) -> None:
    raise NormalizerProviderError(reason_code) from None


def _plain_env(env: Mapping[str, str], name: str, *, secret: bool = False) -> str:
    value = env.get(name)
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    if not secret and _MODEL.fullmatch(value) is None:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    return value


def _timeout(value: object) -> int:
    if type(value) is int:
        parsed = value
    elif type(value) is str and re.fullmatch(r"[1-9][0-9]*", value) is not None:
        parsed = int(value)
    else:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    if not MIN_TIMEOUT_SECONDS <= parsed <= MAX_TIMEOUT_SECONDS:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    return parsed


def _base_url(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or value.endswith("/"):
        _raise(PROVIDER_CONFIGURATION_INVALID)
    try:
        parsed = urlsplit(value)
    except ValueError:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        _raise(PROVIDER_CONFIGURATION_INVALID)
    return value


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    adapter_version: str
    base_url: str
    generation_model: str
    adjudication_model: str
    timeout_seconds: int
    max_retries: int = 0
    max_calls: int = MAX_PROVIDER_CALLS

    def __post_init__(self) -> None:
        if type(self.adapter_version) is not str or self.adapter_version != NORMALIZER_PRODUCTION_ADAPTER_VERSION:
            raise TypeError("adapter_version")
        _base_url(self.base_url)
        for value in (self.generation_model, self.adjudication_model):
            if type(value) is not str or _MODEL.fullmatch(value) is None:
                raise TypeError("model")
        if self.generation_model == self.adjudication_model:
            raise TypeError("model")
        if _timeout(self.timeout_seconds) != self.timeout_seconds:
            raise TypeError("timeout_seconds")
        if type(self.max_retries) is not int or self.max_retries != 0:
            raise TypeError("max_retries")
        if type(self.max_calls) is not int or self.max_calls != MAX_PROVIDER_CALLS:
            raise TypeError("max_calls")

    def safe_summary(self, *, selected_source_count: int) -> dict[str, object]:
        if type(selected_source_count) is not int or selected_source_count != FIRST_FIVE_SOURCE_COUNT:
            _raise(PROVIDER_CONFIGURATION_INVALID)
        return {
            "adapter_version": self.adapter_version,
            "models": {
                "evidence_extraction": self.generation_model,
                "story_generation": self.generation_model,
                "story_repair": self.generation_model,
                "evidence_adjudication": self.adjudication_model,
                "story_adjudication": self.adjudication_model,
            },
            "timeout_seconds": self.timeout_seconds,
            "retry_count": 0,
            "maximum_model_call_budget": self.max_calls,
            "selected_source_count": selected_source_count,
            "calculated_maximum_calls": selected_source_count * 5,
            "live_gate_enabled": True,
            "approval_enabled": False,
            "ready_manifest_enabled": False,
            "reels_actions_enabled": False,
        }


class _ProviderSecret:
    __slots__ = ("__value",)

    def __init__(self, value: str):
        if (
            type(value) is not str
            or not value
            or len(value) > 4096
            or value != value.strip()
            or any(ord(character) < 32 for character in value)
        ):
            _raise(PROVIDER_CONFIGURATION_INVALID)
        object.__setattr__(self, "_ProviderSecret__value", value)

    def reveal(self) -> str:
        return self.__value

    def __repr__(self) -> str:
        return "ProviderSecret(<redacted>)"

    __str__ = __repr__


def _load_configuration(env: Mapping[str, str]) -> tuple[ProviderConfiguration, _ProviderSecret]:
    try:
        if not isinstance(env, Mapping):
            _raise(PROVIDER_CONFIGURATION_INVALID)
        live_value = env.get(LIVE_ENV)
        if type(live_value) is not str or live_value != "1":
            _raise(PROVIDER_DISABLED)
        adapter_version = env.get(ADAPTER_VERSION_ENV)
        if (
            type(adapter_version) is not str
            or adapter_version != NORMALIZER_PRODUCTION_ADAPTER_VERSION
        ):
            _raise(PROVIDER_CONFIGURATION_INVALID)
        secret = _ProviderSecret(_plain_env(env, API_KEY_ENV, secret=True))
        config = ProviderConfiguration(
            adapter_version=NORMALIZER_PRODUCTION_ADAPTER_VERSION,
            base_url=_base_url(env.get(BASE_URL_ENV)),
            generation_model=_plain_env(env, GENERATION_MODEL_ENV),
            adjudication_model=_plain_env(env, ADJUDICATION_MODEL_ENV),
            timeout_seconds=_timeout(env.get(TIMEOUT_ENV, str(DEFAULT_TIMEOUT_SECONDS))),
        )
        return config, secret
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except NormalizerProviderError:
        raise
    except Exception:
        _raise(PROVIDER_CONFIGURATION_INVALID)


class _AuthorizationConsumption:
    __slots__ = ("__config", "__secret", "__consumed", "__lock")

    def __init__(self, config: ProviderConfiguration, secret: _ProviderSecret):
        if type(config) is not ProviderConfiguration or type(secret) is not _ProviderSecret:
            raise TypeError("authorization")
        object.__setattr__(self, "_AuthorizationConsumption__config", config)
        object.__setattr__(self, "_AuthorizationConsumption__secret", secret)
        object.__setattr__(self, "_AuthorizationConsumption__consumed", False)
        object.__setattr__(self, "_AuthorizationConsumption__lock", threading.Lock())

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("authorization consumption is immutable")

    def __repr__(self) -> str:
        return "AuthorizationConsumption(<redacted>)"

    def consume(self) -> tuple[ProviderConfiguration, _ProviderSecret]:
        with self.__lock:
            if self.__consumed:
                _raise(PROVIDER_CONFIGURATION_INVALID)
            object.__setattr__(self, "_AuthorizationConsumption__consumed", True)
            return self.__config, self.__secret


@dataclass(frozen=True, slots=True)
class LiveProviderRunAuthorization:
    adapter_version: str
    adapter_spec: str
    local_execution_authorized: bool
    live_provider_authorized: bool
    live_environment_value: str
    authorized_source_identities: tuple[str, ...]
    global_call_budget: int
    timeout_seconds: int
    content_model: str
    adjudication_model: str
    trust_key_id: str
    review_authority_identity: str
    run_id: str
    _consumption: _AuthorizationConsumption

    def __post_init__(self) -> None:
        invalid = (
            type(self.adapter_version) is not str
            or self.adapter_version != NORMALIZER_PRODUCTION_ADAPTER_VERSION
            or type(self.adapter_spec) is not str
            or self.adapter_spec != PRODUCTION_ADAPTER_SPEC
            or type(self.local_execution_authorized) is not bool
            or self.local_execution_authorized is not True
            or type(self.live_provider_authorized) is not bool
            or self.live_provider_authorized is not True
            or type(self.live_environment_value) is not str
            or self.live_environment_value != "1"
            or type(self.authorized_source_identities) is not tuple
            or len(self.authorized_source_identities) != FIRST_FIVE_SOURCE_COUNT
            or len(set(self.authorized_source_identities)) != FIRST_FIVE_SOURCE_COUNT
            or any(type(item) is not str or _HEX64.fullmatch(item) is None for item in self.authorized_source_identities)
            or type(self.global_call_budget) is not int
            or isinstance(self.global_call_budget, bool)
            or self.global_call_budget != MAX_PROVIDER_CALLS
            or type(self.timeout_seconds) is not int
            or isinstance(self.timeout_seconds, bool)
            or not MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS
            or type(self.content_model) is not str
            or _MODEL.fullmatch(self.content_model) is None
            or type(self.adjudication_model) is not str
            or _MODEL.fullmatch(self.adjudication_model) is None
            or self.content_model == self.adjudication_model
            or type(self.trust_key_id) is not str
            or _HEX24.fullmatch(self.trust_key_id) is None
            or type(self.review_authority_identity) is not str
            or _HEX64.fullmatch(self.review_authority_identity) is None
            or type(self.run_id) is not str
            or _HEX24.fullmatch(self.run_id) is None
            or type(self._consumption) is not _AuthorizationConsumption
        )
        if invalid:
            raise TypeError("authorization")

    def safe_summary(self) -> dict[str, object]:
        return {
            "adapter_version": self.adapter_version,
            "models": {
                "evidence_extraction": self.content_model,
                "story_generation": self.content_model,
                "story_repair": self.content_model,
                "evidence_adjudication": self.adjudication_model,
                "story_adjudication": self.adjudication_model,
            },
            "timeout_seconds": self.timeout_seconds,
            "retry_count": 0,
            "maximum_model_call_budget": self.global_call_budget,
            "selected_source_count": len(self.authorized_source_identities),
            "calculated_maximum_calls": self.global_call_budget,
            "live_gate_enabled": True,
            "approval_enabled": False,
            "ready_manifest_enabled": False,
            "reels_actions_enabled": False,
        }


def authorize_live_provider_run(
    *,
    adapter_spec: str,
    local_execution_enabled: bool,
    live_provider_enabled: bool,
    source_identities: tuple[str, ...],
    env: Mapping[str, str],
    trust_service: trust.NarrativeTrustService,
    review_authority_root: Path,
) -> LiveProviderRunAuthorization:
    """Mint one immutable, source-bound, one-shot live-run authorization."""

    if (
        type(adapter_spec) is not str
        or adapter_spec != PRODUCTION_ADAPTER_SPEC
        or type(local_execution_enabled) is not bool
        or local_execution_enabled is not True
        or type(live_provider_enabled) is not bool
        or live_provider_enabled is not True
        or type(source_identities) is not tuple
        or len(source_identities) != FIRST_FIVE_SOURCE_COUNT
        or len(set(source_identities)) != FIRST_FIVE_SOURCE_COUNT
        or any(type(item) is not str or _HEX64.fullmatch(item) is None for item in source_identities)
        or type(trust_service) is not trust.NarrativeTrustService
        or not isinstance(review_authority_root, Path)
    ):
        _raise(PROVIDER_CONFIGURATION_INVALID)
    config, secret = _load_configuration(env)
    try:
        authority = review_state.ReviewStateStore(review_authority_root, trust_service)
        authority_text = str(authority.root)
    except Exception:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    authority_identity = hashlib.sha256(authority_text.encode("utf-8")).hexdigest()
    consumption = _AuthorizationConsumption(config, secret)
    return LiveProviderRunAuthorization(
        NORMALIZER_PRODUCTION_ADAPTER_VERSION,
        PRODUCTION_ADAPTER_SPEC,
        True,
        True,
        "1",
        tuple(source_identities),
        MAX_PROVIDER_CALLS,
        config.timeout_seconds,
        config.generation_model,
        config.adjudication_model,
        trust_service.key_id,
        authority_identity,
        secrets.token_hex(12),
        consumption,
    )


def inspect_live_configuration(
    env: Mapping[str, str] | None = None,
    *,
    selected_source_count: int,
) -> dict[str, object]:
    """Validate live configuration without importing an SDK or creating a client."""

    config, _secret = _load_configuration(os.environ if env is None else env)
    return config.safe_summary(selected_source_count=selected_source_count)


@dataclass(frozen=True, slots=True)
class _RunCapsule:
    adapter_version: str
    adapter_spec: str
    local_execution_authorized: bool
    live_provider_authorized: bool
    live_environment_value: str
    authorized_source_identities: tuple[str, ...]
    global_call_budget: int
    timeout_seconds: int
    content_model: str
    adjudication_model: str
    trust_key_id: str
    review_authority_identity: str
    run_id: str

    def __post_init__(self) -> None:
        invalid = (
            type(self.adapter_version) is not str
            or self.adapter_version != NORMALIZER_PRODUCTION_ADAPTER_VERSION
            or type(self.adapter_spec) is not str
            or self.adapter_spec != PRODUCTION_ADAPTER_SPEC
            or type(self.local_execution_authorized) is not bool
            or self.local_execution_authorized is not True
            or type(self.live_provider_authorized) is not bool
            or self.live_provider_authorized is not True
            or type(self.live_environment_value) is not str
            or self.live_environment_value != "1"
            or type(self.authorized_source_identities) is not tuple
            or len(self.authorized_source_identities) != FIRST_FIVE_SOURCE_COUNT
            or len(set(self.authorized_source_identities)) != FIRST_FIVE_SOURCE_COUNT
            or any(
                type(item) is not str or _HEX64.fullmatch(item) is None
                for item in self.authorized_source_identities
            )
            or type(self.global_call_budget) is not int
            or self.global_call_budget != MAX_PROVIDER_CALLS
            or type(self.timeout_seconds) is not int
            or not MIN_TIMEOUT_SECONDS <= self.timeout_seconds <= MAX_TIMEOUT_SECONDS
            or type(self.content_model) is not str
            or _MODEL.fullmatch(self.content_model) is None
            or type(self.adjudication_model) is not str
            or _MODEL.fullmatch(self.adjudication_model) is None
            or self.content_model == self.adjudication_model
            or type(self.trust_key_id) is not str
            or _HEX24.fullmatch(self.trust_key_id) is None
            or type(self.review_authority_identity) is not str
            or _HEX64.fullmatch(self.review_authority_identity) is None
            or type(self.run_id) is not str
            or _HEX24.fullmatch(self.run_id) is None
        )
        if invalid:
            raise TypeError("run capsule")


def _sealed_run_capsule(
    authorization: LiveProviderRunAuthorization,
) -> _RunCapsule:
    if type(authorization) is not LiveProviderRunAuthorization:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    try:
        return _RunCapsule(
            authorization.adapter_version,
            authorization.adapter_spec,
            authorization.local_execution_authorized,
            authorization.live_provider_authorized,
            authorization.live_environment_value,
            tuple(item for item in authorization.authorized_source_identities),
            authorization.global_call_budget,
            authorization.timeout_seconds,
            authorization.content_model,
            authorization.adjudication_model,
            authorization.trust_key_id,
            authorization.review_authority_identity,
            authorization.run_id,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except Exception:
        _raise(PROVIDER_CONFIGURATION_INVALID)


@dataclass(frozen=True, slots=True)
class _ProviderCallRecord:
    run_id: str
    source_identity: str
    operation_id: str
    request_id: str
    operation: str
    model_id: str
    attempt_number: int
    timeout_seconds: int
    request_digest: str
    response_digest: str | None
    started: bool
    completed: bool
    safe_outcome: str

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not str
            or _HEX24.fullmatch(self.run_id) is None
            or type(self.source_identity) is not str
            or _HEX64.fullmatch(self.source_identity) is None
            or type(self.operation_id) is not str
            or _HEX24.fullmatch(self.operation_id) is None
            or type(self.request_id) is not str
            or _HEX24.fullmatch(self.request_id) is None
        ):
            raise TypeError("identifier")
        if (
            type(self.operation) is not str
            or _OPERATION.fullmatch(self.operation) is None
            or type(self.model_id) is not str
            or _MODEL.fullmatch(self.model_id) is None
        ):
            raise TypeError("operation")
        if type(self.attempt_number) is not int or self.attempt_number != 1:
            raise TypeError("attempt_number")
        _timeout(self.timeout_seconds)
        if type(self.request_digest) is not str or _HEX64.fullmatch(self.request_digest) is None:
            raise TypeError("request_digest")
        if self.response_digest is not None and (
            type(self.response_digest) is not str or _HEX64.fullmatch(self.response_digest) is None
        ):
            raise TypeError("response_digest")
        if type(self.started) is not bool or type(self.completed) is not bool:
            raise TypeError("state")
        if type(self.safe_outcome) is not str or self.safe_outcome not in {
            "started", "completed", *PROVIDER_REASON_CODES
        }:
            raise TypeError("safe_outcome")


@dataclass(frozen=True, slots=True)
class AuthorizedProviderRequest:
    run_id: str
    source_identity: str
    operation: str
    payload: object

    def __post_init__(self) -> None:
        if (
            type(self.run_id) is not str
            or _HEX24.fullmatch(self.run_id) is None
            or type(self.source_identity) is not str
            or _HEX64.fullmatch(self.source_identity) is None
            or type(self.operation) is not str
            or _OPERATION.fullmatch(self.operation) is None
            or type(self.payload) not in {
                generation.NarrativeModelRequest,
                evidence.EvidenceModelRequest,
            }
        ):
            raise TypeError("authorized request")


@dataclass(frozen=True, slots=True)
class _TransportPermit:
    operation_id: str
    source_identity: str
    operation: str
    _run_guard: object


class _ProviderRunState:
    __slots__ = (
        "__capsule", "__secret", "__records", "__operation_counts",
        "__active", "__lock", "__run_guard",
    )

    def __init__(
        self,
        capsule: _RunCapsule,
        secret: _ProviderSecret,
    ):
        if type(capsule) is not _RunCapsule or type(secret) is not _ProviderSecret:
            raise TypeError("run state")
        self.__capsule = capsule
        self.__secret = secret
        self.__records: tuple[_ProviderCallRecord, ...] = ()
        self.__operation_counts: dict[tuple[str, str], int] = {}
        self.__active: dict[str, _TransportPermit] = {}
        self.__lock = threading.Lock()
        self.__run_guard = object()

    def _capsule(self) -> _RunCapsule:
        return self.__capsule

    def _secret(self) -> _ProviderSecret:
        return self.__secret

    def _snapshot(self) -> tuple[_ProviderCallRecord, ...]:
        with self.__lock:
            return tuple(replace(item) for item in self.__records)

    def _begin(
        self,
        request: AuthorizedProviderRequest,
        *,
        model: str,
        timeout_seconds: int,
        request_digest: str,
        request_id: str | None = None,
    ) -> tuple[_TransportPermit, _ProviderCallRecord]:
        if (
            type(request) is not AuthorizedProviderRequest
            or request.run_id != self.__capsule.run_id
            or request.source_identity not in self.__capsule.authorized_source_identities
            or type(model) is not str
            or type(timeout_seconds) is not int
            or isinstance(timeout_seconds, bool)
            or type(request_digest) is not str
            or _HEX64.fullmatch(request_digest) is None
            or (
                request_id is not None
                and (type(request_id) is not str or _HEX24.fullmatch(request_id) is None)
            )
        ):
            _raise(PROVIDER_CONFIGURATION_INVALID)
        key = (request.source_identity, request.operation)
        with self.__lock:
            if len(self.__records) >= self.__capsule.global_call_budget:
                _raise(PROVIDER_BUDGET_EXCEEDED)
            if self.__operation_counts.get(key, 0) >= 1:
                _raise(PROVIDER_BUDGET_EXCEEDED)
            operation_id = secrets.token_hex(12)
            record = _ProviderCallRecord(
                request.run_id,
                request.source_identity,
                operation_id,
                secrets.token_hex(12) if request_id is None else request_id,
                request.operation,
                model,
                1,
                timeout_seconds,
                request_digest,
                None,
                True,
                False,
                "started",
            )
            permit = _TransportPermit(
                operation_id,
                request.source_identity,
                request.operation,
                self.__run_guard,
            )
            self.__operation_counts[key] = 1
            self.__records = (*self.__records, record)
            self.__active[operation_id] = permit
            return permit, record

    def _finish(
        self,
        permit: _TransportPermit,
        *,
        outcome: str,
        response_digest: str | None,
    ) -> None:
        if (
            type(permit) is not _TransportPermit
            or permit._run_guard is not self.__run_guard
            or type(outcome) is not str
            or outcome not in {"completed", *PROVIDER_REASON_CODES}
            or (
                response_digest is not None
                and (type(response_digest) is not str or _HEX64.fullmatch(response_digest) is None)
            )
        ):
            _raise(PROVIDER_TRANSPORT_FAILED)
        with self.__lock:
            active = self.__active.get(permit.operation_id)
            if active is not permit:
                _raise(PROVIDER_TRANSPORT_FAILED)
            indexes = [
                index for index, item in enumerate(self.__records)
                if item.operation_id == permit.operation_id
            ]
            if len(indexes) != 1 or self.__records[indexes[0]].completed:
                _raise(PROVIDER_TRANSPORT_FAILED)
            records = list(self.__records)
            records[indexes[0]] = replace(
                records[indexes[0]],
                response_digest=response_digest,
                completed=True,
                safe_outcome=outcome,
            )
            self.__records = tuple(records)
            del self.__active[permit.operation_id]


class _ReadOnlyCallLedger:
    __slots__ = ("__client_ref",)

    def __init__(self, client: object):
        try:
            client_ref = weakref.ref(client)
        except TypeError:
            raise TypeError("ledger")
        object.__setattr__(self, "_ReadOnlyCallLedger__client_ref", client_ref)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("ledger is immutable")

    def _client(self) -> object:
        client = self.__client_ref()
        if client is None:
            _raise(PROVIDER_CONFIGURATION_INVALID)
        return client

    @property
    def max_calls(self) -> int:
        return MAX_PROVIDER_CALLS

    def snapshot(self) -> tuple[_ProviderCallRecord, ...]:
        client = self._client()
        channel = object.__getattribute__(client, "_ProductionModelClient__channel")
        return channel.ledger_snapshot()


class ProviderTransport(Protocol):
    def complete(
        self,
        *,
        model: str,
        messages: tuple[dict[str, str], ...],
        response_format: dict[str, object],
        timeout_seconds: int,
        operation_id: str,
        request_id: str,
    ) -> object:
        """Perform exactly one non-streaming, no-retry completion."""


class _OpenAITransport:
    __slots__ = ("_client", "_timeout_types")

    def __init__(self, client: object, timeout_types: tuple[type[BaseException], ...]):
        self._client = client
        self._timeout_types = timeout_types

    def complete(
        self,
        *,
        model: str,
        messages: tuple[dict[str, str], ...],
        response_format: dict[str, object],
        timeout_seconds: int,
        operation_id: str,
        request_id: str,
    ) -> object:
        del operation_id, request_id
        timeout = False
        failed = False
        result: object = None
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=list(messages),
                response_format=response_format,
                temperature=0,
                stream=False,
                timeout=timeout_seconds,
            )
            choices = getattr(response, "choices", None)
            if type(choices) is not list or len(choices) != 1:
                failed = True
            else:
                result = getattr(getattr(choices[0], "message", None), "content", None)
        except self._timeout_types:
            timeout = True
        except Exception:
            failed = True
        if timeout:
            _raise(PROVIDER_TIMEOUT)
        if failed:
            _raise(PROVIDER_TRANSPORT_FAILED)
        return result


def _default_transport_factory(config: ProviderConfiguration, secret: _ProviderSecret) -> ProviderTransport:
    """Lazy SDK construction; called only after every live gate is valid."""

    failed = False
    transport: ProviderTransport | None = None
    try:
        openai = importlib.import_module("openai")

        client = openai.OpenAI(
            api_key=secret.reveal(),
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
        timeout_types = tuple(
            value for value in (TimeoutError, getattr(openai, "APITimeoutError", None))
            if isinstance(value, type) and issubclass(value, BaseException)
        )
        transport = _OpenAITransport(client, timeout_types)
    except Exception:
        failed = True
    if failed or type(transport) is not _OpenAITransport:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    return transport


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        _raise(PROVIDER_RESPONSE_INVALID)


def _request_payload(request: object) -> tuple[str, str, tuple[dict[str, str], ...], dict[str, object], object]:
    if type(request) is generation.NarrativeModelRequest:
        operation = request.request_kind
        messages = (
            {"role": "system", "content": request.system_prompt},
            {"role": "user", "content": request.user_prompt},
        )
        schema_name = f"normalizer_{operation}_response"
        response_format: dict[str, object] = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "strict": True, "schema": dict(request.response_schema)},
        }
        digest_payload: object = {
            "operation": operation,
            "model": request.model,
            "system_prompt": request.system_prompt,
            "user_prompt": request.user_prompt,
            "response_schema": dict(request.response_schema),
        }
        return operation, request.model, messages, response_format, digest_payload
    if type(request) is evidence.EvidenceModelRequest:
        operation = request.request_kind
        messages = (
            {"role": "system", "content": f"Return strict JSON for {operation} under {request.response_schema_version}."},
            {"role": "user", "content": request.payload_json},
        )
        response_format = {"type": "json_object"}
        digest_payload = {
            "operation": operation,
            "model": request.model,
            "payload_json": request.payload_json,
            "response_schema_version": request.response_schema_version,
        }
        return operation, request.model, messages, response_format, digest_payload
    _raise(PROVIDER_CONFIGURATION_INVALID)


def _payload_strings(value: object, output: list[str]) -> None:
    if type(value) is dict:
        for key, nested in value.items():
            if type(key) is not str:
                _raise(PROVIDER_CONFIGURATION_INVALID)
            output.append(key)
            _payload_strings(nested, output)
        return
    if type(value) in {list, tuple}:
        for nested in value:
            _payload_strings(nested, output)
        return
    if type(value) is str:
        output.append(value)
        return
    if value is None or type(value) in {bool, int}:
        return
    if type(value) is float and math.isfinite(value):
        return
    _raise(PROVIDER_CONFIGURATION_INVALID)


def _assert_private_payload(value: object, *, secret_values: tuple[str, ...]) -> None:
    if (
        type(secret_values) is not tuple
        or any(type(item) is not str or not item for item in secret_values)
    ):
        _raise(PROVIDER_CONFIGURATION_INVALID)
    strings: list[str] = []
    _payload_strings(value, strings)
    joined = "".join(strings)
    split_secret = False
    for secret in secret_values:
        for cut in range(1, len(secret)):
            prefix = secret[:cut]
            suffix = secret[cut:]
            if any(
                left.endswith(prefix) and any(right.startswith(suffix) for right in strings[index + 1:])
                for index, left in enumerate(strings)
            ):
                split_secret = True
                break
        if split_secret:
            break
    for text in (*strings, joined):
        if (
            _CREDENTIAL_MARKER.search(text)
            or _CREDENTIAL_ASSIGNMENT.search(text)
            or _ENV_ASSIGNMENT.search(text)
            or _ABSOLUTE_PATH.search(text)
            or any(secret in text for secret in secret_values)
        ):
            _raise(PROVIDER_CONFIGURATION_INVALID)
    if split_secret:
        _raise(PROVIDER_CONFIGURATION_INVALID)


def _strict_response(value: object) -> dict[str, object] | str:
    if type(value) is dict:
        _canonical(value)
        return value
    if type(value) is str and value and value == value.strip():
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            _raise(PROVIDER_RESPONSE_INVALID)
        if type(decoded) is not dict:
            _raise(PROVIDER_RESPONSE_INVALID)
        return value
    _raise(PROVIDER_RESPONSE_INVALID)


def _ipc_value(value: object) -> object:
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            return {"__ipc_unsupported__": True}
        return {key: _ipc_value(item) for key, item in value.items()}
    if type(value) is list:
        return [_ipc_value(item) for item in value]
    if type(value) is tuple:
        return {"__ipc_tuple__": [_ipc_value(item) for item in value]}
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    return {"__ipc_unsupported__": True}


def _ipc_request_payload(request: object) -> dict[str, object]:
    if type(request) is generation.NarrativeModelRequest:
        return {
            "request_type": "narrative",
            "fields": {
                "request_kind": request.request_kind,
                "model": request.model,
                "system_prompt": request.system_prompt,
                "user_prompt": request.user_prompt,
                "response_schema": _ipc_value(dict(request.response_schema)),
            },
        }
    if type(request) is evidence.EvidenceModelRequest:
        return {
            "request_type": "evidence",
            "fields": {
                "request_kind": request.request_kind,
                "model": request.model,
                "payload_json": request.payload_json,
                "response_schema_version": request.response_schema_version,
            },
        }
    _raise(PROVIDER_CONFIGURATION_INVALID)


def _scripted_reply_payload(value: object) -> dict[str, object]:
    if isinstance(value, TimeoutError):
        return {"kind": "exception", "value": "TimeoutError"}
    if isinstance(value, asyncio.CancelledError):
        return {"kind": "exception", "value": "CancelledError"}
    if isinstance(value, KeyboardInterrupt):
        return {"kind": "exception", "value": "KeyboardInterrupt"}
    if isinstance(value, SystemExit):
        return {"kind": "exception", "value": "SystemExit"}
    if isinstance(value, GeneratorExit):
        return {"kind": "exception", "value": "GeneratorExit"}
    if isinstance(value, BaseException):
        return {"kind": "exception", "value": "Exception"}
    if type(value) is bytes:
        return {"kind": "bytes", "value": value.decode("utf-8", errors="replace")}
    if type(value).__name__ in {"generator", "list_iterator", "tuple_iterator"}:
        return {"kind": "iterator", "value": None}
    return {"kind": "value", "value": _ipc_value(value)}


def _validate_outbound_ipc_envelope(value: object) -> None:
    if type(value) is not dict:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    schema_version = value.get("schema_version")
    if type(schema_version) is not str:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    if schema_version == IPC_REQUEST_VERSION:
        if frozenset(value) != _IPC_REQUEST_KEYS:
            _raise(PROVIDER_CONFIGURATION_INVALID)
        run_id = value["run_id"]
        request_id = value["request_id"]
        source_identity = value["source_identity"]
        operation = value["operation"]
        payload = value["payload"]
        if (
            type(run_id) is not str
            or _HEX24.fullmatch(run_id) is None
            or type(request_id) is not str
            or _HEX24.fullmatch(request_id) is None
            or type(source_identity) is not str
            or _HEX64.fullmatch(source_identity) is None
            or type(operation) is not str
            or _OPERATION.fullmatch(operation) is None
            or type(payload) is not dict
        ):
            _raise(PROVIDER_CONFIGURATION_INVALID)
        return
    if schema_version == IPC_CONTROL_VERSION:
        if (
            frozenset(value) != _IPC_CONTROL_KEYS
            or type(value["action"]) is not str
            or value["action"] not in _IPC_CONTROL_ACTIONS
        ):
            _raise(PROVIDER_CONFIGURATION_INVALID)
        return
    _raise(PROVIDER_CONFIGURATION_INVALID)


def _readline_with_timeout(stream: object, timeout_seconds: int) -> str:
    result: list[object] = []

    def read() -> None:
        try:
            result.append(stream.readline())
        except BaseException as error:
            result.append(error)

    worker = threading.Thread(target=read, name="normalizer-provider-ipc-read", daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive() or len(result) != 1 or type(result[0]) is not str or not result[0]:
        _raise(PROVIDER_TRANSPORT_FAILED)
    return result[0]


class _WorkerChannel:
    __slots__ = ("__process", "__run_id", "__timeout_seconds", "__lock", "__failed")

    def __init__(self, process: subprocess.Popen, run_id: str, timeout_seconds: int):
        if (
            type(process) is not subprocess.Popen
            or type(run_id) is not str
            or _HEX24.fullmatch(run_id) is None
            or type(timeout_seconds) is not int
        ):
            raise TypeError("worker channel")
        object.__setattr__(self, "_WorkerChannel__process", process)
        object.__setattr__(self, "_WorkerChannel__run_id", run_id)
        object.__setattr__(self, "_WorkerChannel__timeout_seconds", timeout_seconds)
        object.__setattr__(self, "_WorkerChannel__lock", threading.Lock())
        object.__setattr__(self, "_WorkerChannel__failed", False)

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("worker channel is immutable")

    def _exchange(self, payload: dict[str, object], *, timeout_seconds: int | None = None) -> dict[str, object]:
        _validate_outbound_ipc_envelope(payload)
        with self.__lock:
            if self.__failed or self.__process.poll() is not None:
                object.__setattr__(self, "_WorkerChannel__failed", True)
                _raise(PROVIDER_TRANSPORT_FAILED)
            try:
                encoded = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
                )
                assert self.__process.stdin is not None
                assert self.__process.stdout is not None
                self.__process.stdin.write(encoded + "\n")
                self.__process.stdin.flush()
                line = _readline_with_timeout(
                    self.__process.stdout,
                    self.__timeout_seconds + 10 if timeout_seconds is None else timeout_seconds,
                )
                response = json.loads(line)
                if type(response) is not dict:
                    raise ValueError("response")
                return response
            except (KeyboardInterrupt, SystemExit, GeneratorExit):
                raise
            except NormalizerProviderError:
                object.__setattr__(self, "_WorkerChannel__failed", True)
                self._terminate()
                raise
            except Exception:
                object.__setattr__(self, "_WorkerChannel__failed", True)
                self._terminate()
                _raise(PROVIDER_TRANSPORT_FAILED)

    def call(self, source_identity: str, request: object) -> dict[str, object] | str:
        if type(source_identity) is not str or _HEX64.fullmatch(source_identity) is None:
            _raise(PROVIDER_CONFIGURATION_INVALID)
        operation = getattr(request, "request_kind", None)
        if type(operation) is not str:
            _raise(PROVIDER_CONFIGURATION_INVALID)
        request_id = secrets.token_hex(12)
        response = self._exchange({
            "schema_version": IPC_REQUEST_VERSION,
            "run_id": self.__run_id,
            "request_id": request_id,
            "source_identity": source_identity,
            "operation": operation,
            "payload": _ipc_request_payload(request),
        })
        if response.get("schema_version") != IPC_RESPONSE_VERSION:
            _raise(PROVIDER_TRANSPORT_FAILED)
        if response.get("request_id") not in {None, request_id}:
            _raise(PROVIDER_TRANSPORT_FAILED)
        status = response.get("status")
        if status == "ok" and frozenset(response) == {
            "schema_version", "request_id", "status", "result"
        }:
            return response["result"]
        if status == "base_exception" and frozenset(response) == {
            "schema_version", "request_id", "status", "exception_type"
        }:
            kind = response["exception_type"]
            if kind == "CancelledError":
                raise asyncio.CancelledError("provider worker cancelled")
            if kind == "KeyboardInterrupt":
                raise KeyboardInterrupt("provider worker cancelled")
            if kind == "SystemExit":
                raise SystemExit("provider worker cancelled")
            if kind == "GeneratorExit":
                raise GeneratorExit("provider worker cancelled")
        if status == "error" and type(response.get("reason_code")) is str:
            _raise(response["reason_code"])
        _raise(PROVIDER_TRANSPORT_FAILED)

    def _control(self, action: str, payload: object = None) -> object:
        response = self._exchange({
            "schema_version": IPC_CONTROL_VERSION,
            "action": action,
            "payload": payload,
        })
        if (
            response.get("schema_version") != IPC_CONTROL_RESPONSE_VERSION
            or response.get("status") != "ok"
            or response.get("action") != action
            or frozenset(response) != {"schema_version", "status", "action", "result"}
        ):
            _raise(PROVIDER_TRANSPORT_FAILED)
        return response["result"]

    def ledger_snapshot(self) -> tuple[_ProviderCallRecord, ...]:
        values = self._control("ledger")
        if type(values) is not list:
            _raise(PROVIDER_TRANSPORT_FAILED)
        try:
            return tuple(_ProviderCallRecord(**value) for value in values)
        except (TypeError, ValueError):
            _raise(PROVIDER_TRANSPORT_FAILED)

    def _test_calls(self) -> list[dict[str, object]]:
        values = self._control("calls")
        if type(values) is not list or any(type(item) is not dict for item in values):
            _raise(PROVIDER_TRANSPORT_FAILED)
        return [dict(item) for item in values]

    def _test_messages(self) -> int:
        value = self._control("messages")
        if type(value) is not int or isinstance(value, bool) or value < 0:
            _raise(PROVIDER_TRANSPORT_FAILED)
        return value

    def _test_enqueue(self, values: tuple[object, ...]) -> None:
        if type(values) is not tuple:
            raise TypeError("scripted replies")
        self._control("enqueue", [_scripted_reply_payload(value) for value in values])

    def _test_crash(self) -> None:
        try:
            self._control("crash")
        except NormalizerProviderError:
            return
        _raise(PROVIDER_TRANSPORT_FAILED)

    def _terminate(self) -> None:
        try:
            if self.__process.poll() is None:
                self.__process.kill()
            self.__process.wait(timeout=2)
        except Exception:
            pass

    def close(self) -> None:
        with self.__lock:
            if self.__process.poll() is not None:
                return
            try:
                assert self.__process.stdin is not None
                self.__process.stdin.write(json.dumps({
                    "schema_version": IPC_CONTROL_VERSION,
                    "action": "shutdown",
                    "payload": None,
                }, separators=(",", ":")) + "\n")
                self.__process.stdin.flush()
                self.__process.wait(timeout=2)
            except Exception:
                self._terminate()


def _sanitized_worker_environment() -> dict[str, str]:
    result = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP"):
        value = os.environ.get(name)
        if type(value) is str and value:
            result[name] = value
    return result


def _start_worker_channel(
    config: ProviderConfiguration,
    secret: _ProviderSecret,
    capsule: _RunCapsule,
    *,
    scripted_replies: tuple[object, ...] | None,
) -> _WorkerChannel:
    if (
        type(config) is not ProviderConfiguration
        or type(secret) is not _ProviderSecret
        or type(capsule) is not _RunCapsule
        or (scripted_replies is not None and type(scripted_replies) is not tuple)
    ):
        _raise(PROVIDER_CONFIGURATION_INVALID)
    worker_path = Path(__file__).with_name("narrative_normalizer_provider_worker.py").resolve()
    cwd = worker_path.parent.resolve()
    process: subprocess.Popen | None = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-B", str(worker_path)],
            shell=False,
            cwd=str(cwd),
            env=_sanitized_worker_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
        )
        bootstrap = {
            "schema_version": WORKER_BOOTSTRAP_VERSION,
            "configuration": {
                "adapter_version": config.adapter_version,
                "base_url": config.base_url,
                "generation_model": config.generation_model,
                "adjudication_model": config.adjudication_model,
                "timeout_seconds": config.timeout_seconds,
            },
            "secret": secret.reveal(),
            "run": {
                "adapter_version": capsule.adapter_version,
                "adapter_spec": capsule.adapter_spec,
                "local_execution_authorized": capsule.local_execution_authorized,
                "live_provider_authorized": capsule.live_provider_authorized,
                "live_environment_value": capsule.live_environment_value,
                "authorized_source_identities": list(capsule.authorized_source_identities),
                "global_call_budget": capsule.global_call_budget,
                "timeout_seconds": capsule.timeout_seconds,
                "content_model": capsule.content_model,
                "adjudication_model": capsule.adjudication_model,
                "trust_key_id": capsule.trust_key_id,
                "review_authority_identity": capsule.review_authority_identity,
                "run_id": capsule.run_id,
            },
            "transport_mode": "production" if scripted_replies is None else "scripted",
            "scripted_replies": [] if scripted_replies is None else [
                _scripted_reply_payload(value) for value in scripted_replies
            ],
        }
        encoded = json.dumps(
            bootstrap, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(encoded + "\n")
        process.stdin.flush()
        ready = json.loads(_readline_with_timeout(process.stdout, WORKER_STARTUP_TIMEOUT_SECONDS))
        if ready != {
            "schema_version": WORKER_READY_VERSION,
            "status": "ready",
            "run_id": capsule.run_id,
        }:
            raise ValueError("worker startup")
        return _WorkerChannel(process, capsule.run_id, capsule.timeout_seconds)
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        if process is not None and process.poll() is None:
            process.kill()
        raise
    except Exception:
        if process is not None and process.poll() is None:
            process.kill()
        _raise(PROVIDER_CONFIGURATION_INVALID)


class ProductionModelClient:
    """Process proxy implementing both CP2 and evidence model protocols."""

    __slots__ = ("__channel", "_ledger_view", "_source_context", "__weakref__")

    def __init__(self, *args: object, **kwargs: object):
        del args, kwargs
        raise TypeError("provider client construction is factory-only")

    def __setattr__(self, name: str, value: object) -> None:
        del name, value
        raise AttributeError("provider client is immutable")

    @property
    def ledger(self) -> _ReadOnlyCallLedger:
        return self._ledger_view

    @contextmanager
    def authorized_source_scope(self, source_identity: str):
        if (
            type(source_identity) is not str
            or _HEX64.fullmatch(source_identity) is None
            or self._source_context.get() is not None
        ):
            _raise(PROVIDER_CONFIGURATION_INVALID)
        token = self._source_context.set(source_identity)
        try:
            yield
        finally:
            self._source_context.reset(token)

    def generate_json(self, request: object) -> dict[str, object] | str:
        source_identity = self._source_context.get()
        if type(source_identity) is not str:
            _raise(PROVIDER_CONFIGURATION_INVALID)
        return self.__channel.call(source_identity, request)

    def __repr__(self) -> str:
        return "ProductionModelClient(worker=<isolated>, state=<unavailable>)"


def _construct_production_client(channel: _WorkerChannel) -> ProductionModelClient:
    if type(channel) is not _WorkerChannel:
        raise TypeError("provider client")
    client = object.__new__(ProductionModelClient)
    object.__setattr__(client, "_ProductionModelClient__channel", channel)
    object.__setattr__(client, "_ledger_view", _ReadOnlyCallLedger(client))
    object.__setattr__(
        client,
        "_source_context",
        ContextVar("normalizer_provider_source_scope", default=None),
    )
    weakref.finalize(client, channel.close)
    return client


def _hash_label(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _canon(character_id: str) -> contract.CharacterCanonSnapshot:
    refs = tuple(
        contract.CanonSourceRef(
            character_id,
            f"{character_id}-{kind}-review-v1",
            f"canon/{character_id}/{kind}",
            "review-v1",
            _hash_label(f"{character_id}:{kind}:review-v1"),
            kind,
        )
        for kind in ("personality", "visual", "relationship")
    )
    return contract.CharacterCanonSnapshot(character_id, refs, f"{character_id}-canon-review-v1")


def _context_template() -> generation.NarrativeGenerationInput:
    source_ref = "review-only-template"
    facts = (contract.SourceFact("fact-template", "A verified source detail awaits review."),)
    editorial_refs = ("editorial:theme", "editorial:structure", "editorial:visual")
    editorial = generation.NarrativeEditorialContext(
        "normalizer-review-template-v1", source_ref, "story_first", "story_pack",
        "verified work observations", "review_only", "attentive", "observer",
        "observation_to_verification", "alongside_reader", "three_variations",
        "concrete_detail", "open_observation", "measured", "balanced", "steady",
        "optional", "concrete_objects", "documentary", "source-grounded subject",
        "grounded", editorial_refs,
    )
    naz_state = contract.CharacterStateSnapshot(
        "naz", "naz-core-review-v1", 0, 50, 60, 20, 70, 60, 45,
        "attentive", "attentive", "steady", "review-only baseline", (), "naz-state-review-v1",
    )
    void_state = contract.CharacterStateSnapshot(
        "void", "void-core-review-v1", 0, 45, 50, 20, 65, 60, 35,
        "observant", "observant", "calm", "review-only baseline", (), "void-state-review-v1",
    )
    relationship = contract.RelationshipStateSnapshot(
        "duo-review-v1", 0, 65, 60, 10, 65, 70, "cooperative", "review-only", (), (), (),
        "relationship-state-review-v1",
    )
    naz_canon = _canon("naz")
    void_canon = _canon("void")
    return generation.NarrativeGenerationInput(
        source_ref=source_ref,
        source_facts=facts,
        editorial_plan=editorial,
        naz_state=naz_state,
        void_state=void_state,
        relationship_state=relationship,
        naz_canon=naz_canon,
        void_canon=void_canon,
        naz_prompt_context=generation.CharacterPromptContext(
            "naz", tuple(item.source_id for item in naz_canon.canon_refs), "review-v1",
            naz_state.snapshot_ref, "Naz observes verified details without inventing facts.",
        ),
        void_prompt_context=generation.CharacterPromptContext(
            "void", tuple(item.source_id for item in void_canon.canon_refs), "review-v1",
            void_state.snapshot_ref, "Void may appear only when source-grounded and editorially necessary.",
        ),
        relationship_prompt_context=generation.RelationshipPromptContext(
            relationship.snapshot_ref, "The duo remains cooperative and subordinate to source evidence."
        ),
        diversity_context=contract.NarrativeDiversityContext(()),
    )


@dataclass(frozen=True, slots=True)
class ProductionNarrativeContextProvider:
    """Conservative, versioned review-only context with source rebinding."""

    _delegate: normalizer.TemplateNarrativeContextProvider
    configuration: ProviderConfiguration

    def __post_init__(self) -> None:
        if type(self._delegate) is not normalizer.TemplateNarrativeContextProvider:
            raise TypeError("delegate")
        if type(self.configuration) is not ProviderConfiguration:
            raise TypeError("configuration")

    def build(self, source: normalizer.SourceUnit) -> generation.NarrativeGenerationInput:
        return self._delegate.build(source)

    def safe_summary(self, *, selected_source_count: int) -> dict[str, object]:
        return self.configuration.safe_summary(selected_source_count=selected_source_count)


def _build_authorized_adapter(
    authorization: LiveProviderRunAuthorization,
    *,
    test_transport: object | None = None,
) -> tuple[
    ProductionNarrativeContextProvider,
    generation.NarrativeGenerationService,
    evidence.GenericEvidenceService,
]:
    if type(authorization) is not LiveProviderRunAuthorization:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    config, secret = authorization._consumption.consume()
    if (
        authorization.content_model != config.generation_model
        or authorization.adjudication_model != config.adjudication_model
        or authorization.timeout_seconds != config.timeout_seconds
    ):
        _raise(PROVIDER_CONFIGURATION_INVALID)
    capsule = _sealed_run_capsule(authorization)
    scripted_replies: tuple[object, ...] | None = None
    attach_test_transport: Callable[[object], None] | None = None
    if test_transport is not None:
        export = getattr(test_transport, "_worker_export_replies", None)
        attach = getattr(test_transport, "_worker_attach", None)
        if not callable(export) or not callable(attach):
            _raise(PROVIDER_CONFIGURATION_INVALID)
        try:
            scripted_replies = export()
            if type(scripted_replies) is not tuple:
                raise TypeError("scripted replies")
            attach_test_transport = attach
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except Exception:
            _raise(PROVIDER_CONFIGURATION_INVALID)
    channel: _WorkerChannel | None = None
    try:
        channel = _start_worker_channel(
            config, secret, capsule, scripted_replies=scripted_replies
        )
    except NormalizerProviderError:
        raise
    except Exception:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    if type(channel) is not _WorkerChannel:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    if attach_test_transport is not None:
        try:
            attach_test_transport(channel)
        except Exception:
            channel.close()
            _raise(PROVIDER_CONFIGURATION_INVALID)
    client = _construct_production_client(channel)
    provider = ProductionNarrativeContextProvider(
        normalizer.TemplateNarrativeContextProvider(_context_template()),
        config,
    )
    generation_service = generation.NarrativeGenerationService(
        client,
        generation_model=config.generation_model,
        adjudication_model=config.adjudication_model,
        repair_model=config.generation_model,
    )
    evidence_service = evidence.GenericEvidenceService(
        client,
        extraction_model=config.generation_model,
        adjudication_model=config.adjudication_model,
    )
    result = (provider, generation_service, evidence_service)
    if (
        len(result) != 3
        or type(result[0]) is not ProductionNarrativeContextProvider
        or type(result[1]) is not generation.NarrativeGenerationService
        or type(result[2]) is not evidence.GenericEvidenceService
    ):
        _raise(PROVIDER_CONFIGURATION_INVALID)
    return result


def production_adapter_factory(authorization: object = None) -> tuple[
    ProductionNarrativeContextProvider,
    generation.NarrativeGenerationService,
    evidence.GenericEvidenceService,
]:
    """Consume one reviewed live-run authorization exactly once."""

    if type(authorization) is not LiveProviderRunAuthorization:
        _raise(PROVIDER_CONFIGURATION_INVALID)
    return _build_authorized_adapter(
        authorization,
    )


__all__ = (
    "ADAPTER_VERSION_ENV",
    "ADJUDICATION_MODEL_ENV",
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEFAULT_TIMEOUT_SECONDS",
    "FIRST_FIVE_SOURCE_COUNT",
    "GENERATION_MODEL_ENV",
    "LIVE_ENV",
    "MAX_PROVIDER_CALLS",
    "NORMALIZER_PRODUCTION_ADAPTER_VERSION",
    "AuthorizedProviderRequest",
    "LiveProviderRunAuthorization",
    "NormalizerProviderError",
    "PRODUCTION_ADAPTER_SPEC",
    "PROVIDER_BUDGET_EXCEEDED",
    "PROVIDER_CANCELLED",
    "PROVIDER_CONFIGURATION_INVALID",
    "PROVIDER_DISABLED",
    "PROVIDER_RESPONSE_INVALID",
    "PROVIDER_REQUEST_CONFLICT",
    "PROVIDER_TIMEOUT",
    "PROVIDER_TRANSPORT_FAILED",
    "ProductionModelClient",
    "ProductionNarrativeContextProvider",
    "ProviderConfiguration",
    "TIMEOUT_ENV",
    "authorize_live_provider_run",
    "inspect_live_configuration",
    "production_adapter_factory",
)
