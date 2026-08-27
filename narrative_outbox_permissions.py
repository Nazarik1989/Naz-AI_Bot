"""Closed, versioned filesystem policy for Narrative Normalizer outbox data."""
from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final


PRIVATE_POLICY_VERSION: Final = "private-v1"
SHARED_REVIEW_POLICY_VERSION: Final = "shared-review-v1"
POLICY_VERSIONS: Final = frozenset({
    PRIVATE_POLICY_VERSION,
    SHARED_REVIEW_POLICY_VERSION,
})

PRIVATE_DIRECTORY_MODE: Final = 0o700
PRIVATE_FILE_MODE: Final = 0o600
SHARED_ROOT_MODE: Final = 0o2750
SHARED_DRAFT_DIRECTORY_MODE: Final = 0o3770
SHARED_DRAFT_FILE_MODE: Final = 0o640
SHARED_STATE_MODE: Final = 0o2750
SHARED_LOCK_DIRECTORY_MODE: Final = 0o3770
SHARED_LOCK_FILE_MODE: Final = 0o660
SHARED_CLAIM_DIRECTORY_MODE: Final = 0o2750
SHARED_CLAIM_FILE_MODE: Final = 0o640

DRAFT_FILE_NAMES: Final = (
    "story.md",
    "story.json",
    "draft-manifest.json",
    "review.json",
)
MANUAL_ATTENTION_FILE_NAMES: Final = (
    "manual-attention.json",
    "manual-attention.md",
)
APPROVAL_FILE_NAMES: Final = (
    "approval-attestation.json",
    "narrative_ready.json",
)

_GROUP_NAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}")


class OutboxPermissionError(RuntimeError):
    """Privacy-safe failure at the outbox permission boundary."""

    reason_code = "narrative_outbox_permission_invalid"

    def __init__(self) -> None:
        super().__init__(self.reason_code)


def _fail() -> None:
    raise OutboxPermissionError() from None


@dataclass(frozen=True, slots=True)
class NarrativeOutboxPermissionPolicy:
    """Immutable policy value; no environment or process state is captured."""

    version: str
    shared_gid: int | None = None

    def __post_init__(self) -> None:
        if type(self.version) is not str or self.version not in POLICY_VERSIONS:
            raise TypeError("version")
        if self.version == PRIVATE_POLICY_VERSION:
            if self.shared_gid is not None:
                raise TypeError("shared_gid")
        elif type(self.shared_gid) is not int or self.shared_gid < 0:
            raise TypeError("shared_gid")

    @property
    def shared(self) -> bool:
        return self.version == SHARED_REVIEW_POLICY_VERSION

    @property
    def draft_file_mode(self) -> int:
        return SHARED_DRAFT_FILE_MODE if self.shared else PRIVATE_FILE_MODE

    @property
    def claim_file_mode(self) -> int:
        return SHARED_CLAIM_FILE_MODE if self.shared else PRIVATE_FILE_MODE

    @property
    def lock_file_mode(self) -> int:
        return SHARED_LOCK_FILE_MODE if self.shared else PRIVATE_FILE_MODE


PRIVATE_POLICY: Final = NarrativeOutboxPermissionPolicy(PRIVATE_POLICY_VERSION)


def resolve_permission_policy(
    version: str,
    shared_group: str | None,
) -> NarrativeOutboxPermissionPolicy:
    """Resolve one explicit CLI policy without accepting environment fallback."""

    if type(version) is not str or version not in POLICY_VERSIONS:
        _fail()
    if version == PRIVATE_POLICY_VERSION:
        if shared_group is not None:
            _fail()
        return PRIVATE_POLICY
    if (
        os.name != "posix"
        or type(shared_group) is not str
        or _GROUP_NAME.fullmatch(shared_group) is None
    ):
        _fail()
    try:
        import grp

        group = grp.getgrnam(shared_group)
    except (ImportError, KeyError, OSError):
        _fail()
    gid = group.gr_gid
    if type(gid) is not int or gid < 0:
        _fail()
    return NarrativeOutboxPermissionPolicy(SHARED_REVIEW_POLICY_VERSION, gid)


def _require_shared(policy: NarrativeOutboxPermissionPolicy) -> tuple[int, int]:
    if type(policy) is not NarrativeOutboxPermissionPolicy or not policy.shared:
        _fail()
    if os.name != "posix" or not hasattr(os, "geteuid") or not hasattr(os, "chown"):
        _fail()
    assert policy.shared_gid is not None
    return os.geteuid(), policy.shared_gid


def _lstat(path: Path, *, directory: bool) -> os.stat_result:
    absolute = Path(os.path.abspath(path))
    for component in (absolute, *absolute.parents):
        try:
            component_value = component.lstat()
        except OSError:
            _fail()
        if stat.S_ISLNK(component_value.st_mode):
            _fail()
    try:
        value = path.lstat()
    except OSError:
        _fail()
    if stat.S_ISLNK(value.st_mode):
        _fail()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(value.st_mode):
        _fail()
    return value


def _verify(path: Path, *, directory: bool, uid: int, gid: int, mode: int) -> None:
    value = _lstat(path, directory=directory)
    if value.st_uid != uid or value.st_gid != gid or stat.S_IMODE(value.st_mode) != mode:
        _fail()


