"""Standalone append-only Review Authority Broker core.

The broker is the sole owner of the trust service.  Callers submit identities,
digests, and canonical manifests; they never receive a signing capability.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterable, Iterator, Mapping

import narrative_normalizer_review_state as state
import narrative_normalizer_trust as trust
import narrative_review_authority_protocol as protocol


BROKER_CONTRACT_VERSION = "narrative-review-authority-v2"
STORAGE_ADAPTER_VERSION = "narrative-review-authority-state-adapter-v2"
PREPARED_APPROVAL_VERSION = "narrative-review-authority-prepared-approval-v2"
VERIFY_READY_VERSION = "narrative-review-authority-ready-verdict-v2"
NARRATIVE_OUTBOX_LAYOUT_VERSION = "normalizer-outbox-source-identity-v1"
MAX_NARRATIVE_PACKAGE_BYTES = 2_000_000

AUTHORITY_INVALID = "review_authority_invalid"
AUTHORITY_PATH_INVALID = "review_authority_path_invalid"
AUTHORITY_STATE_MISSING = "review_authority_state_missing"
AUTHORITY_STATE_CONFLICT = "review_authority_state_conflict"
AUTHORITY_TRANSITION_INVALID = "review_authority_transition_invalid"
AUTHORITY_PERSISTENCE_INVALID = "review_authority_persistence_invalid"
AUTHORITY_ATTESTATION_INVALID = "review_authority_attestation_invalid"
AUTHORITY_NOT_READY = "review_authority_not_ready"
AUTHORITY_INTERNAL = "review_authority_internal_error"

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_EVENT_FILE = re.compile(r"(?P<revision>[0-9]{8})-(?P<digest>[0-9a-f]{64})\.json\Z")
_DRAFT_KEYS = frozenset({
    "source_identity", "source_ref", "source_digest", "draft_identity",
    "draft_package_digest", "story_markdown_digest", "draft_manifest_digest",
    "review_digest", "completed_claim_digest", "artifact_binding_digest",
    "contract_versions", "operator_request_id", "timestamp",
})
_REVIEW_KEYS = frozenset({
    "source_identity", "draft_identity", "draft_package_digest",
    "new_state", "operator_request_id",
    "reason_codes", "timestamp", "expected_revision", "expected_event_digest",
})
_PREPARE_KEYS = frozenset({
    "source_identity", "source_ref", "source_digest", "draft_identity",
    "draft_package_digest", "narrative_package_digest",
    "narrative_ready_manifest_digest", "story_markdown_digest",
    "draft_manifest_digest", "review_digest", "completed_claim_digest",
    "artifact_binding_digest", "ready_manifest_contract", "source_contract",
    "draft_contract_versions",
    "operator_request_id", "timestamp", "expected_revision", "expected_event_digest",
})
_COMMIT_KEYS = frozenset({
    "prepared", "ready_manifest", "ready_manifest_digest", "attestation_digest",
    "draft_package_digest", "narrative_package_digest",
})
_VERIFY_KEYS = frozenset({"ready_manifest", "attestation"})
_LATEST_KEYS = frozenset({"source_identity", "draft_identity"})
_READY_KEYS = frozenset({
    "schema_version", "source_ref", "source_digest", "narrative_package_ref",
    "narrative_package_digest", "status", "contract_versions",
})


class AuthorityError(ValueError):
    """Privacy-safe broker error carrying only a stable code."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _fail(reason: str = AUTHORITY_INVALID) -> None:
    raise AuthorityError(reason)


def _keys(value: object, expected: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != expected:
        _fail()
    protocol.exact_json(value)
    return value


def _hex(value: object) -> str:
    if type(value) is not str or _HEX64.fullmatch(value) is None:
        _fail()
    return value


def _text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value.encode("utf-8")) > 4096:
        _fail()
    return value


def _source_ref(value: object) -> str:
    text = _text(value)
    if "\\" in text:
        _fail()
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        _fail()
    return text


def _safe(value: object) -> str:
    if type(value) is not str or _SAFE.fullmatch(value) is None:
        _fail()
    return value


