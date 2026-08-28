from __future__ import annotations

import asyncio
import ast
import copy
import dataclasses
import hashlib
import importlib.util
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

import narrative_generation as generation
import narrative_normalizer as normalizer
import narrative_normalizer_evidence as evidence
import narrative_normalizer_provider as provider
import narrative_normalizer_trust as trust
import narrative_review_authority as review_authority
import narrative_review_authority_client as review_authority_client
import narrative_review_authority_protocol as review_authority_protocol
import reels_failure_quarantine as quarantine
import tools.run_narrative_normalizer as cli
from tools.run_narrative_generation_fixture import fake_adjudication_payload, load_fixture


CONTENT_MODEL = "openai/gpt-5.4-mini"
REVIEW_MODEL = "openai/gpt-5.4"
AUTHORIZED_SOURCES = tuple(f"{index:064x}" for index in range(1, 6))
TEST_TRUST_SERVICE = trust.NarrativeTrustService(b"cp6bp-provider-test-key-material-32-bytes")
TEST_AUTHORITY_ROOT = Path(os.environ.get("TEMP", ".")).resolve() / "cp6bp-provider-authority-prerequisite"


class StringSubclass(str):
    pass


class IntegerSubclass(int):
    pass


class DictSubclass(dict):
    pass


def live_env(**updates: str) -> dict[str, str]:
    values = {
        provider.LIVE_ENV: "1",
        provider.ADAPTER_VERSION_ENV: provider.NORMALIZER_PRODUCTION_ADAPTER_VERSION,
        provider.API_KEY_ENV: "test-provider-key-never-log",
        provider.BASE_URL_ENV: "https://openrouter.ai/api/v1",
        provider.GENERATION_MODEL_ENV: CONTENT_MODEL,
        provider.ADJUDICATION_MODEL_ENV: REVIEW_MODEL,
        provider.TIMEOUT_ENV: "120",
    }
    values.update(updates)
    return values


class _WorkerReplyQueue:
    def __init__(self, owner, replies):
        self.owner = owner
        self.pending = list(replies)

    def extend(self, values):
        values = tuple(values)
        if self.owner._channel is None:
            self.pending.extend(values)
        else:
            self.owner._channel._test_enqueue(values)

    def append(self, value):
        self.extend((value,))


class FakeTransport:
    def __init__(self, replies=()):
        self._channel = None
        self.replies = _WorkerReplyQueue(self, replies)

    @property
    def calls(self):
        return [] if self._channel is None else self._channel._test_calls()

    def _worker_export_replies(self):
        values = tuple(self.replies.pending)
        self.replies.pending.clear()
        return values

    def _worker_attach(self, channel):
        assert self._channel is None
        self._channel = channel


def authorization(
    *,
    env=None,
    sources=AUTHORIZED_SOURCES,
    run_profile=provider.FIRST_FIVE_RUN_PROFILE,
):
    return provider.authorize_live_provider_run(
        adapter_spec=provider.PRODUCTION_ADAPTER_SPEC,
        local_execution_enabled=True,
        live_provider_enabled=True,
        run_profile=run_profile,
        source_identities=tuple(sources),
        env=live_env() if env is None else env,
        trust_service=TEST_TRUST_SERVICE,
        review_authority_root=TEST_AUTHORITY_ROOT,
    )


def broker_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> provider.BrokerReadinessCapability:
    monkeypatch.setattr(
        review_authority_client.socket, "AF_UNIX",
        getattr(review_authority_client.socket, "AF_UNIX", 1), raising=False,
    )
    proxy = review_authority_client.ReviewAuthorityClient(
        (tmp_path / "authority.sock").resolve(), owner_uid=1, owner_gid=2
    )
    monkeypatch.setattr(
        review_authority_client.ReviewAuthorityClient,
        "health",
        lambda self, request_id: {
            "status": "ok",
            "contract_version": review_authority.BROKER_CONTRACT_VERSION,
            "narrative_outbox_layout_version": review_authority.NARRATIVE_OUTBOX_LAYOUT_VERSION,
            "key_id": "a" * 24,
            "authenticated_role": review_authority_protocol.ROLE_NORMALIZER,
        },
    )
    return provider.broker_readiness_capability(proxy)


def test_broker_capability_authorizes_provider_without_local_trust_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability = broker_capability(tmp_path, monkeypatch)
    approved = provider.authorize_live_provider_run(
        adapter_spec=provider.PRODUCTION_ADAPTER_SPEC,
        local_execution_enabled=True,
        live_provider_enabled=True,
        run_profile=provider.CANARY_RUN_PROFILE,
        source_identities=(AUTHORIZED_SOURCES[0],),
        env=live_env(),
        broker_capability=capability,
    )
    assert approved.trust_key_id == "a" * 24
    assert approved.global_call_budget == 5
    assert len(approved.authorized_source_identities) == 1


@pytest.mark.parametrize("forged", [True, False, "/tmp/authority", object()])
def test_provider_rejects_forged_broker_capability(forged: object) -> None:
    with pytest.raises(provider.NormalizerProviderError) as caught:
        provider.authorize_live_provider_run(
            adapter_spec=provider.PRODUCTION_ADAPTER_SPEC,
            local_execution_enabled=True,
            live_provider_enabled=True,
            run_profile=provider.CANARY_RUN_PROFILE,
            source_identities=(AUTHORIZED_SOURCES[0],),
            env=live_env(),
            broker_capability=forged,
        )
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID


def test_broker_capability_requires_normalizer_authenticated_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        review_authority_client.socket, "AF_UNIX",
        getattr(review_authority_client.socket, "AF_UNIX", 1), raising=False,
    )
    proxy = review_authority_client.ReviewAuthorityClient(
        (tmp_path / "authority.sock").resolve(), owner_uid=1, owner_gid=2
    )
    monkeypatch.setattr(
        review_authority_client.ReviewAuthorityClient,
        "health",
        lambda self, request_id: {
            "status": "ok",
            "contract_version": review_authority.BROKER_CONTRACT_VERSION,
            "narrative_outbox_layout_version": review_authority.NARRATIVE_OUTBOX_LAYOUT_VERSION,
            "key_id": "a" * 24,
            "authenticated_role": review_authority_protocol.ROLE_CONSUMER,
        },
    )
    with pytest.raises(provider.NormalizerProviderError):
        provider.broker_readiness_capability(proxy)


def adapter(
    transport: FakeTransport | None = None,
    *,
    env=None,
    sources=AUTHORIZED_SOURCES,
    run_profile=provider.FIRST_FIVE_RUN_PROFILE,
):
    fake = transport or FakeTransport()
    approved = authorization(env=env, sources=sources, run_profile=run_profile)
    return adapter_from_authorization(approved, fake)


def adapter_from_authorization(approved, transport: FakeTransport | None = None):
    fake = transport or FakeTransport()
    dependencies = provider._build_authorized_adapter(
        approved,
        test_transport=fake,
    )
    client = dependencies[1]._client
    return dependencies, client, fake


def invoke(client, request, source_identity=AUTHORIZED_SOURCES[0]):
    with client.authorized_source_scope(source_identity):
        return client.generate_json(request)


def ipc_message(
    client,
    request_id,
    request,
    *,
    source_identity=AUTHORIZED_SOURCES[0],
    run_id=None,
    schema_version=provider.IPC_REQUEST_VERSION,
):
    channel = object.__getattribute__(client, "_ProductionModelClient__channel")
    actual_run_id = object.__getattribute__(channel, "_WorkerChannel__run_id")
    return {
        "schema_version": schema_version,
        "run_id": actual_run_id if run_id is None else run_id,
        "request_id": request_id,
        "source_identity": source_identity,
        "operation": request.request_kind,
        "payload": provider._ipc_request_payload(request),
    }


def exchange_ipc(client, message):
    channel = object.__getattribute__(client, "_ProductionModelClient__channel")
    return channel._exchange(message)


def authorized_sources_for_records(records):
    values = [normalizer.source_identity(item.source_ref, item.source_digest) for item in records]
    filler = iter(AUTHORIZED_SOURCES)
    while len(values) < provider.FIRST_FIVE_SOURCE_COUNT:
        candidate = next(filler)
        if candidate not in values:
            values.append(candidate)
    return tuple(values[:provider.FIRST_FIVE_SOURCE_COUNT])


