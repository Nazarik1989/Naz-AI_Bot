from __future__ import annotations

import base64
import copy
import hashlib
import importlib
import json
import multiprocessing
import os
import socket
import stat
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import narrative_normalizer_review_state as rs
import narrative_normalizer_trust as trust
import narrative_review_authority as a
import narrative_review_authority_client as client
import narrative_review_authority_protocol as p
import narrative_review_authority_server as server


H2 = "b" * 64
H4 = "d" * 64
H5 = "e" * 64
H6 = "f" * 64
SOURCE_REF = "content/inbox/item.json"
SOURCE_CONTRACT = "agent-content-source-v1"
DRAFT_CONTRACT = "normalizer-draft-identity-v1"
H = hashlib.sha256(
    SOURCE_REF.encode("utf-8") + b"\0" + H2.encode("ascii") + b"\0" + SOURCE_CONTRACT.encode("utf-8")
).hexdigest()
H3 = hashlib.sha256(p.canonical({
    "version": DRAFT_CONTRACT, "source_identity": H, "package_digest": H4,
})).hexdigest()
NOW = "2026-08-23T12:00:00Z"


class Text(str):
    pass


class DuplexStream:
    def __init__(self, response: bytes):
        self.sent = bytearray()
        self.response = response
        self.offset = 0

    def write(self, value: bytes) -> int:
        self.sent.extend(value)
        return len(value)

    def flush(self) -> None:
        return None

    def read(self, size: int) -> bytes:
        result = self.response[self.offset:self.offset + size]
        self.offset += len(result)
        return result

    def close(self) -> None:
        return None


class FakeClientSocket:
    def __init__(self, response: bytes):
        self.stream = DuplexStream(response)
        self.shutdown_calls: list[int] = []
        self.connected: str | None = None

    def __enter__(self) -> "FakeClientSocket":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def settimeout(self, value: float) -> None:
        del value

    def connect(self, value: str) -> None:
        self.connected = value

    def makefile(self, mode: str, buffering: int = 0) -> DuplexStream:
        assert mode == "rwb" and buffering == 0
        return self.stream

    def shutdown(self, how: int) -> None:
        self.shutdown_calls.append(how)


class AcceptedOnce:
    def __init__(self, connection: socket.socket):
        self.connection = connection

    def accept(self) -> tuple[socket.socket, None]:
        value = self.connection
        self.connection = None
        return value, None

    def close(self) -> None:
        connection = self.connection
        self.connection = None
        if connection is not None:
            connection.close()


def service() -> trust.NarrativeTrustService:
    return trust.NarrativeTrustService(b"authority-test-key-material-32!!")


def transport_server(
    tmp_path: Path, broker: a.ReviewAuthority, monkeypatch: pytest.MonkeyPatch,
    *, timeout: float = 0.5,
) -> server.ReviewAuthorityServer:
    monkeypatch.setattr(server.socket, "AF_UNIX", getattr(server.socket, "AF_UNIX", 1), raising=False)
    monkeypatch.setattr(
        server.ReviewAuthorityServer, "peer_credentials",
        staticmethod(lambda connection: (1, 1001, 1001)),
    )
    return server.ReviewAuthorityServer(
        broker, socket_path=tmp_path / "authority.sock",
        peer_policy=server.PeerRolePolicy({1001: p.ROLE_REVIEWER}, {}),
        owner_uid=0, owner_gid=0, mode=0o600, request_timeout=timeout,
    )


def raw_connection(
    running: server.ReviewAuthorityServer, chunks: list[bytes], *,
    half_close: bool = True, delays: list[float] | None = None,
) -> bytes:
    server_connection, peer = socket.socketpair()
    running._socket = AcceptedOnce(server_connection)
    worker = threading.Thread(target=running.serve_once, name="authority-serve-once")
    worker.start()
    response = bytearray()
    try:
        with peer:
            peer.settimeout(3)
            for index, chunk in enumerate(chunks):
                peer.sendall(chunk)
                if delays is not None and index < len(delays):
                    time.sleep(delays[index])
            if half_close:
                peer.shutdown(socket.SHUT_WR)
            while True:
                part = peer.recv(65_536)
                if not part:
                    break
                response.extend(part)
    finally:
        worker.join(5)
        running.close()
    assert not worker.is_alive()
    return bytes(response)


@pytest.fixture
def broker(tmp_path: Path) -> a.ReviewAuthority:
    return a.ReviewAuthority(tmp_path / "authority", service())


def request(request_id: str, operation: str, payload: dict[str, object]) -> p.Request:
    return p.Request(request_id, operation, payload)


def _process_register(root: str, operator: str, output: object) -> None:
    broker = a.ReviewAuthority(Path(root), service())
    payload = draft_payload(operator_request_id=operator)
    response = broker.handle(p.ROLE_NORMALIZER, request(f"process-{operator}", p.OP_REGISTER_DRAFT, payload))
    output.put((response.ok, response.error))


def draft_payload(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_identity": H,
        "source_ref": SOURCE_REF,
        "source_digest": H2,
        "draft_identity": H3,
        "package_digest": H4,
        "story_markdown_digest": H5,
        "draft_manifest_digest": H6,
        "review_digest": H,
        "completed_claim_digest": H2,
        "artifact_binding_digest": H3,
        "contract_versions": {"draft": DRAFT_CONTRACT, "source": SOURCE_CONTRACT},
        "operator_request_id": "register-001",
        "timestamp": NOW,
    }
    value.update(changes)
    return value


def register(broker: a.ReviewAuthority, *, request_id: str = "ipc-register-001") -> dict[str, object]:
    response = broker.handle(p.ROLE_NORMALIZER, request(request_id, p.OP_REGISTER_DRAFT, draft_payload()))
    assert response.ok, response.error
    assert response.result is not None
    return response.result


