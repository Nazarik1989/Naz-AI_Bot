from __future__ import annotations

import json
import multiprocessing
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import narrative_normalizer_review_state as rs
import narrative_normalizer_trust as trust


KEY = b"review-state-test-key-material-32-bytes!!"
SOURCE_IDENTITY = "1" * 64
DRAFT_IDENTITY = "2" * 64
SOURCE_DIGEST = "3" * 64
PACKAGE_DIGEST = "4" * 64
READY_DIGEST = "5" * 64
T0 = "2026-08-17T00:00:00Z"
T1 = "2026-08-17T00:00:01Z"
T2 = "2026-08-17T00:00:02Z"


class StringSubclass(str):
    pass


class IntegerSubclass(int):
    pass


def service() -> trust.NarrativeTrustService:
    return trust.NarrativeTrustService(KEY)


def initialized(tmp_path: Path) -> tuple[rs.ReviewStateStore, rs.ReviewLedger]:
    store = rs.ReviewStateStore(tmp_path / "state", service())
    ledger = store.initialize(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        initial_state=rs.STATE_PASSED,
        reason_codes=(),
        drafted_at=T0,
        reviewed_at=T1,
    )
    return store, ledger


def _prepared_reject(store: rs.ReviewStateStore, *, request_id: str = "fault-reject") -> rs.ReviewEvent:
    event, idempotent = store.prepare_transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_REJECTED,
        operator_request_id=request_id,
        reason_codes=("operator-rejected",),
        timestamp=T2,
    )
    assert idempotent is False
    return event


def _event_snapshot(store: rs.ReviewStateStore) -> dict[str, bytes]:
    return {
        path.name: path.read_bytes()
        for path in sorted(store.events_path_for(SOURCE_IDENTITY).glob("*.json"))
    }


def _process_commit_worker(
    authority_root: str,
    state: str,
    request_id: str,
    timestamp: str,
    ready_queue,
    start_event,
    result_queue,
) -> None:
    try:
        store = rs.ReviewStateStore(Path(authority_root), service())
        event, _ = store.prepare_transition(
            source_identity=SOURCE_IDENTITY,
            draft_identity=DRAFT_IDENTITY,
            new_state=state,
            operator_request_id=request_id,
            reason_codes=("operator-rejected",) if state == rs.STATE_REJECTED else (),
            timestamp=timestamp,
        )
        ready_queue.put("prepared")
        if not start_event.wait(20):
            result_queue.put("timeout")
            return
        _, idempotent = store.commit_prepared(event)
        result_queue.put("idempotent" if idempotent else "won")
    except rs.ReviewStateError as error:
        result_queue.put(error.reason_code)
    except BaseException as error:
        result_queue.put(type(error).__name__)


def test_review_ledger_initializes_exact_two_event_chain(tmp_path: Path) -> None:
    store, ledger = initialized(tmp_path)
    assert [item.revision for item in ledger.events] == [1, 2]
    assert [item.state for item in ledger.events] == [rs.STATE_DRAFTED, rs.STATE_PASSED]
    assert ledger.events[1].previous_event_digest == ledger.events[0].event_digest
    assert store.read(SOURCE_IDENTITY) == ledger


def test_review_ledger_initialization_is_byte_idempotent(tmp_path: Path) -> None:
    store, first = initialized(tmp_path)
    path = store.path_for(SOURCE_IDENTITY)
    before = path.read_bytes()
    events_before = _event_snapshot(store)
    second = store.initialize(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        initial_state=rs.STATE_PASSED,
        reason_codes=(),
        drafted_at=T0,
        reviewed_at=T1,
    )
    assert second == first
    assert path.read_bytes() == before
    assert _event_snapshot(store) == events_before


