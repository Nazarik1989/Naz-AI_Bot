from __future__ import annotations

import dataclasses
import base64
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import narrative_normalizer as normalizer
import narrative_outbox_permissions as permissions
import tools.run_narrative_normalizer as cli


POSIX = os.name == "posix"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _shared() -> permissions.NarrativeOutboxPermissionPolicy:
    if not POSIX:
        pytest.skip("real UID/GID/mode semantics require POSIX")
    return permissions.NarrativeOutboxPermissionPolicy(
        permissions.SHARED_REVIEW_POLICY_VERSION,
        os.getegid(),
    )


def _layout(tmp_path: Path):
    root = tmp_path / "outbox"
    state = root / ".normalizer-state"
    locks = state / "locks"
    claims = state / "claims"
    for path in (root, state, locks, claims):
        path.mkdir()
    draft = root / ("a" * 64)
    draft.mkdir(mode=0o700)
    for name in permissions.DRAFT_FILE_NAMES:
        (draft / name).write_bytes(b"{}\n")
        os.chmod(draft / name, 0o600)
    return root, state, locks, claims, draft


def test_permission_policy_is_frozen_and_versioned():
    policy = permissions.PRIVATE_POLICY
    assert dataclasses.is_dataclass(policy)
    assert policy.version == "private-v1"
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.version = "shared-review-v1"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("version", "gid"),
    [
        ("unknown-v1", None),
        ("private-v1", 1),
        ("shared-review-v1", None),
        ("shared-review-v1", -1),
        ("shared-review-v1", True),
    ],
)
def test_policy_constructor_is_closed(version, gid):
    with pytest.raises(TypeError):
        permissions.NarrativeOutboxPermissionPolicy(version, gid)


def test_private_policy_is_explicit_default_and_rejects_group():
    assert permissions.resolve_permission_policy("private-v1", None) is permissions.PRIVATE_POLICY
    with pytest.raises(permissions.OutboxPermissionError):
        permissions.resolve_permission_policy("private-v1", "reviewers")


def test_unknown_or_malformed_shared_group_fails_closed():
    with pytest.raises(permissions.OutboxPermissionError):
        permissions.resolve_permission_policy("not-a-policy", None)
    with pytest.raises(permissions.OutboxPermissionError):
        permissions.resolve_permission_policy("shared-review-v1", "../reviewers")


def test_store_defaults_to_private_v1(tmp_path: Path):
    import reels_failure_quarantine as quarantine

    policy = quarantine.QuarantinePathPolicy(
        tmp_path / "inbox",
        tmp_path / "state" / "registry.json",
        tmp_path / "outbox",
    )
    store = normalizer.NarrativeOutboxStore(policy)
    assert store.permission_policy is permissions.PRIVATE_POLICY
    assert store.permission_policy.draft_file_mode == 0o600
    assert store.permission_policy.lock_file_mode == 0o600


def test_help_does_not_create_outbox(tmp_path: Path, capsys):
    with pytest.raises(SystemExit) as error:
        cli._parser().parse_args(["--help"])
    assert error.value.code == 0
    assert not tuple(tmp_path.iterdir())
    assert "--outbox-permission-policy" in capsys.readouterr().out


@pytest.mark.skipif(POSIX, reason="non-POSIX fail-closed contract")
def test_shared_policy_live_resolution_fails_closed_on_non_posix():
    with pytest.raises(permissions.OutboxPermissionError):
        permissions.resolve_permission_policy("shared-review-v1", "reviewers")


@pytest.mark.skipif(not POSIX, reason="real UID/GID/mode semantics require POSIX")
def test_shared_layout_has_exact_owner_group_modes_and_zero_world_bits(tmp_path: Path):
    policy = _shared()
    root, state, locks, claims, draft = _layout(tmp_path)
    permissions.finalize_shared_internal_layout(policy, root, state, locks, claims)
    permissions.finalize_shared_draft(policy, root, draft)

    assert _mode(root) == 0o2750
    assert _mode(state) == 0o2750
    assert _mode(locks) == 0o3770
    assert _mode(claims) == 0o2750
    assert _mode(draft) == 0o3770
    assert draft.lstat().st_uid == root.lstat().st_uid == os.geteuid()
    for name in permissions.DRAFT_FILE_NAMES:
        target = draft / name
        assert _mode(target) == 0o640
        assert target.lstat().st_uid == os.geteuid()
        assert target.lstat().st_gid == policy.shared_gid
        assert _mode(target) & 0o007 == 0