def review_payload(latest: dict[str, object], **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_identity": H,
        "draft_identity": H3,
        "new_state": rs.STATE_PASSED,
        "operator_request_id": "review-001",
        "reason_codes": [],
        "timestamp": NOW,
        "expected_revision": latest["revision"],
        "expected_event_digest": latest["event_digest"],
    }
    value.update(changes)
    return value


def pass_review(broker: a.ReviewAuthority, latest: dict[str, object] | None = None) -> dict[str, object]:
    latest = latest or register(broker)
    response = broker.handle(p.ROLE_REVIEWER, request("ipc-review-001", p.OP_APPEND_REVIEW, review_payload(latest)))
    assert response.ok, response.error
    assert response.result is not None
    return response.result


def ready_manifest() -> dict[str, object]:
    return {
        "schema_version": "narrative-ready-v1",
        "source_ref": SOURCE_REF,
        "source_digest": H2,
        "narrative_package_ref": f"{H}/story.json",
        "narrative_package_digest": H4,
        "status": "ready",
        "contract_versions": {"director": "review-only-v1", "narrative": "narrative-v1"},
    }


def canonical_digest(value: object) -> str:
    return hashlib.sha256(p.canonical(value)).hexdigest()


def prepare_payload(latest: dict[str, object], **changes: object) -> dict[str, object]:
    ready = ready_manifest()
    value: dict[str, object] = {
        "source_identity": H,
        "source_ref": SOURCE_REF,
        "source_digest": H2,
        "draft_identity": H3,
        "package_digest": H4,
        "narrative_ready_manifest_digest": canonical_digest(ready),
        "story_markdown_digest": H5,
        "draft_manifest_digest": H6,
        "review_digest": H,
        "completed_claim_digest": H2,
        "artifact_binding_digest": H3,
        "ready_manifest_contract": "narrative-ready-v1",
        "source_contract": SOURCE_CONTRACT,
        "draft_contract_versions": {"draft": DRAFT_CONTRACT, "source": SOURCE_CONTRACT},
        "operator_request_id": "approval-001",
        "timestamp": NOW,
        "expected_revision": latest["revision"],
        "expected_event_digest": latest["event_digest"],
    }
    value.update(changes)
    return value


def prepare(broker: a.ReviewAuthority, latest: dict[str, object] | None = None) -> dict[str, object]:
    latest = latest or pass_review(broker)
    response = broker.handle(p.ROLE_REVIEWER, request("ipc-prepare-001", p.OP_PREPARE_APPROVAL, prepare_payload(latest)))
    assert response.ok, response.error
    assert response.result is not None
    return response.result


def commit_payload(prepared: dict[str, object], **changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "prepared": prepared["prepared"],
        "ready_manifest": ready_manifest(),
        "ready_manifest_digest": canonical_digest(ready_manifest()),
        "attestation_digest": prepared["attestation_digest"],
    }
    value.update(changes)
    return value


def commit(broker: a.ReviewAuthority, prepared: dict[str, object] | None = None) -> dict[str, object]:
    prepared = prepared or prepare(broker)
    response = broker.handle(p.ROLE_REVIEWER, request("ipc-commit-001", p.OP_COMMIT_APPROVAL, commit_payload(prepared)))
    assert response.ok, response.error
    assert response.result is not None
    return response.result


def test_full_drafted_passed_prepared_approved_verified_workflow(broker: a.ReviewAuthority) -> None:
    drafted = register(broker)
    assert drafted["state"] == rs.STATE_DRAFTED and drafted["revision"] == 1
    passed = pass_review(broker, drafted)
    assert passed["state"] == rs.STATE_PASSED and passed["revision"] == 2
    prepared = prepare(broker, passed)
    assert prepared["mutated"] is False
    latest_before = broker.handle(p.ROLE_REVIEWER, request("latest-before", p.OP_LATEST_STATE, {"source_identity": H, "draft_identity": H3}))
    assert latest_before.result["state"] == rs.STATE_PASSED
    approved = commit(broker, prepared)
    assert approved["state"] == rs.STATE_APPROVED and approved["revision"] == 3
    verified = broker.handle(
        p.ROLE_CONSUMER,
        request("verify-001", p.OP_VERIFY_READY, {"ready_manifest": ready_manifest(), "attestation": approved["attestation"]}),
    )
    assert verified.ok and verified.result["ready"] is True


@pytest.mark.parametrize("missing", sorted(draft_payload()))
def test_register_draft_rejects_every_missing_field(broker: a.ReviewAuthority, missing: str) -> None:
    payload = draft_payload()
    del payload[missing]
    response = broker.handle(p.ROLE_NORMALIZER, request(f"missing-draft-{missing}", p.OP_REGISTER_DRAFT, payload))
    assert not response.ok and response.error == a.AUTHORITY_INVALID