def test_review_transition_increments_once_and_duplicate_is_idempotent(tmp_path: Path) -> None:
    store, _ = initialized(tmp_path)
    kwargs = dict(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_REJECTED,
        operator_request_id="reject-001",
        reason_codes=("operator-rejected",),
        timestamp=T2,
    )
    first, first_idempotent = store.transition(**kwargs)
    before = store.path_for(SOURCE_IDENTITY).read_bytes()
    second, second_idempotent = store.transition(**dict(kwargs, timestamp="2026-08-17T01:00:00Z"))
    assert first.latest.revision == 3
    assert first_idempotent is False
    assert second_idempotent is True
    assert second == first
    assert store.path_for(SOURCE_IDENTITY).read_bytes() == before


def test_review_duplicate_request_with_different_action_conflicts(tmp_path: Path) -> None:
    store, _ = initialized(tmp_path)
    store.transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_REJECTED,
        operator_request_id="request-001",
        reason_codes=("operator-rejected",),
        timestamp=T2,
    )
    with pytest.raises(rs.ReviewStateError, match=rs.REVIEW_STATE_CONFLICT):
        store.prepare_transition(
            source_identity=SOURCE_IDENTITY,
            draft_identity=DRAFT_IDENTITY,
            new_state=rs.STATE_SUPERSEDED,
            operator_request_id="request-001",
            reason_codes=(),
            timestamp=T2,
        )


def test_two_prepared_events_for_same_revision_have_one_cas_winner(tmp_path: Path) -> None:
    store, _ = initialized(tmp_path)
    first, _ = store.prepare_transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_REJECTED,
        operator_request_id="reject-a",
        reason_codes=("operator-rejected",),
        timestamp=T2,
    )
    second, _ = store.prepare_transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_SUPERSEDED,
        operator_request_id="supersede-b",
        reason_codes=(),
        timestamp=T2,
    )

    def commit(event: rs.ReviewEvent) -> str:
        try:
            store.commit_prepared(event)
            return "won"
        except rs.ReviewStateError as error:
            return error.reason_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(commit, (first, second)))
    assert outcomes.count("won") == 1
    assert outcomes.count(rs.REVIEW_STATE_CONFLICT) == 1
    final = store.read(SOURCE_IDENTITY)
    assert final.latest.revision == 3
    assert len(final.events) == 3


