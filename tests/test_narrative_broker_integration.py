from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

import narrative_normalizer as nn
import narrative_normalizer_review_state as review_state
import narrative_normalizer_trust as trust
import narrative_outbox_permissions as outbox_permissions
import narrative_review_authority as authority
import narrative_review_authority_client as authority_client
import narrative_review_authority_protocol as protocol
import reels_failure_quarantine as rq
import test_narrative_normalizer as normalizer_tests


class CoreBrokerClient:
    """In-process role-mapped transport; no socket, key, or filesystem API."""

    def __init__(self, broker: authority.ReviewAuthority):
        self.broker = broker
        self.calls: list[tuple[str, str, dict[str, object]]] = []
        self.before: dict[str, object] = {}
        self.after: dict[str, object] = {}

    def _exchange(
        self,
        role: str,
        operation: str,
        request_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        detached = copy.deepcopy(payload)
        self.calls.append((operation, request_id, detached))
        hook = self.before.get(operation)
        if callable(hook):
            hook()
        response = self.broker.handle(
            role,
            protocol.Request(request_id, operation, payload),
        )
        if not response.ok:
            raise authority_client.ClientError(str(response.error))
        assert response.result is not None
        result = copy.deepcopy(response.result)
        hook = self.after.get(operation)
        if callable(hook):
            hook(result)
        return result

    def register_draft(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self._exchange(protocol.ROLE_NORMALIZER, protocol.OP_REGISTER_DRAFT, request_id, payload)

    def latest_state(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self._exchange(protocol.ROLE_REVIEWER, protocol.OP_LATEST_STATE, request_id, payload)

    def append_review(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self._exchange(protocol.ROLE_REVIEWER, protocol.OP_APPEND_REVIEW, request_id, payload)

    def prepare_approval(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self._exchange(protocol.ROLE_REVIEWER, protocol.OP_PREPARE_APPROVAL, request_id, payload)

    def commit_approval(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self._exchange(protocol.ROLE_REVIEWER, protocol.OP_COMMIT_APPROVAL, request_id, payload)

    def verify_ready(self, request_id: str, payload: dict[str, object]) -> dict[str, object]:
        return self._exchange(protocol.ROLE_CONSUMER, protocol.OP_VERIFY_READY, request_id, payload)


@pytest.fixture
def integrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setenv(
        "NARRATIVE_NORMALIZER_TRUST_KEY",
        base64.b64encode(normalizer_tests.TEST_TRUST_KEY).decode("ascii"),
    )
    values = normalizer_tests.runtime(tmp_path)
    policy = values[1]
    record = values[3]
    service = values[-1]
    broker = authority.ReviewAuthority(
        tmp_path / "broker-authority",
        trust.NarrativeTrustService(normalizer_tests.TEST_TRUST_KEY),
        narrative_outbox_root=policy.narrative_outbox_root,
    )
    client = CoreBrokerClient(broker)
    service.store.review_authority = client
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    draft = service.store.draft_path(
        record.source_digest,
        source_ref=record.source_ref,
    )
    value = nn.validate_draft_directory(
        draft,
        validate_ready=False,
        trust_service=service.store.trust_service,
        require_trust=True,
    )
    return {
        "policy": policy,
        "record": record,
        "service": service,
        "store": service.store,
        "broker": broker,
        "client": client,
        "outcome": outcome,
        "draft": draft,
        "value": value,
    }


def _pass(env: dict[str, object], request_id: str = "human-pass-001"):
    store = env["store"]
    record = env["record"]
    value = env["value"]
    return store.pass_review(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=value["manifest"]["draft_identity"],
        operator_request_id=request_id,
        reviewed_at="2026-08-24T12:00:00Z",
    )


def _approve(env: dict[str, object], request_id: str = "human-approval-001"):
    store = env["store"]
    record = env["record"]
    value = env["value"]
    return store.approve(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=value["manifest"]["draft_identity"],
        operator_request_id=request_id,
        reviewed_at="2026-08-24T12:01:00Z",
    )


def _consumer_policy(env: dict[str, object]) -> rq.QuarantinePathPolicy:
    return replace(
        env["policy"],
        narrative_trust_service=None,
        narrative_review_authority_root=None,
        review_authority_client=env["client"],
    )


def _event_count(env: dict[str, object]) -> int:
    value = env["value"]
    return len(env["broker"]._store.read(
        value["story"]["source_identity"],
        value["manifest"]["draft_identity"],
    ).events)


@pytest.mark.skipif(
    __import__("os").name != "posix",
    reason="real shared UID/GID/mode semantics require POSIX",
)
def test_shared_review_policy_preserves_broker_approval_and_consumer_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import os
    import stat

    monkeypatch.setenv(
        "NARRATIVE_NORMALIZER_TRUST_KEY",
        base64.b64encode(normalizer_tests.TEST_TRUST_KEY).decode("ascii"),
    )
    values = normalizer_tests.runtime(tmp_path)
    policy = values[1]
    record = values[3]
    service = values[-1]
    broker = authority.ReviewAuthority(
        tmp_path / "broker-authority",
        trust.NarrativeTrustService(normalizer_tests.TEST_TRUST_KEY),
        narrative_outbox_root=policy.narrative_outbox_root,
    )
    client = CoreBrokerClient(broker)
    shared = outbox_permissions.NarrativeOutboxPermissionPolicy(
        outbox_permissions.SHARED_REVIEW_POLICY_VERSION,
        os.getegid(),
    )
    service.store = nn.NarrativeOutboxStore(
        policy,
        trust_service=policy.narrative_trust_service,
        review_authority=client,
        permission_policy=shared,
    )

    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == nn.OUTCOME_DRAFT_READY_FOR_REVIEW
    draft = service.store.draft_path(
        record.source_digest,
        source_ref=record.source_ref,
    )
    value = nn.validate_draft_directory(
        draft,
        validate_ready=False,
        trust_service=service.store.trust_service,
        require_trust=True,
    )
    service.store.pass_review(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=value["manifest"]["draft_identity"],
        operator_request_id="shared-human-pass-001",
        reviewed_at="2026-08-24T12:00:00Z",
    )
    service.store.approve(
        record.source_ref,
        record.source_digest,
        expected_draft_identity=value["manifest"]["draft_identity"],
        operator_request_id="shared-human-approval-001",
        reviewed_at="2026-08-24T12:01:00Z",
    )

    verified = client.verify_ready(
        "shared-consumer-verify-001",
        {
            "ready_manifest": json.loads(
                (draft / "narrative_ready.json").read_text(encoding="utf-8")
            ),
            "attestation": json.loads(
                (draft / "approval-attestation.json").read_text(encoding="utf-8")
            ),
        },
    )
    assert verified["ready"] is True
    assert stat.S_IMODE(draft.lstat().st_mode) == 0o3770
    for name in outbox_permissions.DRAFT_FILE_NAMES:
        assert stat.S_IMODE((draft / name).lstat().st_mode) == 0o640
    for name in outbox_permissions.APPROVAL_FILE_NAMES:
        target = draft / name
        assert stat.S_IMODE(target.lstat().st_mode) == 0o640
        assert target.lstat().st_gid == shared.shared_gid


def test_successful_normalize_registers_only_canonical_binding(integrated):
    assert integrated["outcome"].status == nn.OUTCOME_CREATED
    register = [item for item in integrated["client"].calls if item[0] == protocol.OP_REGISTER_DRAFT]
    assert len(register) == 1
    payload = register[0][2]
    assert frozenset(payload) == authority._DRAFT_KEYS
    assert payload["source_identity"] == integrated["value"]["story"]["source_identity"]
    assert payload["draft_package_digest"] == integrated["value"]["story"]["package_digest"]
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("source_facts", "human_story_package", "raw source", "prompt"):
        assert forbidden not in serialized
    assert _event_count(integrated) == 1


def test_duplicate_register_is_event_idempotent(integrated):
    first_count = _event_count(integrated)
    integrated["store"].register_draft_with_authority(
        integrated["record"].source_ref,
        integrated["record"].source_digest,
    )
    assert _event_count(integrated) == first_count == 1


def test_divergent_register_request_conflicts(integrated):
    operation, request_id, payload = next(
        item for item in integrated["client"].calls
        if item[0] == protocol.OP_REGISTER_DRAFT
    )
    divergent = copy.deepcopy(payload)
    divergent["review_digest"] = "f" * 64
    with pytest.raises(authority_client.ClientError, match=protocol.REQUEST_CONFLICT):
        integrated["client"].register_draft(request_id, divergent)
    assert _event_count(integrated) == 1


def test_broker_unavailable_after_persist_has_no_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "NARRATIVE_NORMALIZER_TRUST_KEY",
        base64.b64encode(normalizer_tests.TEST_TRUST_KEY).decode("ascii"),
    )
    values = normalizer_tests.runtime(tmp_path)
    service = values[-1]
    record = values[3]

    class Unavailable:
        def register_draft(self, request_id, payload):
            raise authority_client.ClientError(authority_client.CLIENT_INVALID)

    service.store.review_authority = Unavailable()
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    draft = service.store.draft_path(record.source_digest, source_ref=record.source_ref)
    assert outcome.status == nn.OUTCOME_MANUAL_ATTENTION
    assert outcome.reason_codes == ("narrative_normalizer_review_authority_unavailable",)
    assert draft.is_dir()
    assert not (draft / "narrative_ready.json").exists()
    assert not (draft / "approval-attestation.json").exists()
    assert not policy_authority_events(values[1])


def policy_authority_events(policy: rq.QuarantinePathPolicy) -> tuple[Path, ...]:
    root = policy.narrative_review_authority_root
    return () if root is None or not root.exists() else tuple(root.rglob("*.json"))


def test_pass_and_reject_are_broker_owned(integrated):
    passed = _pass(integrated)
    assert passed.status == review_state.STATE_PASSED
    rejected = integrated["store"].reject(
        integrated["record"].source_ref,
        integrated["record"].source_digest,
        expected_draft_identity=integrated["value"]["manifest"]["draft_identity"],
        operator_request_id="human-reject-001",
        reason_codes=("narrative_normalizer_meaning_invalid",),
        reviewed_at="2026-08-24T12:02:00Z",
    )
    assert rejected.status == review_state.STATE_REJECTED
    assert _event_count(integrated) == 3
    assert not policy_authority_events(integrated["policy"])


def test_supersede_is_broker_owned_for_two_registered_drafts(integrated):
    _pass(integrated, "human-pass-before-supersede")
    source_path = integrated["policy"].inbox_root / integrated["record"].source_ref
    source_path.joinpath("new.txt").write_text(
        str(normalizer_tests.normalizer_fixture("technical_log")["facts"][0]) + "\n",
        encoding="utf-8",
    )
    rq.reconcile_complete_backlog(
        integrated["policy"],
        now=normalizer_tests.NOW + timedelta(minutes=1),
    )
    [(source_ref, new_digest)] = nn.scan_needs_narrative(integrated["policy"])
    changed = nn.read_source_unit(
        integrated["policy"], source_ref, expected_digest=new_digest,
    )
    fixture = normalizer_tests.load_fixture("env_utf8")
    context = integrated["service"].context_provider.build(changed)
    drafts = normalizer_tests.normalizer_generation_payload(
        fixture, fact_count=len(changed.facts),
    )
    queue = normalizer_tests.QueueClient([
        drafts,
        normalizer_tests.fake_adjudication_payload(drafts, context),
    ])
    second_service = nn.NarrativeNormalizerService(
        policy=integrated["policy"],
        context_provider=integrated["service"].context_provider,
        generation_service=normalizer_tests.ng.NarrativeGenerationService(
            queue,
            generation_model="terra",
            adjudication_model="sol",
        ),
        clock=lambda: normalizer_tests.NOW,
        trust_service=integrated["store"].trust_service,
        review_authority=integrated["client"],
    )
    second = second_service.normalize_source(source_ref, new_digest)
    second_value = nn.validate_draft_directory(
        second_service.store.draft_path(new_digest, source_ref=source_ref),
        validate_ready=False,
        trust_service=integrated["store"].trust_service,
        require_trust=True,
    )
    relation = second_value["manifest"]["supersedes"]
    assert type(relation) is dict
    result = integrated["store"].supersede(
        **relation,
        operator_request_id="human-supersede-001",
        reviewed_at="2026-08-24T12:03:00Z",
    )
    assert second.status == nn.OUTCOME_CREATED
    assert result.status == review_state.STATE_SUPERSEDED
    latest = integrated["client"].latest_state(
        "latest-after-supersede",
        {
            "source_identity": relation["old_source_identity"],
            "draft_identity": relation["old_draft_identity"],
        },
    )
    assert latest["state"] == review_state.STATE_SUPERSEDED
    assert not policy_authority_events(integrated["policy"])


def test_exact_dual_digest_approval_and_consumer_verify(integrated):
    _pass(integrated)
    result = _approve(integrated)
    assert result.status == rq.CLASS_READY
    raw = json.loads((integrated["draft"] / "approval-attestation.json").read_text(encoding="utf-8"))
    ready = json.loads((integrated["draft"] / "narrative_ready.json").read_text(encoding="utf-8"))
    assert raw["draft_package_digest"] == integrated["value"]["story"]["package_digest"]
    assert raw["narrative_package_digest"] == hashlib.sha256(
        (integrated["draft"] / "story.json").read_bytes()
    ).hexdigest()
    assert ready["narrative_package_digest"] == raw["narrative_package_digest"]
    manifest = rq.validate_narrative_ready_manifest(
        _consumer_policy(integrated),
        integrated["record"].source_ref,
    )
    assert manifest.status == rq.CLASS_READY
    assert any(item[0] == protocol.OP_VERIFY_READY for item in integrated["client"].calls)


def test_v2_identity_layout_never_accepts_legacy_tree_digest(integrated):
    _pass(integrated)
    _approve(integrated)
    draft = integrated["draft"]
    ready_path = draft / "narrative_ready.json"
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    raw_story_digest = hashlib.sha256((draft / "story.json").read_bytes()).hexdigest()
    legacy_tree_digest = rq.narrative_package_digest(draft / "story.json")
    assert legacy_tree_digest != raw_story_digest
    ready["narrative_package_digest"] = legacy_tree_digest
    ready_path.write_text(json.dumps(ready, sort_keys=True), encoding="utf-8")
    calls_before = len(integrated["client"].calls)

    with pytest.raises(rq.EligibilityError, match="narrative_manifest_package_digest_mismatch"):
        rq.validate_narrative_ready_manifest(
            _consumer_policy(integrated),
            integrated["record"].source_ref,
        )

    assert len(integrated["client"].calls) == calls_before


def test_v2_ready_without_attestation_has_no_legacy_fallback(integrated):
    _pass(integrated)
    _approve(integrated)
    (integrated["draft"] / "approval-attestation.json").unlink()
    calls_before = len(integrated["client"].calls)

    with pytest.raises(
        rq.EligibilityError,
        match="narrative_approval_(?:attestation_invalid|trust_missing)",
    ):
        rq.validate_narrative_ready_manifest(
            _consumer_policy(integrated),
            integrated["record"].source_ref,
        )

    assert len(integrated["client"].calls) == calls_before


def test_prepare_rejected_creates_no_pair(integrated):
    _pass(integrated)

    def reject():
        raise authority_client.ClientError(authority.AUTHORITY_STATE_CONFLICT)

    integrated["client"].before[protocol.OP_PREPARE_APPROVAL] = reject
    with pytest.raises(nn.NarrativeNormalizerError):
        _approve(integrated)
    assert not (integrated["draft"] / "approval-attestation.json").exists()
    assert not (integrated["draft"] / "narrative_ready.json").exists()
    assert _event_count(integrated) == 2


def test_prepare_rejects_digest_that_omits_story_final_lf(integrated):
    _pass(integrated)

    def stop_after_capture():
        raise authority_client.ClientError(authority.AUTHORITY_STATE_CONFLICT)

    integrated["client"].before[protocol.OP_PREPARE_APPROVAL] = stop_after_capture
    with pytest.raises(nn.NarrativeNormalizerError):
        _approve(integrated)
    captured = next(
        copy.deepcopy(item[2])
        for item in reversed(integrated["client"].calls)
        if item[0] == protocol.OP_PREPARE_APPROVAL
    )
    integrated["client"].before.pop(protocol.OP_PREPARE_APPROVAL)
    story_bytes = (integrated["draft"] / "story.json").read_bytes()
    assert story_bytes.endswith(b"\n")
    captured["narrative_package_digest"] = hashlib.sha256(
        story_bytes[:-1]
    ).hexdigest()
    with pytest.raises(
        authority_client.ClientError,
        match=authority.AUTHORITY_ATTESTATION_INVALID,
    ):
        integrated["client"].prepare_approval("prepare-without-lf", captured)
    assert _event_count(integrated) == 2
    assert not (integrated["draft"] / "approval-attestation.json").exists()
    assert not (integrated["draft"] / "narrative_ready.json").exists()


def test_wrong_prepare_identity_is_rejected_before_promotion(integrated):
    _pass(integrated)

    def corrupt(result):
        result["prepared"]["attestation"]["source_identity"] = "f" * 64

    integrated["client"].after[protocol.OP_PREPARE_APPROVAL] = corrupt
    with pytest.raises(nn.NarrativeNormalizerError):
        _approve(integrated)
    assert not (integrated["draft"] / "approval-attestation.json").exists()
    assert not (integrated["draft"] / "narrative_ready.json").exists()


def test_pair_before_commit_is_consumer_ineligible(integrated):
    _pass(integrated)

    def reject():
        raise authority_client.ClientError(authority_client.CLIENT_INVALID)

    integrated["client"].before[protocol.OP_COMMIT_APPROVAL] = reject
    with pytest.raises(nn.NarrativeNormalizerError):
        _approve(integrated)
    assert (integrated["draft"] / "approval-attestation.json").is_file()
    assert (integrated["draft"] / "narrative_ready.json").is_file()
    with pytest.raises(rq.EligibilityError):
        rq.validate_narrative_ready_manifest(
            _consumer_policy(integrated),
            integrated["record"].source_ref,
        )
    assert _event_count(integrated) == 2


def test_commit_response_lost_is_idempotently_recovered(integrated):
    _pass(integrated)
    lost = {"once": True}

    def lose(_result):
        if lost["once"]:
            lost["once"] = False
            raise authority_client.ClientError(authority_client.CLIENT_INVALID)

    integrated["client"].after[protocol.OP_COMMIT_APPROVAL] = lose
    with pytest.raises(nn.NarrativeNormalizerError):
        _approve(integrated)
    assert _event_count(integrated) == 3
    recovered = _approve(integrated)
    assert recovered.idempotent is True
    assert _event_count(integrated) == 3


def test_story_changed_before_commit_never_approves(integrated):
    _pass(integrated)

    def mutate():
        path = integrated["draft"] / "story.json"
        path.write_bytes(path.read_bytes().rstrip(b"\n"))

    integrated["client"].before[protocol.OP_COMMIT_APPROVAL] = mutate
    with pytest.raises(nn.NarrativeNormalizerError):
        _approve(integrated)
    assert _event_count(integrated) == 2


def test_story_changed_after_commit_fails_consumer_verify(integrated):
    _pass(integrated)

    def mutate(_result):
        path = integrated["draft"] / "story.json"
        path.write_bytes(path.read_bytes() + b" ")

    integrated["client"].after[protocol.OP_COMMIT_APPROVAL] = mutate
    with pytest.raises(nn.NarrativeNormalizerError):
        _approve(integrated)
    assert _event_count(integrated) == 3
    with pytest.raises(rq.EligibilityError):
        rq.validate_narrative_ready_manifest(
            _consumer_policy(integrated),
            integrated["record"].source_ref,
        )


def test_missing_broker_keeps_identity_candidate_needs_narrative(integrated):
    _pass(integrated)
    _approve(integrated)
    policy = replace(
        integrated["policy"],
        narrative_trust_service=None,
        narrative_review_authority_root=None,
        review_authority_client=None,
    )
    discovered = rq.discover_all_agent_material_sources(policy)
    assert discovered[0].classification == rq.CLASS_RAW
    assert rq.reconcile_complete_backlog(policy).narrative_ready_count == 0


def test_stale_attestation_keeps_source_raw(integrated):
    _pass(integrated)
    _approve(integrated)
    path = integrated["draft"] / "approval-attestation.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["narrative_package_digest"] = "f" * 64
    normalizer_tests.write_json(path, raw)
    discovered = rq.discover_all_agent_material_sources(_consumer_policy(integrated))
    assert discovered[0].classification == rq.CLASS_RAW


def test_cancellation_is_reraised_without_local_fallback(integrated):
    _pass(integrated)

    def cancel():
        raise asyncio.CancelledError

    integrated["client"].before[protocol.OP_PREPARE_APPROVAL] = cancel
    with pytest.raises(asyncio.CancelledError):
        _approve(integrated)
    assert not (integrated["draft"] / "approval-attestation.json").exists()
    assert not (integrated["draft"] / "narrative_ready.json").exists()
    assert _event_count(integrated) == 2


def test_broker_client_proxy_has_no_key_or_authority_files(integrated):
    client = integrated["client"]
    assert not hasattr(client, "trust_service")
    assert not hasattr(client, "key")
    for operation, _request_id, payload in client.calls:
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "NARRATIVE_NORMALIZER_TRUST_KEY" not in serialized
        assert "broker-authority" not in serialized
        assert operation in protocol.OPERATIONS


def test_dormant_default_policy_does_not_connect(monkeypatch, tmp_path):
    monkeypatch.delenv("NARRATIVE_REVIEW_AUTHORITY_SOCKET", raising=False)
    policy = rq.default_path_policy(tmp_path / "inbox")
    assert policy.review_authority_client is None