@pytest.mark.parametrize("extra", ["content", "markdown", "path", "key", "receipt", "role", "raw_source", "delete"])
def test_register_draft_rejects_content_or_extra_fields(broker: a.ReviewAuthority, extra: str) -> None:
    payload = draft_payload()
    payload[extra] = "sensitive"
    response = broker.handle(p.ROLE_NORMALIZER, request(f"extra-draft-{extra}", p.OP_REGISTER_DRAFT, payload))
    assert not response.ok and response.error == a.AUTHORITY_INVALID
    assert not (broker._store.root / H).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_identity", H2), ("source_identity", "A" * 64),
        ("source_digest", "x"), ("draft_identity", None),
        ("package_digest", "0" * 63), ("story_markdown_digest", "0" * 65),
        ("draft_manifest_digest", 1), ("review_digest", True),
        ("completed_claim_digest", "../x"), ("artifact_binding_digest", ""),
        ("source_ref", " /absolute"), ("source_ref", ""),
        ("operator_request_id", "bad/id"), ("timestamp", "2026-01-01"),
        ("contract_versions", {}), ("contract_versions", {"source": 1}),
        ("contract_versions", {"z": "1", "a": "2"}),
    ],
)
def test_register_draft_strict_field_validation(broker: a.ReviewAuthority, field: str, value: object) -> None:
    response = broker.handle(
        p.ROLE_NORMALIZER,
        request(f"invalid-draft-{field}-{len(str(value))}", p.OP_REGISTER_DRAFT, draft_payload(**{field: value})),
    )
    assert not response.ok


@pytest.mark.parametrize("role", [p.ROLE_REVIEWER, p.ROLE_CONSUMER])
def test_only_normalizer_can_register_draft(broker: a.ReviewAuthority, role: str) -> None:
    response = broker.handle(role, request(f"denied-register-{role}", p.OP_REGISTER_DRAFT, draft_payload()))
    assert not response.ok and response.error == p.ACCESS_DENIED
    assert not broker._store.root.exists()


@pytest.mark.parametrize(
    ("old", "new", "allowed"),
    [
        (rs.STATE_DRAFTED, rs.STATE_PASSED, True),
        (rs.STATE_DRAFTED, rs.STATE_REJECTED, True),
        (rs.STATE_DRAFTED, rs.STATE_SUPERSEDED, False),
        (rs.STATE_DRAFTED, rs.STATE_APPROVED, False),
        (rs.STATE_PASSED, rs.STATE_REJECTED, True),
        (rs.STATE_PASSED, rs.STATE_SUPERSEDED, True),
        (rs.STATE_PASSED, rs.STATE_PASSED, False),
        (rs.STATE_REJECTED, rs.STATE_PASSED, False),
        (rs.STATE_REJECTED, rs.STATE_REJECTED, False),
        (rs.STATE_REJECTED, rs.STATE_SUPERSEDED, False),
    ],
)
def test_review_transition_matrix(broker: a.ReviewAuthority, old: str, new: str, allowed: bool) -> None:
    latest = register(broker)
    if old != rs.STATE_DRAFTED:
        first = broker.handle(p.ROLE_REVIEWER, request("first-transition", p.OP_APPEND_REVIEW, review_payload(latest, new_state=old)))
        assert first.ok
        latest = first.result
    response = broker.handle(
        p.ROLE_REVIEWER,
        request("second-transition", p.OP_APPEND_REVIEW, review_payload(latest, new_state=new, operator_request_id="review-002")),
    )
    assert response.ok is allowed


@pytest.mark.parametrize("field", sorted(review_payload({"revision": 1, "event_digest": H})))
def test_review_rejects_every_missing_field(broker: a.ReviewAuthority, field: str) -> None:
    latest = register(broker)
    payload = review_payload(latest)
    del payload[field]
    response = broker.handle(p.ROLE_REVIEWER, request(f"review-missing-{field}", p.OP_APPEND_REVIEW, payload))
    assert not response.ok


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_revision", 0), ("expected_revision", True),
        ("expected_revision", 2), ("expected_event_digest", H2),
        ("draft_identity", H2), ("source_identity", H2),
        ("new_state", rs.STATE_APPROVED), ("new_state", "unknown"),
        ("reason_codes", ["z", "a"]), ("reason_codes", ["a", "a"]),
        ("reason_codes", "a"), ("operator_request_id", "bad/id"),
    ],
)
def test_review_fail_closed_on_stale_or_invalid_input(broker: a.ReviewAuthority, field: str, value: object) -> None:
    latest = register(broker)
    response = broker.handle(
        p.ROLE_REVIEWER,
        request(f"bad-review-{field}-{len(str(value))}", p.OP_APPEND_REVIEW, review_payload(latest, **{field: value})),
    )
    assert not response.ok
    unchanged = broker._store.read(H, H3)
    assert unchanged.latest.revision == 1


@pytest.mark.parametrize("role", [p.ROLE_NORMALIZER, p.ROLE_CONSUMER])
def test_nonreviewers_cannot_append_review(broker: a.ReviewAuthority, role: str) -> None:
    latest = register(broker)
    response = broker.handle(role, request(f"denied-review-{role}", p.OP_APPEND_REVIEW, review_payload(latest)))
    assert not response.ok and response.error == p.ACCESS_DENIED
    assert broker._store.read(H).latest.state == rs.STATE_DRAFTED


@pytest.mark.parametrize("missing", sorted(prepare_payload({"revision": 2, "event_digest": H})))
def test_prepare_rejects_every_missing_field(broker: a.ReviewAuthority, missing: str) -> None:
    latest = pass_review(broker)
    payload = prepare_payload(latest)
    del payload[missing]
    response = broker.handle(p.ROLE_REVIEWER, request(f"prepare-missing-{missing}", p.OP_PREPARE_APPROVAL, payload))
    assert not response.ok
    assert broker._store.read(H).latest.state == rs.STATE_PASSED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expected_revision", 1), ("expected_event_digest", H2),
        ("source_identity", H2), ("draft_identity", H2),
        ("source_digest", H), ("package_digest", "x"),
        ("story_markdown_digest", None),
        ("draft_manifest_digest", 1), ("review_digest", True),
        ("completed_claim_digest", ""), ("artifact_binding_digest", "f" * 63),
        ("operator_request_id", "bad/id"), ("timestamp", "bad"),
        ("ready_manifest_contract", ""), ("source_contract", " "),
    ],
)
def test_prepare_stale_or_invalid_input_never_mutates(broker: a.ReviewAuthority, field: str, value: object) -> None:
    latest = pass_review(broker)
    response = broker.handle(
        p.ROLE_REVIEWER,
        request(f"bad-prepare-{field}-{len(str(value))}", p.OP_PREPARE_APPROVAL, prepare_payload(latest, **{field: value})),
    )
    assert not response.ok
    assert broker._store.read(H).latest.state == rs.STATE_PASSED


