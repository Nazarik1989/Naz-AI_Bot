from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import os
import socket
import stat
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import reels_failure_quarantine as rq


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=10)


@pytest.fixture(autouse=True)
def network_tripwire(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> object:
        raise AssertionError("network access is forbidden in quarantine tests")

    original_connect = socket.socket.connect

    def local_only(sock: socket.socket, address: object) -> object:
        if (
            type(address) is tuple
            and address
            and address[0] in {"127.0.0.1", "::1", "localhost"}
        ):
            return original_connect(sock, address)
        return blocked(sock, address)

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", local_only)


def make_policy(root: Path) -> rq.QuarantinePathPolicy:
    inbox = root / "inbox"
    outbox = root / "narrative-outbox"
    inbox.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    return rq.QuarantinePathPolicy(inbox, root / "state" / "registry.json", outbox)


def write_raw(policy: rq.QuarantinePathPolicy, source_ref: str, content: str = "raw") -> Path:
    source = policy.inbox_root.joinpath(*source_ref.split("/"))
    source.mkdir(parents=True, exist_ok=True)
    (source / "material.md").write_text(content, encoding="utf-8")
    return source


def ready_payload(
    policy: rq.QuarantinePathPolicy,
    source_ref: str = "Naz/2026-08-09",
    *,
    source_content: str = "normalized source",
    package_content: str = "narrative package",
    director_version: str = "director-v1",
    narrative_version: str = "narrative-v1",
) -> tuple[Path, Path, dict[str, object]]:
    source = write_raw(policy, source_ref, source_content)
    package_ref = f"packages/{source_ref.replace('/', '-')}.json"
    package = policy.narrative_outbox_root.joinpath(*package_ref.split("/"))
    package.parent.mkdir(parents=True, exist_ok=True)
    package.write_text(package_content, encoding="utf-8")
    payload: dict[str, object] = {
        "schema_version": rq.MANIFEST_SCHEMA_VERSION,
        "source_ref": source_ref,
        "source_digest": rq.source_digest(source),
        "narrative_package_ref": package_ref,
        "narrative_package_digest": rq.narrative_package_digest(package),
        "status": rq.CLASS_READY,
        "contract_versions": {
            "director": director_version,
            "narrative": narrative_version,
        },
    }
    (source / "narrative_ready.json").write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
    )
    return source, package, payload


def reconcile_ready(root: Path) -> tuple[rq.QuarantinePathPolicy, rq.EligibleCandidate]:
    policy = make_policy(root)
    ready_payload(policy)
    rq.reconcile_complete_backlog(policy, now=NOW)
    candidate = rq.select_ready_candidate(policy, project_name="Naz")
    assert candidate is not None
    return policy, candidate


def story_first_source_row(source_ref: str) -> dict[str, object]:
    return {
        "source_ref": source_ref,
        "topic": "A bounded story-first source",
        "source_type": "work_chronicle",
        "rubric_keys": (),
        "safe_facts": (
            "First the builder tested one bounded local input.",
            "Then the builder changed one visible configuration.",
            "After that change the same local test completed.",
            "The verified result worked on the repeated check.",
        ),
        "source_verified": True,
        "concrete_action": True,
        "visualizable_process": True,
        "causal_bits": 4,
        "real_result": True,
        "contains_secrets": False,
        "contains_private_data": False,
    }


def blocked_ready(root: Path) -> tuple[rq.QuarantinePathPolicy, rq.AttemptClaim, str]:
    policy, candidate = reconcile_ready(root)
    claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert claim is not None
    notification_id = rq.mark_attempt_blocked(
        policy, claim, ("director_contract_invalid",), now=NOW
    )
    return policy, claim, notification_id


def raw_metadata(source: Path) -> dict[str, object]:
    rows: list[tuple[str, int, int, int, int, str]] = []
    for item in sorted(source.rglob("*"), key=lambda row: row.relative_to(source).as_posix()):
        info = item.lstat()
        rows.append(
            (
                item.relative_to(source).as_posix(),
                info.st_size,
                info.st_mtime_ns,
                stat.S_IMODE(info.st_mode),
                info.st_mode,
                hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else "",
            )
        )
    root_info = source.lstat()
    return {
        "root": str(source.resolve()),
        "parent": str(source.parent.resolve()),
        "name": source.name,
        "mtime_ns": root_info.st_mtime_ns,
        "mode": root_info.st_mode,
        "rows": rows,
        "listing": tuple(item.name for item in sorted(source.iterdir())),
    }


def process_reconcile(inbox: str, registry: str, outbox: str, queue: object) -> None:
    try:
        policy = rq.QuarantinePathPolicy(Path(inbox), Path(registry), Path(outbox))
        result = rq.reconcile_complete_backlog(policy, now=NOW)
        claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
        queue.put(("ok", result.discovered_count, claim is not None))
    except BaseException as exc:  # pragma: no cover - asserted in parent
        queue.put(("error", type(exc).__name__, str(exc)))


def process_ready_claim(inbox: str, registry: str, outbox: str, queue: object) -> None:
    try:
        policy = rq.QuarantinePathPolicy(Path(inbox), Path(registry), Path(outbox))
        candidate = rq.select_ready_candidate(policy, project_name="Naz")
        claim = None if candidate is None else rq.claim_ready_candidate(policy, candidate, now=NOW)
        queue.put(("ok", claim is not None))
    except BaseException as exc:  # pragma: no cover - asserted in parent
        queue.put(("error", type(exc).__name__, str(exc)))


def process_add_and_reconcile(
    inbox: str, registry: str, outbox: str, source_ref: str, queue: object
) -> None:
    try:
        policy = rq.QuarantinePathPolicy(Path(inbox), Path(registry), Path(outbox))
        write_raw(policy, source_ref, source_ref)
        result = rq.reconcile_complete_backlog(policy, now=NOW)
        queue.put(("ok", result.discovered_count))
    except BaseException as exc:  # pragma: no cover - asserted in parent
        queue.put(("error", type(exc).__name__, str(exc)))


def process_die_holding_lock(registry_root: str) -> None:
    with rq._InterProcessLock(Path(registry_root)):
        os._exit(0)