def _core_normalizer_test_helpers():
    path = Path(__file__).with_name("test_narrative_normalizer.py")
    spec = importlib.util.spec_from_file_location("_cp6bp_core_test_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind_story_payload_to_review_context(payload, context):
    value = copy.deepcopy(payload)
    canon_ids = {
        "naz-personality": "naz-personality-review-v1",
        "naz-visual": "naz-visual-review-v1",
        "naz-relationship": "naz-relationship-review-v1",
        "void-personality": "void-personality-review-v1",
        "void-visual": "void-visual-review-v1",
        "void-relationship": "void-relationship-review-v1",
    }

    def visit(item):
        if type(item) is dict:
            for key, nested in tuple(item.items()):
                if type(nested) is str and nested in canon_ids:
                    item[key] = canon_ids[nested]
                else:
                    visit(nested)
        elif type(item) is list:
            for index, nested in enumerate(item):
                if type(nested) is str and nested in canon_ids:
                    item[index] = canon_ids[nested]
                else:
                    visit(nested)

    visit(value)
    for candidate in value["candidates"]:
        candidate["visual_direction"]["mode_hint"] = context.editorial_plan.visual_mode
        candidate["primary_interpretation"]["ending_mode"] = context.editorial_plan.ending
        if candidate["secondary_interpretation"] is not None:
            candidate["secondary_interpretation"]["ending_mode"] = context.editorial_plan.ending
    return value


def _generic_v2_responses(documents, propositions):
    core = _core_normalizer_test_helpers()
    legacy_extraction, _legacy_adjudication = core.generic_evidence_responses(
        documents, propositions
    )
    inventory = evidence.build_source_block_inventory(documents)
    coverage = {
        "schema_version": evidence.EVIDENCE_COVERAGE_CONTRACT_VERSION,
        "source_identity": documents.source_identity,
        "document_bundle_digest": documents.bundle_digest,
        "inventory_digest": inventory.inventory_digest,
        "run_id": "coverage-run-v2",
        "block_dispositions": {
            block.block_id: (
                "sensitive_withheld"
                if block.sensitivity_status == "sensitive_withheld"
                else "evidence_candidate"
            )
            for block in inventory.ordered_blocks
        },
    }
    plan = evidence.parse_coverage_response(coverage, documents, inventory)
    block_for_segment = {
        segment_id: block.block_id
        for block in inventory.ordered_blocks
        for segment_id in block.ordered_segment_ids
    }
    extraction = {
        "schema_version": evidence.EVIDENCE_EXTRACTION_V2_CONTRACT_VERSION,
        "source_identity": documents.source_identity,
        "document_bundle_digest": documents.bundle_digest,
        "coverage_plan_digest": plan.plan_digest,
        "run_id": "extraction-run-v2",
        "evidence": [
            dict(
                item,
                ordered_block_refs=list(dict.fromkeys(
                    block_for_segment[segment_id]
                    for segment_id in item["ordered_segment_refs"]
                )),
            )
            for item in legacy_extraction["evidence"]
        ],
    }
    parsed = evidence.parse_extraction_v2_response(
        extraction, documents, inventory, plan
    )
    adjudication = {
        "schema_version": evidence.EVIDENCE_ADJUDICATION_CONTRACT_VERSION,
        "source_identity": documents.source_identity,
        "extraction_bundle_digest": parsed.bundle_digest,
        "run_id": "adjudication-run-v2",
        "decisions": [
            {
                "evidence_id": item.evidence_id,
                "evidence_digest": evidence.evidence_digest(item),
                "decision": "supported",
                "reason_codes": [],
            }
            for item in parsed.ordered_evidence
        ],
    }
    return coverage, extraction, adjudication


def _generic_provider_batch(
    tmp_path,
    *,
    source_count: int,
    repair: bool,
    fail_index: int | None = None,
    run_profile: str = provider.FIRST_FIVE_RUN_PROFILE,
):
    core = _core_normalizer_test_helpers()
    _shape, filename, body, propositions = core._GENERIC_E2E_SHAPES[0]
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    registry.parent.mkdir(parents=True)
    for index in range(source_count):
        source_path = inbox / f"Generic-Provider-{index + 1}" / "2026-08-18"
        source_path.mkdir(parents=True)
        (source_path / filename).write_text(body, encoding="utf-8")
    trust_service = trust.NarrativeTrustService(core.TEST_TRUST_KEY)
    policy = quarantine.QuarantinePathPolicy(
        inbox, registry, outbox, trust_service, tmp_path / "review-authority"
    )
    quarantine.reconcile_complete_backlog(policy, now=core.NOW)
    records = quarantine.read_registry(registry).records
    assert len(records) == source_count
    transport = FakeTransport()
    selected_count = (
        provider.CANARY_SOURCE_COUNT
        if run_profile == provider.CANARY_RUN_PROFILE
        else provider.FIRST_FIVE_SOURCE_COUNT
    )
    selected_sources = tuple(
        normalizer.source_identity(item.source_ref, item.source_digest)
        for item in records[:selected_count]
    )
    if run_profile == provider.FIRST_FIVE_RUN_PROFILE:
        selected_sources = authorized_sources_for_records(records[:selected_count])
    dependencies, client, _ = adapter(
        transport,
        sources=selected_sources,
        run_profile=run_profile,
    )
    replies = []
    for index, record in enumerate(records):
        documents = normalizer.read_source_documents(
            policy, record.source_ref, expected_digest=record.source_digest
        )
        extraction, evidence_adjudication = core.generic_evidence_responses(
            documents, propositions
        )
        coverage_v2, extraction_v2, adjudication_v2 = _generic_v2_responses(
            documents, propositions
        )
        if fail_index is not None and index == fail_index:
            replies.append(RuntimeError("private provider detail must not escape"))
            continue
        raw_source = normalizer.read_source_unit(
            policy,
            record.source_ref,
            expected_digest=record.source_digest,
            allow_insufficient=True,
        )
        preparation = evidence.GenericEvidenceService(
            core.QueueClient((copy.deepcopy(extraction), copy.deepcopy(evidence_adjudication))),
            extraction_model=CONTENT_MODEL,
            adjudication_model=REVIEW_MODEL,
        ).resolve(documents)
        assert preparation.status == "verified" and preparation.verified_bundle is not None
        verified_source = normalizer._source_from_verified_evidence(
            raw_source, documents, preparation.verified_bundle
        )
        context = dependencies[0].build(verified_source)
        story = _bind_story_payload_to_review_context(
            core.generic_story_response(load_fixture("quiet_object"), propositions), context
        )
        story_adjudication = fake_adjudication_payload(story, context)
        replies.extend((coverage_v2, extraction_v2, adjudication_v2))
        replies.extend((story, story_adjudication))
    transport.replies.extend(replies)
    service = normalizer.NarrativeNormalizerService(
        policy=policy,
        context_provider=dependencies[0],
        generation_service=dependencies[1],
        evidence_service=dependencies[2],
        trust_service=trust_service,
        clock=lambda: core.NOW,
    )
    return records, service, client, transport


def narrative_request(operation: str, model: str):
    return generation.NarrativeModelRequest(
        operation,
        model,
        "Return JSON only.",
        '{"safe":"payload"}',
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    )


def evidence_request(operation: str, model: str):
    version = (
        evidence.EVIDENCE_COVERAGE_CONTRACT_VERSION
        if operation == "evidence_coverage"
        else evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION
        if operation == "evidence_extraction"
        else evidence.EVIDENCE_ADJUDICATION_CONTRACT_VERSION
    )
    return evidence.EvidenceModelRequest(
        operation,
        model,
        '{"safe":"payload"}',
        version,
        ("block-0001",) if operation == "evidence_coverage" else (),
    )


@pytest.mark.parametrize(
    ("operation", "model", "required"),
    (
        (
            "evidence_extraction",
            CONTENT_MODEL,
            {"schema_version", "source_identity", "document_bundle_digest", "run_id", "evidence", "segment_dispositions"},
        ),
        (
            "evidence_adjudication",
            REVIEW_MODEL,
            {"schema_version", "source_identity", "extraction_bundle_digest", "run_id", "decisions"},
        ),
    ),
    ids=("extraction", "adjudication"),
)
def test_evidence_transport_uses_code_owned_strict_closed_schema(operation, model, required):
    _dependencies, client, transport = adapter()
    request = evidence_request(operation, model)
    response_format = provider._request_payload(request)[3]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == required
    assert set(schema["properties"]) == required
    assert "withheld_segments" not in schema["properties"]
    invoke(client, request)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    ("operation", "version"),
    (
        ("evidence_extraction", evidence.EVIDENCE_ADJUDICATION_CONTRACT_VERSION),
        ("evidence_adjudication", evidence.EVIDENCE_EXTRACTION_CONTRACT_VERSION),
        ("evidence_extraction", "unknown-version"),
    ),
    ids=("extraction-wrong-version", "adjudication-wrong-version", "unknown-version"),
)
def test_evidence_schema_version_mismatch_rejects_before_transport(operation, version):
    _dependencies, client, transport = adapter()
    model = CONTENT_MODEL if operation == "evidence_extraction" else REVIEW_MODEL
    request = evidence.EvidenceModelRequest(operation, model, '{"safe":"payload"}', version)
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, request)
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert transport.calls == []


@pytest.mark.parametrize(
    ("operation", "request_factory", "model"),
        (
            ("evidence_extraction", evidence_request, CONTENT_MODEL),
            ("evidence_coverage", evidence_request, CONTENT_MODEL),
            ("generation", narrative_request, CONTENT_MODEL),
            ("evidence_adjudication", evidence_request, REVIEW_MODEL),
        ("adjudication", narrative_request, REVIEW_MODEL),
    ),
    ids=(
            "correct-extraction-model",
            "correct-coverage-model",
            "correct-generation-model",
        "correct-evidence-adjudication-model",
        "correct-story-adjudication-model",
    ),
)
def test_operation_to_model_mapping_is_closed(operation, request_factory, model):
    _dependencies, client, transport = adapter()
    assert invoke(client, request_factory(operation, model)) == '{"ok":true}'
    assert len(transport.calls) == 1
    assert transport.calls[0]["model"] == model
    assert transport.calls[0]["timeout_seconds"] == 120


@pytest.mark.parametrize(
    ("operation", "request_factory", "wrong_model"),
    (
        ("generation", narrative_request, REVIEW_MODEL),
        ("adjudication", narrative_request, CONTENT_MODEL),
        ("evidence_extraction", evidence_request, REVIEW_MODEL),
        ("evidence_adjudication", evidence_request, CONTENT_MODEL),
    ),
)
def test_caller_cannot_override_operation_model(operation, request_factory, wrong_model):
    _dependencies, client, transport = adapter()
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, request_factory(operation, wrong_model))
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert transport.calls == []


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    (
        (provider.LIVE_ENV, "", provider.PROVIDER_DISABLED),
        (provider.LIVE_ENV, "true", provider.PROVIDER_DISABLED),
        (provider.LIVE_ENV, "yes", provider.PROVIDER_DISABLED),
        (provider.LIVE_ENV, "01", provider.PROVIDER_DISABLED),
        (provider.LIVE_ENV, " 1", provider.PROVIDER_DISABLED),
        (provider.LIVE_ENV, "1 ", provider.PROVIDER_DISABLED),
        (provider.ADAPTER_VERSION_ENV, "wrong", provider.PROVIDER_CONFIGURATION_INVALID),
        (provider.API_KEY_ENV, "", provider.PROVIDER_CONFIGURATION_INVALID),
        (provider.API_KEY_ENV, " key", provider.PROVIDER_CONFIGURATION_INVALID),
        (provider.BASE_URL_ENV, "", provider.PROVIDER_CONFIGURATION_INVALID),
        (provider.BASE_URL_ENV, "http://openrouter.ai/api/v1", provider.PROVIDER_CONFIGURATION_INVALID),
        (provider.BASE_URL_ENV, "https://user@example.com/v1", provider.PROVIDER_CONFIGURATION_INVALID),
        (provider.GENERATION_MODEL_ENV, "", provider.PROVIDER_CONFIGURATION_INVALID),
        (provider.GENERATION_MODEL_ENV, " model", provider.PROVIDER_CONFIGURATION_INVALID),
        (provider.ADJUDICATION_MODEL_ENV, "", provider.PROVIDER_CONFIGURATION_INVALID),
        (provider.ADJUDICATION_MODEL_ENV, "model with spaces", provider.PROVIDER_CONFIGURATION_INVALID),
    ),
)
def test_configuration_fails_closed(name, value, reason):
    values = live_env(**{name: value})
    with pytest.raises(provider.NormalizerProviderError) as caught:
        authorization(env=values)
    assert caught.value.reason_code == reason


@pytest.mark.parametrize("missing", (
    provider.API_KEY_ENV,
    provider.BASE_URL_ENV,
    provider.GENERATION_MODEL_ENV,
    provider.ADJUDICATION_MODEL_ENV,
    provider.ADAPTER_VERSION_ENV,
))
def test_required_configuration_cannot_be_missing(missing):
    values = live_env()
    values.pop(missing)
    with pytest.raises(provider.NormalizerProviderError):
        authorization(env=values)


@pytest.mark.parametrize("value", ("9", "301", "01", "10.0", " 120", "120 ", "true", ""))
def test_timeout_invalid_forms_are_rejected(value):
    with pytest.raises(provider.NormalizerProviderError) as caught:
        provider.inspect_live_configuration(
            live_env(**{provider.TIMEOUT_ENV: value}),
            selected_source_count=5,
        )
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID


@pytest.mark.parametrize("value", (10, 120, 300))
def test_timeout_bounds_are_accepted(value):
    summary = provider.inspect_live_configuration(
        live_env(**{provider.TIMEOUT_ENV: str(value)}),
        selected_source_count=5,
    )
    assert summary["timeout_seconds"] == value
    assert summary["retry_count"] == 0


def test_timeout_default_is_bounded_and_documented_value():
    values = live_env()
    values.pop(provider.TIMEOUT_ENV)
    assert provider.inspect_live_configuration(values, selected_source_count=5)["timeout_seconds"] == 120


@pytest.mark.parametrize("value", (True, 120.0, object()))
def test_internal_timeout_value_rejects_bool_float_and_objects(value):
    with pytest.raises(provider.NormalizerProviderError):
        provider._timeout(value)


def test_sdk_constructor_disables_sdk_retries(monkeypatch):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    module = types.ModuleType("openai")
    module.OpenAI = FakeOpenAI
    module.APITimeoutError = TimeoutError
    monkeypatch.setitem(sys.modules, "openai", module)
    config, secret = provider._load_configuration(live_env())
    transport = provider._default_transport_factory(config, secret)
    assert type(transport).__name__ == "_OpenAITransport"
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 120


@pytest.mark.parametrize(
    ("error", "reason"),
    (
        (TimeoutError("secret timeout"), provider.PROVIDER_TIMEOUT),
        (RuntimeError("429 secret response"), provider.PROVIDER_TRANSPORT_FAILED),
        (RuntimeError("500 secret response"), provider.PROVIDER_TRANSPORT_FAILED),
    ),
    ids=("timeout-no-retry", "429-no-retry", "5xx-no-retry"),
)
def test_transport_failures_never_retry_and_are_private(error, reason):
    transport = FakeTransport([error])
    _dependencies, client, _ = adapter(transport)
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, narrative_request("generation", CONTENT_MODEL))
    assert caught.value.reason_code == reason
    assert str(caught.value) == reason
    assert repr(caught.value).find("secret") == -1
    assert caught.value.__cause__ is None
    assert len(transport.calls) == 1
    record = client.ledger.snapshot()[0]
    assert record.completed is True
    assert record.attempt_number == 1
    assert record.safe_outcome == reason