@pytest.mark.parametrize("role", [p.ROLE_NORMALIZER, p.ROLE_CONSUMER])
def test_only_reviewer_can_prepare_or_commit(broker: a.ReviewAuthority, role: str) -> None:
    latest = pass_review(broker)
    response = broker.handle(role, request(f"denied-prepare-{role}", p.OP_PREPARE_APPROVAL, prepare_payload(latest)))
    assert not response.ok and response.error == p.ACCESS_DENIED


@pytest.mark.parametrize("field", ["prepared", "ready_manifest", "ready_manifest_digest", "attestation_digest"])
def test_commit_rejects_every_missing_field(broker: a.ReviewAuthority, field: str) -> None:
    prepared = prepare(broker)
    payload = commit_payload(prepared)
    del payload[field]
    response = broker.handle(p.ROLE_REVIEWER, request(f"commit-missing-{field}", p.OP_COMMIT_APPROVAL, payload))
    assert not response.ok and broker._store.read(H).latest.state == rs.STATE_PASSED


@pytest.mark.parametrize(
    "mutation",
    [
        ("ready_manifest_digest", H), ("attestation_digest", H),
        ("ready_manifest", {"source_ref": "wrong"}),
        ("prepared.schema_version", "v2"),
        ("prepared.prepared_identity", H),
        ("prepared.event.event_digest", H),
        ("prepared.event.source_identity", H2),
        ("prepared.event.draft_identity", H2),
        ("prepared.event.revision", 99),
        ("prepared.attestation.source_identity", H2),
        ("prepared.attestation.source_digest", H),
        ("prepared.attestation.draft_identity", H2),
        ("prepared.attestation.package_digest", H2),
        ("prepared.attestation.review_event_digest", H),
        ("prepared.attestation.review_revision", 99),
        ("prepared.attestation.key_id", "0" * 24),
        ("prepared.attestation.trust_receipt.seal", H),
    ],
)
def test_commit_tamper_matrix_fails_before_append(broker: a.ReviewAuthority, mutation: tuple[str, object]) -> None:
    prepared = prepare(broker)
    payload = copy.deepcopy(commit_payload(prepared))
    path, value = mutation
    cursor = payload
    parts = path.split(".")
    for part in parts[:-1]:
        cursor = cursor[part]
    cursor[parts[-1]] = value
    response = broker.handle(p.ROLE_REVIEWER, request(f"tamper-{path.replace('.', '-')}", p.OP_COMMIT_APPROVAL, payload))
    assert not response.ok
    assert broker._store.read(H).latest.state == rs.STATE_PASSED


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_ref", "wrong"), ("source_digest", H),
        ("narrative_package_digest", H2), ("status", "blocked"),
        ("schema_version", "v2"), ("narrative_package_ref", "wrong"),
    ],
)
def test_consumer_verification_tamper_returns_safe_failure(broker: a.ReviewAuthority, field: str, value: object) -> None:
    approved = commit(broker)
    ready = ready_manifest()
    ready[field] = value
    response = broker.handle(
        p.ROLE_CONSUMER,
        request(f"verify-tamper-{field}", p.OP_VERIFY_READY, {"ready_manifest": ready, "attestation": approved["attestation"]}),
    )
    assert not response.ok and response.error == a.AUTHORITY_NOT_READY


@pytest.mark.parametrize("role", [p.ROLE_NORMALIZER, p.ROLE_REVIEWER])
def test_only_consumer_can_verify_ready(broker: a.ReviewAuthority, role: str) -> None:
    response = broker.handle(role, request(f"denied-verify-{role}", p.OP_VERIFY_READY, {"ready_manifest": {}, "attestation": {}}))
    assert not response.ok and response.error == p.ACCESS_DENIED


def test_sequential_exact_request_replay_returns_cached_result_without_second_event(broker: a.ReviewAuthority) -> None:
    req = request("same-request", p.OP_REGISTER_DRAFT, draft_payload())
    first = broker.handle(p.ROLE_NORMALIZER, req)
    second = broker.handle(p.ROLE_NORMALIZER, req)
    assert first == second and first.ok
    assert len(broker._store.read(H).events) == 1


@pytest.mark.parametrize("divergence", ["role", "operation", "payload", "schema-semantics"])
def test_divergent_request_replay_conflicts_without_mutation(broker: a.ReviewAuthority, divergence: str) -> None:
    first = request("reused-request", p.OP_REGISTER_DRAFT, draft_payload())
    assert broker.handle(p.ROLE_NORMALIZER, first).ok
    role = p.ROLE_NORMALIZER
    second = first
    if divergence == "role":
        role = p.ROLE_REVIEWER
    elif divergence == "operation":
        second = request("reused-request", p.OP_LATEST_STATE, {"source_identity": H, "draft_identity": H3})
    elif divergence == "payload":
        second = request("reused-request", p.OP_REGISTER_DRAFT, draft_payload(source_digest=H))
    else:
        second = request("reused-request", p.OP_REGISTER_DRAFT, draft_payload(operator_request_id="register-002"))
    response = broker.handle(role, second)
    assert not response.ok and response.error == p.REQUEST_CONFLICT
    assert len(broker._store.read(H).events) == 1