@pytest.mark.parametrize("same_request", (False, True), ids=("different-actions", "exact-request"))
def test_two_processes_have_one_immutable_revision_winner(
    tmp_path: Path,
    same_request: bool,
) -> None:
    store, _ = initialized(tmp_path)
    context = multiprocessing.get_context("spawn")
    ready_queue = context.Queue()
    result_queue = context.Queue()
    start_event = context.Event()
    arguments = (
        (rs.STATE_REJECTED, "shared-request", T2),
        (
            rs.STATE_REJECTED if same_request else rs.STATE_SUPERSEDED,
            "shared-request" if same_request else "different-request",
            "2026-08-17T00:00:03Z",
        ),
    )
    processes = [
        context.Process(
            target=_process_commit_worker,
            args=(
                str(store.root), state, request_id, timestamp,
                ready_queue, start_event, result_queue,
            ),
        )
        for state, request_id, timestamp in arguments
    ]
    for process in processes:
        process.start()
    assert [ready_queue.get(timeout=30) for _ in processes] == ["prepared", "prepared"]
    start_event.set()
    outcomes = [result_queue.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    if same_request:
        assert sorted(outcomes) == ["idempotent", "won"]
    else:
        assert outcomes.count("won") == 1
        assert outcomes.count(rs.REVIEW_STATE_CONFLICT) == 1
    final = store.read(SOURCE_IDENTITY)
    assert final.latest.revision == 3
    assert len(tuple(store.events_path_for(SOURCE_IDENTITY).glob("00000003-*.json"))) == 1


def test_event_serialization_failure_is_private_and_keeps_previous_chain(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store, before = initialized(tmp_path)
    snapshot = _event_snapshot(store)
    sensitive = r"C:\private\event credential=raw"

    def serialization_failure(value):
        raise RuntimeError(sensitive)

    monkeypatch.setattr(rs.trust, "canonical_payload", serialization_failure)
    with pytest.raises(rs.ReviewStateError) as captured:
        store.prepare_transition(
            source_identity=SOURCE_IDENTITY,
            draft_identity=DRAFT_IDENTITY,
            new_state=rs.STATE_REJECTED,
            operator_request_id="serialization-fault",
            reason_codes=("operator-rejected",),
            timestamp=T2,
        )
    error = captured.value
    assert error.__cause__ is None and error.__context__ is None
    rendered = "".join(traceback.format_exception(error)).casefold()
    assert "credential=raw" not in rendered and r"c:\private\event" not in rendered
    assert _event_snapshot(store) == snapshot
    monkeypatch.undo()
    assert store.read(SOURCE_IDENTITY) == before


@pytest.mark.parametrize(
    "fault",
    ("write", "file-fsync", "validation", "promotion"),
    ids=("event-write", "event-file-fsync", "staging-validation", "no-clobber-promotion"),
)
def test_event_pre_promotion_fault_keeps_previous_chain_and_cleans_staging(
    tmp_path: Path,
    monkeypatch,
    fault: str,
) -> None:
    store, before = initialized(tmp_path)
    event = _prepared_reject(store, request_id=f"pre-{fault}")
    snapshot = _event_snapshot(store)
    if fault == "write":
        monkeypatch.setattr(
            rs,
            "_write_file",
            lambda *args: (_ for _ in ()).throw(OSError(r"C:\private\write")),
        )
    elif fault == "file-fsync":
        monkeypatch.setattr(
            rs.os,
            "fsync",
            lambda *args: (_ for _ in ()).throw(OSError(r"C:\private\fsync")),
        )
    elif fault == "validation":
        monkeypatch.setattr(
            store,
            "_read_event",
            lambda *args: (_ for _ in ()).throw(ValueError(r"C:\private\validate")),
        )
    else:
        monkeypatch.setattr(
            rs.os,
            "link",
            lambda *args: (_ for _ in ()).throw(OSError(r"C:\private\link")),
        )
    with pytest.raises(rs.ReviewStateError) as captured:
        store.commit_prepared(event)
    error = captured.value
    assert error.__cause__ is None and error.__context__ is None
    assert "private" not in "".join(traceback.format_exception(error)).casefold()
    assert _event_snapshot(store) == snapshot
    assert not tuple(store.events_path_for(SOURCE_IDENTITY).glob(".event-staging-*"))
    monkeypatch.undo()
    assert store.read(SOURCE_IDENTITY) == before


@pytest.mark.parametrize(
    "fault",
    ("directory-fsync", "link-then-raise", "cleanup-raises", "cleanup-noop"),
    ids=("post-link-directory-fsync", "link-mutates-then-raises", "cleanup-raises", "cleanup-noop"),
)
def test_event_post_promotion_fault_never_hides_durable_event(
    tmp_path: Path,
    monkeypatch,
    fault: str,
) -> None:
    store, _ = initialized(tmp_path)
    event = _prepared_reject(store, request_id=f"post-{fault}")
    original_link = rs.os.link
    if fault == "directory-fsync":
        monkeypatch.setattr(
            rs,
            "_fsync_directory",
            lambda *args: (_ for _ in ()).throw(OSError(r"C:\private\dir-fsync")),
        )
    elif fault == "link-then-raise":
        def link_then_raise(source, target):
            original_link(source, target)
            raise OSError(r"C:\private\link-after-mutation")
        monkeypatch.setattr(rs.os, "link", link_then_raise)
    elif fault == "cleanup-raises":
        monkeypatch.setattr(
            rs.os,
            "unlink",
            lambda *args: (_ for _ in ()).throw(OSError(r"C:\private\cleanup")),
        )
    else:
        monkeypatch.setattr(rs.os, "unlink", lambda *args: None)
    with pytest.raises(rs.ReviewStateError) as captured:
        store.commit_prepared(event)
    error = captured.value
    assert error.__cause__ is None and error.__context__ is None
    assert "private" not in "".join(traceback.format_exception(error)).casefold()
    assert store.read(SOURCE_IDENTITY).latest == event
    assert len(tuple(store.events_path_for(SOURCE_IDENTITY).glob("00000003-*.json"))) == 1
    monkeypatch.undo()
    ledger, idempotent = store.transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_REJECTED,
        operator_request_id=f"post-{fault}",
        reason_codes=("operator-rejected",),
        timestamp="2026-08-17T01:00:00Z",
    )
    assert idempotent is True
    assert ledger.latest == event


@pytest.mark.parametrize(
    ("stage", "cancellation"),
    (
        ("before-promotion", KeyboardInterrupt),
        ("before-promotion", SystemExit),
        ("before-promotion", GeneratorExit),
        ("after-promotion", KeyboardInterrupt),
        ("after-promotion", SystemExit),
        ("after-promotion", GeneratorExit),
    ),
    ids=(
        "before-keyboard-interrupt", "before-system-exit", "before-generator-exit",
        "after-keyboard-interrupt", "after-system-exit", "after-generator-exit",
    ),
)
def test_event_cancellation_is_never_normalized(
    tmp_path: Path,
    monkeypatch,
    stage: str,
    cancellation: type[BaseException],
) -> None:
    store, before = initialized(tmp_path)
    event = _prepared_reject(store, request_id=f"cancel-{stage}-{cancellation.__name__}")
    if stage == "before-promotion":
        monkeypatch.setattr(
            rs,
            "_write_file",
            lambda *args: (_ for _ in ()).throw(cancellation("cancel-event")),
        )
    else:
        monkeypatch.setattr(
            rs,
            "_fsync_directory",
            lambda *args: (_ for _ in ()).throw(cancellation("cancel-event")),
        )
    with pytest.raises(cancellation, match="cancel-event"):
        store.commit_prepared(event)
    monkeypatch.undo()
    latest = store.read(SOURCE_IDENTITY)
    assert latest == before if stage == "before-promotion" else latest.latest == event


def test_interrupted_staging_is_never_current_but_promoted_event_is(
    tmp_path: Path,
) -> None:
    store, before = initialized(tmp_path)
    event = _prepared_reject(store, request_id="process-death-boundary")
    encoded = trust.canonical_payload(event.to_payload()) + b"\n"
    events = store.events_path_for(SOURCE_IDENTITY)
    staging = events / ".event-staging-deadbeefdeadbeef"
    rs._write_file(staging, encoded)
    assert store.read(SOURCE_IDENTITY) == before
    os.link(staging, store.event_path_for(event))
    assert store.read(SOURCE_IDENTITY).latest == event


@pytest.mark.parametrize(
    "mutation",
    [
        "event_digest", "previous_event_digest", "revision", "source_identity",
        "draft_identity", "operator_request_id", "state", "extra", "missing",
    ],
)
def test_append_only_event_tamper_fails_closed(tmp_path: Path, mutation: str) -> None:
    store, ledger = initialized(tmp_path)
    path = store.event_path_for(ledger.events[1])
    value = json.loads(path.read_text(encoding="utf-8"))
    if mutation in {"event_digest", "previous_event_digest"}:
        value[mutation] = "f" * 64
    elif mutation == "revision":
        value[mutation] = 7
    elif mutation in {"source_identity", "draft_identity"}:
        value[mutation] = "f" * 64
    elif mutation == "operator_request_id":
        value[mutation] = "changed-request"
    elif mutation == "state":
        value[mutation] = rs.STATE_APPROVED
    elif mutation == "extra":
        value["extra"] = True
    else:
        value.pop("timestamp")
    path.write_bytes(trust.canonical_payload(value) + b"\n")
    with pytest.raises(rs.ReviewStateError, match=rs.REVIEW_STATE_INVALID):
        store.read(SOURCE_IDENTITY)


@pytest.mark.parametrize(
    "mutation",
    ["latest_revision", "latest_state", "latest_event_digest", "removed", "legacy-ledger"],
)
def test_mutable_head_or_legacy_ledger_is_never_authoritative(
    tmp_path: Path,
    mutation: str,
) -> None:
    store, ledger = initialized(tmp_path)
    head = store.path_for(SOURCE_IDENTITY)
    if mutation == "removed":
        head.unlink()
    elif mutation == "legacy-ledger":
        legacy = store.root / "review-ledger" / f"{SOURCE_IDENTITY}.json"
        legacy.parent.mkdir()
        legacy.write_bytes(head.read_bytes())
        head.write_text("{}", encoding="utf-8")
    else:
        value = json.loads(head.read_text(encoding="utf-8"))
        if mutation == "latest_revision":
            value[mutation] += 1
        elif mutation == "latest_state":
            value[mutation] = rs.STATE_REJECTED
        else:
            value[mutation] = "f" * 64
        head.write_text(json.dumps(value), encoding="utf-8")
    assert store.read(SOURCE_IDENTITY) == ledger


def test_event_layout_uses_immutable_revision_digest_names(tmp_path: Path) -> None:
    store, ledger = initialized(tmp_path)
    assert [item.name for item in sorted(store.events_path_for(SOURCE_IDENTITY).iterdir())] == [
        f"00000001-{ledger.events[0].event_digest}.json",
        f"00000002-{ledger.events[1].event_digest}.json",
    ]
    assert not any(name.startswith(("update", "delete", "truncate")) for name in dir(store))


@pytest.mark.parametrize("case", ["gap", "duplicate-revision", "missing-middle", "copied-source"])
def test_authority_scan_rejects_gap_fork_missing_and_cross_source_events(
    tmp_path: Path,
    case: str,
) -> None:
    store, ledger = initialized(tmp_path)
    store.transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_REJECTED,
        operator_request_id="reject-for-structure",
        reason_codes=("operator-rejected",),
        timestamp=T2,
    )
    events = store.events_path_for(SOURCE_IDENTITY)
    second = store.event_path_for(ledger.events[1])
    third = max(events.glob("00000003-*.json"))
    if case == "gap":
        second.rename(events / second.name.replace("00000002-", "00000004-"))
    elif case == "missing-middle":
        second.unlink()
    elif case == "duplicate-revision":
        fork = rs.build_review_event(
            service(),
            revision=2,
            previous_revision=1,
            source_identity=SOURCE_IDENTITY,
            draft_identity=DRAFT_IDENTITY,
            state=rs.STATE_REJECTED,
            operator_request_id="fork-at-two",
            reason_codes=("operator-rejected",),
            timestamp=T1,
            previous_event_digest=ledger.events[0].event_digest,
        )
        (events / f"00000002-{fork.event_digest}.json").write_bytes(
            trust.canonical_payload(fork.to_payload()) + b"\n"
        )
    else:
        other_identity = "a" * 64
        copied = rs.build_review_event(
            service(),
            revision=3,
            previous_revision=2,
            source_identity=other_identity,
            draft_identity=DRAFT_IDENTITY,
            state=rs.STATE_REJECTED,
            operator_request_id="copied-source",
            reason_codes=("operator-rejected",),
            timestamp=T2,
            previous_event_digest=ledger.events[1].event_digest,
        )
        third.unlink()
        (events / f"00000003-{copied.event_digest}.json").write_bytes(
            trust.canonical_payload(copied.to_payload()) + b"\n"
        )
    with pytest.raises(rs.ReviewStateError, match=rs.REVIEW_STATE_INVALID):
        store.read(SOURCE_IDENTITY)