@pytest.mark.skipif(not POSIX, reason="real UID/GID/mode semantics require POSIX")
def test_staging_remains_private_until_promotion(tmp_path: Path):
    policy = _shared()
    root, state, locks, claims, _ = _layout(tmp_path)
    permissions.finalize_shared_internal_layout(policy, root, state, locks, claims)
    staging = root / ".staging-test"
    staging.mkdir(mode=0o700)
    target = staging / "story.json"
    target.write_bytes(b"{}\n")
    os.chmod(target, 0o600)
    assert _mode(staging) == 0o700
    assert _mode(target) == 0o600


@pytest.mark.skipif(not POSIX, reason="real UID/GID/mode semantics require POSIX")
def test_mode_tamper_is_rejected(tmp_path: Path):
    policy = _shared()
    root, state, locks, claims, draft = _layout(tmp_path)
    permissions.finalize_shared_internal_layout(policy, root, state, locks, claims)
    permissions.finalize_shared_draft(policy, root, draft)
    os.chmod(draft / "story.json", 0o660)
    with pytest.raises(permissions.OutboxPermissionError):
        permissions.verify_shared_draft(policy, root, draft)


@pytest.mark.skipif(not POSIX, reason="real symlink boundary requires POSIX")
def test_symlink_file_is_rejected_without_following(tmp_path: Path):
    policy = _shared()
    root, state, locks, claims, draft = _layout(tmp_path)
    permissions.finalize_shared_internal_layout(policy, root, state, locks, claims)
    story = draft / "story.json"
    story.unlink()
    story.symlink_to(draft / "review.json")
    with pytest.raises(permissions.OutboxPermissionError):
        permissions.finalize_shared_draft(policy, root, draft)