def _apply(path: Path, *, directory: bool, uid: int, gid: int, mode: int) -> None:
    _lstat(path, directory=directory)
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if directory:
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(opened.st_mode):
            _fail()
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except (OSError, NotImplementedError, TypeError):
        _fail()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _verify(path, directory=directory, uid=uid, gid=gid, mode=mode)


def finalize_shared_root(policy: NarrativeOutboxPermissionPolicy, root: Path) -> int:
    uid, gid = _require_shared(policy)
    _apply(root, directory=True, uid=uid, gid=gid, mode=SHARED_ROOT_MODE)
    return uid


def verify_shared_root(policy: NarrativeOutboxPermissionPolicy, root: Path) -> int:
    _, gid = _require_shared(policy)
    value = _lstat(root, directory=True)
    _verify(root, directory=True, uid=value.st_uid, gid=gid, mode=SHARED_ROOT_MODE)
    return value.st_uid


def finalize_shared_internal_layout(
    policy: NarrativeOutboxPermissionPolicy,
    root: Path,
    state: Path,
    locks: Path,
    claims: Path,
) -> None:
    uid = finalize_shared_root(policy, root)
    assert policy.shared_gid is not None
    for path, mode in (
        (state, SHARED_STATE_MODE),
        (locks, SHARED_LOCK_DIRECTORY_MODE),
        (claims, SHARED_CLAIM_DIRECTORY_MODE),
    ):
        _apply(path, directory=True, uid=uid, gid=policy.shared_gid, mode=mode)


def finalize_shared_draft(
    policy: NarrativeOutboxPermissionPolicy,
    root: Path,
    draft: Path,
) -> None:
    uid = verify_shared_root(policy, root)
    assert policy.shared_gid is not None
    for name in DRAFT_FILE_NAMES:
        _apply(
            draft / name,
            directory=False,
            uid=uid,
            gid=policy.shared_gid,
            mode=SHARED_DRAFT_FILE_MODE,
        )
    _apply(
        draft,
        directory=True,
        uid=uid,
        gid=policy.shared_gid,
        mode=SHARED_DRAFT_DIRECTORY_MODE,
    )


def verify_shared_draft(
    policy: NarrativeOutboxPermissionPolicy,
    root: Path,
    draft: Path,
) -> None:
    uid = verify_shared_root(policy, root)
    assert policy.shared_gid is not None
    _verify(
        draft,
        directory=True,
        uid=uid,
        gid=policy.shared_gid,
        mode=SHARED_DRAFT_DIRECTORY_MODE,
    )
    for name in DRAFT_FILE_NAMES:
        _verify(
            draft / name,
            directory=False,
            uid=uid,
            gid=policy.shared_gid,
            mode=SHARED_DRAFT_FILE_MODE,
        )


def finalize_shared_manual_attention(
    policy: NarrativeOutboxPermissionPolicy,
    root: Path,
    package: Path,
) -> None:
    uid = verify_shared_root(policy, root)
    assert policy.shared_gid is not None
    for name in MANUAL_ATTENTION_FILE_NAMES:
        _apply(package / name, directory=False, uid=uid, gid=policy.shared_gid, mode=SHARED_DRAFT_FILE_MODE)
    _apply(package, directory=True, uid=uid, gid=policy.shared_gid, mode=SHARED_DRAFT_DIRECTORY_MODE)


def verify_shared_manual_attention(
    policy: NarrativeOutboxPermissionPolicy,
    root: Path,
    package: Path,
) -> None:
    uid = verify_shared_root(policy, root)
    assert policy.shared_gid is not None
    _verify(package, directory=True, uid=uid, gid=policy.shared_gid, mode=SHARED_DRAFT_DIRECTORY_MODE)
    for name in MANUAL_ATTENTION_FILE_NAMES:
        _verify(package / name, directory=False, uid=uid, gid=policy.shared_gid, mode=SHARED_DRAFT_FILE_MODE)


def finalize_shared_claim(
    policy: NarrativeOutboxPermissionPolicy,
    root: Path,
    claim: Path,
) -> None:
    uid = verify_shared_root(policy, root)
    assert policy.shared_gid is not None
    _apply(
        claim,
        directory=False,
        uid=uid,
        gid=policy.shared_gid,
        mode=SHARED_CLAIM_FILE_MODE,
    )


def finalize_shared_approval_file(
    policy: NarrativeOutboxPermissionPolicy,
    target: Path,
) -> None:
    uid, gid = _require_shared(policy)
    if target.name not in APPROVAL_FILE_NAMES:
        _fail()
    _apply(
        target,
        directory=False,
        uid=uid,
        gid=gid,
        mode=SHARED_DRAFT_FILE_MODE,
    )


def verify_shared_approval_file(
    policy: NarrativeOutboxPermissionPolicy,
    target: Path,
) -> None:
    _, gid = _require_shared(policy)
    if target.name not in APPROVAL_FILE_NAMES:
        _fail()
    value = _lstat(target, directory=False)
    _verify(
        target,
        directory=False,
        uid=value.st_uid,
        gid=gid,
        mode=SHARED_DRAFT_FILE_MODE,
    )