def test_stale_head_cache_restoration_cannot_hide_new_terminal_event(tmp_path: Path) -> None:
    store, before = initialized(tmp_path)
    old_head = store.path_for(SOURCE_IDENTITY).read_bytes()
    store.transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_REJECTED,
        operator_request_id="reject-before-head-replay",
        reason_codes=("operator-rejected",),
        timestamp=T2,
    )
    store.path_for(SOURCE_IDENTITY).write_bytes(old_head)
    latest = store.read(SOURCE_IDENTITY)
    assert before.latest.revision == 2
    assert latest.latest.revision == 3
    assert latest.latest.state == rs.STATE_REJECTED


def test_same_privilege_newest_event_deletion_is_explicitly_outside_local_detection(
    tmp_path: Path,
) -> None:
    store, passed = initialized(tmp_path)
    rejected, _ = store.transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_REJECTED,
        operator_request_id="same-privilege-deletion-probe",
        reason_codes=("operator-rejected",),
        timestamp=T2,
    )
    store.event_path_for(rejected.latest).unlink()
    # This is not claimed as protected: an external permission/high-water
    # boundary must prevent deletion of the newest authoritative object.
    assert store.read(SOURCE_IDENTITY) == passed


def test_approved_is_terminal(tmp_path: Path) -> None:
    store, _ = initialized(tmp_path)
    store.transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_APPROVED,
        operator_request_id="approve-terminal",
        reason_codes=(),
        timestamp=T2,
    )
    with pytest.raises(rs.ReviewStateError, match=rs.REVIEW_STATE_TRANSITION_INVALID):
        store.prepare_transition(
            source_identity=SOURCE_IDENTITY,
            draft_identity=DRAFT_IDENTITY,
            new_state=rs.STATE_REJECTED,
            operator_request_id="reject-after-approval",
            reason_codes=("operator-rejected",),
            timestamp="2026-08-17T00:00:03Z",
        )