@pytest.mark.parametrize(
    "response",
    (
        b'{"ok":true}',
        7,
        ["not", "mapping"],
        iter(("x",)),
        '{"ok":true} trailing prose',
        "```json\n{}\n```",
        "[]",
        "null",
        "",
        " {} ",
    ),
    ids=(
        "bytes", "scalar", "list", "generator", "prose-wrapped-json", "fenced-json",
        "json-array", "json-null", "empty", "surrounding-whitespace",
    ),
)
def test_transport_response_type_and_json_boundary_is_strict(response):
    transport = FakeTransport([response])
    _dependencies, client, _ = adapter(transport)
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, narrative_request("generation", CONTENT_MODEL))
    assert caught.value.reason_code == provider.PROVIDER_RESPONSE_INVALID
    assert len(transport.calls) == 1


def test_coverage_provider_response_failure_returns_typed_hard_invalid():
    transport = FakeTransport(["```json\n{}\n```"])
    _dependencies, client, _ = adapter(transport)

    result = invoke(client, evidence_request("evidence_coverage", CONTENT_MODEL))

    assert type(result) is evidence.CoverageFailureEvidence
    assert result.category == "coverage_hard_invalid"
    assert result.stable_reason == "unsupported_object_type"
    assert result.summary.block_count == 1
    assert len(transport.calls) == 1


def test_coverage_provider_transport_failure_returns_typed_hard_invalid():
    transport = FakeTransport([TimeoutError("private-timeout-detail")])
    _dependencies, client, _ = adapter(transport)

    result = invoke(client, evidence_request("evidence_coverage", CONTENT_MODEL))

    assert type(result) is evidence.CoverageFailureEvidence
    assert result.category == "coverage_hard_invalid"
    assert result.stable_reason == "transport_failure"
    assert result.transport_diagnostic is not None
    assert result.transport_diagnostic.category == "timeout"
    assert result.transport_diagnostic.http_status is None
    assert result.transport_diagnostic.response_received is False
    assert result.transport_diagnostic.timeout_phase == "request"
    assert len(transport.calls) == 1
    record = client.ledger.snapshot()[0]
    assert record.transport_diagnostic == result.transport_diagnostic


class _SafeHTTPError(RuntimeError):
    def __init__(self, status_code, code="provider_code", request_id="req_safe_123"):
        self.status_code = status_code
        self.code = code
        self.request_id = request_id
        super().__init__("private body must not be inspected")


class RemoteProtocolError(RuntimeError):
    pass


@pytest.mark.parametrize(
    ("error", "category", "status", "response_received", "timeout_phase"),
    (
        (TimeoutError("private"), "timeout", None, False, "request"),
        (socket.gaierror(1, "private"), "dns_connect", None, False, "not_applicable"),
        (ssl.SSLError("private"), "tls", None, False, "not_applicable"),
        (ConnectionResetError("private"), "connection_reset", None, False, "not_applicable"),
        (_SafeHTTPError(400), "http_400", 400, True, "not_applicable"),
        (_SafeHTTPError(401), "http_401_403", 401, True, "not_applicable"),
        (_SafeHTTPError(403), "http_401_403", 403, True, "not_applicable"),
        (_SafeHTTPError(404), "http_404", 404, True, "not_applicable"),
        (_SafeHTTPError(429), "http_429", 429, True, "not_applicable"),
        (_SafeHTTPError(503), "http_5xx", 503, True, "not_applicable"),
        (RemoteProtocolError("private"), "response_read_failure", None, False, "not_applicable"),
        (RuntimeError("private"), "unknown_transport_failure", None, False, "not_applicable"),
    ),
)
def test_transport_failure_classifier_is_closed_and_privacy_safe(
    error, category, status, response_received, timeout_phase,
):
    diagnostic = provider._classify_transport_failure(error)

    assert diagnostic.category == category
    assert diagnostic.http_status == status
    assert diagnostic.response_received is response_received
    assert diagnostic.timeout_phase == timeout_phase
    payload = diagnostic.safe_payload()
    assert "private" not in json.dumps(payload, sort_keys=True)
    if status is not None:
        assert diagnostic.provider_error_code == "provider_code"
        assert diagnostic.provider_request_id == "req_safe_123"


def test_unknown_transport_diagnostic_survives_worker_ipc_and_ledger():
    transport = FakeTransport([RuntimeError("private-response-body")])
    _dependencies, client, _ = adapter(transport)

    result = invoke(client, evidence_request("evidence_coverage", CONTENT_MODEL))

    assert result.transport_diagnostic is not None
    assert result.transport_diagnostic.category == "unknown_transport_failure"
    assert result.transport_diagnostic.response_received is False
    assert "private-response-body" not in json.dumps(result.safe_payload(), sort_keys=True)
    records = client.ledger.snapshot()
    assert len(records) == 1
    assert records[0].transport_diagnostic == result.transport_diagnostic


@pytest.mark.parametrize("response", ({"ok": True}, '{"ok":true}'))
def test_exact_mapping_or_plain_json_object_string_is_accepted(response):
    transport = FakeTransport([response])
    _dependencies, client, _ = adapter(transport)
    assert invoke(client, narrative_request("generation", CONTENT_MODEL)) == response


@pytest.mark.parametrize("error_type", (asyncio.CancelledError, KeyboardInterrupt, SystemExit, GeneratorExit))
def test_cancellation_and_baseexceptions_propagate_by_exact_type_without_private_text(error_type):
    original = error_type("cancel-private")
    transport = FakeTransport([original])
    _dependencies, client, _ = adapter(transport)
    with pytest.raises(error_type) as caught:
        invoke(client, narrative_request("generation", CONTENT_MODEL))
    assert type(caught.value) is error_type
    assert "cancel-private" not in str(caught.value)
    assert len(transport.calls) == 1


def test_call_ledger_contains_only_safe_digests_and_states():
    _dependencies, client, _transport = adapter()
    invoke(client, narrative_request("generation", CONTENT_MODEL))
    record = client.ledger.snapshot()[0]
    assert record.operation == "generation"
    assert record.model_id == CONTENT_MODEL
    assert record.attempt_number == 1
    assert record.timeout_seconds == 120
    assert len(record.request_digest) == 64
    assert len(record.response_digest or "") == 64
    assert record.started is record.completed is True
    assert record.safe_outcome == "completed"
    rendered = repr(record)
    assert "Return JSON only" not in rendered
    assert "test-provider-key" not in rendered


def test_hard_call_budget_blocks_call_twenty_six_before_transport():
    transport = FakeTransport()
    _dependencies, client, _ = adapter(transport)
    operations = (
        "evidence_coverage", "evidence_extraction", "evidence_adjudication",
        "generation", "adjudication",
    )
    for source_identity in AUTHORIZED_SOURCES:
        for operation in operations:
            if operation.startswith("evidence_"):
                model = CONTENT_MODEL if operation in {
                    "evidence_coverage", "evidence_extraction",
                } else REVIEW_MODEL
                request = evidence_request(operation, model)
            else:
                model = CONTENT_MODEL if operation == "generation" else REVIEW_MODEL
                request = narrative_request(operation, model)
            invoke(client, request, source_identity)
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, narrative_request("generation", CONTENT_MODEL), AUTHORIZED_SOURCES[0])
    assert caught.value.reason_code == provider.PROVIDER_BUDGET_EXCEEDED
    assert len(transport.calls) == 25
    assert len(client.ledger.snapshot()) == 25


@pytest.mark.parametrize(
    ("operations", "expected"),
    (
        (("generation", "adjudication"), 2),
        (("evidence_extraction", "evidence_adjudication", "generation", "adjudication"), 4),
        (("evidence_coverage", "evidence_extraction", "evidence_adjudication", "generation", "adjudication"), 5),
    ),
    ids=("fast-path-two", "generic-four", "generic-coverage-five"),
)
def test_exact_normalizer_operation_budgets(operations, expected):
    _dependencies, client, transport = adapter()
    for operation in operations:
        if operation.startswith("evidence_"):
            model = CONTENT_MODEL if operation in {
                "evidence_coverage", "evidence_extraction",
            } else REVIEW_MODEL
            request = evidence_request(operation, model)
        else:
            model = CONTENT_MODEL if operation == "generation" else REVIEW_MODEL
            request = narrative_request(operation, model)
        invoke(client, request)
    assert len(transport.calls) == expected
    assert len(client.ledger.snapshot()) == expected


def test_request_privacy_rejects_secrets_and_absolute_paths_before_transport():
    _dependencies, client, transport = adapter()
    for private in (
        "api_key=private-value",
        "credential=private-value",
        r"C:\private\source.txt",
        "/var/lib/private/source.txt",
        "NARRATIVE_NORMALIZER_TRUST_KEY=value",
        "naz_ai_bot.sqlite3",
    ):
        request = generation.NarrativeModelRequest(
            "generation", CONTENT_MODEL, "Return JSON only.", private, {"type": "object"}
        )
        with pytest.raises(provider.NormalizerProviderError):
            invoke(client, request)
    assert transport.calls == []


def test_logs_never_contain_prompt_response_or_credential(caplog):
    caplog.set_level("DEBUG", logger=provider.__name__)
    transport = FakeTransport(['{"private_response":"not-logged"}'])
    _dependencies, client, _ = adapter(transport)
    invoke(client, narrative_request("generation", CONTENT_MODEL))
    rendered = caplog.text
    assert "Return JSON only" not in rendered
    assert "private_response" not in rendered
    assert "test-provider-key" not in rendered
    assert rendered == ""


def test_factory_tuple_is_exact_and_generic_service_is_mandatory():
    dependencies, _client, _transport = adapter()
    assert type(dependencies) is tuple
    assert len(dependencies) == 3
    assert type(dependencies[0]) is provider.ProductionNarrativeContextProvider
    assert type(dependencies[1]) is generation.NarrativeGenerationService
    assert type(dependencies[2]) is evidence.GenericEvidenceService


def test_factory_rejects_unexpected_transport_type():
    approved = authorization()
    with pytest.raises(provider.NormalizerProviderError) as caught:
        provider._build_authorized_adapter(approved, test_transport=object())
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID


def test_context_provider_rebinds_source_without_runtime_or_files(tmp_path):
    dependencies, _client, _transport = adapter()
    receipt = normalizer.FactExtractionReceipt(
        normalizer.SOURCE_CONTRACT_VERSION,
        1,
        2,
        0,
        0,
        True,
    )
    source = normalizer.SourceUnit(
        "safe/ref",
        "a" * 64,
        (
            normalizer.SourceFact("fact-1", "First safe fact.", "safe/ref", 1),
            normalizer.SourceFact("fact-2", "Second safe fact.", "safe/ref", 2),
        ),
        receipt,
    )
    value = dependencies[0].build(source)
    assert value.source_ref == "safe/ref"
    assert tuple(item.text for item in value.source_facts) == ("First safe fact.", "Second safe fact.")
    assert value.editorial_plan.source_ref == "safe/ref"
    assert value.naz_prompt_context.prompt_text == "Naz observes verified details without inventing facts."
    assert not list(tmp_path.iterdir())