@pytest.mark.skipif(not POSIX, reason="real chmod boundary requires POSIX")
def test_chmod_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    policy = _shared()
    root, *_ = _layout(tmp_path)
    monkeypatch.setattr(permissions.os, "fchmod", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(permissions.OutboxPermissionError):
        permissions.finalize_shared_root(policy, root)


@pytest.mark.skipif(not POSIX, reason="real chgrp boundary requires POSIX")
def test_chgrp_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    policy = _shared()
    root, *_ = _layout(tmp_path)
    monkeypatch.setattr(permissions.os, "fchown", lambda *_: (_ for _ in ()).throw(OSError()))
    with pytest.raises(permissions.OutboxPermissionError):
        permissions.finalize_shared_root(policy, root)


@pytest.mark.skipif(not POSIX, reason="real UID/GID/mode semantics require POSIX")
def test_approval_pair_is_reviewer_owned_group_readable_and_not_world_accessible(tmp_path: Path):
    policy = _shared()
    root, state, locks, claims, draft = _layout(tmp_path)
    permissions.finalize_shared_internal_layout(policy, root, state, locks, claims)
    permissions.finalize_shared_draft(policy, root, draft)
    for name in permissions.APPROVAL_FILE_NAMES:
        target = draft / name
        target.write_bytes(b"{}\n")
        os.chmod(target, 0o600)
        permissions.finalize_shared_approval_file(policy, target)
        permissions.verify_shared_approval_file(policy, target)
        assert target.lstat().st_uid == os.geteuid()
        assert target.lstat().st_gid == policy.shared_gid
        assert _mode(target) == 0o640


@pytest.mark.skipif(not POSIX, reason="real UID/GID/mode semantics require POSIX")
def test_shared_normalizer_promotion_and_local_approval_are_exact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import test_narrative_normalizer as fixtures

    monkeypatch.setenv(
        "NARRATIVE_NORMALIZER_TRUST_KEY",
        base64.b64encode(fixtures.TEST_TRUST_KEY).decode("ascii"),
    )
    values = fixtures.runtime(tmp_path)
    policy = values[1]
    record = values[3]
    service = values[-1]
    shared = _shared()
    service.store = normalizer.NarrativeOutboxStore(
        policy,
        trust_service=policy.narrative_trust_service,
        permission_policy=shared,
    )
    outcome = service.normalize_source(record.source_ref, record.source_digest)
    assert outcome.status == normalizer.OUTCOME_DRAFT_READY_FOR_REVIEW
    draft = service.store.draft_path(
        record.source_digest,
        source_ref=record.source_ref,
    )
    permissions.verify_shared_draft(shared, service.store.root, draft)
    claim = service.store.claim_path(record.source_ref, record.source_digest)
    assert _mode(claim) == 0o640
    assert claim.lstat().st_gid == shared.shared_gid

    fixtures.approve_created(service.store, record, draft)
    for name in permissions.APPROVAL_FILE_NAMES:
        permissions.verify_shared_approval_file(shared, draft / name)
    assert not tuple(draft.glob(".*staging*"))


@pytest.mark.skipif(not POSIX, reason="real UID/GID/mode semantics require POSIX")
def test_permission_failure_prevents_broker_registration_and_ready_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import test_narrative_normalizer as fixtures

    class BrokerMustNotBeCalled:
        def __init__(self):
            self.calls = 0

        def __getattr__(self, _name):
            self.calls += 1
            raise AssertionError("broker called before permission gate")

    monkeypatch.setenv(
        "NARRATIVE_NORMALIZER_TRUST_KEY",
        base64.b64encode(fixtures.TEST_TRUST_KEY).decode("ascii"),
    )
    values = fixtures.runtime(tmp_path)
    policy = values[1]
    record = values[3]
    service = values[-1]
    broker = BrokerMustNotBeCalled()
    service.store = normalizer.NarrativeOutboxStore(
        policy,
        trust_service=policy.narrative_trust_service,
        review_authority=broker,
        permission_policy=_shared(),
    )
    monkeypatch.setattr(
        permissions,
        "finalize_shared_draft",
        lambda *_: (_ for _ in ()).throw(permissions.OutboxPermissionError()),
    )
    with pytest.raises(normalizer.NarrativeNormalizerError):
        service.normalize_source(record.source_ref, record.source_digest)
    assert broker.calls == 0
    assert not tuple(service.store.root.rglob("narrative_ready.json"))


@pytest.mark.skipif(
    not POSIX or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="real cross-identity sticky-directory probe requires POSIX root",
)
def test_reviewer_identity_can_read_and_create_but_cannot_mutate_or_unlink_story(tmp_path: Path):
    import pwd

    reviewer = pwd.getpwnam("nobody")
    policy = permissions.NarrativeOutboxPermissionPolicy(
        permissions.SHARED_REVIEW_POLICY_VERSION,
        reviewer.pw_gid,
    )
    os.chmod(tmp_path, 0o755)
    root, state, locks, claims, draft = _layout(tmp_path)
    permissions.finalize_shared_internal_layout(policy, root, state, locks, claims)
    permissions.finalize_shared_draft(policy, root, draft)
    story = draft / "story.json"
    approval = draft / "approval-attestation.json"
    script = (
        "from pathlib import Path; import os,sys; "
        "story=Path(sys.argv[1]); approval=Path(sys.argv[2]); "
        "assert story.read_bytes()==b'{}\\n'; "
        "blocked=0; "
        "\ntry: story.write_bytes(b'x')\nexcept PermissionError: blocked+=1\n"
        "try: story.unlink()\nexcept PermissionError: blocked+=1\n"
        "approval.write_bytes(b'{}\\n'); os.chmod(approval,0o640); "
        "raise SystemExit(0 if blocked==2 else 9)"
    )

    def demote() -> None:
        os.setgroups([reviewer.pw_gid])
        os.setgid(reviewer.pw_gid)
        os.setuid(reviewer.pw_uid)

    completed = subprocess.run(
        [sys.executable, "-c", script, str(story), str(approval)],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        preexec_fn=demote,
    )
    assert completed.returncode == 0
    assert story.read_bytes() == b"{}\n"
    assert approval.read_bytes() == b"{}\n"
    assert approval.lstat().st_uid == reviewer.pw_uid
    assert approval.lstat().st_gid == reviewer.pw_gid
    assert _mode(approval) == 0o640