def test_review_ledger_wrong_key_fails_closed(tmp_path: Path) -> None:
    store, _ = initialized(tmp_path)
    wrong = rs.ReviewStateStore(tmp_path / "state", trust.NarrativeTrustService(b"x" * 32))
    with pytest.raises(rs.ReviewStateError, match=rs.REVIEW_STATE_INVALID):
        wrong.read(SOURCE_IDENTITY)


def approved_event(tmp_path: Path) -> tuple[trust.NarrativeTrustService, rs.ReviewEvent]:
    signer = service()
    store = rs.ReviewStateStore(tmp_path / "state", signer)
    store.initialize(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        initial_state=rs.STATE_PASSED,
        reason_codes=(),
        drafted_at=T0,
        reviewed_at=T1,
    )
    event, _ = store.prepare_transition(
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        new_state=rs.STATE_APPROVED,
        operator_request_id="approval-001",
        reason_codes=(),
        timestamp=T2,
    )
    return signer, event


def test_approval_attestation_roundtrip_binds_approved_event(tmp_path: Path) -> None:
    signer, event = approved_event(tmp_path)
    value = rs.build_approval_attestation(
        signer,
        source_identity=SOURCE_IDENTITY,
        source_ref="Project/2026-08-17",
        source_digest=SOURCE_DIGEST,
        draft_identity=DRAFT_IDENTITY,
        package_digest=PACKAGE_DIGEST,
        ready_manifest_digest=READY_DIGEST,
        story_markdown_digest="6" * 64,
        draft_manifest_digest="7" * 64,
        review_digest="8" * 64,
        completed_claim_digest="9" * 64,
        artifact_binding_digest="a" * 64,
        approved_event=event,
        ready_manifest_contract="naz-narrative-ready-v1",
        source_contract="agent-content-source-v1",
    )
    parsed = rs.approval_attestation_from_payload(
        value.to_payload(), signer,
        ready_manifest_contract="naz-narrative-ready-v1",
        source_contract="agent-content-source-v1",
    )
    assert parsed == value
    assert parsed.review_revision == 3
    assert parsed.review_event_digest == event.event_digest