def test_adapter_source_has_no_main_import_or_application_bootstrap():
    source = Path(provider.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "main" not in imported
    assert "memory" not in imported
    assert "telegram" not in imported
    assert "story_production" not in imported
    assert "openai" not in imported
    assert "httpx" not in imported


def test_fresh_process_import_has_zero_env_client_network_and_file_side_effects(tmp_path):
    script = r'''
import builtins, os, socket
original_getenv = os.getenv
def blocked_getenv(*args, **kwargs): raise AssertionError("env read")
os.getenv = blocked_getenv
class BlockedSocket:
    def __init__(self, *args, **kwargs): raise AssertionError("socket")
socket.socket = BlockedSocket
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "openai" or name.startswith("openai."): raise AssertionError("client import")
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
import narrative_normalizer_provider as provider
print(provider.NORMALIZER_PRODUCTION_ADAPTER_VERSION)
'''
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "external-pycache")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(provider.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert provider.NORMALIZER_PRODUCTION_ADAPTER_VERSION in result.stdout


def test_configuration_inspection_never_constructs_transport():
    summary = provider.inspect_live_configuration(live_env(), selected_source_count=5)
    assert summary["calculated_maximum_calls"] == 25
    assert summary["retry_count"] == 0


@pytest.mark.parametrize(
    ("profile", "count", "accepted"),
    (
        (provider.CANARY_RUN_PROFILE, 0, False),
        (provider.CANARY_RUN_PROFILE, 1, True),
        (provider.CANARY_RUN_PROFILE, 2, False),
        (provider.CANARY_RUN_PROFILE, 5, False),
        (provider.FIRST_FIVE_RUN_PROFILE, 1, False),
        (provider.FIRST_FIVE_RUN_PROFILE, 4, False),
        (provider.FIRST_FIVE_RUN_PROFILE, 5, True),
        (provider.FIRST_FIVE_RUN_PROFILE, 6, False),
    ),
    ids=(
        "canary-zero-rejected", "canary-one-accepted", "canary-two-rejected",
        "canary-five-rejected", "first-five-one-rejected", "first-five-four-rejected",
        "first-five-five-accepted", "first-five-six-rejected",
    ),
)
def test_closed_live_run_profile_source_counts(profile, count, accepted):
    sources = tuple(f"{index + 20:064x}" for index in range(count))
    if accepted:
        approved = authorization(sources=sources, run_profile=profile)
        assert approved.run_profile == profile
        assert approved.global_call_budget == count * 5
    else:
        with pytest.raises(provider.NormalizerProviderError) as caught:
            authorization(sources=sources, run_profile=profile)
        assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID


def test_live_run_profile_rules_are_immutable_code_owned_values():
    rules = normalizer.run_profiles.LIVE_RUN_PROFILE_RULES
    assert type(rules) is tuple
    assert tuple(rule.profile for rule in rules) == provider.LIVE_RUN_PROFILES
    with pytest.raises(dataclasses.FrozenInstanceError):
        rules[0].call_budget = 25


@pytest.mark.parametrize(
    "profile",
    (None, "canary-v1", "unknown-profile", StringSubclass(provider.CANARY_RUN_PROFILE)),
    ids=("missing", "alias", "unknown", "str-subclass"),
)
def test_unknown_or_nonexact_live_run_profile_rejected_before_worker(profile, monkeypatch):
    constructions = []
    monkeypatch.setattr(provider, "_start_worker_channel", lambda *_a, **_k: constructions.append(True))
    with pytest.raises(provider.NormalizerProviderError):
        authorization(
            sources=(AUTHORIZED_SOURCES[0],),
            run_profile=profile,
        )
    assert constructions == []


def test_caller_budget_override_is_rejected_before_worker(monkeypatch):
    constructions = []
    monkeypatch.setattr(provider, "_start_worker_channel", lambda *_a, **_k: constructions.append(True))
    with pytest.raises(provider.NormalizerProviderError) as caught:
        provider.authorize_live_provider_run(
            adapter_spec=provider.PRODUCTION_ADAPTER_SPEC,
            local_execution_enabled=True,
            live_provider_enabled=True,
            run_profile=provider.CANARY_RUN_PROFILE,
            source_identities=(AUTHORIZED_SOURCES[0],),
            env=live_env(),
            trust_service=TEST_TRUST_SERVICE,
            review_authority_root=TEST_AUTHORITY_ROOT,
            global_call_budget=25,
        )
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert constructions == []


def test_canary_profile_enforces_one_source_five_operations_and_no_repair():
    transport = FakeTransport()
    _dependencies, client, _ = adapter(
        transport,
        sources=(AUTHORIZED_SOURCES[0],),
        run_profile=provider.CANARY_RUN_PROFILE,
    )
    operations = (
        "evidence_coverage", "evidence_extraction", "evidence_adjudication",
        "generation", "adjudication",
    )
    for operation in operations:
        model = CONTENT_MODEL if operation in {
            "evidence_coverage", "evidence_extraction", "generation",
        } else REVIEW_MODEL
        request = (
            evidence_request(operation, model)
            if operation.startswith("evidence_")
            else narrative_request(operation, model)
        )
        invoke(client, request, AUTHORIZED_SOURCES[0])
    assert len(transport.calls) == provider.CANARY_MAX_PROVIDER_CALLS
    assert client.ledger.max_calls == provider.CANARY_MAX_PROVIDER_CALLS
    with pytest.raises(provider.NormalizerProviderError):
        invoke(client, narrative_request("generation", CONTENT_MODEL), AUTHORIZED_SOURCES[0])
    with pytest.raises(provider.NormalizerProviderError):
        invoke(client, narrative_request("repair", CONTENT_MODEL), AUTHORIZED_SOURCES[0])
    with pytest.raises(provider.NormalizerProviderError):
        invoke(client, narrative_request("generation", CONTENT_MODEL), AUTHORIZED_SOURCES[1])
    assert len(transport.calls) == provider.CANARY_MAX_PROVIDER_CALLS


def _cli_paths(tmp_path: Path) -> list[str]:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    state = tmp_path / "state"
    registry = state / "registry.json"
    inbox.mkdir()
    outbox.mkdir()
    state.mkdir()
    return [
        "--inbox-root", str(inbox),
        "--registry-path", str(registry),
        "--outbox-root", str(outbox),
    ]


@pytest.mark.parametrize("command", ("coverage-snapshot", "scan", "list", "status"))
def test_nonexecution_cli_commands_never_load_adapter(tmp_path, monkeypatch, command):
    monkeypatch.setattr(normalizer, "scan_needs_narrative", lambda _policy: ())
    monkeypatch.setattr(normalizer, "load_adapter", lambda _spec: (_ for _ in ()).throw(AssertionError("adapter")))
    monkeypatch.setattr(provider, "_start_worker_channel", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker")))
    assert cli.run([*_cli_paths(tmp_path), command]) == 0


def test_cli_help_never_imports_provider_in_fresh_process(tmp_path):
    script = r'''
import builtins, sys
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == "narrative_normalizer_provider": raise AssertionError("provider import")
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from tools import run_narrative_normalizer as cli
try: cli.run(["--help"])
except SystemExit as exc: raise SystemExit(0 if exc.code == 0 else exc.code)
'''
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = str(tmp_path / "external-pycache")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=Path(provider.__file__).parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_dry_run_never_loads_or_constructs_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(normalizer, "scan_needs_narrative", lambda _policy: ())
    monkeypatch.setattr(normalizer, "load_adapter", lambda _spec: (_ for _ in ()).throw(AssertionError("adapter")))
    monkeypatch.setattr(provider, "_start_worker_channel", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("worker")))
    rc = cli.run([
        *_cli_paths(tmp_path),
        "normalize", "--all", "--dry-run",
        "--enable-local-execution", "--enable-live-provider",
        "--adapter", provider.PRODUCTION_ADAPTER_SPEC,
    ])
    assert rc == 0


def test_approval_never_constructs_provider(tmp_path, monkeypatch):
    key = tmp_path / "trust.key"
    key.write_text("eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg=\n", encoding="ascii")
    if os.name != "nt":
        key.chmod(0o600)
    authority = tmp_path / "authority"
    monkeypatch.setattr(normalizer, "load_adapter", lambda _spec: (_ for _ in ()).throw(AssertionError("adapter")))
    rc = cli.run([
        *_cli_paths(tmp_path),
        "--trust-key-file", str(key),
        "--review-authority-root", str(authority),
        "approve", "safe/ref", "a" * 64, "b" * 64,
    ])
    assert rc == 2


@pytest.mark.parametrize(
    ("local_gate", "provider_gate", "live_value", "expected"),
    (
        (False, False, "", 2),
        (True, False, "1", 2),
        (False, True, "1", 2),
        (True, True, "true", 2),
        (True, True, "1", 2),
    ),
    ids=("no-gates", "local-only", "provider-only", "wrong-env", "five-targets-still-required"),
)
def test_cli_live_gate_combinations_fail_before_adapter(
    tmp_path, monkeypatch, local_gate, provider_gate, live_value, expected
):
    rows = tuple((f"source/{index}", f"{index:064x}") for index in range(1, 6))
    monkeypatch.setattr(normalizer, "scan_needs_narrative", lambda _policy: rows)
    monkeypatch.setattr(normalizer, "load_adapter", lambda _spec: (_ for _ in ()).throw(AssertionError("adapter")))
    monkeypatch.setenv(provider.LIVE_ENV, live_value)
    args = [*_cli_paths(tmp_path), "normalize", "--all", "--adapter", provider.PRODUCTION_ADAPTER_SPEC]
    if local_gate:
        args.append("--enable-local-execution")
    if provider_gate:
        args.append("--enable-live-provider")
    assert cli.run(args) == expected


def test_cli_exact_five_opaque_targets_prints_preflight_before_factory(tmp_path, monkeypatch, capsys):
    rows = tuple((f"source/{index}", f"{index:064x}") for index in range(1, 7))
    identities = tuple(normalizer.source_identity(ref, digest) for ref, digest in rows[:5])
    monkeypatch.setattr(normalizer, "scan_needs_narrative", lambda _policy: rows)
    monkeypatch.setattr(cli, "_load_trust_service", lambda _args: trust.NarrativeTrustService(b"x" * 32))
    class FakeAuthorization:
        def safe_summary(self):
            return {
            "adapter_version": provider.NORMALIZER_PRODUCTION_ADAPTER_VERSION,
            "selected_source_count": 5,
            "calculated_maximum_calls": 25,
            "retry_count": 0,
            }

    class Result:
        def safe_summary(self):
            return {"status_counts": {}, "total": 5}

    class Service:
        def __init__(self, **kwargs):
            del kwargs

        def normalize_batch(self, selected, **kwargs):
            del kwargs
            assert tuple(selected) == rows[:5]
            return Result()

    fake_dependencies, _client, _transport = adapter()
    fake_authorization = FakeAuthorization()
    captured_authorization = {}

    def authorize_from_cli(**kwargs):
        captured_authorization.update(kwargs)
        return fake_authorization

    monkeypatch.setattr(
        provider,
        "authorize_live_provider_run",
        authorize_from_cli,
    )
    monkeypatch.setattr(
        provider,
        "production_adapter_factory",
        lambda value: fake_dependencies if value is fake_authorization else None,
    )
    monkeypatch.setattr(normalizer, "NarrativeNormalizerService", Service)
    args = [
        *_cli_paths(tmp_path),
        "--review-authority-root", str(tmp_path / "authority"),
        "normalize", "--enable-local-execution", "--enable-live-provider",
        "--live-run-profile", provider.FIRST_FIVE_RUN_PROFILE,
        "--adapter", provider.PRODUCTION_ADAPTER_SPEC,
    ]
    for identity in identities:
        args.extend(("--source-identity", identity))
    assert cli.run(args, _allow_local_review_authority_for_tests=True) == 0
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]["live_provider_preflight"]["selected_source_count"] == 5
    assert output[0]["live_provider_preflight"]["calculated_maximum_calls"] == 25
    assert output[1]["total"] == 5
    assert captured_authorization["adapter_spec"] == provider.PRODUCTION_ADAPTER_SPEC
    assert captured_authorization["local_execution_enabled"] is True
    assert captured_authorization["live_provider_enabled"] is True
    assert captured_authorization["run_profile"] == provider.FIRST_FIVE_RUN_PROFILE
    assert captured_authorization["source_identities"] == identities