def run_processes(target: object, policy: rq.QuarantinePathPolicy, count: int) -> list[tuple[object, ...]]:
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    args = (str(policy.inbox_root), str(policy.registry_path), str(policy.narrative_outbox_root), queue)
    processes = [context.Process(target=target, args=args) for _ in range(count)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    rows: list[tuple[object, ...]] = []
    for _ in processes:
        try:
            rows.append(queue.get(timeout=5))
        except Empty:  # pragma: no cover - process assertion aid
            pytest.fail("child process returned no result")
    queue.close()
    return rows


# ---------------------------------------------------------------------------
# Global backlog, aggregate identity, and raw immutability


def test_five_old_dates_first_run_registers_all_five(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    for day in range(1, 6):
        write_raw(policy, f"Naz/2026-07-0{day}", f"raw-{day}")
    result = rq.reconcile_complete_backlog(policy, now=NOW)
    registry = rq.read_registry(policy.registry_path)
    assert (result.discovered_count, result.raw_count, result.newly_registered_count) == (5, 5, 5)
    assert len(registry.records) == 5
    assert {row.status for row in registry.records} == {rq.STATUS_NEEDS_NARRATIVE}


def test_five_old_dates_create_one_aggregate_max(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    for day in range(1, 6):
        write_raw(policy, f"2026-07-0{day}", f"raw-{day}")
    rq.reconcile_complete_backlog(policy, now=NOW)
    registry = rq.read_registry(policy.registry_path)
    assert len(registry.notifications) == 1
    assert registry.notifications[0].kind == rq.RAW_AGGREGATE_KIND


@pytest.mark.parametrize("run_number", [2, 3], ids=["second-run-silent", "third-run-silent"])
def test_unchanged_backlog_runs_are_silent(tmp_path: Path, run_number: int) -> None:
    policy = make_policy(tmp_path)
    for day in range(1, 6):
        write_raw(policy, f"Naz/2026-07-0{day}", f"raw-{day}")
    first = rq.reconcile_complete_backlog(policy, now=NOW)
    first_claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert first_claim is not None
    rq.finalize_notification(policy, first_claim.notification_id, rq.NOTIFICATION_SENT, now=NOW)
    result = first
    for offset in range(1, run_number):
        result = rq.reconcile_complete_backlog(policy, now=NOW + timedelta(minutes=offset))
    assert result.newly_registered_count == 0
    assert result.changed_count == 0
    assert rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW) is None
    assert len(rq.read_registry(policy.registry_path).notifications) == 1


def test_shuffled_discovery_order_same_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first_policy = make_policy(tmp_path / "first")
    second_policy = make_policy(tmp_path / "second")
    refs = ["Naz/2026-07-03", "Naz/2026-07-01", "Naz/2026-07-02"]
    for ref in refs:
        write_raw(first_policy, ref, ref)
    for ref in reversed(refs):
        write_raw(second_policy, ref, ref)
    first = rq.reconcile_complete_backlog(first_policy, now=NOW)
    second = rq.reconcile_complete_backlog(second_policy, now=LATER)
    assert first.aggregate_fingerprint == second.aggregate_fingerprint


def test_next_scheduler_date_does_not_change_aggregate(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "Naz/2026-08-01")
    first = rq.reconcile_complete_backlog(policy, now=NOW)
    second = rq.reconcile_complete_backlog(policy, now=LATER)
    assert first.aggregate_fingerprint == second.aggregate_fingerprint
    assert second.newly_registered_count == 0


@pytest.mark.parametrize("change", ["new-sixth-source", "changed-existing-source"])
def test_backlog_identity_change_creates_one_new_aggregate(tmp_path: Path, change: str) -> None:
    policy = make_policy(tmp_path)
    for day in range(1, 6):
        write_raw(policy, f"Naz/2026-07-0{day}", f"raw-{day}")
    first = rq.reconcile_complete_backlog(policy, now=NOW)
    claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert claim is not None
    rq.finalize_notification(policy, claim.notification_id, rq.NOTIFICATION_SENT, now=NOW)
    if change == "new-sixth-source":
        write_raw(policy, "Naz/2026-07-06", "raw-6")
    else:
        (policy.inbox_root / "Naz" / "2026-07-03" / "material.md").write_text("changed", encoding="utf-8")
    second = rq.reconcile_complete_backlog(policy, now=LATER)
    assert second.aggregate_fingerprint != first.aggregate_fingerprint
    assert len(rq.read_registry(policy.registry_path).notifications) == 2
    assert rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=LATER) is not None


RAW_IMMUTABILITY_IDS = [
    "content", "size", "mtime", "filename", "parent", "listing", "file-type",
    "permissions", "no-seen-sidecar", "source-directory",
]


@pytest.mark.parametrize("property_name", RAW_IMMUTABILITY_IDS)
def test_raw_backlog_metadata_is_immutable(tmp_path: Path, property_name: str) -> None:
    policy = make_policy(tmp_path)
    source = write_raw(policy, "Naz/2026-07-01", "immutable raw")
    before = raw_metadata(source)
    rq.reconcile_complete_backlog(policy, now=NOW)
    after = raw_metadata(source)
    assert after == before, property_name
    assert not any("seen" in item.name.casefold() for item in source.iterdir())
    assert not policy.registry_path.is_relative_to(policy.inbox_root)


def test_digest_ignores_mtime_but_binds_names_types_and_content(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    source = write_raw(policy, "2026-08-01", "alpha")
    first = rq.source_digest(source)
    os.utime(source / "material.md", (1_700_000_000, 1_700_000_000))
    assert rq.source_digest(source) == first
    (source / "material.md").write_text("beta", encoding="utf-8")
    assert rq.source_digest(source) != first


def test_duplicate_source_digest_distinct_refs_are_allowed(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "Naz/2026-08-01", "same")
    write_raw(policy, "Naz/2026-08-02", "same")
    rq.reconcile_complete_backlog(policy, now=NOW)
    rows = rq.read_registry(policy.registry_path).records
    assert len(rows) == 2
    assert len({row.source_ref for row in rows}) == 2


# ---------------------------------------------------------------------------
# Strict manifest, exact scalar policy, refs, and binding


MANIFEST_CASES = [
    "bogus-schema", "schema-case", "schema-whitespace", "bool-schema", "int-schema",
    "missing-schema", "extra-key", "missing-status", "status-case", "status-whitespace",
    "bool-status", "missing-source-ref", "empty-source-ref", "missing-source-digest",
    "short-source-digest", "uppercase-source-digest", "int-source-digest",
    "missing-package-ref", "empty-package-ref", "missing-package-digest",
    "short-package-digest", "uppercase-package-digest", "int-package-digest",
    "missing-contracts", "empty-contracts", "extra-contract", "missing-director-version",
    "missing-narrative-version", "empty-director-version", "int-narrative-version",
]


def mutate_manifest(payload: dict[str, object], case: str) -> None:
    if case == "bogus-schema": payload["schema_version"] = "narrative-ready.v1"
    elif case == "schema-case": payload["schema_version"] = rq.MANIFEST_SCHEMA_VERSION.upper()
    elif case == "schema-whitespace": payload["schema_version"] = f" {rq.MANIFEST_SCHEMA_VERSION}"
    elif case == "bool-schema": payload["schema_version"] = True
    elif case == "int-schema": payload["schema_version"] = 1
    elif case == "missing-schema": payload.pop("schema_version")
    elif case == "extra-key": payload["unexpected"] = "value"
    elif case == "missing-status": payload.pop("status")
    elif case == "status-case": payload["status"] = rq.CLASS_READY.upper()
    elif case == "status-whitespace": payload["status"] = f"{rq.CLASS_READY} "
    elif case == "bool-status": payload["status"] = False
    elif case == "missing-source-ref": payload.pop("source_ref")
    elif case == "empty-source-ref": payload["source_ref"] = ""
    elif case == "missing-source-digest": payload.pop("source_digest")
    elif case == "short-source-digest": payload["source_digest"] = "a" * 63
    elif case == "uppercase-source-digest": payload["source_digest"] = "A" * 64
    elif case == "int-source-digest": payload["source_digest"] = 7
    elif case == "missing-package-ref": payload.pop("narrative_package_ref")
    elif case == "empty-package-ref": payload["narrative_package_ref"] = ""
    elif case == "missing-package-digest": payload.pop("narrative_package_digest")
    elif case == "short-package-digest": payload["narrative_package_digest"] = "b" * 63
    elif case == "uppercase-package-digest": payload["narrative_package_digest"] = "B" * 64
    elif case == "int-package-digest": payload["narrative_package_digest"] = 9
    elif case == "missing-contracts": payload.pop("contract_versions")
    elif case == "empty-contracts": payload["contract_versions"] = {}
    elif case == "extra-contract": payload["contract_versions"] = {"director": "v1", "narrative": "v1", "other": "v1"}
    elif case == "missing-director-version": payload["contract_versions"] = {"narrative": "v1"}
    elif case == "missing-narrative-version": payload["contract_versions"] = {"director": "v1"}
    elif case == "empty-director-version": payload["contract_versions"] = {"director": "", "narrative": "v1"}
    elif case == "int-narrative-version": payload["contract_versions"] = {"director": "v1", "narrative": 1}
    else: raise AssertionError(case)


@pytest.mark.parametrize("case", MANIFEST_CASES, ids=MANIFEST_CASES)
def test_manifest_exact_shape_and_types_fail_closed(case: str) -> None:
    payload: dict[str, object] = {
        "schema_version": rq.MANIFEST_SCHEMA_VERSION,
        "source_ref": "Naz/2026-08-09",
        "source_digest": "a" * 64,
        "narrative_package_ref": "packages/item.json",
        "narrative_package_digest": "b" * 64,
        "status": rq.CLASS_READY,
        "contract_versions": {"director": "v1", "narrative": "v1"},
    }
    mutate_manifest(payload, case)
    with pytest.raises(rq.EligibilityError):
        rq.NarrativeReadyManifest.from_mapping(payload)


class StringSubclass(str):
    pass


@pytest.mark.parametrize(
    "field",
    ["schema_version", "source_ref", "source_digest", "narrative_package_ref", "narrative_package_digest", "status"],
)
def test_manifest_rejects_str_subclasses(field: str) -> None:
    payload: dict[str, object] = {
        "schema_version": rq.MANIFEST_SCHEMA_VERSION,
        "source_ref": "Naz/2026-08-09",
        "source_digest": "a" * 64,
        "narrative_package_ref": "packages/item.json",
        "narrative_package_digest": "b" * 64,
        "status": rq.CLASS_READY,
        "contract_versions": {"director": "v1", "narrative": "v1"},
    }
    payload[field] = StringSubclass(str(payload[field]))
    with pytest.raises(rq.EligibilityError):
        rq.NarrativeReadyManifest.from_mapping(payload)


UNSAFE_REFS = [
    "C:/secret", "C:\\secret", "/absolute", "//server/share", "https://host/item",
    "file:///tmp/item", "../escape", "Naz/../escape", "Naz//2026-08-09", "./item",
    "Naz/./item", "Naz:project/item", "Naz\\item", "\x00item", "", " item",
]


@pytest.mark.parametrize("source_ref", UNSAFE_REFS, ids=[f"unsafe-ref-{index:02d}" for index in range(len(UNSAFE_REFS))])
def test_source_ref_safety_rejects_unsafe_forms(source_ref: str) -> None:
    payload: dict[str, object] = {
        "schema_version": rq.MANIFEST_SCHEMA_VERSION,
        "source_ref": source_ref,
        "source_digest": "a" * 64,
        "narrative_package_ref": "packages/item.json",
        "narrative_package_digest": "b" * 64,
        "status": rq.CLASS_READY,
        "contract_versions": {"director": "v1", "narrative": "v1"},
    }
    with pytest.raises(rq.EligibilityError):
        rq.NarrativeReadyManifest.from_mapping(payload)


@pytest.mark.parametrize(
    "binding_case",
    ["missing-source", "source-digest-mismatch", "missing-package", "package-digest-mismatch", "source-ref-mismatch"],
)
def test_manifest_actual_artifact_binding_fails_closed(tmp_path: Path, binding_case: str) -> None:
    policy = make_policy(tmp_path)
    source, package, payload = ready_payload(policy)
    if binding_case == "missing-source":
        source.rename(source.with_name("2026-08-08"))
    elif binding_case == "source-digest-mismatch":
        (source / "material.md").write_text("changed after manifest", encoding="utf-8")
    elif binding_case == "missing-package":
        package.unlink()
    elif binding_case == "package-digest-mismatch":
        package.write_text("changed package", encoding="utf-8")
    elif binding_case == "source-ref-mismatch":
        payload["source_ref"] = "Naz/2026-08-08"
        (source / "narrative_ready.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(rq.EligibilityError):
        rq.validate_narrative_ready_manifest(policy, "Naz/2026-08-09")


def test_manifest_is_frozen_and_slotted() -> None:
    manifest = rq.NarrativeReadyManifest.from_mapping({
        "schema_version": rq.MANIFEST_SCHEMA_VERSION,
        "source_ref": "Naz/2026-08-09",
        "source_digest": "a" * 64,
        "narrative_package_ref": "packages/item.json",
        "narrative_package_digest": "b" * 64,
        "status": rq.CLASS_READY,
        "contract_versions": {"director": "v1", "narrative": "v1"},
    })
    with pytest.raises(FrozenInstanceError):
        manifest.status = "raw"  # type: ignore[misc]
    assert not hasattr(manifest, "__dict__")


def test_valid_manifest_is_discovered_ready(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    ready_payload(policy)
    discovered = rq.discover_all_agent_material_sources(policy)
    assert len(discovered) == 1
    assert discovered[0].classification == rq.CLASS_READY
    assert discovered[0].source_ref == "Naz/2026-08-09"


def test_symlink_source_fails_closed(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "material.md").write_text("secret", encoding="utf-8")
    link = policy.inbox_root / "2026-08-09"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(rq.EligibilityError):
        rq.discover_all_agent_material_sources(policy)


@pytest.mark.parametrize("placement", ["inside-inbox", "inbox-under-registry", "inside-outbox", "outbox-under-registry", "inbox-outbox-overlap"])
def test_invalid_path_overlap_is_rejected(tmp_path: Path, placement: str) -> None:
    inbox = tmp_path / "inbox"
    outbox = tmp_path / "outbox"
    registry = tmp_path / "state" / "registry.json"
    if placement == "inside-inbox": registry = inbox / "state" / "registry.json"
    elif placement == "inbox-under-registry": inbox = tmp_path / "state" / "inbox"
    elif placement == "inside-outbox": registry = outbox / "state" / "registry.json"
    elif placement == "outbox-under-registry": outbox = tmp_path / "state" / "outbox"
    elif placement == "inbox-outbox-overlap": outbox = inbox / "outbox"
    with pytest.raises(rq.RegistryError, match="quarantine_registry_path_invalid"):
        rq.QuarantinePathPolicy(inbox, registry, outbox)


def test_external_env_paths_keep_runtime_state_outside_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "source-checkout"
    inbox = tmp_path / "agent-inbox"
    runtime_root = tmp_path / "external-runtime"
    checkout.mkdir()
    inbox.mkdir()
    registry = runtime_root / "quarantine" / "registry.json"
    outbox = runtime_root / "narrative-outbox"
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("REELS_QUARANTINE_REGISTRY", str(registry))
    monkeypatch.setenv("REELS_NARRATIVE_OUTBOX", str(outbox))

    policy = rq.default_path_policy(inbox)

    assert policy.registry_path == registry.resolve()
    assert policy.narrative_outbox_root == outbox.resolve()
    assert not policy.registry_path.is_relative_to(checkout.resolve())
    assert not policy.narrative_outbox_root.is_relative_to(checkout.resolve())
    assert not policy.registry_path.is_relative_to(policy.inbox_root)
    assert not policy.narrative_outbox_root.is_relative_to(policy.inbox_root)


# ---------------------------------------------------------------------------
# Strict versioned registry and privacy-safe errors


REGISTRY_CASES = [
    "unknown-schema", "missing-schema", "extra-top-key", "bool-revision", "negative-revision",
    "records-not-list", "notifications-not-list", "aggregates-not-dict", "extra-aggregate-key",
    "extra-record-key", "missing-record-key", "bool-attempt", "negative-attempt",
    "unknown-classification", "classification-case", "unknown-status", "status-whitespace",
    "arbitrary-reason", "duplicate-reason", "unsorted-reasons", "bad-source-digest",
    "bad-first-time", "naive-last-time", "bad-attempt-id", "bad-failure-fingerprint",
    "bad-eligibility-version", "notifications-wrong-type", "history-wrong-type",
    "extra-history-key", "unknown-history-event",
]


def mutate_registry(payload: dict[str, object], case: str) -> None:
    record = payload["records"][0]  # type: ignore[index]
    if case == "unknown-schema": payload["schema_version"] = "future-v2"
    elif case == "missing-schema": payload.pop("schema_version")
    elif case == "extra-top-key": payload["extra"] = 1
    elif case == "bool-revision": payload["registry_revision"] = True
    elif case == "negative-revision": payload["registry_revision"] = -1
    elif case == "records-not-list": payload["records"] = {}
    elif case == "notifications-not-list": payload["notifications"] = {}
    elif case == "aggregates-not-dict": payload["aggregates"] = []
    elif case == "extra-aggregate-key": payload["aggregates"]["extra"] = ""  # type: ignore[index]
    elif case == "extra-record-key": record["extra"] = "x"
    elif case == "missing-record-key": record.pop("status")
    elif case == "bool-attempt": record["attempt_count"] = True
    elif case == "negative-attempt": record["attempt_count"] = -1
    elif case == "unknown-classification": record["classification"] = "other"
    elif case == "classification-case": record["classification"] = rq.CLASS_RAW.upper()
    elif case == "unknown-status": record["status"] = "other"
    elif case == "status-whitespace": record["status"] = f"{rq.STATUS_NEEDS_NARRATIVE} "
    elif case == "arbitrary-reason": record["safe_reason_codes"] = ["/private/path secret"]
    elif case == "duplicate-reason": record["safe_reason_codes"] = ["reels_director_rejected", "reels_director_rejected"]
    elif case == "unsorted-reasons": record["safe_reason_codes"] = ["reels_director_rejected", "director_contract_invalid"]
    elif case == "bad-source-digest": record["source_digest"] = "x"
    elif case == "bad-first-time": record["first_observed_at"] = "yesterday"
    elif case == "naive-last-time": record["last_observed_at"] = "2026-08-09T12:00:00"
    elif case == "bad-attempt-id": record["active_attempt_id"] = "not-hex"
    elif case == "bad-failure-fingerprint": record["failure_fingerprint"] = "bad"
    elif case == "bad-eligibility-version": record["eligibility_contract_version"] = "bad version"
    elif case == "notifications-wrong-type": record["notification_ids"] = "none"
    elif case == "history-wrong-type": record["history"] = {}
    elif case == "extra-history-key": record["history"][0]["extra"] = 1
    elif case == "unknown-history-event": record["history"][0]["event"] = "invented"
    else: raise AssertionError(case)


@pytest.mark.parametrize("case", REGISTRY_CASES, ids=REGISTRY_CASES)
def test_registry_contract_rejects_invalid_payload(tmp_path: Path, case: str) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    payload = json.loads(policy.registry_path.read_text(encoding="utf-8"))
    mutate_registry(payload, case)
    policy.registry_path.write_text(json.dumps(payload), encoding="utf-8")
    before = policy.registry_path.read_bytes()
    with pytest.raises(rq.RegistryError):
        rq.read_registry(policy.registry_path)
    assert policy.registry_path.read_bytes() == before


@pytest.mark.parametrize("raw", [b"{", b"not-json", b"\xff", b"[]", b"null"])
def test_corrupt_registry_fails_closed_and_is_byte_identical(tmp_path: Path, raw: bytes) -> None:
    policy = make_policy(tmp_path)
    policy.registry_path.parent.mkdir(parents=True)
    policy.registry_path.write_bytes(raw)
    with pytest.raises(rq.RegistryError):
        rq.reconcile_complete_backlog(policy, now=NOW)
    assert policy.registry_path.read_bytes() == raw


def test_duplicate_source_ref_is_rejected(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    payload = json.loads(policy.registry_path.read_text(encoding="utf-8"))
    payload["records"].append(dict(payload["records"][0]))
    policy.registry_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(rq.RegistryError, match="quarantine_duplicate_source_ref"):
        rq.read_registry(policy.registry_path)


def test_registry_serialization_is_canonical_and_deterministic(tmp_path: Path) -> None:
    first = make_policy(tmp_path / "first")
    second = make_policy(tmp_path / "second")
    for policy in (first, second):
        write_raw(policy, "Naz/2026-08-02", "b")
        write_raw(policy, "Naz/2026-08-01", "a")
        rq.reconcile_complete_backlog(policy, now=NOW)
    assert first.registry_path.read_bytes() == second.registry_path.read_bytes()
    assert first.registry_path.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("secret", ["C:\\private\\raw.md", "/private/raw.md", "https://secret", "prompt body", "api-key-123"])
def test_error_reason_does_not_leak_context(secret: str) -> None:
    error = rq.RegistryError(secret)
    assert error.reason_code == "quarantine_error"
    assert secret not in str(error)


# ---------------------------------------------------------------------------
# Atomic write, CAS, durability, cleanup, and lock release


def prepare_atomic(root: Path) -> tuple[rq.QuarantinePathPolicy, rq._RegistrySnapshot, rq.MaterialRegistry, bytes]:
    policy = make_policy(root)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    snapshot = rq._read_registry_snapshot(policy.registry_path)
    updated = replace(snapshot.registry, registry_revision=snapshot.registry.registry_revision + 1)
    return policy, snapshot, updated, policy.registry_path.read_bytes()


def assert_no_staging(policy: rq.QuarantinePathPolicy) -> None:
    assert not list(policy.registry_path.parent.glob(f".{policy.registry_path.name}.*.tmp"))


def test_atomic_write_success_and_revision(tmp_path: Path) -> None:
    policy, snapshot, updated, _ = prepare_atomic(tmp_path)
    rq._atomic_write_registry(policy.registry_path, updated, snapshot)
    assert rq.read_registry(policy.registry_path).registry_revision == updated.registry_revision
    assert_no_staging(policy)


def test_registry_mutation_retries_one_transient_cas_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    original = rq._atomic_write_registry
    calls = 0

    def transient_conflict(
        path: Path,
        registry: rq.MaterialRegistry,
        snapshot: rq._RegistrySnapshot,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise rq.RegistryError("quarantine_registry_conflict")
        original(path, registry, snapshot)

    monkeypatch.setattr(rq, "_atomic_write_registry", transient_conflict)

    result = rq.reconcile_complete_backlog(policy, now=NOW)

    assert result.newly_registered_count == 1
    assert calls == 2
    assert len(rq.read_registry(policy.registry_path).records) == 1
    assert_no_staging(policy)


@pytest.mark.parametrize("failure", ["serialize", "file-fsync", "staging-validation", "pre-directory-fsync", "promotion"])
def test_pre_promotion_failures_preserve_old_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    policy, snapshot, updated, before = prepare_atomic(tmp_path)
    if failure == "serialize":
        monkeypatch.setattr(rq, "_canonical_json", lambda value: (_ for _ in ()).throw(rq.RegistryError("quarantine_registry_serialize_failed")))
    elif failure == "file-fsync":
        monkeypatch.setattr(rq.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("fsync")))
    elif failure == "staging-validation":
        original = rq._read_registry_snapshot
        monkeypatch.setattr(rq, "_read_registry_snapshot", lambda path: rq._RegistrySnapshot(rq.MaterialRegistry.empty(), False, "") if str(path).endswith(".tmp") else original(path))
    elif failure == "pre-directory-fsync":
        monkeypatch.setattr(rq, "_fsync_directory", lambda path: (_ for _ in ()).throw(rq.RegistryError("quarantine_registry_directory_fsync_failed")))
    elif failure == "promotion":
        monkeypatch.setattr(rq.os, "replace", lambda source, target: (_ for _ in ()).throw(OSError("replace")))
    with pytest.raises(BaseException):
        rq._atomic_write_registry(policy.registry_path, updated, snapshot)
    assert policy.registry_path.read_bytes() == before
    assert_no_staging(policy)


@pytest.mark.parametrize("failure", ["post-directory-fsync", "final-validation"])
def test_post_promotion_failure_rolls_back_old_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    policy, snapshot, updated, before = prepare_atomic(tmp_path)
    if failure == "post-directory-fsync":
        calls = 0
        original = rq._fsync_directory
        def fail_second(path: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise rq.RegistryError("quarantine_registry_directory_fsync_failed")
            original(path)
        monkeypatch.setattr(rq, "_fsync_directory", fail_second)
    else:
        calls = 0
        original_read = rq._read_registry_snapshot
        def fail_final(path: Path) -> rq._RegistrySnapshot:
            nonlocal calls
            calls += 1
            if calls == 3:
                raise rq.RegistryError("quarantine_registry_final_invalid")
            return original_read(path)
        monkeypatch.setattr(rq, "_read_registry_snapshot", fail_final)
    with pytest.raises(rq.RegistryError):
        rq._atomic_write_registry(policy.registry_path, updated, snapshot)
    assert policy.registry_path.read_bytes() == before
    assert_no_staging(policy)


def test_cleanup_noop_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy, snapshot, updated, before = prepare_atomic(tmp_path)
    monkeypatch.setattr(rq.os, "replace", lambda source, target: (_ for _ in ()).throw(OSError("replace")))
    original_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, *args, **kwargs: None)
    with pytest.raises(rq.RegistryError, match="quarantine_registry_cleanup_failed"):
        rq._atomic_write_registry(policy.registry_path, updated, snapshot)
    assert policy.registry_path.read_bytes() == before
    monkeypatch.setattr(Path, "unlink", original_unlink)
    for item in policy.registry_path.parent.glob(f".{policy.registry_path.name}.*.tmp"):
        item.unlink()
    assert_no_staging(policy)


def test_cleanup_exception_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy, snapshot, updated, before = prepare_atomic(tmp_path)
    monkeypatch.setattr(rq.os, "replace", lambda source, target: (_ for _ in ()).throw(OSError("replace")))
    original_unlink = Path.unlink
    monkeypatch.setattr(Path, "unlink", lambda self, *args, **kwargs: (_ for _ in ()).throw(OSError("unlink")))
    with pytest.raises(rq.RegistryError, match="quarantine_registry_cleanup_failed"):
        rq._atomic_write_registry(policy.registry_path, updated, snapshot)
    assert policy.registry_path.read_bytes() == before
    monkeypatch.setattr(Path, "unlink", original_unlink)
    for item in policy.registry_path.parent.glob(f".{policy.registry_path.name}.*.tmp"):
        item.unlink()


def test_stale_cas_conflict_preserves_newer_writer(tmp_path: Path) -> None:
    policy, stale, stale_update, _ = prepare_atomic(tmp_path)
    write_raw(policy, "2026-08-10", "new writer")
    rq.reconcile_complete_backlog(policy, now=LATER)
    current = policy.registry_path.read_bytes()
    with pytest.raises(rq.RegistryError, match="quarantine_registry_conflict"):
        rq._atomic_write_registry(policy.registry_path, stale_update, stale)
    assert policy.registry_path.read_bytes() == current


def test_writer_ignoring_lock_is_caught_by_cas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy, snapshot, updated, _ = prepare_atomic(tmp_path)
    intruder = replace(snapshot.registry, registry_revision=snapshot.registry.registry_revision + 9)
    intruder_raw = rq._canonical_json(intruder.to_dict())
    original = rq._read_registry_snapshot
    calls = 0

    def inject_writer(path: Path) -> rq._RegistrySnapshot:
        nonlocal calls
        calls += 1
        if calls == 2:
            policy.registry_path.write_bytes(intruder_raw)
        return original(path)

    monkeypatch.setattr(rq, "_read_registry_snapshot", inject_writer)
    with pytest.raises(rq.RegistryError, match="quarantine_registry_conflict"):
        rq._atomic_write_registry(policy.registry_path, updated, snapshot)
    assert policy.registry_path.read_bytes() == intruder_raw
    assert_no_staging(policy)


def test_lock_failure_stops_before_registry_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    policy = make_policy(tmp_path)
    source = write_raw(policy, "2026-08-09")
    before = raw_metadata(source)
    monkeypatch.setattr(
        rq._InterProcessLock,
        "__enter__",
        lambda self: (_ for _ in ()).throw(rq.RegistryError("quarantine_registry_lock_failed")),
    )
    with pytest.raises(rq.RegistryError, match="quarantine_registry_lock_failed"):
        rq.reconcile_complete_backlog(policy, now=NOW)
    assert not policy.registry_path.exists()
    assert raw_metadata(source) == before


@pytest.mark.parametrize("exc_type", [RuntimeError, KeyboardInterrupt, SystemExit], ids=["exception", "keyboardinterrupt", "systemexit"])
def test_lock_releases_after_baseexception(tmp_path: Path, exc_type: type[BaseException]) -> None:
    policy = make_policy(tmp_path)
    with pytest.raises(exc_type):
        with rq._InterProcessLock(policy.registry_path.parent):
            raise exc_type()
    with rq._InterProcessLock(policy.registry_path.parent):
        pass


def test_process_death_releases_os_lock(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=process_die_holding_lock, args=(str(policy.registry_path.parent),))
    process.start()
    process.join(15)
    assert process.exitcode == 0
    with rq._InterProcessLock(policy.registry_path.parent):
        pass


# ---------------------------------------------------------------------------
# Real process races


def test_concurrent_full_scans_have_no_lost_update_and_one_claim(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    for day in range(1, 7):
        write_raw(policy, f"Naz/2026-07-0{day}", f"raw-{day}")
    rows = run_processes(process_reconcile, policy, 4)
    assert all(row[0] == "ok" and row[1] == 6 for row in rows), rows
    assert sum(bool(row[2]) for row in rows) == 1, rows
    registry = rq.read_registry(policy.registry_path)
    assert len(registry.records) == 6
    assert len(registry.notifications) == 1


def test_concurrent_different_date_writers_have_no_lost_update(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    common = (str(policy.inbox_root), str(policy.registry_path), str(policy.narrative_outbox_root))
    processes = [
        context.Process(target=process_add_and_reconcile, args=(*common, ref, queue))
        for ref in ("Naz/2026-08-01", "Naz/2026-08-02")
    ]
    for process in processes: process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    rows = [queue.get(timeout=5) for _ in processes]
    queue.close()
    assert all(row[0] == "ok" for row in rows)
    registry = rq.read_registry(policy.registry_path)
    assert {record.source_ref for record in registry.records} == {"Naz/2026-08-01", "Naz/2026-08-02"}
    assert len(registry.notifications) in {1, 2}
def test_concurrent_same_ready_candidate_has_one_winner(tmp_path: Path) -> None:
    policy, _ = reconcile_ready(tmp_path)
    rows = run_processes(process_ready_claim, policy, 4)
    assert all(row[0] == "ok" for row in rows)
    assert sum(bool(row[1]) for row in rows) == 1
    registry = rq.read_registry(policy.registry_path)
    assert registry.records[0].status == rq.STATUS_PROCESSING
    assert registry.records[0].attempt_count == 1


def test_registry_remains_strictly_valid_after_process_race(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    for day in range(1, 4):
        write_raw(policy, f"2026-08-0{day}")
    run_processes(process_reconcile, policy, 3)
    raw = policy.registry_path.read_bytes()
    assert rq.MaterialRegistry.from_dict(json.loads(raw.decode("utf-8"))) == rq.read_registry(policy.registry_path)
    assert_no_staging(policy)


# ---------------------------------------------------------------------------
# Ready/processing/blocked/consumed, fingerprints, and retries


def test_ready_claim_transitions_to_processing_once(tmp_path: Path) -> None:
    policy, candidate = reconcile_ready(tmp_path)
    first = rq.claim_ready_candidate(policy, candidate, now=NOW)
    second = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert first is not None and second is None
    record = rq.read_registry(policy.registry_path).records[0]
    assert (record.status, record.attempt_count, record.active_attempt_id) == (rq.STATUS_PROCESSING, 1, first.attempt_id)


def test_stale_crash_lease_recovers_fail_closed_for_manual_retry(
    tmp_path: Path,
) -> None:
    policy, candidate = reconcile_ready(tmp_path)
    claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert claim is not None

    rq.reconcile_complete_backlog(
        policy,
        now=NOW + timedelta(seconds=rq.MATERIAL_CLAIM_STALE_SECONDS + 1),
    )

    blocked = rq.read_registry(policy.registry_path).records[0]
    assert blocked.status == rq.STATUS_BLOCKED
    assert blocked.active_attempt_id == ""
    assert blocked.safe_reason_codes == ("processing_lease_expired",)
    assert blocked.history[-1].event == "attempt_expired"

    recovered = rq.request_manual_retry(
        policy,
        blocked.source_ref,
        blocked.source_digest,
        blocked.narrative_package_digest,
        "stale-crash-recovery-1",
        now=NOW + timedelta(seconds=rq.MATERIAL_CLAIM_STALE_SECONDS + 2),
    )
    assert recovered.status == rq.STATUS_READY
    assert recovered.active_attempt_id == ""


@pytest.mark.parametrize("durable_state", [rq.STATUS_PROCESSING, rq.STATUS_CONSUMED])
def test_idempotency_candidate_revalidates_non_ready_durable_state(
    tmp_path: Path,
    durable_state: str,
) -> None:
    policy, candidate = reconcile_ready(tmp_path)
    claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert claim is not None
    if durable_state == rq.STATUS_CONSUMED:
        rq.mark_attempt_consumed(policy, claim, now=LATER)

    selected = rq.select_idempotency_candidate(
        policy,
        project_name="Naz",
        date_text="2026-08-09",
    )

    assert selected == candidate


def test_attempt_release_returns_processing_lease_to_ready(tmp_path: Path) -> None:
    policy, candidate = reconcile_ready(tmp_path)
    claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert claim is not None

    released = rq.release_attempt(policy, claim, now=LATER)

    assert released.status == rq.STATUS_READY
    assert released.active_attempt_id == ""
    assert released.attempt_count == 1
    assert released.history[-1].event == "attempt_released"
    assert rq.select_ready_candidate(policy, project_name="Naz") == candidate


def test_cancelled_attempt_finalizer_is_idempotent_and_preserves_identity(
    tmp_path: Path,
) -> None:
    policy, candidate = reconcile_ready(tmp_path)
    claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert claim is not None

    first = rq.finalize_cancelled_attempt(policy, claim, now=LATER)
    revision = rq.read_registry(policy.registry_path).registry_revision
    second = rq.finalize_cancelled_attempt(policy, claim, now=LATER)

    assert first == second
    assert second.status == rq.STATUS_READY
    assert second.active_attempt_id == ""
    assert second.attempt_count == 1
    assert second.source_digest == candidate.source_digest
    assert second.narrative_package_digest == candidate.narrative_package_digest
    assert rq.read_registry(policy.registry_path).registry_revision == revision


@pytest.mark.parametrize("terminal", (rq.STATUS_CONSUMED, rq.STATUS_BLOCKED))
def test_cancelled_attempt_finalizer_does_not_revert_terminal_state(
    tmp_path: Path,
    terminal: str,
) -> None:
    policy, candidate = reconcile_ready(tmp_path)
    claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert claim is not None
    if terminal == rq.STATUS_CONSUMED:
        expected = rq.mark_attempt_consumed(policy, claim, now=LATER)
    else:
        rq.mark_attempt_blocked(
            policy,
            claim,
            ("director_contract_invalid",),
            now=LATER,
        )
        expected = rq.read_registry(policy.registry_path).records[0]
    revision = rq.read_registry(policy.registry_path).registry_revision

    actual = rq.finalize_cancelled_attempt(policy, claim, now=LATER)

    assert actual == expected
    assert actual.status == terminal
    assert actual.active_attempt_id == ""
    assert rq.read_registry(policy.registry_path).registry_revision == revision


def test_stale_cancelled_attempt_finalizer_does_not_overwrite_new_attempt(
    tmp_path: Path,
) -> None:
    policy, candidate = reconcile_ready(tmp_path)
    old_claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert old_claim is not None
    rq.finalize_cancelled_attempt(policy, old_claim, now=LATER)
    new_claim = rq.claim_ready_candidate(policy, candidate, now=LATER)
    assert new_claim is not None
    revision = rq.read_registry(policy.registry_path).registry_revision

    current = rq.finalize_cancelled_attempt(policy, old_claim, now=LATER)

    assert current.status == rq.STATUS_PROCESSING
    assert current.active_attempt_id == new_claim.attempt_id
    assert current.attempt_count == 2
    assert rq.read_registry(policy.registry_path).registry_revision == revision
    rq.finalize_cancelled_attempt(policy, new_claim, now=LATER)


def test_idempotency_candidate_never_exposes_raw_or_blocked(tmp_path: Path) -> None:
    raw_policy = make_policy(tmp_path / "raw")
    write_raw(raw_policy, "Naz/2026-08-09")
    rq.reconcile_complete_backlog(raw_policy, now=NOW)
    assert rq.select_idempotency_candidate(
        raw_policy,
        project_name="Naz",
        date_text="2026-08-09",
    ) is None

    blocked_policy, _, _ = blocked_ready(tmp_path / "blocked")
    assert rq.select_idempotency_candidate(
        blocked_policy,
        project_name="Naz",
        date_text="2026-08-09",
    ) is None


def test_director_rejection_transitions_blocked_and_not_selectable(tmp_path: Path) -> None:
    policy, claim, notification_id = blocked_ready(tmp_path)
    record = rq.read_registry(policy.registry_path).records[0]
    assert record.status == rq.STATUS_BLOCKED
    assert record.attempt_count == 1
    assert record.active_attempt_id == ""
    assert record.notification_ids == (notification_id,)
    assert rq.select_ready_candidate(policy, project_name="Naz") is None


def test_unchanged_blocked_reconcile_does_not_retry_or_alert(tmp_path: Path) -> None:
    policy, _, notification_id = blocked_ready(tmp_path)
    claim = rq.claim_notification(policy, notification_id=notification_id, now=NOW)
    assert claim is not None
    rq.finalize_notification(policy, notification_id, rq.NOTIFICATION_SENT, now=NOW)
    for minute in (1, 2, 3):
        rq.reconcile_complete_backlog(policy, now=NOW + timedelta(minutes=minute))
    record = rq.read_registry(policy.registry_path).records[0]
    assert record.status == rq.STATUS_BLOCKED
    assert record.attempt_count == 1
    assert rq.claim_notification(policy, notification_id=notification_id, now=LATER) is None


FINGERPRINT_DIMENSIONS = [
    "source", "package", "reasons", "director", "narrative", "eligibility",
]


@pytest.mark.parametrize("dimension", FINGERPRINT_DIMENSIONS)
def test_failure_fingerprint_binds_each_identity_dimension(dimension: str) -> None:
    args: list[object] = ["a" * 64, "b" * 64, ("director_contract_invalid",), "director-v1", "narrative-v1", "eligibility-v1"]
    first = rq.failure_fingerprint(*args)  # type: ignore[arg-type]
    index = {"source": 0, "package": 1, "reasons": 2, "director": 3, "narrative": 4, "eligibility": 5}[dimension]
    args[index] = {
        0: "c" * 64, 1: "d" * 64, 2: ("director_response_invalid",),
        3: "director-v2", 4: "narrative-v2", 5: "eligibility-v2",
    }[index]
    assert rq.failure_fingerprint(*args) != first  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "reasons",
    [
        ("director_contract_invalid", "director_response_invalid"),
        ("director_response_invalid", "director_contract_invalid"),
        ("director_contract_invalid", "director_response_invalid", "director_contract_invalid"),
    ],
    ids=["ordered", "reversed", "duplicate"],
)
def test_failure_fingerprint_reason_set_is_canonical(reasons: tuple[str, ...]) -> None:
    baseline = rq.failure_fingerprint("a" * 64, "b" * 64, ("director_contract_invalid", "director_response_invalid"), "d1", "n1")
    assert rq.failure_fingerprint("a" * 64, "b" * 64, reasons, "d1", "n1") == baseline


@pytest.mark.parametrize("unsafe", ["secret prose", "/raw/private/path", "Exception: token=abc", 7, None])
def test_unsafe_reasons_collapse_to_safe_fallback(unsafe: object) -> None:
    assert rq.canonical_reason_codes((unsafe,)) == (rq.DIRECTOR_SUMMARY_CODE,)


@pytest.mark.parametrize("identity_change", ["source", "package", "director-version", "narrative-version", "eligibility-version"])
def test_identity_change_reopens_blocked_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identity_change: str) -> None:
    policy, _, _ = blocked_ready(tmp_path)
    source = policy.inbox_root / "Naz" / "2026-08-09"
    package = policy.narrative_outbox_root / "packages" / "Naz-2026-08-09.json"
    payload = json.loads((source / "narrative_ready.json").read_text(encoding="utf-8"))
    if identity_change == "source":
        (source / "material.md").write_text("changed source", encoding="utf-8")
        payload["source_digest"] = rq.source_digest(source)
    elif identity_change == "package":
        package.write_text("changed package", encoding="utf-8")
        payload["narrative_package_digest"] = rq.narrative_package_digest(package)
    elif identity_change == "director-version":
        payload["contract_versions"]["director"] = "director-v2"
    elif identity_change == "narrative-version":
        payload["contract_versions"]["narrative"] = "narrative-v2"
    elif identity_change == "eligibility-version":
        monkeypatch.setattr(rq, "ELIGIBILITY_CONTRACT_VERSION", "reels-eligibility-v3")
    (source / "narrative_ready.json").write_text(json.dumps(payload), encoding="utf-8")
    result = rq.reconcile_complete_backlog(policy, now=LATER)
    record = rq.read_registry(policy.registry_path).records[0]
    assert result.changed_count == 1
    assert record.status == rq.STATUS_READY
    assert record.attempt_count == 1


def test_new_scheduler_date_does_not_retry_blocked_identity(tmp_path: Path) -> None:
    policy, _, _ = blocked_ready(tmp_path)
    before = rq.read_registry(policy.registry_path).records[0]
    rq.reconcile_complete_backlog(policy, now=NOW + timedelta(days=1))
    after = rq.read_registry(policy.registry_path).records[0]
    assert after.status == rq.STATUS_BLOCKED
    assert after.attempt_count == before.attempt_count


def test_manual_retry_is_explicit_idempotent_and_preserves_history(tmp_path: Path) -> None:
    policy, _, _ = blocked_ready(tmp_path)
    blocked = rq.read_registry(policy.registry_path).records[0]
    first = rq.request_manual_retry(
        policy, blocked.source_ref, blocked.source_digest, blocked.narrative_package_digest,
        "operator-request-1", now=LATER,
    )
    revision = rq.read_registry(policy.registry_path).registry_revision
    second = rq.request_manual_retry(
        policy, blocked.source_ref, blocked.source_digest, blocked.narrative_package_digest,
        "operator-request-1", now=LATER,
    )
    assert first == second
    assert first.status == rq.STATUS_READY
    assert first.attempt_count == blocked.attempt_count
    assert len(first.history) == len(blocked.history) + 1
    assert rq.read_registry(policy.registry_path).registry_revision == revision


def test_failure_history_and_operator_ids_are_bounded(tmp_path: Path) -> None:
    policy, _, _ = blocked_ready(tmp_path)
    for index in range(40):
        blocked = rq.read_registry(policy.registry_path).records[0]
        rq.request_manual_retry(
            policy,
            blocked.source_ref,
            blocked.source_digest,
            blocked.narrative_package_digest,
            f"operator-{index:02d}",
            now=NOW + timedelta(minutes=index + 1),
        )
        candidate = rq.select_ready_candidate(policy, project_name="Naz")
        assert candidate is not None
        claim = rq.claim_ready_candidate(policy, candidate, now=NOW + timedelta(minutes=index + 1))
        assert claim is not None
        rq.mark_attempt_blocked(
            policy,
            claim,
            ("director_contract_invalid",),
            now=NOW + timedelta(minutes=index + 1),
        )
    final = rq.read_registry(policy.registry_path).records[0]
    assert len(final.history) == rq._MAX_HISTORY
    assert len(final.manual_retry_ids) == rq._MAX_RETRY_IDS
    assert final.attempt_count == 41


@pytest.mark.parametrize("conflict", ["wrong-source", "wrong-package", "not-blocked", "bad-operator", "missing-source"])
def test_manual_retry_conflicts_fail_closed(tmp_path: Path, conflict: str) -> None:
    policy, _, _ = blocked_ready(tmp_path)
    record = rq.read_registry(policy.registry_path).records[0]
    source_ref = record.source_ref
    source_digest = record.source_digest
    package_digest = record.narrative_package_digest
    operator = "operator-request-1"
    if conflict == "wrong-source": source_digest = "f" * 64
    elif conflict == "wrong-package": package_digest = "e" * 64
    elif conflict == "not-blocked": rq.request_manual_retry(policy, source_ref, source_digest, package_digest, operator, now=LATER); operator = "operator-request-2"
    elif conflict == "bad-operator": operator = "bad operator id"
    elif conflict == "missing-source": source_ref = "Naz/2026-08-08"
    with pytest.raises((rq.RegistryError, rq.EligibilityError)):
        rq.request_manual_retry(policy, source_ref, source_digest, package_digest, operator, now=LATER)


def test_successful_attempt_transitions_consumed_and_never_reselects(tmp_path: Path) -> None:
    policy, candidate = reconcile_ready(tmp_path)
    claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert claim is not None
    consumed = rq.mark_attempt_consumed(policy, claim, now=LATER)
    assert consumed.status == rq.STATUS_CONSUMED
    assert rq.select_ready_candidate(policy, project_name="Naz") is None
    rq.reconcile_complete_backlog(policy, now=LATER + timedelta(minutes=1))
    assert rq.read_registry(policy.registry_path).records[0].status == rq.STATUS_CONSUMED


@pytest.mark.parametrize("transition", ["consume-twice", "block-after-consume", "retry-consumed", "wrong-attempt-token"])
def test_unknown_or_stale_state_transition_fails_closed(tmp_path: Path, transition: str) -> None:
    policy, candidate = reconcile_ready(tmp_path)
    claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
    assert claim is not None
    if transition == "wrong-attempt-token":
        bad = replace(claim, attempt_id="0" * 32)
        with pytest.raises(rq.RegistryError): rq.mark_attempt_consumed(policy, bad, now=LATER)
        return
    rq.mark_attempt_consumed(policy, claim, now=LATER)
    with pytest.raises(rq.RegistryError):
        if transition == "consume-twice": rq.mark_attempt_consumed(policy, claim, now=LATER)
        elif transition == "block-after-consume": rq.mark_attempt_blocked(policy, claim, ("director_contract_invalid",), now=LATER)
        else:
            row = rq.read_registry(policy.registry_path).records[0]
            rq.request_manual_retry(policy, row.source_ref, row.source_digest, row.narrative_package_digest, "operator-2", now=LATER)


# ---------------------------------------------------------------------------
# Notification transaction and crash policy


def test_registry_persisted_before_notification_can_be_claimed_next_run(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    assert rq.read_registry(policy.registry_path).notifications[0].state == rq.NOTIFICATION_PENDING
    claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert claim is not None


def test_notification_success_marks_sent_and_never_resends(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert claim is not None
    sent = rq.finalize_notification(policy, claim.notification_id, rq.NOTIFICATION_SENT, now=NOW)
    assert sent.state == rq.NOTIFICATION_SENT
    rq.reconcile_complete_backlog(policy, now=LATER)
    assert rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=LATER) is None


def test_notification_error_marks_failed_without_auto_retry(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert claim is not None
    failed = rq.finalize_notification(policy, claim.notification_id, rq.NOTIFICATION_FAILED, now=NOW)
    assert failed.state == rq.NOTIFICATION_FAILED
    rq.reconcile_complete_backlog(policy, now=LATER)
    assert rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=LATER) is None


def test_live_claim_is_not_stolen_by_concurrent_reconcile(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert claim is not None
    rq.reconcile_complete_backlog(policy, now=NOW + timedelta(seconds=rq.NOTIFICATION_CLAIM_STALE_SECONDS - 1))
    assert rq.read_registry(policy.registry_path).notifications[0].state == rq.NOTIFICATION_CLAIMED


def test_stale_claim_becomes_uncertain_and_does_not_auto_resend(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert claim is not None
    rq.reconcile_complete_backlog(policy, now=NOW + timedelta(seconds=rq.NOTIFICATION_CLAIM_STALE_SECONDS))
    notification = rq.read_registry(policy.registry_path).notifications[0]
    assert notification.state == rq.NOTIFICATION_UNCERTAIN
    assert rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=LATER) is None


@pytest.mark.parametrize("terminal_state", [rq.NOTIFICATION_FAILED, rq.NOTIFICATION_UNCERTAIN])
def test_explicit_notification_retry_creates_one_idempotent_attempt(tmp_path: Path, terminal_state: str) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert claim is not None
    rq.finalize_notification(policy, claim.notification_id, terminal_state, now=NOW)
    retry = rq.request_notification_retry(policy, claim.notification_id, "operator-notify-1", now=LATER)
    revision = rq.read_registry(policy.registry_path).registry_revision
    duplicate = rq.request_notification_retry(policy, claim.notification_id, "operator-notify-1", now=LATER)
    assert retry == duplicate
    assert retry.notification_id != claim.notification_id
    assert retry.state == rq.NOTIFICATION_PENDING
    assert rq.read_registry(policy.registry_path).registry_revision == revision


@pytest.mark.parametrize("state", [rq.NOTIFICATION_PENDING, rq.NOTIFICATION_CLAIMED, rq.NOTIFICATION_SENT])
def test_notification_retry_rejects_non_retryable_states(tmp_path: Path, state: str) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    notification = rq.read_registry(policy.registry_path).notifications[0]
    if state != rq.NOTIFICATION_PENDING:
        claim = rq.claim_notification(policy, notification_id=notification.notification_id, now=NOW)
        assert claim is not None
        if state == rq.NOTIFICATION_SENT:
            rq.finalize_notification(policy, claim.notification_id, rq.NOTIFICATION_SENT, now=NOW)
    with pytest.raises(rq.RegistryError):
        rq.request_notification_retry(policy, notification.notification_id, "operator-notify-1", now=LATER)


@pytest.mark.parametrize("outcome", [rq.NOTIFICATION_FAILED, rq.NOTIFICATION_UNCERTAIN])
def test_failed_and_uncertain_are_visible_in_safe_summary(tmp_path: Path, outcome: str) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "Naz/2026-08-09", "private body")
    rq.reconcile_complete_backlog(policy, now=NOW)
    claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert claim is not None
    rq.finalize_notification(policy, claim.notification_id, outcome, now=NOW)
    summary = rq.summarize_quarantine_notifications(policy)
    assert summary["notification_counts"][outcome] == 1
    encoded = json.dumps(summary, sort_keys=True)
    assert "private body" not in encoded
    assert str(policy.inbox_root) not in encoded


def test_notification_record_contains_no_raw_payload_or_path(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "Naz/2026-08-09", "TOP SECRET RAW BODY")
    rq.reconcile_complete_backlog(policy, now=NOW)
    raw = policy.registry_path.read_text(encoding="utf-8")
    assert "TOP SECRET RAW BODY" not in raw
    assert str(policy.inbox_root) not in raw
    assert "payload" not in rq.NotificationRecord.__dataclass_fields__


def test_status_listing_contains_only_privacy_safe_identifiers(tmp_path: Path) -> None:
    policy = make_policy(tmp_path)
    write_raw(policy, "PrivateProject/2026-08-09", "TOP SECRET RAW BODY")
    rq.reconcile_complete_backlog(policy, now=NOW)
    rows = rq.list_quarantine_status(policy)
    encoded = repr(rows)
    assert len(rows) == 1
    assert rows[0].status == rq.STATUS_NEEDS_NARRATIVE
    assert "PrivateProject" not in encoded
    assert "2026-08-09" not in encoded
    assert "TOP SECRET RAW BODY" not in encoded
    assert str(policy.inbox_root) not in encoded


# ---------------------------------------------------------------------------
# Main integration: global reconcile before selection and direct-call gate


def import_main():
    import main
    return main


def test_scheduler_five_raw_dates_is_one_alert_then_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = import_main()
    policy = make_policy(tmp_path)
    for day in range(1, 6):
        write_raw(policy, f"Naz/2026-07-0{day}", f"raw-{day}")
    calls = {"notify": 0, "process": 0}
    async def fake_notify(bot: object, text: str) -> None:
        calls["notify"] += 1
        assert str(policy.inbox_root) not in text
    async def fake_process(*args: object, **kwargs: object) -> str:
        calls["process"] += 1
        return "unexpected"
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    monkeypatch.setattr(main, "notify_admin", fake_notify)
    monkeypatch.setattr(main, "process_agent_content_date", fake_process)
    monkeypatch.setattr(main, "ADMIN_ID", 1)
    context = type("Context", (), {"bot": object()})()
    for expected_notify in (1, 1, 1):
        asyncio.run(main.agent_content_sync_job(context))
        assert calls == {"notify": expected_notify, "process": 0}
    registry = rq.read_registry(policy.registry_path)
    assert len(registry.records) == 5
    assert len(registry.notifications) == 1
    assert registry.notifications[0].state == rq.NOTIFICATION_SENT


@pytest.mark.parametrize("send_fails", [False, True], ids=["send-success", "telegram-error"])
def test_main_notification_boundary_persists_claim_before_send_and_finalizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, send_fails: bool
) -> None:
    main = import_main()
    policy = make_policy(tmp_path)
    write_raw(policy, "2026-08-09", "private raw")
    rq.reconcile_complete_backlog(policy, now=NOW)
    claim = rq.claim_notification(policy, kind=rq.RAW_AGGREGATE_KIND, now=NOW)
    assert claim is not None

    async def fake_send(bot: object, text: str) -> None:
        current = rq.read_registry(policy.registry_path).notifications[0]
        assert current.state == rq.NOTIFICATION_CLAIMED
        assert "private raw" not in text
        if send_fails:
            raise RuntimeError("private Telegram failure")

    monkeypatch.setattr(main, "notify_admin", fake_send)
    asyncio.run(main.finalize_reels_quarantine_notification(object(), policy, claim, "safe summary"))
    expected = rq.NOTIFICATION_FAILED if send_fails else rq.NOTIFICATION_SENT
    assert rq.read_registry(policy.registry_path).notifications[0].state == expected


def test_scheduler_mixed_raw_preferred_and_ready_other_project_processes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = import_main()
    policy = make_policy(tmp_path)
    write_raw(policy, "Naz/2026-08-09", "raw preferred")
    ready_payload(policy, "Other/2026-08-09")
    calls = {"process": 0}
    async def fake_notify(bot: object, text: str) -> None: return None
    async def fake_process(*args: object, **kwargs: object) -> str:
        calls["process"] += 1
        return "unexpected"
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    monkeypatch.setattr(main, "notify_admin", fake_notify)
    monkeypatch.setattr(main, "process_agent_content_date", fake_process)
    monkeypatch.setattr(main, "ADMIN_ID", 1)
    monkeypatch.setattr(main, "AGENT_CONTENT_PROJECT", "Naz")
    context = type("Context", (), {"bot": object()})()
    asyncio.run(main.agent_content_sync_job(context))
    assert calls["process"] == 0


def test_scheduler_passes_explicit_ready_candidate_after_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = import_main()
    policy = make_policy(tmp_path)
    ready_payload(policy, "Naz/2026-08-09")
    captured: list[rq.EligibleCandidate] = []
    async def fake_process(*args: object, **kwargs: object) -> str:
        captured.append(kwargs["eligible_candidate"])
        return "ok"
    async def fake_notify(bot: object, text: str) -> None: pytest.fail("ready-only backlog must not alert")
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    monkeypatch.setattr(main, "process_agent_content_date", fake_process)
    monkeypatch.setattr(main, "notify_admin", fake_notify)
    monkeypatch.setattr(main, "ADMIN_ID", 1)
    monkeypatch.setattr(main, "AGENT_CONTENT_PROJECT", "Naz")
    context = type("Context", (), {"bot": object()})()
    asyncio.run(main.agent_content_sync_job(context))
    assert len(captured) == 1
    assert captured[0].source_ref == "Naz/2026-08-09"


def test_direct_process_call_with_raw_source_stops_before_editorial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = import_main()
    policy = make_policy(tmp_path)
    write_raw(policy, "Naz/2026-08-09")
    rq.reconcile_complete_backlog(policy, now=NOW)
    calls = {"collect": 0, "plan": 0, "seen": 0}
    monkeypatch.setattr(main, "AGENT_CONTENT_INBOX", policy.inbox_root)
    monkeypatch.setattr(main, "AGENT_CONTENT_PROJECT", "Naz")
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    def collect_raw(*args: object) -> tuple[str, list[str], str]:
        calls["collect"] += 1
        return (
            "First the builder tested one bounded input on the local workbench. "
            "Then the builder changed one visible configuration value. "
            "After the change the same local test completed successfully. "
            "The verified result worked and remained stable on the repeated check.",
            [],
            "2026-08-09",
        )

    monkeypatch.setattr(main, "collect_project_first_agent_materials", collect_raw)
    monkeypatch.setattr(main, "scheduled_plan", lambda *args, **kwargs: calls.__setitem__("plan", calls["plan"] + 1))
    monkeypatch.setattr(main, "mark_agent_content_seen", lambda *args: calls.__setitem__("seen", calls["seen"] + 1))
    asyncio.run(main.process_agent_content_date(object(), 1, "2026-08-09", force=True))
    assert calls == {"collect": 1, "plan": 0, "seen": 0}


@pytest.mark.parametrize(
    "scenario",
    (
        "corrupt-registry",
        "no-manifest",
        "invalid-manifest",
        "stale-manifest",
    ),
)
def test_producing_route_eligibility_failures_stop_before_editorial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    main = import_main()
    policy = make_policy(tmp_path)
    source = write_raw(policy, "Naz/2026-08-09", "bounded source")
    if scenario == "corrupt-registry":
        policy.registry_path.parent.mkdir(parents=True, exist_ok=True)
        policy.registry_path.write_bytes(b"{corrupt-registry")
    elif scenario == "no-manifest":
        rq.reconcile_complete_backlog(policy, now=NOW)
    else:
        ready_payload(policy, "Naz/2026-08-09", source_content="bounded source")
        rq.reconcile_complete_backlog(policy, now=NOW)
        if scenario == "invalid-manifest":
            (source / "narrative_ready.json").write_bytes(b"{invalid-manifest")
        else:
            (source / "material.md").write_text(
                "bounded source changed after manifest",
                encoding="utf-8",
            )

    calls = {"plan": 0, "text": 0, "director": 0}

    def forbidden_plan(*args: object, **kwargs: object) -> object:
        calls["plan"] += 1
        raise AssertionError("eligibility rejection must precede EditorialPlan")

    async def forbidden_text(*args: object, **kwargs: object) -> object:
        calls["text"] += 1
        raise AssertionError("eligibility rejection must precede text provider")

    async def forbidden_director(*args: object, **kwargs: object) -> object:
        calls["director"] += 1
        raise AssertionError("eligibility rejection must precede Director")

    monkeypatch.setattr(main, "AGENT_CONTENT_INBOX", policy.inbox_root)
    monkeypatch.setattr(main, "AGENT_CONTENT_PROJECT", "Naz")
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    monkeypatch.setattr(
        main,
        "collect_project_first_agent_materials",
        lambda date, focus, dirs: ("bounded producing source", [], date),
    )
    monkeypatch.setattr(
        main,
        "chronicle_source_row",
        lambda **values: story_first_source_row(values["source_ref"]),
    )
    monkeypatch.setattr(main, "scheduled_plan", forbidden_plan)
    monkeypatch.setattr(main, "generate_scheduled_package", forbidden_text)
    monkeypatch.setattr(main, "generate_reels_director_treatment", forbidden_director)

    result = asyncio.run(
        main.process_agent_content_date(
            object(),
            1,
            "2026-08-09",
            force=True,
        )
    )

    assert "eligibility" in result
    assert calls == {"plan": 0, "text": 0, "director": 0}


def test_direct_ready_process_reads_only_claimed_source_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = import_main()
    policy = make_policy(tmp_path)
    ready_source, _, _ = ready_payload(policy, "Naz/2026-08-09")
    write_raw(policy, "Other/2026-08-09", "must not merge")
    rq.reconcile_complete_backlog(policy, now=NOW)
    candidate = rq.select_ready_candidate(policy, project_name="Naz")
    assert candidate is not None
    captured: list[list[Path]] = []
    def fake_collect(date: str, focus: str, dirs: list[Path]) -> tuple[str, list[str], str]:
        captured.append(dirs)
        return "", [], date
    monkeypatch.setattr(main, "AGENT_CONTENT_INBOX", policy.inbox_root)
    monkeypatch.setattr(main, "AGENT_CONTENT_PROJECT", "Naz")
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    monkeypatch.setattr(main, "collect_project_first_agent_materials", fake_collect)
    monkeypatch.setattr(main, "load_agent_content_seen", lambda: {})
    asyncio.run(main.process_agent_content_date(object(), 1, "2026-08-09", force=True, eligible_candidate=candidate))
    assert captured == [[ready_source.resolve()]]


def test_main_director_rejection_alerts_once_then_blocked_run_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = import_main()
    policy, candidate = reconcile_ready(tmp_path)
    plan = SimpleNamespace(production_mode="story_first", plan_id="a" * 24, slot="agent_content_sync")
    calls = {"director": 0, "notify": 0, "queue": 0, "seen": 0}

    async def reject(*args: object, **kwargs: object) -> object:
        calls["director"] += 1
        raise RuntimeError("private provider detail")

    async def notify(bot: object, text: str) -> None:
        calls["notify"] += 1
        assert "private provider detail" not in text
        assert str(policy.inbox_root) not in text

    monkeypatch.setattr(main, "AGENT_CONTENT_INBOX", policy.inbox_root)
    monkeypatch.setattr(main, "AGENT_CONTENT_PROJECT", "Naz")
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    monkeypatch.setattr(main, "load_agent_content_seen", lambda: {})
    monkeypatch.setattr(main, "collect_project_first_agent_materials", lambda date, focus, dirs: ("safe fact", [], date))
    monkeypatch.setattr(
        main,
        "chronicle_source_row",
        lambda **values: story_first_source_row(values["source_ref"]),
    )
    monkeypatch.setattr(main.memory, "load_character_state", lambda user_id: object())
    monkeypatch.setattr(main.naz_character, "apply_event", lambda state, event: object())
    monkeypatch.setattr(main, "scheduled_plan", lambda **kwargs: plan)
    monkeypatch.setattr(main.memory, "update_editorial_release_event", lambda **kwargs: None)
    monkeypatch.setattr(main, "generate_reels_director_treatment", reject)
    monkeypatch.setattr(main, "notify_admin", notify)
    monkeypatch.setattr(main, "queue_story_first_pack", lambda *args: calls.__setitem__("queue", calls["queue"] + 1))
    monkeypatch.setattr(main, "mark_agent_content_seen", lambda *args: calls.__setitem__("seen", calls["seen"] + 1))

    first = asyncio.run(main.process_agent_content_date(object(), 1, candidate.date_text, force=True, eligible_candidate=candidate))
    second = asyncio.run(main.process_agent_content_date(object(), 1, candidate.date_text, force=True, eligible_candidate=candidate))
    assert "отклонён" in first
    assert "valid narrative_ready eligibility" in second
    assert calls == {"director": 1, "notify": 1, "queue": 0, "seen": 0}
    record = rq.read_registry(policy.registry_path).records[0]
    assert record.status == rq.STATUS_BLOCKED
    assert record.attempt_count == 1


def test_director_cancellation_releases_claim_and_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = import_main()
    policy, candidate = reconcile_ready(tmp_path)
    plan = SimpleNamespace(
        production_mode="story_first",
        plan_id="c" * 24,
        slot="agent_content_sync",
    )
    director = AsyncMock(
        side_effect=(
            asyncio.CancelledError(),
            RuntimeError("synthetic director reject"),
        )
    )
    queue = Mock()
    seen = Mock()
    notification = AsyncMock()
    monkeypatch.setattr(main, "AGENT_CONTENT_INBOX", policy.inbox_root)
    monkeypatch.setattr(main, "AGENT_CONTENT_PROJECT", "Naz")
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    monkeypatch.setattr(main, "load_agent_content_seen", lambda: {})
    monkeypatch.setattr(
        main,
        "collect_project_first_agent_materials",
        lambda date, focus, dirs: ("safe fact", [], date),
    )
    monkeypatch.setattr(
        main,
        "chronicle_source_row",
        lambda **values: story_first_source_row(values["source_ref"]),
    )
    monkeypatch.setattr(main.memory, "load_character_state", lambda user_id: object())
    monkeypatch.setattr(main.naz_character, "apply_event", lambda state, event: object())
    monkeypatch.setattr(main, "scheduled_plan", lambda **kwargs: plan)
    monkeypatch.setattr(main.memory, "update_editorial_release_event", lambda **kwargs: None)
    monkeypatch.setattr(main, "generate_reels_director_treatment", director)
    monkeypatch.setattr(main, "queue_story_first_pack", queue)
    monkeypatch.setattr(main, "mark_agent_content_seen", seen)
    monkeypatch.setattr(main, "notify_admin", notification)

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(main.process_agent_content_date(
            object(),
            1,
            candidate.date_text,
            force=True,
            eligible_candidate=candidate,
        ))

    assert type(raised.value) is asyncio.CancelledError
    assert raised.value.__cause__ is None
    after_cancel = rq.read_registry(policy.registry_path).records[0]
    assert after_cancel.status == rq.STATUS_READY
    assert after_cancel.active_attempt_id == ""
    assert after_cancel.attempt_count == 1
    queue.assert_not_called()
    seen.assert_not_called()
    notification.assert_not_awaited()

    result = asyncio.run(main.process_agent_content_date(
        object(),
        1,
        candidate.date_text,
        force=True,
        eligible_candidate=candidate,
    ))
    assert "режиссёрский план отклонён" in result
    final = rq.read_registry(policy.registry_path).records[0]
    assert final.status == rq.STATUS_BLOCKED
    assert final.active_attempt_id == ""
    assert final.attempt_count == 2
    assert director.await_count == 2
    queue.assert_not_called()
    seen.assert_not_called()


def test_story_delivery_cancellation_does_not_revert_consumed_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = import_main()
    policy, candidate = reconcile_ready(tmp_path)
    plan = SimpleNamespace(
        production_mode="story_first",
        plan_id="d" * 24,
        slot="agent_content_sync",
    )
    pack_dir = tmp_path / "pack-cancelled-delivery"
    pack_dir.mkdir()
    director = AsyncMock(return_value=object())
    queue = Mock(return_value=pack_dir)
    seen = Mock()
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=asyncio.CancelledError()))
    monkeypatch.setattr(main, "ADMIN_ID", 42)
    monkeypatch.setattr(main, "AGENT_CONTENT_INBOX", policy.inbox_root)
    monkeypatch.setattr(main, "AGENT_CONTENT_PROJECT", "Naz")
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    monkeypatch.setattr(main, "load_agent_content_seen", lambda: {})
    monkeypatch.setattr(
        main,
        "collect_project_first_agent_materials",
        lambda date, focus, dirs: ("safe fact", [], date),
    )
    monkeypatch.setattr(
        main,
        "chronicle_source_row",
        lambda **values: story_first_source_row(values["source_ref"]),
    )
    monkeypatch.setattr(main.memory, "load_character_state", lambda user_id: object())
    monkeypatch.setattr(main.naz_character, "apply_event", lambda state, event: object())
    monkeypatch.setattr(main, "scheduled_plan", lambda **kwargs: plan)
    monkeypatch.setattr(main.memory, "update_editorial_release_event", lambda **kwargs: None)
    monkeypatch.setattr(main, "generate_reels_director_treatment", director)
    monkeypatch.setattr(main, "queue_story_first_pack", queue)
    monkeypatch.setattr(
        main.story_production,
        "read_manifest",
        lambda path: {"plan_id": plan.plan_id},
    )
    monkeypatch.setattr(main, "mark_agent_content_seen", seen)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(main.process_agent_content_date(
            bot,
            1,
            candidate.date_text,
            force=True,
            eligible_candidate=candidate,
        ))

    after_cancel = rq.read_registry(policy.registry_path).records[0]
    assert after_cancel.status == rq.STATUS_CONSUMED
    assert after_cancel.active_attempt_id == ""
    assert after_cancel.attempt_count == 1
    seen.assert_not_called()
    second = asyncio.run(main.process_agent_content_date(
        bot,
        1,
        candidate.date_text,
        force=True,
        eligible_candidate=candidate,
    ))
    assert "valid narrative_ready eligibility" in second
    director.assert_awaited_once()
    queue.assert_called_once()
    bot.send_message.assert_awaited_once()
    seen.assert_not_called()


@pytest.mark.parametrize(
    "error_type",
    (KeyboardInterrupt, SystemExit, GeneratorExit),
    ids=("keyboard-interrupt", "system-exit", "generator-exit"),
)
def test_post_claim_baseexception_releases_exact_attempt_and_reraises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[BaseException],
) -> None:
    main = import_main()
    policy, candidate = reconcile_ready(tmp_path)
    original = error_type("sensitive-baseexception-detail")

    async def fail_after_claim(*args: object, attempt_lifecycle, **kwargs: object) -> str:
        claim = rq.claim_ready_candidate(policy, candidate, now=NOW)
        assert claim is not None
        attempt_lifecycle.activate(policy, claim)
        raise original

    monkeypatch.setattr(main, "_process_agent_content_date", fail_after_claim)

    with pytest.raises(error_type) as raised:
        asyncio.run(main.process_agent_content_date(object(), 1, candidate.date_text))

    assert raised.value is original
    assert raised.value.__cause__ is None
    record = rq.read_registry(policy.registry_path).records[0]
    assert record.status == rq.STATUS_READY
    assert record.active_attempt_id == ""
    assert record.attempt_count == 1


def test_main_story_first_success_marks_consumed_and_seen_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = import_main()
    policy, candidate = reconcile_ready(tmp_path)
    plan = SimpleNamespace(production_mode="story_first", plan_id="b" * 24, slot="agent_content_sync")
    pack_dir = tmp_path / "pack"
    pack_dir.mkdir()
    calls = {"director": 0, "queue": 0, "seen": 0}

    async def accept(*args: object, **kwargs: object) -> object:
        calls["director"] += 1
        return object()

    def queue_pack(*args: object) -> Path:
        calls["queue"] += 1
        return pack_dir

    monkeypatch.setattr(main, "ADMIN_ID", 0)
    monkeypatch.setattr(main, "AGENT_CONTENT_INBOX", policy.inbox_root)
    monkeypatch.setattr(main, "AGENT_CONTENT_PROJECT", "Naz")
    monkeypatch.setattr(main, "reels_quarantine_policy", lambda: policy)
    monkeypatch.setattr(main, "load_agent_content_seen", lambda: {})
    monkeypatch.setattr(main, "collect_project_first_agent_materials", lambda date, focus, dirs: ("safe fact", [], date))
    monkeypatch.setattr(
        main,
        "chronicle_source_row",
        lambda **values: story_first_source_row(values["source_ref"]),
    )
    monkeypatch.setattr(main.memory, "load_character_state", lambda user_id: object())
    monkeypatch.setattr(main.naz_character, "apply_event", lambda state, event: object())
    monkeypatch.setattr(main, "scheduled_plan", lambda **kwargs: plan)
    monkeypatch.setattr(main.memory, "update_editorial_release_event", lambda **kwargs: None)
    monkeypatch.setattr(main, "generate_reels_director_treatment", accept)
    monkeypatch.setattr(main, "queue_story_first_pack", queue_pack)
    monkeypatch.setattr(main.story_production, "read_manifest", lambda path: {"plan_id": plan.plan_id})
    monkeypatch.setattr(main, "mark_agent_content_seen", lambda *args: calls.__setitem__("seen", calls["seen"] + 1))

    first = asyncio.run(main.process_agent_content_date(object(), 1, candidate.date_text, force=True, eligible_candidate=candidate))
    second = asyncio.run(main.process_agent_content_date(object(), 1, candidate.date_text, force=True, eligible_candidate=candidate))
    assert "awaits approval" in first
    assert "valid narrative_ready eligibility" in second
    assert calls == {"director": 1, "queue": 1, "seen": 1}
    assert rq.read_registry(policy.registry_path).records[0].status == rq.STATUS_CONSUMED