def test_concurrent_exact_duplicates_append_one_event(broker: a.ReviewAuthority) -> None:
    req = request("concurrent-same", p.OP_REGISTER_DRAFT, draft_payload())
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(lambda _: broker.handle(p.ROLE_NORMALIZER, req), range(24)))
    assert all(item.ok for item in responses)
    assert len({json.dumps(item.to_payload(), sort_keys=True) for item in responses}) == 1
    assert len(broker._store.read(H).events) == 1


def test_concurrent_divergent_duplicates_append_at_most_one_event(broker: a.ReviewAuthority) -> None:
    requests = [
        request("concurrent-divergent", p.OP_REGISTER_DRAFT, draft_payload(operator_request_id=f"register-{i:03d}"))
        for i in range(24)
    ]
    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(lambda req: broker.handle(p.ROLE_NORMALIZER, req), requests))
    assert sum(item.ok for item in responses) == 1
    assert all(item.ok or item.error == p.REQUEST_CONFLICT for item in responses)
    assert len(broker._store.read(H).events) == 1


@pytest.mark.parametrize("divergent", [False, True])
def test_two_process_append_race_has_one_no_clobber_event(tmp_path: Path, divergent: bool) -> None:
    root = tmp_path / "authority"
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    operators = ("register-process-a", "register-process-b" if divergent else "register-process-a")
    processes = [context.Process(target=_process_register, args=(str(root), operator, output)) for operator in operators]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    results = [output.get(timeout=5) for _ in processes]
    output.close()
    if divergent:
        assert sum(ok for ok, _ in results) == 1
        assert {error for ok, error in results if not ok} == {a.AUTHORITY_STATE_CONFLICT}
    else:
        assert all(ok for ok, _ in results)
    assert len(a.ReviewAuthority(root, service())._store.read(H).events) == 1


def test_crash_before_event_promotion_leaves_previous_chain_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = a._no_clobber

    def fail_before(path: Path, data: bytes) -> bool:
        if path.parent.name == "events":
            raise OSError("simulated pre-promotion crash")
        return original(path, data)

    broker = a.ReviewAuthority(tmp_path / "authority", service())
    monkeypatch.setattr(a, "_no_clobber", fail_before)
    response = broker.handle(p.ROLE_NORMALIZER, request("crash-before", p.OP_REGISTER_DRAFT, draft_payload()))
    assert not response.ok
    restarted = a.ReviewAuthority(tmp_path / "authority", service())
    with pytest.raises(a.AuthorityError, match=a.AUTHORITY_STATE_MISSING):
        restarted._store.read(H)


def test_crash_after_event_promotion_is_discovered_on_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = a._no_clobber

    def fail_after(path: Path, data: bytes) -> bool:
        result = original(path, data)
        if path.parent.name == "events":
            raise OSError("simulated post-promotion crash")
        return result

    broker = a.ReviewAuthority(tmp_path / "authority", service())
    monkeypatch.setattr(a, "_no_clobber", fail_after)
    response = broker.handle(p.ROLE_NORMALIZER, request("crash-after", p.OP_REGISTER_DRAFT, draft_payload()))
    assert not response.ok
    restarted = a.ReviewAuthority(tmp_path / "authority", service())
    assert restarted._store.read(H).latest.state == rs.STATE_DRAFTED