def test_cli_rejects_sixth_target_before_factory(tmp_path, monkeypatch):
    rows = tuple((f"source/{index}", f"{index:064x}") for index in range(1, 7))
    identities = tuple(normalizer.source_identity(ref, digest) for ref, digest in rows)
    monkeypatch.setattr(normalizer, "scan_needs_narrative", lambda _policy: rows)
    monkeypatch.setattr(normalizer, "load_adapter", lambda _spec: (_ for _ in ()).throw(AssertionError("adapter")))
    args = [
        *_cli_paths(tmp_path), "normalize", "--enable-local-execution", "--enable-live-provider",
        "--adapter", provider.PRODUCTION_ADAPTER_SPEC,
    ]
    for identity in identities:
        args.extend(("--source-identity", identity))
    assert cli.run(args) == 2


def test_cli_one_source_canary_requires_profile_and_builds_non_destructive_retry(
    tmp_path, monkeypatch, capsys
):
    row = ("source/one", "a" * 64)
    identity = normalizer.source_identity(*row)
    monkeypatch.setattr(normalizer, "scan_needs_narrative", lambda _policy: (row,))
    monkeypatch.setattr(
        cli,
        "_load_trust_service",
        lambda _args: trust.NarrativeTrustService(b"x" * 32),
    )

    class FakeAuthorization:
        def safe_summary(self):
            return {
                "adapter_version": provider.NORMALIZER_PRODUCTION_ADAPTER_VERSION,
                "run_profile": provider.CANARY_RUN_PROFILE,
                "selected_source_count": 1,
                "calculated_maximum_calls": 5,
                "retry_count": 0,
            }

    class Result:
        def safe_summary(self):
            return {"status_counts": {}, "total": 1}

    captured = {}

    class Service:
        def __init__(self, **kwargs):
            captured["service"] = kwargs

        def normalize_batch(self, selected, **kwargs):
            captured["selected"] = tuple(selected)
            captured["batch"] = kwargs
            return Result()

    fake_dependencies, _client, _transport = adapter(
        sources=(identity,), run_profile=provider.CANARY_RUN_PROFILE
    )
    fake_authorization = FakeAuthorization()
    def authorize(**kwargs):
        captured["authorization"] = kwargs
        return fake_authorization

    monkeypatch.setattr(provider, "authorize_live_provider_run", authorize)
    monkeypatch.setattr(
        provider,
        "production_adapter_factory",
        lambda value: fake_dependencies if value is fake_authorization else None,
    )
    monkeypatch.setattr(normalizer, "NarrativeNormalizerService", Service)
    args = [
        *_cli_paths(tmp_path),
        "--review-authority-root", str(tmp_path / "authority"),
        "normalize",
        "--enable-local-execution",
        "--enable-live-provider",
        "--live-run-profile", provider.CANARY_RUN_PROFILE,
        "--adapter", provider.PRODUCTION_ADAPTER_SPEC,
        "--source-identity", identity,
        "--manual-retry-request-id", "manual-canary-retry-0001",
        "--expected-failed-attempt-id", "b" * 32,
        "--expected-failed-claim-digest", "c" * 64,
    ]

    assert cli.run(args, _allow_local_review_authority_for_tests=True) == 0
    retry = captured["batch"]["manual_retry"]
    assert type(retry) is normalizer.ManualRetryRequest
    assert retry.source_identity == identity
    assert retry.run_profile == provider.CANARY_RUN_PROFILE
    assert captured["authorization"]["source_identities"] == (identity,)
    assert captured["authorization"]["run_profile"] == provider.CANARY_RUN_PROFILE
    assert captured["selected"] == (row,)
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert output[0]["live_provider_preflight"]["calculated_maximum_calls"] == 5


def test_cli_live_construction_without_explicit_profile_is_rejected_before_authorization(
    tmp_path, monkeypatch
):
    row = ("source/one", "a" * 64)
    identity = normalizer.source_identity(*row)
    monkeypatch.setattr(normalizer, "scan_needs_narrative", lambda _policy: (row,))
    calls = []
    monkeypatch.setattr(
        provider,
        "authorize_live_provider_run",
        lambda **kwargs: calls.append(kwargs),
    )
    rc = cli.run([
        *_cli_paths(tmp_path),
        "normalize",
        "--enable-local-execution",
        "--enable-live-provider",
        "--adapter", provider.PRODUCTION_ADAPTER_SPEC,
        "--source-identity", identity,
    ])
    assert rc == 2
    assert calls == []


def test_fake_production_adapter_fast_path_uses_two_calls_then_resume_and_approval_use_zero(
    tmp_path,
):
    core = _core_normalizer_test_helpers()
    spec, policy, _source_path, record = core.write_source(tmp_path, "quiet_object")
    source = normalizer.read_source_unit(
        policy, record.source_ref, expected_digest=record.source_digest
    )
    transport = FakeTransport()
    dependencies, client, _ = adapter(
        transport, sources=authorized_sources_for_records((record,))
    )
    context = dependencies[0].build(source)
    fixture = load_fixture(str(spec["context_fixture"]))
    story = _bind_story_payload_to_review_context(
        core.normalizer_generation_payload(
            fixture, fact_count=len(source.facts), normalizer_name="quiet_object"
        ),
        context,
    )
    transport.replies.extend((story, fake_adjudication_payload(story, context)))
    service = normalizer.NarrativeNormalizerService(
        policy=policy,
        context_provider=dependencies[0],
        generation_service=dependencies[1],
        evidence_service=dependencies[2],
        trust_service=policy.narrative_trust_service,
        clock=lambda: core.NOW,
    )

    created = service.normalize_source(record.source_ref, record.source_digest)
    assert created.status == normalizer.OUTCOME_DRAFT_READY_FOR_REVIEW
    assert created.model_call_count == 2
    assert [item.operation for item in client.ledger.snapshot()] == ["generation", "adjudication"]
    assert [item.model_id for item in client.ledger.snapshot()] == [CONTENT_MODEL, REVIEW_MODEL]

    call_count = len(transport.calls)
    existing = service.normalize_source(record.source_ref, record.source_digest)
    assert existing.status == normalizer.OUTCOME_EXISTING_DRAFT
    assert existing.model_call_count == 0
    assert len(transport.calls) == call_count

    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    value = normalizer.validate_draft_directory(
        draft,
        trust_service=policy.narrative_trust_service,
        review_authority_root=policy.narrative_review_authority_root,
        require_trust=True,
    )
    approval = service.store.approve(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=value["manifest"]["draft_identity"],
        reviewed_at=core.NOW.isoformat(),
    )
    assert approval.status == quarantine.CLASS_READY
    assert len(transport.calls) == call_count


def test_fake_production_adapter_generic_path_obeys_exact_budget_and_model_free_approval(
    tmp_path,
):
    core = _core_normalizer_test_helpers()
    _shape, filename, body, propositions = core._GENERIC_E2E_SHAPES[0]
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    source_path = inbox / "Generic-Provider" / "2026-08-18"
    source_path.mkdir(parents=True)
    registry.parent.mkdir(parents=True)
    (source_path / filename).write_text(body, encoding="utf-8")
    trust_service = trust.NarrativeTrustService(core.TEST_TRUST_KEY)
    policy = quarantine.QuarantinePathPolicy(
        inbox, registry, outbox, trust_service, tmp_path / "review-authority"
    )
    quarantine.reconcile_complete_backlog(policy, now=core.NOW)
    record = quarantine.read_registry(registry).records[0]
    raw_source = normalizer.read_source_unit(
        policy,
        record.source_ref,
        expected_digest=record.source_digest,
        allow_insufficient=True,
    )
    documents = normalizer.read_source_documents(
        policy, record.source_ref, expected_digest=record.source_digest
    )
    extraction, evidence_adjudication = core.generic_evidence_responses(
        documents, propositions
    )
    coverage_v2, extraction_v2, adjudication_v2 = _generic_v2_responses(
        documents, propositions
    )
    preparation = evidence.GenericEvidenceService(
        core.QueueClient((copy.deepcopy(extraction), copy.deepcopy(evidence_adjudication))),
        extraction_model=CONTENT_MODEL,
        adjudication_model=REVIEW_MODEL,
    ).resolve(documents)
    assert preparation.status == "verified" and preparation.verified_bundle is not None
    verified_source = normalizer._source_from_verified_evidence(
        raw_source, documents, preparation.verified_bundle
    )

    transport = FakeTransport()
    dependencies, client, _ = adapter(
        transport, sources=authorized_sources_for_records((record,))
    )
    context = dependencies[0].build(verified_source)
    story = _bind_story_payload_to_review_context(
        core.generic_story_response(load_fixture("quiet_object"), propositions), context
    )
    story_adjudication = fake_adjudication_payload(story, context)
    transport.replies.extend((coverage_v2, extraction_v2, adjudication_v2, story, story_adjudication))
    service = normalizer.NarrativeNormalizerService(
        policy=policy,
        context_provider=dependencies[0],
        generation_service=dependencies[1],
        evidence_service=dependencies[2],
        trust_service=trust_service,
        clock=lambda: core.NOW,
    )

    try:
        outcome = service.normalize_source(record.source_ref, record.source_digest)
    except normalizer.NarrativeNormalizerError as error:
        pytest.fail(
            f"{error.reason_code}; transport_calls={len(transport.calls)}; "
            f"ledger={client.ledger.snapshot()}"
        )
    expected_calls = 5
    assert outcome.status == normalizer.OUTCOME_DRAFT_READY_FOR_REVIEW, (
        outcome, client.ledger.snapshot(), transport.calls
    )
    assert outcome.evidence_path == "generic"
    assert outcome.model_call_count == expected_calls
    assert [item.operation for item in client.ledger.snapshot()] == [
        "evidence_coverage", "evidence_extraction", "evidence_adjudication",
        "generation", "adjudication",
    ]
    assert [item.model_id for item in client.ledger.snapshot()] == [
        CONTENT_MODEL, CONTENT_MODEL, REVIEW_MODEL, CONTENT_MODEL, REVIEW_MODEL,
    ]

    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    value = normalizer.validate_draft_directory(
        draft,
        trust_service=trust_service,
        review_authority_root=policy.narrative_review_authority_root,
        require_trust=True,
    )
    before_approval = len(transport.calls)
    approval = service.store.approve(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=value["manifest"]["draft_identity"],
        reviewed_at=core.NOW.isoformat(),
    )
    assert approval.status == quarantine.CLASS_READY
    assert len(transport.calls) == before_approval


def test_fake_production_canary_manual_retry_uses_one_source_and_exact_five_calls(tmp_path):
    core = _core_normalizer_test_helpers()
    records, service, client, transport = _generic_provider_batch(
        tmp_path,
        source_count=1,
        repair=False,
        run_profile=provider.CANARY_RUN_PROFILE,
    )
    record = records[0]
    source = normalizer.read_source_unit(
        service.policy,
        record.source_ref,
        expected_digest=record.source_digest,
        allow_insufficient=True,
    )
    timestamp = core.NOW.isoformat().replace("+00:00", "Z")
    processing = normalizer._claim_payload(
        source,
        attempt_id="a" * 32,
        state=normalizer.CLAIM_PROCESSING,
        started_at=timestamp,
        updated_at=timestamp,
    )
    service.store.write_claim(processing)
    service.store.write_claim(dict(
        processing,
        state=normalizer.CLAIM_FAILED,
        reason_code="narrative_normalizer_evidence_invalid",
    ))
    old_path = service.store.claim_path(record.source_ref, record.source_digest)
    old_bytes = old_path.read_bytes()
    old_digest = hashlib.sha256(old_bytes).hexdigest()
    retry = normalizer.ManualRetryRequest(
        normalizer.source_identity(record.source_ref, record.source_digest),
        record.source_digest,
        "a" * 32,
        old_digest,
        "production-shape-canary-retry-0001",
        provider.CANARY_RUN_PROFILE,
    )

    outcome = service.normalize_source(
        record.source_ref,
        record.source_digest,
        manual_retry=retry,
    )

    assert outcome.status == normalizer.OUTCOME_DRAFT_READY_FOR_REVIEW
    assert outcome.model_call_count == provider.CANARY_MAX_PROVIDER_CALLS
    assert len(transport.calls) == provider.CANARY_MAX_PROVIDER_CALLS
    assert len(client.ledger.snapshot()) == provider.CANARY_MAX_PROVIDER_CALLS
    assert service.store.archived_attempt_bytes(
        retry.source_identity, retry.previous_failed_attempt_id
    ) == old_bytes
    current = service.store.read_claim(record.source_ref, record.source_digest)
    assert current is not None and current["state"] == normalizer.CLAIM_COMPLETED
    assert current["attempt_id"] != retry.previous_failed_attempt_id
    with pytest.raises(provider.NormalizerProviderError):
        invoke(client, narrative_request("generation", CONTENT_MODEL), AUTHORIZED_SOURCES[0])
    assert len(transport.calls) == provider.CANARY_MAX_PROVIDER_CALLS