@pytest.mark.parametrize(
    "field",
    [
        "source_identity", "source_ref", "source_digest", "draft_identity",
        "package_digest", "narrative_ready_manifest_digest", "review_revision",
        "story_markdown_digest", "draft_manifest_digest", "review_digest",
        "completed_claim_digest", "artifact_binding_digest",
        "review_event_digest", "approval_request_id", "contract_versions",
        "key_id", "trust_receipt", "extra", "missing",
    ],
)
def test_approval_attestation_tamper_fails_closed(tmp_path: Path, field: str) -> None:
    signer, event = approved_event(tmp_path)
    payload = rs.build_approval_attestation(
        signer,
        source_identity=SOURCE_IDENTITY,
        source_ref="Project/2026-08-17",
        source_digest=SOURCE_DIGEST,
        draft_identity=DRAFT_IDENTITY,
        package_digest=PACKAGE_DIGEST,
        ready_manifest_digest=READY_DIGEST,
        story_markdown_digest="6" * 64,
        draft_manifest_digest="7" * 64,
        review_digest="8" * 64,
        completed_claim_digest="9" * 64,
        artifact_binding_digest="a" * 64,
        approved_event=event,
        ready_manifest_contract="naz-narrative-ready-v1",
        source_contract="agent-content-source-v1",
    ).to_payload()
    if field == "source_ref":
        payload[field] = "Other/2026-08-17"
    elif field == "review_revision":
        payload[field] += 1
    elif field == "approval_request_id":
        payload[field] = "approval-other"
    elif field == "contract_versions":
        payload[field]["source"] = "stale"
    elif field == "key_id":
        payload[field] = "f" * 24
    elif field == "trust_receipt":
        payload[field]["seal"] = "f" * 64
    elif field == "extra":
        payload[field] = True
    elif field == "missing":
        payload.pop("package_digest")
    else:
        payload[field] = "f" * 64
    with pytest.raises(rs.ReviewStateError, match=rs.APPROVAL_ATTESTATION_INVALID):
        rs.approval_attestation_from_payload(
            payload, signer,
            ready_manifest_contract="naz-narrative-ready-v1",
            source_contract="agent-content-source-v1",
        )