def test_baseexception_from_dispatch_is_not_normalized_or_swallowed(
    broker: a.ReviewAuthority, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cancel(self: a.ReviewAuthority, operation: str, payload: dict[str, object]) -> dict[str, object]:
        raise KeyboardInterrupt

    monkeypatch.setattr(a.ReviewAuthority, "_dispatch", cancel)
    cancelled = request("cancel-request", p.OP_HEALTH, {})
    with pytest.raises(KeyboardInterrupt):
        broker.handle(p.ROLE_CONSUMER, cancelled)
    replay = broker.handle(p.ROLE_CONSUMER, cancelled)
    assert not replay.ok and replay.error == a.AUTHORITY_INTERNAL


def test_operator_identity_exact_duplicate_is_durable_across_restart(tmp_path: Path) -> None:
    root = tmp_path / "authority"
    first = a.ReviewAuthority(root, service())
    register(first)
    second = a.ReviewAuthority(root, service())
    replay = second.handle(p.ROLE_NORMALIZER, request("new-ipc-id", p.OP_REGISTER_DRAFT, draft_payload()))
    assert replay.ok and replay.result["idempotent"] is True
    assert len(second._store.read(H).events) == 1


@pytest.mark.parametrize("tamper", ["gap", "fork", "filename", "content", "signature", "previous", "draft", "source", "revision", "extra"])
def test_event_chain_corruption_matrix_fails_closed(tmp_path: Path, tamper: str) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    pass_review(broker)
    events = sorted((tmp_path / "authority" / H / "events").iterdir())
    first, second = events
    if tamper == "gap":
        second.rename(second.with_name(second.name.replace("00000002", "00000003")))
    elif tamper == "fork":
        (second.parent / f"00000002-{H}.json").write_bytes(second.read_bytes())
    elif tamper == "filename":
        first.rename(first.with_name(f"00000001-{H}.json"))
    elif tamper == "extra":
        (second.parent / "unexpected.txt").write_text("x", encoding="utf-8")
    else:
        raw = json.loads(second.read_text(encoding="utf-8"))
        if tamper == "content": raw["reason_codes"] = ["changed"]
        if tamper == "signature": raw["trust_receipt"]["seal"] = H
        if tamper == "previous": raw["previous_event_digest"] = H
        if tamper == "draft": raw["draft_identity"] = H2
        if tamper == "source": raw["source_identity"] = H2
        if tamper == "revision": raw["revision"] = 99
        second.write_bytes(p.canonical(raw) + b"\n")
    with pytest.raises(a.AuthorityError, match=a.AUTHORITY_PERSISTENCE_INVALID):
        broker._store.read(H)


def test_stale_head_cache_cannot_roll_back_authority(tmp_path: Path) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    drafted = register(broker)
    head = tmp_path / "authority" / H / "head.json"
    stale = head.read_bytes()
    passed = pass_review(broker, drafted)
    head.write_bytes(stale)
    ledger = broker._store.read(H)
    assert ledger.latest.state == rs.STATE_PASSED and ledger.latest.event_digest == passed["event_digest"]


@pytest.mark.parametrize("relative", ["relative", "./state", "../state", "", ".", "authority/state"])
def test_authority_root_must_be_absolute(tmp_path: Path, relative: str) -> None:
    with pytest.raises(a.AuthorityError, match=a.AUTHORITY_PATH_INVALID):
        a.validate_authority_root(relative)


@pytest.mark.parametrize("relationship", ["same", "inside", "contains"])
def test_authority_root_cannot_overlap_protected_roots(tmp_path: Path, relationship: str) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    root = {"same": protected, "inside": protected / "authority", "contains": tmp_path}[relationship]
    with pytest.raises(a.AuthorityError, match=a.AUTHORITY_PATH_INVALID):
        a.validate_authority_root(root, protected_roots=[protected])


def test_authority_root_cannot_be_inside_git_checkout(tmp_path: Path) -> None:
    git = tmp_path / "repo"
    (git / ".git").mkdir(parents=True)
    with pytest.raises(a.AuthorityError, match=a.AUTHORITY_PATH_INVALID):
        a.validate_authority_root(git / "state", git_root=git)


def test_authority_root_rejects_symlink_component(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(a.AuthorityError, match=a.AUTHORITY_PATH_INVALID):
        a.validate_authority_root(link / "state")


def write_key(path: Path, raw: bytes = b"k" * 32) -> None:
    path.write_text(base64.b64encode(raw).decode("ascii"), encoding="ascii")
    if os.name != "nt":
        path.chmod(0o600)


@pytest.mark.parametrize("kind", ["missing", "short", "garbage", "newline", "symlink", "directory"])
def test_key_file_validation_fails_closed(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "key"
    if kind == "short": write_key(path, b"short")
    elif kind == "garbage": path.write_text("%%%", encoding="ascii")
    elif kind == "newline": path.write_text(base64.b64encode(b"k" * 32).decode() + "\nextra", encoding="ascii")
    elif kind == "directory": path.mkdir()
    elif kind == "symlink":
        target = tmp_path / "target-key"
        write_key(target)
        try: path.symlink_to(target)
        except OSError: pytest.skip("symlink unavailable")
    with pytest.raises(a.AuthorityError):
        a.load_authority(authority_root=tmp_path / "authority", key_file=path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX key mode contract")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o400 | 0o040])
def test_key_file_requires_posix_0600(tmp_path: Path, mode: int) -> None:
    key = tmp_path / "key"
    write_key(key)
    key.chmod(mode)
    with pytest.raises(a.AuthorityError):
        a.load_authority(authority_root=tmp_path / "authority", key_file=key)


def test_key_never_appears_in_broker_repr_or_health(tmp_path: Path) -> None:
    key = tmp_path / "key"
    raw = b"secret-material-never-returned-1234"
    write_key(key, raw)
    broker = a.load_authority(authority_root=tmp_path / "authority", key_file=key)
    health = broker.handle(p.ROLE_CONSUMER, request("health", p.OP_HEALTH, {}))
    rendered = repr(broker) + json.dumps(health.to_payload())
    assert raw.decode("ascii") not in rendered
    assert base64.b64encode(raw).decode("ascii") not in rendered


@pytest.mark.parametrize(
    ("uid", "gid", "expected"),
    [(1, 9, p.ROLE_NORMALIZER), (8, 2, p.ROLE_REVIEWER), (3, 3, p.ROLE_CONSUMER)],
)
def test_peer_role_policy_derives_role_from_kernel_ids(uid: int, gid: int, expected: str) -> None:
    policy = server.PeerRolePolicy({1: p.ROLE_NORMALIZER, 8: p.ROLE_REVIEWER}, {2: p.ROLE_REVIEWER, 3: p.ROLE_CONSUMER})
    assert policy.role_for(uid, gid) == expected


@pytest.mark.parametrize(("uid", "gid"), [(99, 99), (1, 2), (8, 3)])
def test_peer_role_policy_rejects_unknown_or_conflicting_ids(uid: int, gid: int) -> None:
    policy = server.PeerRolePolicy({1: p.ROLE_NORMALIZER, 8: p.ROLE_REVIEWER}, {2: p.ROLE_REVIEWER, 3: p.ROLE_CONSUMER})
    with pytest.raises(server.ServerError, match=server.PEER_UNAUTHORIZED):
        policy.role_for(uid, gid)


@pytest.mark.parametrize("mapping", [{-1: p.ROLE_NORMALIZER}, {1: "admin"}, {True: p.ROLE_REVIEWER}, {1: Text(p.ROLE_REVIEWER)}])
def test_peer_role_policy_is_closed_and_exact(mapping: dict[object, object]) -> None:
    with pytest.raises(server.ServerError, match=server.SERVER_INVALID):
        server.PeerRolePolicy(mapping, {})


def test_peer_role_policy_copies_mutable_input() -> None:
    raw = {1: p.ROLE_NORMALIZER}
    policy = server.PeerRolePolicy(raw, {})
    raw[1] = p.ROLE_REVIEWER
    assert policy.role_for(1, 9) == p.ROLE_NORMALIZER
    with pytest.raises(TypeError):
        policy.uid_roles[1] = p.ROLE_REVIEWER


@pytest.mark.skipif(os.name == "nt" or not hasattr(__import__("socket"), "SO_PEERCRED"), reason="Linux SO_PEERCRED contract")
def test_unix_server_uses_real_peer_credentials(tmp_path: Path) -> None:
    import socket
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    policy = server.PeerRolePolicy({os.getuid(): p.ROLE_CONSUMER}, {})
    path = tmp_path / "broker.sock"
    value = server.ReviewAuthorityServer(
        broker, socket_path=path, peer_policy=policy,
        owner_uid=os.getuid(), owner_gid=os.getgid(), mode=0o600,
    )
    value.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.connect(str(path))
            pid, uid, gid = value.peer_credentials(peer)
        assert pid == os.getpid() and uid == os.getuid() and gid == os.getgid()
    finally:
        value.close()


def test_two_concatenated_frames_are_rejected_before_broker_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    calls: list[str] = []
    original = a.ReviewAuthority.handle

    def counted(self: a.ReviewAuthority, role: str, value: p.Request) -> p.Response:
        calls.append(value.request_id)
        return original(self, role, value)

    monkeypatch.setattr(a.ReviewAuthority, "handle", counted)
    first = p.encode_frame(request("first-frame", p.OP_HEALTH, {}).to_payload())
    second = p.encode_frame(request("second-frame", p.OP_HEALTH, {}).to_payload())
    response = raw_connection(transport_server(tmp_path, broker, monkeypatch), [first + second])
    parsed = p.response_from_payload(p.decode_frame_bytes(response))
    assert not parsed.ok and calls == []
    assert len(broker._requests) == 0 and not broker._store.root.exists()


def test_delayed_second_frame_is_rejected_before_broker_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    calls: list[str] = []
    original = a.ReviewAuthority.handle

    def counted(self: a.ReviewAuthority, role: str, value: p.Request) -> p.Response:
        calls.append(value.request_id)
        return original(self, role, value)

    monkeypatch.setattr(a.ReviewAuthority, "handle", counted)
    frames = [
        p.encode_frame(request("delayed-first", p.OP_HEALTH, {}).to_payload()),
        p.encode_frame(request("delayed-second", p.OP_HEALTH, {}).to_payload()),
    ]
    response = raw_connection(
        transport_server(tmp_path, broker, monkeypatch), frames,
        delays=[0.15],
    )
    assert not p.response_from_payload(p.decode_frame_bytes(response)).ok
    assert calls == [] and len(broker._requests) == 0


@pytest.mark.parametrize("trailing", [b"x", b"trailing prose"])
def test_single_frame_with_trailing_data_is_rejected_before_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trailing: bytes,
) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    calls: list[str] = []
    original = a.ReviewAuthority.handle

    def counted(self: a.ReviewAuthority, role: str, value: p.Request) -> p.Response:
        calls.append(value.request_id)
        return original(self, role, value)

    monkeypatch.setattr(a.ReviewAuthority, "handle", counted)
    frame = p.encode_frame(request("trailing-request", p.OP_HEALTH, {}).to_payload())
    response = raw_connection(transport_server(tmp_path, broker, monkeypatch), [frame + trailing])
    assert not p.response_from_payload(p.decode_frame_bytes(response)).ok
    assert calls == [] and len(broker._requests) == 0


def test_fragmented_single_frame_is_dispatched_once_after_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    calls: list[str] = []
    original = a.ReviewAuthority.handle

    def counted(self: a.ReviewAuthority, role: str, value: p.Request) -> p.Response:
        calls.append(value.request_id)
        return original(self, role, value)

    monkeypatch.setattr(a.ReviewAuthority, "handle", counted)
    frame = p.encode_frame(request("fragmented-request", p.OP_HEALTH, {}).to_payload())
    chunks = [frame[index:index + 3] for index in range(0, len(frame), 3)]
    response = raw_connection(transport_server(tmp_path, broker, monkeypatch), chunks)
    parsed = p.response_from_payload(p.decode_frame_bytes(response))
    assert parsed.ok and calls == ["fragmented-request"]
    assert len(broker._requests) == 1


def test_missing_client_write_half_close_times_out_before_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    calls: list[str] = []
    original = a.ReviewAuthority.handle

    def counted(self: a.ReviewAuthority, role: str, value: p.Request) -> p.Response:
        calls.append(value.request_id)
        return original(self, role, value)

    monkeypatch.setattr(a.ReviewAuthority, "handle", counted)
    frame = p.encode_frame(request("no-half-close", p.OP_HEALTH, {}).to_payload())
    response = raw_connection(
        transport_server(tmp_path, broker, monkeypatch, timeout=0.15),
        [frame], half_close=False,
    )
    parsed = p.response_from_payload(p.decode_frame_bytes(response))
    assert not parsed.ok and parsed.error == p.WRITE_HALF_CLOSE_REQUIRED
    assert calls == [] and len(broker._requests) == 0


def test_state_changing_first_frame_plus_second_request_executes_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    latest = register(broker)
    before_registry = len(broker._requests)
    events_root = broker._store.root / H / "events"
    before_events = {path.name: path.read_bytes() for path in events_root.iterdir()}
    calls: list[str] = []
    original = a.ReviewAuthority.handle

    def counted(self: a.ReviewAuthority, role: str, value: p.Request) -> p.Response:
        calls.append(value.request_id)
        return original(self, role, value)

    monkeypatch.setattr(a.ReviewAuthority, "handle", counted)
    append = p.encode_frame(
        request("invalid-append-first", p.OP_APPEND_REVIEW, review_payload(latest)).to_payload()
    )
    another = p.encode_frame(request("invalid-health-second", p.OP_HEALTH, {}).to_payload())
    response = raw_connection(transport_server(tmp_path, broker, monkeypatch), [append + another])
    assert not p.response_from_payload(p.decode_frame_bytes(response)).ok
    assert calls == [] and len(broker._requests) == before_registry
    assert broker._store.read(H).latest.state == rs.STATE_DRAFTED
    assert {path.name: path.read_bytes() for path in events_root.iterdir()} == before_events


def test_exact_single_request_returns_exactly_one_closed_response_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    frame = p.encode_frame(request("single-request", p.OP_HEALTH, {}).to_payload())
    running = transport_server(tmp_path, broker, monkeypatch)
    response = raw_connection(running, [frame])
    parsed = p.response_from_payload(p.decode_frame_bytes(response))
    assert parsed.ok and parsed.request_id == "single-request"
    assert not os.path.lexists(running.socket_path)
    assert not broker._store.root.exists()


def test_client_sends_one_frame_then_write_half_closes_before_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = p.Response("client-request", True, {"status": "ok"}, None)
    fake = FakeClientSocket(p.encode_frame(response.to_payload()))
    monkeypatch.setattr(client, "validate_socket", lambda *args, **kwargs: None)
    monkeypatch.setattr(client.socket, "AF_UNIX", getattr(client.socket, "AF_UNIX", 1), raising=False)
    monkeypatch.setattr(client.socket, "socket", lambda *args, **kwargs: fake)
    proxy = client.ReviewAuthorityClient(tmp_path / "authority.sock", owner_uid=0, owner_gid=0)
    assert proxy.health("client-request") == {"status": "ok"}
    assert fake.shutdown_calls == [socket.SHUT_WR]
    sent = bytes(fake.stream.sent)
    parsed = p.request_from_payload(p.decode_frame_bytes(sent))
    assert parsed.request_id == "client-request"


@pytest.mark.parametrize("module", [
    "narrative_review_authority", "narrative_review_authority_protocol",
    "narrative_review_authority_server", "narrative_review_authority_client",
])
def test_import_is_inert(tmp_path: Path, module: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = tmp_path / "must-not-exist"
    monkeypatch.setenv("NARRATIVE_REVIEW_AUTHORITY_ROOT", str(sentinel))
    importlib.reload(importlib.import_module(module))
    assert not sentinel.exists()


def test_cli_help_is_inert_and_does_not_create_authority(tmp_path: Path) -> None:
    sentinel = tmp_path / "must-not-exist"
    env = dict(os.environ, NARRATIVE_REVIEW_AUTHORITY_ROOT=str(sentinel), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [sys.executable, "tools/run_narrative_review_authority.py", "--help"],
        cwd=Path(__file__).parents[1], env=env, capture_output=True, text=True,
        timeout=20, check=False,
    )
    assert result.returncode == 0 and "--authority-root" in result.stdout
    assert not sentinel.exists()


def test_client_proxy_slots_contain_only_transport_configuration() -> None:
    assert set(client.ReviewAuthorityClient.__slots__) == {"_socket_path", "_owner_uid", "_owner_gid", "_mode", "_timeout"}
    forbidden = {"key", "service", "trust", "authority", "allowlist", "ledger", "requests", "sign"}
    assert not forbidden.intersection(client.ReviewAuthorityClient.__slots__)


@pytest.mark.parametrize("name", ["delete", "replace", "truncate", "list_events", "sign", "key", "trust_service", "raw_path"])
def test_client_exposes_no_dangerous_or_signing_surface(name: str) -> None:
    assert not hasattr(client.ReviewAuthorityClient, name)


def test_no_public_storage_delete_replace_or_truncate_api() -> None:
    for name in ("delete", "replace", "truncate", "unlink", "repair", "reset"):
        assert not hasattr(a.ReviewStateAdapter, name)


def test_prepare_failure_is_cached_without_hidden_retry(broker: a.ReviewAuthority) -> None:
    latest = register(broker)
    req = request("cached-failure", p.OP_PREPARE_APPROVAL, prepare_payload(latest))
    first = broker.handle(p.ROLE_REVIEWER, req)
    second = broker.handle(p.ROLE_REVIEWER, req)
    assert first == second and not first.ok
    assert len(broker._store.read(H).events) == 1


def test_head_cache_failure_does_not_erase_durable_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broker = a.ReviewAuthority(tmp_path / "authority", service())
    original = os.replace
    monkeypatch.setattr(os, "replace", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("cache fault")))
    result = register(broker)
    monkeypatch.setattr(os, "replace", original)
    assert result["state"] == rs.STATE_DRAFTED
    assert broker._store.read(H).latest.state == rs.STATE_DRAFTED


def test_draft_receipt_and_event_are_signed_by_reviewed_contract(broker: a.ReviewAuthority) -> None:
    result = register(broker)
    event = rs.review_event_from_payload(result["event"], service())
    assert event.state == rs.STATE_DRAFTED
    assert event.trust_receipt.domain == trust.TRUST_DOMAIN_REVIEW_LEDGER


def test_latest_state_is_detached_serializable_snapshot(broker: a.ReviewAuthority) -> None:
    register(broker)
    response = broker.handle(p.ROLE_CONSUMER, request("latest", p.OP_LATEST_STATE, {"source_identity": H, "draft_identity": H3}))
    encoded = json.dumps(response.result, sort_keys=True)
    response.result["state"] = "forged"
    assert broker._store.read(H).latest.state == rs.STATE_DRAFTED
    assert "drafted" in encoded