def test_fake_production_adapter_source_three_failure_has_no_retry_and_batch_continues(tmp_path):
    records, service, client, transport = _generic_provider_batch(
        tmp_path, source_count=6, repair=False, fail_index=2
    )
    result = service.normalize_batch(
        tuple((item.source_ref, item.source_digest) for item in records[:5])
    )
    assert [item.status for item in result.outcomes] == [
        normalizer.OUTCOME_DRAFT_READY_FOR_REVIEW,
        normalizer.OUTCOME_DRAFT_READY_FOR_REVIEW,
        normalizer.OUTCOME_FAILED,
        normalizer.OUTCOME_DRAFT_READY_FOR_REVIEW,
        normalizer.OUTCOME_DRAFT_READY_FOR_REVIEW,
    ]
    assert [item.model_call_count for item in result.outcomes] == [5, 5, 1, 5, 5]
    assert len(transport.calls) == 21
    ledger = client.ledger.snapshot()
    assert ledger[10].source_identity == normalizer.source_identity(
        records[2].source_ref, records[2].source_digest
    )
    assert ledger[10].safe_outcome == provider.PROVIDER_TRANSPORT_FAILED
    assert all(item.attempt_number == 1 for item in ledger)
    before = len(transport.calls)
    ledger_before = len(ledger)
    outcome = service.normalize_source(records[5].source_ref, records[5].source_digest)
    assert outcome.status == normalizer.OUTCOME_FAILED
    assert len(transport.calls) == before
    assert len(client.ledger.snapshot()) == ledger_before


def test_fake_production_adapter_exact_five_generic_sources_never_begin_a_sixth(
    tmp_path,
):
    records, service, client, transport = _generic_provider_batch(
        tmp_path, source_count=6, repair=False
    )
    selected = records[:5]
    result = service.normalize_batch(
        tuple((item.source_ref, item.source_digest) for item in selected)
    )
    expected_calls = 25
    assert len(result.outcomes) == 5
    assert all(item.status == normalizer.OUTCOME_DRAFT_READY_FOR_REVIEW for item in result.outcomes)
    assert sum(item.model_call_count for item in result.outcomes) == expected_calls
    assert len(transport.calls) == expected_calls
    assert len(client.ledger.snapshot()) == expected_calls
    sixth = records[5]
    assert not service.store.draft_path(
        sixth.source_digest, source_ref=sixth.source_ref
    ).exists()

    before = len(transport.calls)
    ledger_before = len(client.ledger.snapshot())
    outcome = service.normalize_source(sixth.source_ref, sixth.source_digest)
    assert outcome.status == normalizer.OUTCOME_FAILED
    assert len(transport.calls) == before
    assert len(client.ledger.snapshot()) == ledger_before


@pytest.mark.parametrize(
    "fake",
    (None, {}, object(), True, False, "authorized"),
    ids=("none", "mapping", "object", "true", "false", "string"),
)
def test_direct_factory_rejects_unverified_authorization_before_client(monkeypatch, fake):
    constructions = []
    monkeypatch.setattr(
        provider,
        "_start_worker_channel",
        lambda *_args: constructions.append(True),
    )
    with pytest.raises(provider.NormalizerProviderError) as caught:
        provider.production_adapter_factory(fake)
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert constructions == []


def test_zero_argument_factory_is_rejected_before_client(monkeypatch):
    constructions = []
    monkeypatch.setattr(
        provider,
        "_start_worker_channel",
        lambda *_args: constructions.append(True),
    )
    with pytest.raises(provider.NormalizerProviderError):
        provider.production_adapter_factory()
    assert constructions == []


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("adapter_spec", "narrative_normalizer_provider:other"),
        ("adapter_spec", StringSubclass(provider.PRODUCTION_ADAPTER_SPEC)),
        ("local_execution_enabled", False),
        ("local_execution_enabled", 1),
        ("local_execution_enabled", "true"),
        ("live_provider_enabled", False),
        ("live_provider_enabled", 1),
        ("live_provider_enabled", "1"),
        ("source_identities", AUTHORIZED_SOURCES[:4]),
        ("source_identities", (*AUTHORIZED_SOURCES, "f" * 64)),
        ("source_identities", (*AUTHORIZED_SOURCES[:4], AUTHORIZED_SOURCES[0])),
        ("source_identities", list(AUTHORIZED_SOURCES)),
        ("source_identities", (*AUTHORIZED_SOURCES[:4], StringSubclass("f" * 64))),
        ("trust_service", None),
        ("trust_service", object()),
        ("review_authority_root", "C:/review-authority"),
    ),
    ids=(
        "wrong-spec", "spec-subclass", "local-false", "local-int", "local-string",
        "live-false", "live-int", "live-string", "four-sources", "six-sources",
        "duplicate-source", "mutable-source-list", "source-subclass", "missing-trust",
        "fake-trust", "authority-not-path",
    ),
)
def test_authorization_boundary_rejects_each_missing_or_forged_gate(field, value):
    kwargs = {
        "adapter_spec": provider.PRODUCTION_ADAPTER_SPEC,
        "local_execution_enabled": True,
        "live_provider_enabled": True,
        "run_profile": provider.FIRST_FIVE_RUN_PROFILE,
        "source_identities": AUTHORIZED_SOURCES,
        "env": live_env(),
        "trust_service": TEST_TRUST_SERVICE,
        "review_authority_root": TEST_AUTHORITY_ROOT,
    }
    kwargs[field] = value
    with pytest.raises(provider.NormalizerProviderError) as caught:
        provider.authorize_live_provider_run(**kwargs)
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID


@pytest.mark.parametrize(
    "live_value",
    ("true", "TRUE", "yes", "01", "1 ", " 1", StringSubclass("1")),
    ids=("true", "uppercase", "yes", "zero-one", "suffix-space", "prefix-space", "subclass"),
)
def test_authorization_requires_exact_plain_live_environment_value(live_value):
    with pytest.raises(provider.NormalizerProviderError) as caught:
        authorization(env=live_env(**{provider.LIVE_ENV: live_value}))
    assert caught.value.reason_code == provider.PROVIDER_DISABLED


def test_authorization_copies_source_order_into_frozen_tuple():
    values = list(AUTHORIZED_SOURCES)
    approved = authorization(sources=tuple(values))
    values.reverse()
    assert approved.authorized_source_identities == AUTHORIZED_SOURCES
    with pytest.raises(dataclasses.FrozenInstanceError):
        approved.run_id = "f" * 24


def test_authorization_does_not_construct_transport(monkeypatch):
    constructions = []
    monkeypatch.setattr(
        provider,
        "_start_worker_channel",
        lambda *_args: constructions.append(True),
    )
    approved = authorization()
    assert type(approved) is provider.LiveProviderRunAuthorization
    assert constructions == []


def test_one_authorization_constructs_factory_only_once(monkeypatch):
    approved = authorization()
    calls = []
    original = provider._start_worker_channel

    def start(config, secret, capsule, *, scripted_replies):
        calls.append(True)
        return original(config, secret, capsule, scripted_replies=())

    monkeypatch.setattr(
        provider,
        "_start_worker_channel",
        start,
    )
    provider.production_adapter_factory(approved)
    with pytest.raises(provider.NormalizerProviderError) as caught:
        provider.production_adapter_factory(approved)
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert calls == [True]


def test_identical_content_and_adjudication_models_fail_before_client(monkeypatch):
    constructions = []
    monkeypatch.setattr(
        provider,
        "_start_worker_channel",
        lambda *_args: constructions.append(True),
    )
    with pytest.raises(provider.NormalizerProviderError) as caught:
        authorization(env=live_env(**{provider.ADJUDICATION_MODEL_ENV: CONTENT_MODEL}))
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert constructions == []


@pytest.mark.parametrize(
    "source_identity",
    ("f" * 64, StringSubclass(AUTHORIZED_SOURCES[0]), "not-a-source"),
    ids=("unknown", "subclass", "malformed"),
)
def test_source_scope_rejects_unapproved_identity_before_transport(source_identity):
    _dependencies, client, transport = adapter()
    with pytest.raises(provider.NormalizerProviderError):
        invoke(client, narrative_request("generation", CONTENT_MODEL), source_identity)
    assert transport.calls == []
    assert client.ledger.snapshot() == ()


def test_failed_source_operation_is_consumed_and_cannot_be_replaced():
    transport = FakeTransport((RuntimeError("private failure"),))
    _dependencies, client, _ = adapter(transport)
    with pytest.raises(provider.NormalizerProviderError):
        invoke(client, narrative_request("generation", CONTENT_MODEL), AUTHORIZED_SOURCES[0])
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, narrative_request("generation", CONTENT_MODEL), AUTHORIZED_SOURCES[0])
    assert caught.value.reason_code == provider.PROVIDER_BUDGET_EXCEEDED
    with pytest.raises(provider.NormalizerProviderError):
        invoke(client, narrative_request("generation", CONTENT_MODEL), "f" * 64)
    assert len(transport.calls) == 1
    assert len(client.ledger.snapshot()) == 1


def test_returned_services_and_client_expose_no_mutable_run_state():
    dependencies, client, _transport = adapter()
    forbidden = {
        "_run_state", "run_state", "authorization", "_records", "records",
        "_operation_counts", "operation_counts", "_active", "_run_guard",
    }
    for value in (*dependencies, client, client.ledger):
        assert forbidden.isdisjoint(dir(value))
    for value in (client, client.ledger):
        with pytest.raises(TypeError):
            vars(value)
    assert not hasattr(client.ledger, "_state")
    assert not hasattr(client.ledger, "begin")
    assert not hasattr(client.ledger, "complete")
    assert not hasattr(client.ledger, "finish")
    assert "authorized_source_identities" not in ProductionModelClient_slots(client)
    channel = object.__getattribute__(client, "_ProductionModelClient__channel")
    with pytest.raises(TypeError):
        vars(channel)
    assert {
        "authorization", "authorized_source_identities", "source_allowlist", "budget",
        "ledger", "records", "provider_client",
    }.isdisjoint(dir(channel))
    function_globals = provider.ProductionModelClient.generate_json.__globals__
    assert "_CLIENT_STATES" not in function_globals
    assert "narrative_normalizer_provider_worker" not in {
        getattr(value, "__name__", "") for value in function_globals.values()
    }
    assert not any(type(value) is provider._ProviderRunState for value in function_globals.values())


def ProductionModelClient_slots(client):
    return tuple(name for name in dir(client) if name.startswith("_ProductionModelClient__"))