def _timestamp(value: object) -> str:
    text = _text(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _fail()
    if parsed.tzinfo is None:
        _fail()
    return text


def _digest(value: object) -> str:
    return hashlib.sha256(protocol.canonical(value)).hexdigest()


def _canonical_file_digest(value: object) -> str:
    return hashlib.sha256(protocol.canonical(value) + b"\n").hexdigest()


def _contracts(value: object) -> dict[str, str]:
    if type(value) is not dict or not value or len(value) > 16:
        _fail()
    result: dict[str, str] = {}
    for key, item in value.items():
        result[_safe(key)] = _text(item)
    if list(value) != sorted(value):
        _fail()
    return result


def _reasons(value: object) -> tuple[str, ...]:
    if type(value) is not list:
        _fail()
    result = tuple(_safe(item) for item in value)
    if result != tuple(sorted(set(result))):
        _fail()
    return result


def _ready_manifest(value: object, *, contract: str) -> dict[str, object]:
    ready = _keys(value, _READY_KEYS)
    if (
        _text(ready["schema_version"]) != _text(contract)
        or _source_ref(ready["source_ref"]) != ready["source_ref"]
        or _hex(ready["source_digest"]) != ready["source_digest"]
        or _source_ref(ready["narrative_package_ref"]) != ready["narrative_package_ref"]
        or _hex(ready["narrative_package_digest"]) != ready["narrative_package_digest"]
        or type(ready["status"]) is not str
        or ready["status"] != "narrative_ready"
    ):
        _fail(AUTHORITY_ATTESTATION_INVALID)
    contracts = _contracts(ready["contract_versions"])
    if frozenset(contracts) != {"director", "narrative"}:
        _fail(AUTHORITY_ATTESTATION_INVALID)
    return ready


def _json_file(path: Path) -> object:
    try:
        if path.is_symlink() or not path.is_file():
            _fail(AUTHORITY_PERSISTENCE_INVALID)
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if raw != protocol.canonical(value) + b"\n":
            _fail(AUTHORITY_PERSISTENCE_INVALID)
        return value
    except AuthorityError:
        raise
    except Exception:
        raise AuthorityError(AUTHORITY_PERSISTENCE_INVALID) from None


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _no_clobber(path: Path, data: bytes) -> bool:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            _fail(AUTHORITY_STATE_CONFLICT)
        return True
    try:
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    if os.name != "nt":
        os.chmod(path, 0o600)
    _fsync_dir(path.parent)
    if path.read_bytes() != data:
        _fail(AUTHORITY_PERSISTENCE_INVALID)
    return False


def _validate_absolute_path(value: str | os.PathLike[str], *, allow_missing: bool) -> Path:
    if not isinstance(value, (str, os.PathLike)) or isinstance(value, bytes):
        _fail(AUTHORITY_PATH_INVALID)
    path = Path(value)
    if not path.is_absolute():
        _fail(AUTHORITY_PATH_INVALID)
    cursor = path
    while True:
        if os.path.lexists(cursor) and cursor.is_symlink():
            _fail(AUTHORITY_PATH_INVALID)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    try:
        resolved = path.resolve(strict=not allow_missing)
    except (OSError, RuntimeError):
        raise AuthorityError(AUTHORITY_PATH_INVALID) from None
    return resolved


def validate_authority_root(
    authority_root: str | os.PathLike[str], *,
    git_root: str | os.PathLike[str] | None = None,
    protected_roots: Iterable[str | os.PathLike[str]] = (),
) -> Path:
    root = _validate_absolute_path(authority_root, allow_missing=True)
    blocked: list[Path] = []
    if git_root is not None:
        blocked.append(_validate_absolute_path(git_root, allow_missing=False))
    blocked.extend(_validate_absolute_path(item, allow_missing=True) for item in protected_roots)
    for other in blocked:
        if root == other or root in other.parents or other in root.parents:
            _fail(AUTHORITY_PATH_INVALID)
    cursor = root
    while cursor != cursor.parent:
        if (cursor / ".git").exists():
            _fail(AUTHORITY_PATH_INVALID)
        cursor = cursor.parent
    return root


def validate_narrative_outbox_root(
    narrative_outbox_root: str | os.PathLike[str], *,
    authority_root: str | os.PathLike[str],
    git_root: str | os.PathLike[str] | None = None,
    protected_roots: Iterable[str | os.PathLike[str]] = (),
) -> Path:
    root = _validate_absolute_path(narrative_outbox_root, allow_missing=False)
    authority = _validate_absolute_path(authority_root, allow_missing=True)
    blocked = [authority]
    if git_root is not None:
        blocked.append(_validate_absolute_path(git_root, allow_missing=False))
    blocked.extend(_validate_absolute_path(item, allow_missing=True) for item in protected_roots)
    if root.is_symlink() or not root.is_dir():
        _fail(AUTHORITY_PATH_INVALID)
    for other in blocked:
        if root == other or root in other.parents or other in root.parents:
            _fail(AUTHORITY_PATH_INVALID)
    cursor = root
    while cursor != cursor.parent:
        if (cursor / ".git").exists():
            _fail(AUTHORITY_PATH_INVALID)
        cursor = cursor.parent
    return root


class NarrativePackageReader:
    """Read-only resolver for the reviewed source-identity outbox layout."""

    __slots__ = ("root",)

    def __init__(
        self, narrative_outbox_root: str | os.PathLike[str], *,
        authority_root: str | os.PathLike[str],
        git_root: str | os.PathLike[str] | None = None,
        protected_roots: Iterable[str | os.PathLike[str]] = (),
    ):
        self.root = validate_narrative_outbox_root(
            narrative_outbox_root,
            authority_root=authority_root,
            git_root=git_root,
            protected_roots=protected_roots,
        )

    @staticmethod
    def _no_symlink_components(path: Path) -> None:
        cursor = path
        while True:
            if os.path.lexists(cursor) and cursor.is_symlink():
                _fail(AUTHORITY_PERSISTENCE_INVALID)
            if cursor == cursor.parent:
                break
            cursor = cursor.parent

    def digest(self, source_identity: str, draft_identity: str) -> str:
        source = _hex(source_identity)
        _hex(draft_identity)
        source_root = self.root / source
        story_path = source_root / "story.json"
        descriptor = -1
        try:
            self._no_symlink_components(story_path)
            root_info = os.lstat(self.root)
            source_info = os.lstat(source_root)
            story_info = os.lstat(story_path)
            if (
                not stat.S_ISDIR(root_info.st_mode)
                or not stat.S_ISDIR(source_info.st_mode)
                or stat.S_ISLNK(source_info.st_mode)
                or not stat.S_ISREG(story_info.st_mode)
                or stat.S_ISLNK(story_info.st_mode)
                or source_root.name != source
                or source_root.parent.resolve(strict=True) != self.root.resolve(strict=True)
                or story_path.name != "story.json"
                or story_info.st_size < 1
                or story_info.st_size > MAX_NARRATIVE_PACKAGE_BYTES
            ):
                _fail(AUTHORITY_PERSISTENCE_INVALID)
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(story_path, flags)
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != story_info.st_size
                or (before.st_dev, before.st_ino) != (story_info.st_dev, story_info.st_ino)
            ):
                _fail(AUTHORITY_PERSISTENCE_INVALID)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65_536, MAX_NARRATIVE_PACKAGE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_NARRATIVE_PACKAGE_BYTES:
                    _fail(AUTHORITY_PERSISTENCE_INVALID)
            after = os.fstat(descriptor)
            final_info = os.lstat(story_path)
            if (
                total != before.st_size
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                or (final_info.st_dev, final_info.st_ino, final_info.st_size)
                != (before.st_dev, before.st_ino, before.st_size)
                or not stat.S_ISREG(final_info.st_mode)
                or stat.S_ISLNK(final_info.st_mode)
            ):
                _fail(AUTHORITY_PERSISTENCE_INVALID)
            raw = b"".join(chunks)
            parsed = json.loads(raw.decode("utf-8"))
            if raw != protocol.canonical(parsed) + b"\n":
                _fail(AUTHORITY_PERSISTENCE_INVALID)
            return hashlib.sha256(raw).hexdigest()
        except AuthorityError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, protocol.ProtocolError):
            raise AuthorityError(AUTHORITY_PERSISTENCE_INVALID) from None
        finally:
            if descriptor >= 0:
                os.close(descriptor)