@pytest.mark.parametrize("field", ("source_ref", "approval_request_id", "review_revision"))
def test_approval_attestation_rejects_scalar_subclasses(tmp_path: Path, field: str) -> None:
    signer, event = approved_event(tmp_path)
    payload = rs.build_approval_attestation(
        signer,
        source_identity=SOURCE_IDENTITY,
        source_ref="Project/2026-08-17",
        source_digest=SOURCE_DIGEST,
        draft_identity=DRAFT_IDENTITY,
        package_digest=PACKAGE_DIGEST,
        ready_manifest_digest=READY_DIGEST,
        story_markdown_digest="6" * 64,
        draft_manifest_digest="7" * 64,
        review_digest="8" * 64,
        completed_claim_digest="9" * 64,
        artifact_binding_digest="a" * 64,
        approved_event=event,
        ready_manifest_contract="naz-narrative-ready-v1",
        source_contract="agent-content-source-v1",
    ).to_payload()
    payload[field] = (
        IntegerSubclass(payload[field])
        if field == "review_revision"
        else StringSubclass(payload[field])
    )
    with pytest.raises(rs.ReviewStateError, match=rs.APPROVAL_ATTESTATION_INVALID):
        rs.approval_attestation_from_payload(
            payload, signer,
            ready_manifest_contract="naz-narrative-ready-v1",
            source_contract="agent-content-source-v1",
        )


def dual_digest_approved_event() -> tuple[trust.NarrativeTrustService, rs.ReviewEvent]:
    signer = service()
    event = rs.build_review_event(
        signer,
        revision=3,
        previous_revision=2,
        source_identity=SOURCE_IDENTITY,
        draft_identity=DRAFT_IDENTITY,
        draft_package_digest=PACKAGE_DIGEST,
        state=rs.STATE_APPROVED,
        operator_request_id="approval-v2-001",
        reason_codes=(),
        action_digest="b" * 64,
        timestamp=T2,
        previous_event_digest="c" * 64,
    )
    return signer, event