def test_worker_process_launch_is_explicit_sanitized_and_non_shell():
    tree = ast.parse(Path(provider.__file__).read_text(encoding="utf-8"))
    popen = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    )
    keywords = {item.arg: item.value for item in popen.keywords}
    assert isinstance(keywords["shell"], ast.Constant) and keywords["shell"].value is False
    assert {"cwd", "env", "stdin", "stdout", "stderr", "encoding"} <= set(keywords)
    assert isinstance(keywords["stderr"], ast.Attribute)
    assert keywords["stderr"].attr == "DEVNULL"

    environment = provider._sanitized_worker_environment()
    assert set(environment) <= {
        "PYTHONIOENCODING", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
        "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
    }
    assert provider.API_KEY_ENV not in environment
    assert "PATH" not in environment
    assert "NARRATIVE_NORMALIZER_TRUST_KEY" not in environment


@pytest.mark.parametrize("message", ({}, {"schema_version": provider.IPC_REQUEST_VERSION}), ids=("empty", "missing-fields"))
def test_parent_rejects_malformed_ipc_before_worker_message(message):
    _dependencies, client, transport = adapter()
    channel = object.__getattribute__(client, "_ProductionModelClient__channel")
    assert channel._test_messages() == 0
    with pytest.raises(provider.NormalizerProviderError) as caught:
        channel._exchange(message)
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert channel._test_messages() == 0
    assert transport.calls == []
    assert client.ledger.snapshot() == ()


def test_worker_defensively_rejects_plain_but_wrong_run_id_without_transport():
    _dependencies, client, transport = adapter()
    channel = object.__getattribute__(client, "_ProductionModelClient__channel")
    message = ipc_message(
        client,
        "1" * 24,
        narrative_request("generation", CONTENT_MODEL),
        run_id="0" * 24,
    )
    response = channel._exchange(message)
    assert response == {
        "schema_version": provider.IPC_RESPONSE_VERSION,
        "status": "error",
        "reason_code": provider.PROVIDER_CONFIGURATION_INVALID,
    }
    assert channel._test_messages() == 1
    assert transport.calls == []
    assert client.ledger.snapshot() == ()


@pytest.mark.parametrize(
    ("field", "mutate"),
    (
        ("request_id", lambda value: StringSubclass(value)),
        ("run_id", lambda value: StringSubclass(value)),
        ("source_identity", lambda value: StringSubclass(value)),
        ("operation", lambda value: StringSubclass(value)),
        ("schema_version", lambda value: StringSubclass(value)),
        ("payload", lambda value: DictSubclass(value)),
        ("payload", lambda value: types.MappingProxyType(value)),
    ),
    ids=(
        "request-id-str-subclass", "run-id-str-subclass", "source-str-subclass",
        "operation-str-subclass", "schema-str-subclass", "payload-dict-subclass",
        "payload-mapping-proxy",
    ),
)
def test_parent_exact_ipc_types_reject_before_pipe(field, mutate):
    _dependencies, client, transport = adapter()
    channel = object.__getattribute__(client, "_ProductionModelClient__channel")
    message = ipc_message(client, "4" * 24, narrative_request("generation", CONTENT_MODEL))
    message[field] = mutate(message[field])
    assert channel._test_messages() == 0
    with pytest.raises(provider.NormalizerProviderError) as caught:
        channel._exchange(message)
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert channel._test_messages() == 0
    assert transport.calls == []
    assert client.ledger.snapshot() == ()


@pytest.mark.parametrize("request_id", ("", " ", "a" * 23, "g" * 24), ids=("empty", "whitespace", "short", "non-hex"))
def test_parent_rejects_invalid_plain_request_id_before_pipe(request_id):
    _dependencies, client, transport = adapter()
    channel = object.__getattribute__(client, "_ProductionModelClient__channel")
    message = ipc_message(client, request_id, narrative_request("generation", CONTENT_MODEL))
    with pytest.raises(provider.NormalizerProviderError) as caught:
        channel._exchange(message)
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert channel._test_messages() == 0
    assert transport.calls == []
    assert client.ledger.snapshot() == ()


def test_plain_exact_ipc_envelope_still_reaches_worker_and_transport_once():
    _dependencies, client, transport = adapter()
    channel = object.__getattribute__(client, "_ProductionModelClient__channel")
    message = ipc_message(client, "5" * 24, narrative_request("generation", CONTENT_MODEL))
    assert all(type(message[field]) is str for field in (
        "schema_version", "run_id", "request_id", "source_identity", "operation"
    ))
    assert type(message["payload"]) is dict
    assert channel._exchange(message)["status"] == "ok"
    assert channel._test_messages() == 1
    assert len(transport.calls) == 1
    assert len(client.ledger.snapshot()) == 1


def test_sequential_exact_request_id_duplicate_replays_without_transport_or_budget():
    _dependencies, client, transport = adapter()
    message = ipc_message(client, "a" * 24, narrative_request("generation", CONTENT_MODEL))
    first = exchange_ipc(client, message)
    second = exchange_ipc(client, copy.deepcopy(message))
    assert second == first
    assert len(transport.calls) == 1
    records = client.ledger.snapshot()
    assert len(records) == 1
    assert records[0].request_id == "a" * 24


def test_same_request_id_generation_then_adjudication_is_conflict_before_transport():
    _dependencies, client, transport = adapter()
    request_id = "b" * 24
    first = ipc_message(client, request_id, narrative_request("generation", CONTENT_MODEL))
    second = ipc_message(client, request_id, narrative_request("adjudication", REVIEW_MODEL))
    assert exchange_ipc(client, first)["status"] == "ok"
    response = exchange_ipc(client, second)
    assert response["reason_code"] == provider.PROVIDER_REQUEST_CONFLICT
    assert len(transport.calls) == 1
    assert len(client.ledger.snapshot()) == 1


def test_same_request_id_different_source_is_conflict_before_transport():
    _dependencies, client, transport = adapter()
    request_id = "c" * 24
    request = narrative_request("generation", CONTENT_MODEL)
    assert exchange_ipc(client, ipc_message(client, request_id, request))["status"] == "ok"
    response = exchange_ipc(client, ipc_message(
        client, request_id, request, source_identity=AUTHORIZED_SOURCES[1]
    ))
    assert response["reason_code"] == provider.PROVIDER_REQUEST_CONFLICT
    assert len(transport.calls) == 1


def test_same_request_id_different_payload_is_conflict_before_transport():
    _dependencies, client, transport = adapter()
    request_id = "d" * 24
    first = narrative_request("generation", CONTENT_MODEL)
    second = generation.NarrativeModelRequest(
        "generation", CONTENT_MODEL, "Return JSON only.", '{"changed":true}', {"type": "object"}
    )
    assert exchange_ipc(client, ipc_message(client, request_id, first))["status"] == "ok"
    response = exchange_ipc(client, ipc_message(client, request_id, second))
    assert response["reason_code"] == provider.PROVIDER_REQUEST_CONFLICT
    assert len(transport.calls) == 1


def test_same_request_id_changed_run_is_conflict():
    _dependencies, client, transport = adapter()
    request_id = "e" * 24
    original = ipc_message(client, request_id, narrative_request("generation", CONTENT_MODEL))
    assert exchange_ipc(client, original)["status"] == "ok"
    changed = copy.deepcopy(original)
    changed["run_id"] = "0" * 24
    response = exchange_ipc(client, changed)
    assert response["reason_code"] == provider.PROVIDER_REQUEST_CONFLICT
    assert len(transport.calls) == 1


def test_concurrent_exact_request_id_duplicates_use_one_transport_call():
    _dependencies, client, transport = adapter()
    message = ipc_message(client, "f" * 24, narrative_request("generation", CONTENT_MODEL))
    barrier = threading.Barrier(2)
    responses = []

    def call():
        barrier.wait()
        responses.append(exchange_ipc(client, copy.deepcopy(message)))

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(responses) == 2
    assert responses[0] == responses[1]
    assert len(transport.calls) == 1
    assert len(client.ledger.snapshot()) == 1


def test_concurrent_divergent_request_id_duplicates_allow_at_most_one_transport_call():
    _dependencies, client, transport = adapter()
    request_id = "1" * 24
    messages = (
        ipc_message(client, request_id, narrative_request("generation", CONTENT_MODEL)),
        ipc_message(client, request_id, narrative_request("adjudication", REVIEW_MODEL)),
    )
    barrier = threading.Barrier(2)
    responses = []

    def call(message):
        barrier.wait()
        responses.append(exchange_ipc(client, message))

    threads = [threading.Thread(target=call, args=(message,)) for message in messages]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert len(responses) == 2
    assert sorted(item["status"] for item in responses) == ["error", "ok"]
    assert next(item for item in responses if item["status"] == "error")["reason_code"] == (
        provider.PROVIDER_REQUEST_CONFLICT
    )
    assert len(transport.calls) == 1
    assert len(client.ledger.snapshot()) == 1


@pytest.mark.parametrize(
    ("failure", "reason_code"),
    (
        (TimeoutError("private-timeout"), provider.PROVIDER_TIMEOUT),
        (RuntimeError("private-429"), provider.PROVIDER_TRANSPORT_FAILED),
        (RuntimeError("private-500"), provider.PROVIDER_TRANSPORT_FAILED),
    ),
    ids=("timeout", "http-429", "http-500"),
)
def test_exact_duplicate_after_safe_transport_failure_never_retries(failure, reason_code):
    transport = FakeTransport((failure,))
    _dependencies, client, _ = adapter(transport)
    message = ipc_message(client, "2" * 24, narrative_request("generation", CONTENT_MODEL))
    first = exchange_ipc(client, message)
    second = exchange_ipc(client, copy.deepcopy(message))
    assert first == second
    assert first["reason_code"] == reason_code
    assert len(transport.calls) == 1
    records = client.ledger.snapshot()
    assert len(records) == 1
    assert records[0].safe_outcome == reason_code


def test_exact_duplicate_does_not_consume_twenty_sixth_budget_slot():
    _dependencies, client, transport = adapter()
    operations = (
        "evidence_coverage", "evidence_extraction", "evidence_adjudication",
        "generation", "adjudication",
    )
    first = ipc_message(client, "3" * 24, narrative_request("generation", CONTENT_MODEL))
    assert exchange_ipc(client, first)["status"] == "ok"
    assert exchange_ipc(client, copy.deepcopy(first))["status"] == "ok"
    counter = 4
    for source_identity in AUTHORIZED_SOURCES:
        for operation in operations:
            if source_identity == AUTHORIZED_SOURCES[0] and operation == "generation":
                continue
            model = CONTENT_MODEL if operation in {
                "evidence_coverage", "evidence_extraction", "generation"
            } else REVIEW_MODEL
            request = (
                evidence_request(operation, model)
                if operation.startswith("evidence_")
                else narrative_request(operation, model)
            )
            message = ipc_message(
                client, f"{counter:024x}", request, source_identity=source_identity
            )
            assert exchange_ipc(client, message)["status"] == "ok"
            counter += 1
    assert len(transport.calls) == 25
    assert len(client.ledger.snapshot()) == 25


def test_worker_crash_is_privacy_safe_and_never_restarts():
    _dependencies, client, transport = adapter()
    channel = object.__getattribute__(client, "_ProductionModelClient__channel")
    process = object.__getattribute__(channel, "_WorkerChannel__process")
    original_pid = process.pid
    assert transport.calls == []
    channel._test_crash()
    for _ in range(2):
        with pytest.raises(provider.NormalizerProviderError) as caught:
            invoke(client, narrative_request("generation", CONTENT_MODEL))
        assert caught.value.reason_code == provider.PROVIDER_TRANSPORT_FAILED
        assert "worker" not in str(caught.value).lower()
    assert object.__getattribute__(channel, "_WorkerChannel__process") is process
    assert process.pid == original_pid
    assert process.poll() is not None
    with pytest.raises(provider.NormalizerProviderError):
        client.ledger.snapshot()