class _ProcessFileLock:
    def __init__(self, path: Path):
        self.path = path
        self.descriptor = -1

    def __enter__(self) -> "_ProcessFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            _fail(AUTHORITY_PERSISTENCE_INVALID)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name != "nt":
                os.chmod(self.path, 0o600)
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            else:
                import msvcrt
                if os.fstat(descriptor).st_size == 0:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    os.write(descriptor, b"0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        except BaseException:
            os.close(descriptor)
            raise
        self.descriptor = descriptor
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        descriptor = self.descriptor
        self.descriptor = -1
        if descriptor < 0:
            return
        if os.name != "nt":
            import fcntl
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        else:
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        os.close(descriptor)


class ReviewStateAdapter:
    """Versioned drafted-only adapter over reviewed event/HMAC contracts."""

    version = STORAGE_ADAPTER_VERSION

    def __init__(self, root: Path, service: trust.NarrativeTrustService):
        if not isinstance(root, Path) or type(service) is not trust.NarrativeTrustService:
            _fail()
        self.root = root
        self.service = service
        self._thread_locks_guard = threading.Lock()
        self._thread_locks: dict[str, threading.RLock] = {}

    def _source(self, identity: str) -> Path:
        return self.root / _hex(identity)

    def _events(self, identity: str) -> Path:
        return self._source(identity) / "events"

    @contextmanager
    def locked(self, identity: str) -> Iterator[None]:
        identity = _hex(identity)
        with self._thread_locks_guard:
            lock = self._thread_locks.setdefault(identity, threading.RLock())
        with lock:
            with _ProcessFileLock(self.root / ".locks" / f"{identity}.lock"):
                yield

    def _layout(self, identity: str) -> Path:
        for path in (self.root, self.root / ".locks", self._source(identity), self._events(identity)):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink() or not path.is_dir():
                _fail(AUTHORITY_PERSISTENCE_INVALID)
            if os.name != "nt":
                os.chmod(path, 0o700)
        return self._events(identity)

    def read(self, identity: str, draft_identity: str | None = None) -> state.ReviewLedger:
        identity = _hex(identity)
        events = self._events(identity)
        if not os.path.lexists(events):
            _fail(AUTHORITY_STATE_MISSING)
        try:
            indexed: list[tuple[int, str, Path]] = []
            if events.is_symlink() or not events.is_dir():
                _fail(AUTHORITY_PERSISTENCE_INVALID)
            for path in events.iterdir():
                match = _EVENT_FILE.fullmatch(path.name)
                if match is None:
                    _fail(AUTHORITY_PERSISTENCE_INVALID)
                indexed.append((int(match.group("revision")), match.group("digest"), path))
            indexed.sort()
            if [item[0] for item in indexed] != list(range(1, len(indexed) + 1)):
                _fail(AUTHORITY_PERSISTENCE_INVALID)
            parsed: list[state.ReviewEvent] = []
            for revision, digest, path in indexed:
                event = state.review_event_from_payload(_json_file(path), self.service)
                if (
                    event.revision != revision
                    or event.event_digest != digest
                    or event.source_identity != identity
                    or event.draft_package_digest is None
                ):
                    _fail(AUTHORITY_PERSISTENCE_INVALID)
                if parsed and (
                    event.previous_revision != parsed[-1].revision
                    or event.previous_event_digest != parsed[-1].event_digest
                    or event.draft_package_digest != parsed[-1].draft_package_digest
                ):
                    _fail(AUTHORITY_PERSISTENCE_INVALID)
                parsed.append(event)
            if not parsed:
                _fail(AUTHORITY_STATE_MISSING)
            ledger = state.ReviewLedger(identity, parsed[0].draft_identity, tuple(parsed))
            # Reuse the reviewed parser as the canonical transition/chain check.
            ledger = state.review_ledger_from_payload(ledger.to_payload(), self.service)
            if draft_identity is not None and ledger.draft_identity != _hex(draft_identity):
                _fail(AUTHORITY_STATE_CONFLICT)
            return ledger
        except AuthorityError:
            raise
        except state.ReviewStateError:
            raise AuthorityError(AUTHORITY_PERSISTENCE_INVALID) from None
        except Exception:
            raise AuthorityError(AUTHORITY_PERSISTENCE_INVALID) from None

    def append(self, event: state.ReviewEvent) -> tuple[state.ReviewLedger, bool]:
        if type(event) is not state.ReviewEvent or event.draft_package_digest is None:
            _fail()
        with self.locked(event.source_identity):
            self._layout(event.source_identity)
            try:
                ledger = self.read(event.source_identity)
            except AuthorityError as error:
                if error.reason_code != AUTHORITY_STATE_MISSING or event.revision != 1:
                    raise
                ledger = None
            if ledger is None:
                if event.previous_revision != 0 or event.previous_event_digest != "" or event.state != state.STATE_DRAFTED:
                    _fail(AUTHORITY_STATE_CONFLICT)
            else:
                for old in ledger.events:
                    if old.operator_request_id == event.operator_request_id:
                        if old == event or (
                            old.state == event.state and old.reason_codes == event.reason_codes
                            and old.action_digest == event.action_digest
                        ):
                            return ledger, True
                        _fail(AUTHORITY_STATE_CONFLICT)
                if (
                    event.draft_identity != ledger.draft_identity
                    or event.draft_package_digest != ledger.latest.draft_package_digest
                    or event.revision != ledger.latest.revision + 1
                    or event.previous_revision != ledger.latest.revision
                    or event.previous_event_digest != ledger.latest.event_digest
                ):
                    _fail(AUTHORITY_STATE_CONFLICT)
            target = self._events(event.source_identity) / f"{event.revision:08d}-{event.event_digest}.json"
            duplicate = _no_clobber(target, protocol.canonical(event.to_payload()) + b"\n")
            final = self.read(event.source_identity, event.draft_identity)
            # head is a diagnostic cache only; an interrupted update is harmless.
            head = self._source(event.source_identity) / "head.json"
            encoded = protocol.canonical(final.to_payload()) + b"\n"
            staging = head.with_name(f".head-{os.getpid()}-{threading.get_ident()}.tmp")
            try:
                _no_clobber(staging, encoded)
                os.replace(staging, head)
                _fsync_dir(head.parent)
            except Exception:
                try:
                    if os.path.lexists(staging) and not staging.is_symlink():
                        os.unlink(staging)
                except Exception:
                    pass
            return final, duplicate


@dataclass
class _RequestRecord:
    digest: str
    done: bool = False
    response: protocol.Response | None = None


class ReviewAuthority:
    """Role-gated broker core.  This object alone contains the trust service."""

    __slots__ = ("__service", "_packages", "_store", "_requests", "_request_condition")

    def __init__(
        self, authority_root: Path, service: trust.NarrativeTrustService, *,
        narrative_outbox_root: Path,
        git_root: Path | None = None,
        protected_roots: Iterable[Path] = (),
    ):
        if not isinstance(authority_root, Path) or type(service) is not trust.NarrativeTrustService:
            _fail()
        object.__setattr__(self, "_ReviewAuthority__service", service)
        self._packages = NarrativePackageReader(
            narrative_outbox_root,
            authority_root=authority_root,
            git_root=git_root,
            protected_roots=protected_roots,
        )
        self._store = ReviewStateAdapter(authority_root, service)
        self._requests: dict[str, _RequestRecord] = {}
        self._request_condition = threading.Condition()

    @property
    def key_id(self) -> str:
        return self.__service.key_id

    def _event_result(self, ledger: state.ReviewLedger, idempotent: bool) -> dict[str, object]:
        latest = ledger.latest
        return {
            "source_identity": ledger.source_identity,
            "draft_identity": ledger.draft_identity,
            "draft_package_digest": latest.draft_package_digest,
            "revision": latest.revision,
            "state": latest.state,
            "event_digest": latest.event_digest,
            "event": latest.to_payload(),
            "idempotent": idempotent,
            "storage_adapter_version": STORAGE_ADAPTER_VERSION,
        }

    def handle(self, role: str, request: protocol.Request) -> protocol.Response:
        request_digest = _digest({"role": role, **request.to_payload()})
        with self._request_condition:
            old = self._requests.get(request.request_id)
            if old is not None:
                if old.digest != request_digest:
                    return protocol.make_error(request.request_id, protocol.REQUEST_CONFLICT)
                while not old.done:
                    self._request_condition.wait()
                assert old.response is not None
                return old.response
            record = _RequestRecord(request_digest)
            self._requests[request.request_id] = record
        try:
            protocol.require_capability(role, request.operation)
            result = self._dispatch(request.operation, request.payload)
            response = protocol.Response(request.request_id, True, result, None)
        except AuthorityError as error:
            response = protocol.make_error(request.request_id, error.reason_code)
        except protocol.ProtocolError as error:
            response = protocol.make_error(request.request_id, error.reason_code)
        except (state.ReviewStateError, trust.TrustError):
            response = protocol.make_error(request.request_id, AUTHORITY_INVALID)
        except Exception:
            response = protocol.make_error(request.request_id, AUTHORITY_INTERNAL)
        except BaseException:
            # Cancellation must propagate, but the reserved request ID must not
            # remain permanently pending for concurrent/exact duplicates.
            with self._request_condition:
                record.response = protocol.make_error(request.request_id, AUTHORITY_INTERNAL)
                record.done = True
                self._request_condition.notify_all()
            raise
        with self._request_condition:
            record.response = response
            record.done = True
            self._request_condition.notify_all()
        return response

    def _dispatch(self, operation: str, payload: dict[str, object]) -> dict[str, object]:
        if operation == protocol.OP_HEALTH:
            _keys(payload, frozenset())
            return {
                "status": "ok",
                "contract_version": BROKER_CONTRACT_VERSION,
                "narrative_outbox_layout_version": NARRATIVE_OUTBOX_LAYOUT_VERSION,
                "key_id": self.key_id,
            }
        if operation == protocol.OP_REGISTER_DRAFT:
            return self._register(payload)
        if operation == protocol.OP_LATEST_STATE:
            return self._latest(payload)
        if operation == protocol.OP_APPEND_REVIEW:
            return self._review(payload)
        if operation == protocol.OP_PREPARE_APPROVAL:
            return self._prepare(payload)
        if operation == protocol.OP_COMMIT_APPROVAL:
            return self._commit(payload)
        if operation == protocol.OP_VERIFY_READY:
            return self._verify(payload)
        _fail()

    def _draft_binding(self, value: Mapping[str, object]) -> dict[str, object]:
        contracts = _contracts(value["contract_versions"])
        if frozenset(contracts) != {"draft", "source"} or contracts["draft"] != "normalizer-draft-identity-v1":
            _fail()
        source_ref = _source_ref(value["source_ref"])
        source_digest = _hex(value["source_digest"])
        expected_source = hashlib.sha256(
            source_ref.encode("utf-8") + b"\0" + source_digest.encode("ascii")
            + b"\0" + contracts["source"].encode("utf-8")
        ).hexdigest()
        source_identity = _hex(value["source_identity"])
        draft_package_digest = _hex(value["draft_package_digest"])
        expected_draft = _digest({
            "version": contracts["draft"],
            "source_identity": source_identity,
            "package_digest": draft_package_digest,
        })
        if source_identity != expected_source or _hex(value["draft_identity"]) != expected_draft:
            _fail()
        return {
            "version": "narrative-review-authority-draft-binding-v1",
            "source_identity": source_identity,
            "source_ref": source_ref,
            "source_digest": source_digest,
            "draft_identity": expected_draft,
            "draft_package_digest": draft_package_digest,
            "story_markdown_digest": _hex(value["story_markdown_digest"]),
            "draft_manifest_digest": _hex(value["draft_manifest_digest"]),
            "review_digest": _hex(value["review_digest"]),
            "completed_claim_digest": _hex(value["completed_claim_digest"]),
            "artifact_binding_digest": _hex(value["artifact_binding_digest"]),
            "contract_versions": contracts,
        }

    def _register(self, payload: dict[str, object]) -> dict[str, object]:
        value = _keys(payload, _DRAFT_KEYS)
        binding = self._draft_binding(value)
        source = str(binding["source_identity"])
        draft = str(binding["draft_identity"])
        operator = _safe(value["operator_request_id"])
        when = _timestamp(value["timestamp"])
        event = state.build_review_event(
            self.__service, revision=1, previous_revision=0,
            source_identity=source, draft_identity=draft,
            draft_package_digest=str(binding["draft_package_digest"]),
            state=state.STATE_DRAFTED,
            operator_request_id=operator, reason_codes=(), timestamp=when,
            previous_event_digest="", action_digest=_digest(binding),
        )
        ledger, idem = self._store.append(event)
        result = self._event_result(ledger, idem)
        result["draft_binding_digest"] = event.action_digest
        return result

    def _latest(self, payload: dict[str, object]) -> dict[str, object]:
        value = _keys(payload, _LATEST_KEYS)
        ledger = self._store.read(_hex(value["source_identity"]), _hex(value["draft_identity"]))
        return self._event_result(ledger, True)

    def _review(self, payload: dict[str, object]) -> dict[str, object]:
        value = _keys(payload, _REVIEW_KEYS)
        source = _hex(value["source_identity"])
        draft = _hex(value["draft_identity"])
        draft_package_digest = _hex(value["draft_package_digest"])
        if draft != _digest({
            "version": "normalizer-draft-identity-v1",
            "source_identity": source,
            "package_digest": draft_package_digest,
        }):
            _fail(AUTHORITY_STATE_CONFLICT)
        new_state = value["new_state"]
        if type(new_state) is not str or new_state not in {
            state.STATE_PASSED, state.STATE_REJECTED, state.STATE_SUPERSEDED,
        }:
            _fail(AUTHORITY_TRANSITION_INVALID)
        operator = _safe(value["operator_request_id"])
        reasons = _reasons(value["reason_codes"])
        when = _timestamp(value["timestamp"])
        expected_revision = value["expected_revision"]
        expected_digest = _hex(value["expected_event_digest"])
        if type(expected_revision) is not int or expected_revision < 1:
            _fail()
        ledger = self._store.read(source, draft)
        for old in ledger.events:
            if old.operator_request_id == operator:
                action = _digest({
                    "version": "narrative-review-authority-review-action-v2",
                    "source_identity": source,
                    "draft_identity": draft,
                    "draft_package_digest": draft_package_digest,
                    "state": new_state,
                    "operator_request_id": operator,
                    "reason_codes": list(reasons),
                })
                if old.state == new_state and old.reason_codes == reasons and old.action_digest == action:
                    return self._event_result(ledger, True)
                _fail(AUTHORITY_STATE_CONFLICT)
        if ledger.latest.revision != expected_revision or ledger.latest.event_digest != expected_digest:
            _fail(AUTHORITY_STATE_CONFLICT)
        allowed = (
            ledger.latest.state == state.STATE_DRAFTED and new_state in {state.STATE_PASSED, state.STATE_REJECTED}
        ) or (
            ledger.latest.state == state.STATE_PASSED and new_state in {state.STATE_REJECTED, state.STATE_SUPERSEDED}
        )
        if not allowed:
            _fail(AUTHORITY_TRANSITION_INVALID)
        event = state.build_review_event(
            self.__service, revision=ledger.latest.revision + 1,
            previous_revision=ledger.latest.revision, source_identity=source,
            draft_identity=draft, draft_package_digest=draft_package_digest,
            state=new_state, operator_request_id=operator,
            reason_codes=reasons, action_digest=_digest({
                "version": "narrative-review-authority-review-action-v2",
                "source_identity": source,
                "draft_identity": draft,
                "draft_package_digest": draft_package_digest,
                "state": new_state,
                "operator_request_id": operator,
                "reason_codes": list(reasons),
            }), timestamp=when, previous_event_digest=ledger.latest.event_digest,
        )
        final, idem = self._store.append(event)
        return self._event_result(final, idem)

    def _prepare(self, payload: dict[str, object]) -> dict[str, object]:
        value = _keys(payload, _PREPARE_KEYS)
        source = _hex(value["source_identity"])
        draft = _hex(value["draft_identity"])
        ledger = self._store.read(source, draft)
        expected_revision = value["expected_revision"]
        if type(expected_revision) is not int or expected_revision != ledger.latest.revision:
            _fail(AUTHORITY_STATE_CONFLICT)
        if _hex(value["expected_event_digest"]) != ledger.latest.event_digest:
            _fail(AUTHORITY_STATE_CONFLICT)
        if ledger.latest.state != state.STATE_PASSED:
            _fail(AUTHORITY_TRANSITION_INVALID)
        draft_binding = self._draft_binding({
            "source_identity": value["source_identity"],
            "source_ref": value["source_ref"],
            "source_digest": value["source_digest"],
            "draft_identity": value["draft_identity"],
            "draft_package_digest": value["draft_package_digest"],
            "story_markdown_digest": value["story_markdown_digest"],
            "draft_manifest_digest": value["draft_manifest_digest"],
            "review_digest": value["review_digest"],
            "completed_claim_digest": value["completed_claim_digest"],
            "artifact_binding_digest": value["artifact_binding_digest"],
            "contract_versions": value["draft_contract_versions"],
        })
        if (
            ledger.events[0].action_digest != _digest(draft_binding)
            or _text(value["source_contract"])
            != draft_binding["contract_versions"]["source"]
        ):
            _fail(AUTHORITY_STATE_CONFLICT)
        actual_narrative_package_digest = self._packages.digest(source, draft)
        if (
            _hex(value["narrative_package_digest"])
            != actual_narrative_package_digest
        ):
            _fail(AUTHORITY_ATTESTATION_INVALID)
        operator = _safe(value["operator_request_id"])
        when = _timestamp(value["timestamp"])
        action = _digest({
            "version": "narrative-review-authority-approval-action-v2",
            "source_identity": source, "draft_identity": draft,
            "operator_request_id": operator,
            "draft_package_digest": _hex(value["draft_package_digest"]),
            "narrative_package_digest": actual_narrative_package_digest,
            "artifact_binding_digest": _hex(value["artifact_binding_digest"]),
        })
        event = state.build_review_event(
            self.__service, revision=ledger.latest.revision + 1,
            previous_revision=ledger.latest.revision, source_identity=source,
            draft_identity=draft,
            draft_package_digest=_hex(value["draft_package_digest"]),
            state=state.STATE_APPROVED,
            operator_request_id=operator, reason_codes=(), timestamp=when,
            previous_event_digest=ledger.latest.event_digest, action_digest=action,
        )
        attestation = state.build_dual_digest_approval_attestation(
            self.__service, source_identity=source, source_ref=_text(value["source_ref"]),
            source_digest=_hex(value["source_digest"]), draft_identity=draft,
            draft_package_digest=_hex(value["draft_package_digest"]),
            narrative_package_digest=actual_narrative_package_digest,
            ready_manifest_digest=_hex(value["narrative_ready_manifest_digest"]),
            story_markdown_digest=_hex(value["story_markdown_digest"]),
            draft_manifest_digest=_hex(value["draft_manifest_digest"]),
            review_digest=_hex(value["review_digest"]),
            completed_claim_digest=_hex(value["completed_claim_digest"]),
            artifact_binding_digest=_hex(value["artifact_binding_digest"]),
            approved_event=event, ready_manifest_contract=_text(value["ready_manifest_contract"]),
            source_contract=_text(value["source_contract"]),
            draft_contract=_text(value["draft_contract_versions"]["draft"]),
        )
        prepared = {
            "schema_version": PREPARED_APPROVAL_VERSION,
            "event": event.to_payload(),
            "attestation": attestation.to_payload(),
            "prepared_identity": _digest({"event": event.to_payload(), "attestation": attestation.to_payload()}),
        }
        return {"prepared": prepared, "attestation_digest": _digest(attestation.to_payload()), "mutated": False}

    def _parse_prepared(
        self, value: object,
    ) -> tuple[state.ReviewEvent, state.DualDigestApprovalAttestation, dict[str, object]]:
        if type(value) is not dict or frozenset(value) != {
            "schema_version", "event", "attestation", "prepared_identity"
        } or value["schema_version"] != PREPARED_APPROVAL_VERSION:
            _fail(AUTHORITY_ATTESTATION_INVALID)
        event = state.review_event_from_payload(value["event"], self.__service)
        raw_attestation = value["attestation"]
        if type(raw_attestation) is not dict:
            _fail(AUTHORITY_ATTESTATION_INVALID)
        versions = raw_attestation.get("contract_versions")
        if type(versions) is not dict:
            _fail(AUTHORITY_ATTESTATION_INVALID)
        attestation = state.dual_digest_approval_attestation_from_payload(
            raw_attestation, self.__service,
            ready_manifest_contract=versions.get("ready_manifest"),
            source_contract=versions.get("source"),
            draft_contract=versions.get("draft"),
        )
        binding = self._draft_binding({
            "source_identity": attestation.source_identity,
            "source_ref": attestation.source_ref,
            "source_digest": attestation.source_digest,
            "draft_identity": attestation.draft_identity,
            "draft_package_digest": attestation.draft_package_digest,
            "story_markdown_digest": attestation.story_markdown_digest,
            "draft_manifest_digest": attestation.draft_manifest_digest,
            "review_digest": attestation.review_digest,
            "completed_claim_digest": attestation.completed_claim_digest,
            "artifact_binding_digest": attestation.artifact_binding_digest,
            "contract_versions": {
                "draft": versions["draft"], "source": versions["source"],
            },
        })
        expected_action = _digest({
            "version": "narrative-review-authority-approval-action-v2",
            "source_identity": attestation.source_identity,
            "draft_identity": attestation.draft_identity,
            "operator_request_id": attestation.approval_request_id,
            "draft_package_digest": attestation.draft_package_digest,
            "narrative_package_digest": attestation.narrative_package_digest,
            "artifact_binding_digest": attestation.artifact_binding_digest,
        })
        identity = _digest({"event": event.to_payload(), "attestation": attestation.to_payload()})
        if (
            value["prepared_identity"] != identity
            or event.state != state.STATE_APPROVED
            or event.source_identity != binding["source_identity"]
            or event.draft_identity != binding["draft_identity"]
            or event.draft_package_digest != binding["draft_package_digest"]
            or event.revision != attestation.review_revision
            or event.event_digest != attestation.review_event_digest
            or event.operator_request_id != attestation.approval_request_id
            or event.action_digest != expected_action
        ):
            _fail(AUTHORITY_ATTESTATION_INVALID)
        return event, attestation, value

    def _commit(self, payload: dict[str, object]) -> dict[str, object]:
        value = _keys(payload, _COMMIT_KEYS)
        event, attestation, prepared = self._parse_prepared(value["prepared"])
        actual_narrative_package_digest = self._packages.digest(
            attestation.source_identity, attestation.draft_identity,
        )
        ready = _ready_manifest(
            value["ready_manifest"],
            contract=dict(attestation.contract_versions)["ready_manifest"],
        )
        ready_digest = _canonical_file_digest(ready)
        if (
            _hex(value["draft_package_digest"]) != attestation.draft_package_digest
            or _hex(value["narrative_package_digest"]) != attestation.narrative_package_digest
            or actual_narrative_package_digest != attestation.narrative_package_digest
            or _hex(value["ready_manifest_digest"]) != ready_digest
            or ready_digest != attestation.narrative_ready_manifest_digest
            or _hex(value["attestation_digest"]) != _digest(attestation.to_payload())
            or ready.get("source_ref") != attestation.source_ref
            or ready.get("source_digest") != attestation.source_digest
            or ready.get("narrative_package_ref")
            != f"{attestation.source_identity}/story.json"
            or ready.get("narrative_package_digest") != attestation.narrative_package_digest
        ):
            _fail(AUTHORITY_ATTESTATION_INVALID)
        final, idem = self._store.append(event)
        if final.latest.state != state.STATE_APPROVED or final.latest.event_digest != event.event_digest:
            _fail(AUTHORITY_STATE_CONFLICT)
        return {
            **self._event_result(final, idem),
            "attestation": attestation.to_payload(),
            "attestation_digest": _digest(attestation.to_payload()),
            "prepared_identity": prepared["prepared_identity"],
        }

    def _verify(self, payload: dict[str, object]) -> dict[str, object]:
        value = _keys(payload, _VERIFY_KEYS)
        ready = value["ready_manifest"]
        attestation_raw = value["attestation"]
        if type(ready) is not dict or type(attestation_raw) is not dict:
            _fail(AUTHORITY_NOT_READY)
        versions = attestation_raw.get("contract_versions")
        if type(versions) is not dict:
            _fail(AUTHORITY_NOT_READY)
        try:
            attestation = state.dual_digest_approval_attestation_from_payload(
                attestation_raw, self.__service,
                ready_manifest_contract=versions.get("ready_manifest"),
                source_contract=versions.get("source"),
                draft_contract=versions.get("draft"),
            )
            ledger = self._store.read(attestation.source_identity, attestation.draft_identity)
            binding = self._draft_binding({
                "source_identity": attestation.source_identity,
                "source_ref": attestation.source_ref,
                "source_digest": attestation.source_digest,
                "draft_identity": attestation.draft_identity,
                "draft_package_digest": attestation.draft_package_digest,
                "story_markdown_digest": attestation.story_markdown_digest,
                "draft_manifest_digest": attestation.draft_manifest_digest,
                "review_digest": attestation.review_digest,
                "completed_claim_digest": attestation.completed_claim_digest,
                "artifact_binding_digest": attestation.artifact_binding_digest,
                "contract_versions": {
                    "draft": versions["draft"], "source": versions["source"],
                },
            })
            actual_narrative_package_digest = self._packages.digest(
                attestation.source_identity, attestation.draft_identity,
            )
        except Exception:
            _fail(AUTHORITY_NOT_READY)
        try:
            ready = _ready_manifest(
                ready,
                contract=dict(attestation.contract_versions)["ready_manifest"],
            )
            ready_digest = _canonical_file_digest(ready)
        except Exception:
            _fail(AUTHORITY_NOT_READY)
        valid = (
            ledger.latest.state == state.STATE_APPROVED
            and ledger.events[0].action_digest == _digest(binding)
            and all(
                event.draft_package_digest == attestation.draft_package_digest
                for event in ledger.events
            )
            and ledger.latest.revision == attestation.review_revision
            and ledger.latest.event_digest == attestation.review_event_digest
            and ready_digest == attestation.narrative_ready_manifest_digest
            and ready.get("source_ref") == attestation.source_ref
            and ready.get("source_digest") == attestation.source_digest
            and ready.get("narrative_package_ref")
            == f"{attestation.source_identity}/story.json"
            and ready.get("narrative_package_digest") == attestation.narrative_package_digest
            and actual_narrative_package_digest == attestation.narrative_package_digest
        )
        if not valid:
            _fail(AUTHORITY_NOT_READY)
        return {
            "ready": True, "verdict_version": VERIFY_READY_VERSION,
            "source_identity": attestation.source_identity,
            "draft_identity": attestation.draft_identity,
            "review_revision": attestation.review_revision,
            "review_event_digest": attestation.review_event_digest,
            "attestation_digest": _digest(attestation.to_payload()),
        }


def load_authority(
    *, authority_root: str | os.PathLike[str], key_file: str | os.PathLike[str],
    narrative_outbox_root: str | os.PathLike[str],
    git_root: str | os.PathLike[str] | None = None,
    protected_roots: Iterable[str | os.PathLike[str]] = (),
) -> ReviewAuthority:
    protected = tuple(protected_roots)
    root = validate_authority_root(authority_root, git_root=git_root, protected_roots=protected)
    outbox = validate_narrative_outbox_root(
        narrative_outbox_root,
        authority_root=root,
        git_root=git_root,
        protected_roots=protected,
    )
    key_path = _validate_absolute_path(key_file, allow_missing=False)
    if (
        root == key_path or root in key_path.parents or key_path in root.parents
        or outbox == key_path or outbox in key_path.parents or key_path in outbox.parents
    ):
        _fail(AUTHORITY_PATH_INVALID)
    try:
        service = trust.load_trust_service({}, key_path)
    except trust.TrustError as error:
        raise AuthorityError(error.reason_code) from None
    return ReviewAuthority(
        root, service,
        narrative_outbox_root=outbox,
        git_root=None if git_root is None else Path(git_root),
        protected_roots=tuple(Path(item) for item in protected),
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = (
    "AUTHORITY_ATTESTATION_INVALID", "AUTHORITY_INTERNAL", "AUTHORITY_INVALID",
    "AUTHORITY_NOT_READY", "AUTHORITY_PATH_INVALID", "AUTHORITY_PERSISTENCE_INVALID",
    "AUTHORITY_STATE_CONFLICT", "AUTHORITY_STATE_MISSING", "AUTHORITY_TRANSITION_INVALID",
    "AuthorityError", "BROKER_CONTRACT_VERSION", "MAX_NARRATIVE_PACKAGE_BYTES",
    "NARRATIVE_OUTBOX_LAYOUT_VERSION", "NarrativePackageReader",
    "PREPARED_APPROVAL_VERSION", "ReviewAuthority", "ReviewStateAdapter",
    "STORAGE_ADAPTER_VERSION", "VERIFY_READY_VERSION", "load_authority", "utc_now",
    "validate_authority_root", "validate_narrative_outbox_root",
)