def test_dual_digest_attestation_v2_roundtrip_has_no_ambiguous_field() -> None:
    signer, event = dual_digest_approved_event()
    value = rs.build_dual_digest_approval_attestation(
        signer,
        source_identity=SOURCE_IDENTITY,
        source_ref="Project/2026-08-17",
        source_digest=SOURCE_DIGEST,
        draft_identity=DRAFT_IDENTITY,
        draft_package_digest=PACKAGE_DIGEST,
        narrative_package_digest="d" * 64,
        ready_manifest_digest=READY_DIGEST,
        story_markdown_digest="6" * 64,
        draft_manifest_digest="7" * 64,
        review_digest="8" * 64,
        completed_claim_digest="9" * 64,
        artifact_binding_digest="a" * 64,
        approved_event=event,
        ready_manifest_contract="naz-narrative-ready-v1",
        source_contract="agent-content-source-v1",
        draft_contract="normalizer-draft-identity-v1",
    )
    payload = value.to_payload()
    assert payload["schema_version"] == rs.DUAL_DIGEST_APPROVAL_ATTESTATION_SCHEMA_VERSION
    assert payload["draft_package_digest"] == PACKAGE_DIGEST
    assert payload["narrative_package_digest"] == "d" * 64
    assert "package_digest" not in payload
    parsed = rs.dual_digest_approval_attestation_from_payload(
        payload,
        signer,
        ready_manifest_contract="naz-narrative-ready-v1",
        source_contract="agent-content-source-v1",
        draft_contract="normalizer-draft-identity-v1",
    )
    assert parsed == value


@pytest.mark.parametrize(
    "field",
    ["draft_package_digest", "narrative_package_digest", "schema_version", "extra"],
)
def test_dual_digest_attestation_rejects_missing_old_or_extra_contract_fields(field: str) -> None:
    signer, event = dual_digest_approved_event()
    payload = rs.build_dual_digest_approval_attestation(
        signer,
        source_identity=SOURCE_IDENTITY,
        source_ref="Project/2026-08-17",
        source_digest=SOURCE_DIGEST,
        draft_identity=DRAFT_IDENTITY,
        draft_package_digest=PACKAGE_DIGEST,
        narrative_package_digest="d" * 64,
        ready_manifest_digest=READY_DIGEST,
        story_markdown_digest="6" * 64,
        draft_manifest_digest="7" * 64,
        review_digest="8" * 64,
        completed_claim_digest="9" * 64,
        artifact_binding_digest="a" * 64,
        approved_event=event,
        ready_manifest_contract="naz-narrative-ready-v1",
        source_contract="agent-content-source-v1",
        draft_contract="normalizer-draft-identity-v1",
    ).to_payload()
    if field == "extra":
        payload["package_digest"] = PACKAGE_DIGEST
    else:
        payload.pop(field)
    with pytest.raises(rs.ReviewStateError, match=rs.APPROVAL_ATTESTATION_INVALID):
        rs.dual_digest_approval_attestation_from_payload(
            payload,
            signer,
            ready_manifest_contract="naz-narrative-ready-v1",
            source_contract="agent-content-source-v1",
            draft_contract="normalizer-draft-identity-v1",
        )


@pytest.mark.parametrize(
    "field",
    ["schema_version", "draft_package_digest", "narrative_package_digest"],
)
def test_dual_digest_attestation_rejects_string_subclasses(field: str) -> None:
    signer, event = dual_digest_approved_event()
    payload = rs.build_dual_digest_approval_attestation(
        signer,
        source_identity=SOURCE_IDENTITY,
        source_ref="Project/2026-08-17",
        source_digest=SOURCE_DIGEST,
        draft_identity=DRAFT_IDENTITY,
        draft_package_digest=PACKAGE_DIGEST,
        narrative_package_digest="d" * 64,
        ready_manifest_digest=READY_DIGEST,
        story_markdown_digest="6" * 64,
        draft_manifest_digest="7" * 64,
        review_digest="8" * 64,
        completed_claim_digest="9" * 64,
        artifact_binding_digest="a" * 64,
        approved_event=event,
        ready_manifest_contract="naz-narrative-ready-v1",
        source_contract="agent-content-source-v1",
        draft_contract="normalizer-draft-identity-v1",
    ).to_payload()
    payload[field] = StringSubclass(payload[field])
    with pytest.raises(rs.ReviewStateError, match=rs.APPROVAL_ATTESTATION_INVALID):
        rs.dual_digest_approval_attestation_from_payload(
            payload,
            signer,
            ready_manifest_contract="naz-narrative-ready-v1",
            source_contract="agent-content-source-v1",
            draft_contract="normalizer-draft-identity-v1",
        )