def test_factory_capsule_is_detached_from_later_authorization_mutation():
    approved = authorization()
    original_run_id = approved.run_id
    dependencies, client, transport = adapter_from_authorization(approved)
    object.__setattr__(approved, "authorized_source_identities", ("f" * 64, *AUTHORIZED_SOURCES[1:]))
    object.__setattr__(approved, "run_id", "e" * 24)
    object.__setattr__(approved, "global_call_budget", 1)
    object.__setattr__(approved, "content_model", "attacker/content-model")
    object.__setattr__(approved, "adjudication_model", "attacker/review-model")
    invoke(dependencies[1]._client, narrative_request("generation", CONTENT_MODEL))
    record = client.ledger.snapshot()[0]
    assert record.run_id == original_run_id
    assert record.model_id == CONTENT_MODEL
    assert client.ledger.max_calls == provider.MAX_PROVIDER_CALLS
    assert len(transport.calls) == 1


def test_caller_created_record_cannot_be_inserted_into_authoritative_history():
    approved = authorization()
    _dependencies, client, _transport = adapter_from_authorization(approved)
    invoke(client, narrative_request("generation", CONTENT_MODEL), AUTHORIZED_SOURCES[2])
    forged = provider._ProviderCallRecord(
        approved.run_id,
        AUTHORIZED_SOURCES[0],
        "a" * 24,
        "b" * 24,
        "generation",
        CONTENT_MODEL,
        1,
        120,
        "c" * 64,
        "d" * 64,
        True,
        True,
        "completed",
    )
    with pytest.raises(AttributeError):
        client.ledger._records = (forged,)
    with pytest.raises(AttributeError):
        client.ledger._state = object()
    records = client.ledger.snapshot()
    assert len(records) == 1
    assert records[0] is not forged
    assert not hasattr(provider, "ProviderCallLedger")
    assert not hasattr(provider, "ProviderCallRecord")


def test_caller_cannot_reset_internal_budget_or_operation_slots():
    _dependencies, client, transport = adapter()
    invoke(client, narrative_request("generation", CONTENT_MODEL))
    for name, value in (
        ("_run_state", object()),
        ("_records", ()),
        ("_operation_counts", {}),
        ("authorization", authorization()),
    ):
        with pytest.raises(AttributeError):
            setattr(client, name, value)
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, narrative_request("generation", CONTENT_MODEL))
    assert caught.value.reason_code == provider.PROVIDER_BUDGET_EXCEEDED
    assert len(transport.calls) == 1
    assert len(client.ledger.snapshot()) == 1


def test_caller_cannot_inject_external_ledger_or_budget_into_client():
    config, _secret = provider._load_configuration(live_env())
    with pytest.raises(TypeError):
        provider.ProductionModelClient(config, FakeTransport(), object())


def test_generation_and_evidence_services_share_one_private_run_budget():
    dependencies, client, transport = adapter()
    assert dependencies[1]._client is client
    assert dependencies[2]._client is client
    assert dependencies[1]._client.ledger is dependencies[2]._client.ledger
    assert client.ledger.max_calls == provider.MAX_PROVIDER_CALLS
    invoke(dependencies[2]._client, evidence_request("evidence_extraction", CONTENT_MODEL))
    invoke(
        dependencies[1]._client,
        narrative_request("generation", CONTENT_MODEL),
        AUTHORIZED_SOURCES[1],
    )
    assert len(transport.calls) == 2
    assert len(client.ledger.snapshot()) == 2


def test_sixth_source_stays_rejected_after_authorization_mutation():
    approved = authorization()
    _dependencies, client, transport = adapter_from_authorization(approved)
    sixth = "f" * 64
    object.__setattr__(
        approved,
        "authorized_source_identities",
        (sixth, *AUTHORIZED_SOURCES[1:]),
    )
    with pytest.raises(provider.NormalizerProviderError):
        invoke(client, narrative_request("generation", CONTENT_MODEL), sixth)
    assert transport.calls == []
    assert client.ledger.snapshot() == ()


def test_ledger_snapshot_is_detached_and_cannot_change_internal_history():
    _dependencies, client, _transport = adapter()
    invoke(client, narrative_request("generation", CONTENT_MODEL))
    first = client.ledger.snapshot()
    with pytest.raises((AttributeError, TypeError)):
        first[0].completed = False
    local = (*first, first[0])
    assert len(local) == 2
    second = client.ledger.snapshot()
    assert len(second) == 1
    assert second is not first
    assert second[0] is not first[0]


def test_concurrent_call_twenty_six_is_rejected_before_transport():
    transport = FakeTransport()
    _dependencies, client, _ = adapter(transport)
    operations = (
        "evidence_coverage", "evidence_extraction", "evidence_adjudication",
        "generation", "adjudication",
    )
    for source_index, source_identity in enumerate(AUTHORIZED_SOURCES):
        for operation in operations:
            if source_index == 4 and operation == "adjudication":
                continue
            if operation.startswith("evidence_"):
                model = CONTENT_MODEL if operation in {
                    "evidence_coverage", "evidence_extraction",
                } else REVIEW_MODEL
                request = evidence_request(operation, model)
            else:
                model = CONTENT_MODEL if operation == "generation" else REVIEW_MODEL
                request = narrative_request(operation, model)
            invoke(client, request, source_identity)
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt():
        barrier.wait()
        try:
            invoke(
                client,
                narrative_request("adjudication", REVIEW_MODEL),
                AUTHORIZED_SOURCES[4],
            )
            outcomes.append("completed")
        except provider.NormalizerProviderError as error:
            outcomes.append(error.reason_code)

    workers = [threading.Thread(target=attempt) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert not worker.is_alive()
    assert sorted(outcomes) == ["completed", provider.PROVIDER_BUDGET_EXCEEDED]
    assert len(transport.calls) == 25
    assert len(client.ledger.snapshot()) == 25


@pytest.mark.parametrize(
    "private_value",
    (
        r"C:\secret\file.txt",
        r"\\server\share\file.txt",
        "/Users/alice/private.txt",
        "/srv/private/source.txt",
        "/opt/project/file",
        "file:///secret/file",
        "OPENAI_API_KEY=actual-value",
        "SERVICE_TOKEN:actual-value",
    ),
    ids=(
        "windows-path", "unc-path", "users-path", "srv-path", "opt-path", "file-uri",
        "env-key-assignment", "token-assignment",
    ),
)
def test_recursive_request_privacy_rejects_paths_and_assignments(private_value):
    _dependencies, client, transport = adapter()
    schema = {
        "type": "object",
        "properties": {"nested": {"type": "string", "description": private_value}},
    }
    request = generation.NarrativeModelRequest(
        "generation", CONTENT_MODEL, "Return JSON only.", '{"safe":true}', schema
    )
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, request)
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert transport.calls == []
    assert client.ledger.snapshot() == ()


@pytest.mark.parametrize(
    "placement",
    ("system", "user", "schema", "nested-list", "split-fields"),
    ids=("system", "user", "response-schema", "nested-list", "split-across-fields"),
)
def test_actual_configured_secret_never_reaches_transport(placement):
    secret = live_env()[provider.API_KEY_ENV]
    system = "Return JSON only."
    user = '{"safe":true}'
    schema = {"type": "object", "properties": {}}
    if placement == "system":
        system = f"private {secret} value"
    elif placement == "user":
        user = f'{{"value":"{secret}"}}'
    elif placement == "schema":
        schema["description"] = secret
    elif placement == "nested-list":
        schema["examples"] = [[{"value": secret}]]
    else:
        midpoint = len(secret) // 2
        system = secret[:midpoint]
        user = secret[midpoint:]
    _dependencies, client, transport = adapter()
    request = generation.NarrativeModelRequest(
        "generation", CONTENT_MODEL, system, user, schema
    )
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, request)
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert secret not in str(caught.value)
    assert transport.calls == []
    assert client.ledger.snapshot() == ()


@pytest.mark.parametrize(
    "text",
    (
        "Проверили файл `.env`.",
        "Название файла — .env.",
        "Конфигурация хранится в файле .env.",
    ),
    ids=("quoted-env", "env-filename", "env-discussion"),
)
def test_literal_env_filename_discussion_is_allowed(text):
    _dependencies, client, transport = adapter()
    request = generation.NarrativeModelRequest(
        "generation", CONTENT_MODEL, "Return JSON only.", text, {"type": "object"}
    )
    assert invoke(client, request) == '{"ok":true}'
    assert len(transport.calls) == 1


def test_unknown_nested_payload_object_is_rejected_before_transport():
    _dependencies, client, transport = adapter()
    request = generation.NarrativeModelRequest(
        "generation",
        CONTENT_MODEL,
        "Return JSON only.",
        '{"safe":true}',
        {"type": "object", "description": object()},
    )
    with pytest.raises(provider.NormalizerProviderError):
        invoke(client, request)
    assert transport.calls == []


def test_private_provider_response_is_rejected_without_disclosure(caplog):
    secret = live_env()[provider.API_KEY_ENV]
    transport = FakeTransport(({"nested": {"value": secret}},))
    _dependencies, client, _ = adapter(transport)
    caplog.set_level("DEBUG", logger=provider.__name__)
    with pytest.raises(provider.NormalizerProviderError) as caught:
        invoke(client, narrative_request("generation", CONTENT_MODEL))
    assert caught.value.reason_code == provider.PROVIDER_CONFIGURATION_INVALID
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert secret not in caplog.text
    assert len(transport.calls) == 1
    record = client.ledger.snapshot()[0]
    assert record.safe_outcome == provider.PROVIDER_CONFIGURATION_INVALID
    assert record.response_digest is None


def test_authorized_request_is_frozen_and_source_cannot_change_after_construction():
    value = provider.AuthorizedProviderRequest(
        "a" * 24,
        AUTHORIZED_SOURCES[0],
        "generation",
        narrative_request("generation", CONTENT_MODEL),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        value.source_identity = AUTHORIZED_SOURCES[1]


@pytest.mark.parametrize(
    "constructor",
    (
        lambda: provider.ProviderConfiguration(
            StringSubclass(provider.NORMALIZER_PRODUCTION_ADAPTER_VERSION),
            "https://openrouter.ai/api/v1", CONTENT_MODEL, REVIEW_MODEL, 120,
        ),
        lambda: provider.ProviderConfiguration(
            provider.NORMALIZER_PRODUCTION_ADAPTER_VERSION,
            "https://openrouter.ai/api/v1", StringSubclass(CONTENT_MODEL), REVIEW_MODEL, 120,
        ),
        lambda: provider.ProviderConfiguration(
            provider.NORMALIZER_PRODUCTION_ADAPTER_VERSION,
            "https://openrouter.ai/api/v1", CONTENT_MODEL, REVIEW_MODEL, IntegerSubclass(120),
        ),
        lambda: provider.AuthorizedProviderRequest(
            "a" * 24, StringSubclass("b" * 64), "generation",
            narrative_request("generation", CONTENT_MODEL),
        ),
    ),
    ids=("adapter-version-subclass", "model-subclass", "timeout-subclass", "source-subclass"),
)
def test_public_provider_contracts_reject_scalar_subclasses(constructor):
    with pytest.raises((TypeError, provider.NormalizerProviderError)):
        constructor()


def test_provider_documentation_matches_sealed_contract():
    text_value = (Path(__file__).parents[1] / "docs" / "NARRATIVE_NORMALIZER_PROVIDER.md").read_text(
        encoding="utf-8"
    )
    required = (
        "LiveProviderRunAuthorization",
        provider.CANARY_RUN_PROFILE,
        provider.FIRST_FIVE_RUN_PROFILE,
        "byte-identical",
        "one-shot",
        "shared private",
        "recursive",
        "literal `.env`",
        "distinct",
        "max_retries=0",
        "Review Authority Broker",
        "not exercised",
    )
    assert all(fragment in text_value for fragment in required)


def test_no_real_network_is_attempted(monkeypatch):
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("network attempted")

    monkeypatch.setattr(socket, "create_connection", blocked)
    transport = FakeTransport()
    _dependencies, client, _ = adapter(transport)
    invoke(client, narrative_request("generation", CONTENT_MODEL))
    assert attempts == []
