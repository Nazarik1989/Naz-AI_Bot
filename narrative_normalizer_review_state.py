"""Authoritative monotonic review state and approval attestations.

This module is deliberately independent from the Narrative Normalizer runtime
and from CP1/CP2.  Draft files are evidence; immutable HMAC-authenticated event
objects under the separately owned authority root define the latest review
state.  A mutable head cache is never an authority.
"""
from __future__ import annotations

import hashlib
import functools
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

import narrative_normalizer_trust as trust


REVIEW_LEDGER_SCHEMA_VERSION = "normalizer-review-ledger-v1"
REVIEW_EVENT_SCHEMA_VERSION = "normalizer-review-event-v1"
REVIEW_STATE_POLICY_VERSION = "normalizer-review-state-policy-v1"
APPROVAL_ATTESTATION_SCHEMA_VERSION = "normalizer-approval-attestation-v1"
APPROVAL_ATTESTATION_POLICY_VERSION = "normalizer-approval-attestation-policy-v1"

STATE_DRAFTED = "drafted"
STATE_PASSED = "passed"
STATE_REJECTED = "rejected"
STATE_SUPERSEDED = "superseded"
STATE_APPROVED = "approved"
REVIEW_STATES = frozenset({
    STATE_DRAFTED,
    STATE_PASSED,
    STATE_REJECTED,
    STATE_SUPERSEDED,
    STATE_APPROVED,
})

REVIEW_STATE_INVALID = "normalizer_review_state_invalid"
REVIEW_STATE_MISSING = "normalizer_review_state_missing"
REVIEW_STATE_CONFLICT = "normalizer_review_state_conflict"
REVIEW_STATE_TRANSITION_INVALID = "normalizer_review_state_transition_invalid"
REVIEW_STATE_PERSISTENCE_INVALID = "normalizer_review_state_persistence_invalid"
APPROVAL_ATTESTATION_INVALID = "normalizer_approval_attestation_invalid"

_HEX24 = re.compile(r"[0-9a-f]{24}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_EVENT_FILE = re.compile(r"(?P<revision>[0-9]{8})-(?P<digest>[0-9a-f]{64})\.json\Z")
_EVENT_STAGING = re.compile(r"\.event-staging-[0-9a-f]{16}\Z")
_EVENT_KEYS = frozenset({
    "schema_version", "revision", "previous_revision", "source_identity",
    "draft_identity", "state", "operator_request_id", "reason_codes",
    "action_digest", "timestamp", "policy_version", "previous_event_digest", "event_digest",
    "trust_receipt",
})
_LEDGER_KEYS = frozenset({
    "schema_version", "source_identity", "draft_identity", "latest_revision",
    "latest_state", "latest_event_digest", "events",
})
_ATTESTATION_KEYS = frozenset({
    "schema_version", "source_identity", "source_ref", "source_digest",
    "draft_identity", "package_digest", "narrative_ready_manifest_digest",
    "story_markdown_digest", "draft_manifest_digest", "review_digest",
    "completed_claim_digest", "artifact_binding_digest",
    "review_revision", "review_event_digest", "approval_request_id",
    "contract_versions", "key_id", "trust_receipt",
})
_ATTESTATION_CONTRACT_KEYS = frozenset({
    "attestation", "review_state", "ready_manifest", "source", "trust",
})


class ReviewStateError(ValueError):
    """Privacy-safe state error carrying only a stable reason code."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _raise(reason: str) -> None:
    raise ReviewStateError(reason)


def _canonical(value: object) -> bytes:
    try:
        return trust.canonical_payload(value)
    except Exception:
        pass
    raise ReviewStateError(REVIEW_STATE_INVALID) from None


def _privacy_boundary(default_reason: str):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            normalize = False
            try:
                return function(*args, **kwargs)
            except ReviewStateError:
                raise
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                normalize = True
            if normalize:
                raise ReviewStateError(default_reason) from None
            raise AssertionError("unreachable")

        return wrapped

    return decorate


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _plain(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        _raise(REVIEW_STATE_INVALID)
    return value


def _hex(value: object, *, short: bool = False) -> str:
    pattern = _HEX24 if short else _HEX64
    if type(value) is not str or pattern.fullmatch(value) is None:
        _raise(REVIEW_STATE_INVALID)
    return value


def _safe(value: object) -> str:
    if type(value) is not str or _SAFE.fullmatch(value) is None:
        _raise(REVIEW_STATE_INVALID)
    return value


def _timestamp(value: object) -> str:
    text = _plain(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        _raise(REVIEW_STATE_INVALID)
    if parsed.tzinfo is None:
        _raise(REVIEW_STATE_INVALID)
    return text


def _reason_codes(value: object) -> tuple[str, ...]:
    if type(value) not in {list, tuple}:
        _raise(REVIEW_STATE_INVALID)
    result = tuple(_safe(item) for item in value)
    if result != tuple(sorted(set(result))):
        _raise(REVIEW_STATE_INVALID)
    return result


def _event_core(
    *,
    revision: int,
    previous_revision: int,
    source_identity: str,
    draft_identity: str,
    state: str,
    operator_request_id: str,
    reason_codes: tuple[str, ...],
    action_digest: str,
    timestamp: str,
    previous_event_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": REVIEW_EVENT_SCHEMA_VERSION,
        "revision": revision,
        "previous_revision": previous_revision,
        "source_identity": source_identity,
        "draft_identity": draft_identity,
        "state": state,
        "operator_request_id": operator_request_id,
        "reason_codes": list(reason_codes),
        "action_digest": action_digest,
        "timestamp": timestamp,
        "policy_version": REVIEW_STATE_POLICY_VERSION,
        "previous_event_digest": previous_event_digest,
    }


@dataclass(frozen=True, slots=True)
class ReviewEvent:
    revision: int
    previous_revision: int
    source_identity: str
    draft_identity: str
    state: str
    operator_request_id: str
    reason_codes: tuple[str, ...]
    action_digest: str
    timestamp: str
    previous_event_digest: str
    event_digest: str
    trust_receipt: trust.TrustReceipt

    def __post_init__(self) -> None:
        if type(self.revision) is not int or self.revision < 1:
            _raise(REVIEW_STATE_INVALID)
        if type(self.previous_revision) is not int or self.previous_revision != self.revision - 1:
            _raise(REVIEW_STATE_INVALID)
        _hex(self.source_identity)
        _hex(self.draft_identity)
        if self.state not in REVIEW_STATES:
            _raise(REVIEW_STATE_INVALID)
        _safe(self.operator_request_id)
        if self.reason_codes != _reason_codes(self.reason_codes):
            _raise(REVIEW_STATE_INVALID)
        _hex(self.action_digest)
        _timestamp(self.timestamp)
        if self.revision == 1:
            if self.previous_event_digest != "" or self.state != STATE_DRAFTED:
                _raise(REVIEW_STATE_INVALID)
        else:
            _hex(self.previous_event_digest)
        _hex(self.event_digest)
        if type(self.trust_receipt) is not trust.TrustReceipt:
            _raise(REVIEW_STATE_INVALID)

    def core_payload(self) -> dict[str, object]:
        return _event_core(
            revision=self.revision,
            previous_revision=self.previous_revision,
            source_identity=self.source_identity,
            draft_identity=self.draft_identity,
            state=self.state,
            operator_request_id=self.operator_request_id,
            reason_codes=self.reason_codes,
            action_digest=self.action_digest,
            timestamp=self.timestamp,
            previous_event_digest=self.previous_event_digest,
        )

    def signed_payload(self) -> dict[str, object]:
        return {"event": self.core_payload(), "event_digest": self.event_digest}

    def to_payload(self) -> dict[str, object]:
        return dict(
            self.core_payload(),
            event_digest=self.event_digest,
            trust_receipt=trust.receipt_to_payload(self.trust_receipt),
        )


def build_review_event(
    service: trust.NarrativeTrustService,
    *,
    revision: int,
    previous_revision: int,
    source_identity: str,
    draft_identity: str,
    state: str,
    operator_request_id: str,
    reason_codes: Iterable[str],
    timestamp: str,
    previous_event_digest: str,
    action_digest: str | None = None,
) -> ReviewEvent:
    if type(service) is not trust.NarrativeTrustService:
        _raise(REVIEW_STATE_INVALID)
    reasons = _reason_codes(tuple(reason_codes))
    if action_digest is None:
        action_digest = _digest({
            "state": state,
            "operator_request_id": operator_request_id,
            "reason_codes": list(reasons),
        })
    action_digest = _hex(action_digest)
    core = _event_core(
        revision=revision,
        previous_revision=previous_revision,
        source_identity=_hex(source_identity),
        draft_identity=_hex(draft_identity),
        state=state,
        operator_request_id=_safe(operator_request_id),
        reason_codes=reasons,
        action_digest=action_digest,
        timestamp=_timestamp(timestamp),
        previous_event_digest=previous_event_digest,
    )
    event_digest = _digest(core)
    try:
        receipt = service.sign(
            trust.TRUST_DOMAIN_REVIEW_LEDGER,
            {"event": core, "event_digest": event_digest},
        )
    except trust.TrustError:
        _raise(REVIEW_STATE_INVALID)
    return ReviewEvent(
        revision,
        previous_revision,
        source_identity,
        draft_identity,
        state,
        operator_request_id,
        reasons,
        action_digest,
        timestamp,
        previous_event_digest,
        event_digest,
        receipt,
    )


def review_event_from_payload(value: object, service: trust.NarrativeTrustService) -> ReviewEvent:
    try:
        if type(value) is not dict or frozenset(value) != _EVENT_KEYS:
            _raise(REVIEW_STATE_INVALID)
        receipt = trust.receipt_from_payload(value["trust_receipt"])
        event = ReviewEvent(
            value["revision"], value["previous_revision"], value["source_identity"],
            value["draft_identity"], value["state"], value["operator_request_id"],
            _reason_codes(value["reason_codes"]), value["action_digest"], value["timestamp"],
            value["previous_event_digest"], value["event_digest"], receipt,
        )
        if event.event_digest != _digest(event.core_payload()):
            _raise(REVIEW_STATE_INVALID)
        service.require_valid(
            trust.TRUST_DOMAIN_REVIEW_LEDGER,
            event.signed_payload(),
            event.trust_receipt,
        )
        return event
    except ReviewStateError:
        raise
    except (KeyError, TypeError, ValueError, trust.TrustError):
        raise ReviewStateError(REVIEW_STATE_INVALID) from None


def _transition_allowed(old: str, new: str) -> bool:
    return (
        (old == STATE_DRAFTED and new in {STATE_PASSED, STATE_REJECTED})
        or (old == STATE_PASSED and new in {STATE_REJECTED, STATE_SUPERSEDED, STATE_APPROVED})
    )


@dataclass(frozen=True, slots=True)
class ReviewLedger:
    source_identity: str
    draft_identity: str
    events: tuple[ReviewEvent, ...]

    @property
    def latest(self) -> ReviewEvent:
        return self.events[-1]

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": REVIEW_LEDGER_SCHEMA_VERSION,
            "source_identity": self.source_identity,
            "draft_identity": self.draft_identity,
            "latest_revision": self.latest.revision,
            "latest_state": self.latest.state,
            "latest_event_digest": self.latest.event_digest,
            "events": [item.to_payload() for item in self.events],
        }


def review_ledger_from_payload(value: object, service: trust.NarrativeTrustService) -> ReviewLedger:
    try:
        if type(value) is not dict or frozenset(value) != _LEDGER_KEYS:
            _raise(REVIEW_STATE_INVALID)
        if value["schema_version"] != REVIEW_LEDGER_SCHEMA_VERSION or type(value["events"]) is not list:
            _raise(REVIEW_STATE_INVALID)
        source_identity = _hex(value["source_identity"])
        draft_identity = _hex(value["draft_identity"])
        events = tuple(review_event_from_payload(item, service) for item in value["events"])
        if not events:
            _raise(REVIEW_STATE_INVALID)
        for index, event in enumerate(events):
            if event.source_identity != source_identity or event.draft_identity != draft_identity:
                _raise(REVIEW_STATE_INVALID)
            if event.revision != index + 1 or event.previous_revision != index:
                _raise(REVIEW_STATE_INVALID)
            if index and (
                event.previous_event_digest != events[index - 1].event_digest
                or not _transition_allowed(events[index - 1].state, event.state)
            ):
                _raise(REVIEW_STATE_INVALID)
        ledger = ReviewLedger(source_identity, draft_identity, events)
        if (
            value["latest_revision"] != ledger.latest.revision
            or value["latest_state"] != ledger.latest.state
            or value["latest_event_digest"] != ledger.latest.event_digest
        ):
            _raise(REVIEW_STATE_INVALID)
        return ledger
    except ReviewStateError:
        raise
    except (KeyError, TypeError, ValueError):
        raise ReviewStateError(REVIEW_STATE_INVALID) from None


class _LedgerLock:
    def __init__(self, path: Path):
        self.path = path
        self._stream = None

    def __enter__(self) -> "_LedgerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.parent.is_symlink() or not self.path.parent.is_dir():
            _raise(REVIEW_STATE_PERSISTENCE_INVALID)
        if os.path.lexists(self.path) and (self.path.is_symlink() or not self.path.is_file()):
            _raise(REVIEW_STATE_PERSISTENCE_INVALID)
        stream = self.path.open("a+b")
        if os.name != "nt":
            os.chmod(self.path, 0o600)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        except BaseException:
            stream.close()
            raise
        self._stream = stream
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        cleanup_error: BaseException | None = None
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        except BaseException as error:
            cleanup_error = error
        try:
            stream.close()
        except BaseException as error:
            if cleanup_error is None or not isinstance(error, Exception):
                cleanup_error = error
        if cleanup_error is None:
            return
        if exc is not None and not isinstance(exc, Exception) and isinstance(cleanup_error, Exception):
            # An ordinary unlock/close error must never replace cancellation.
            return
        raise cleanup_error


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_replace(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink() or not path.parent.is_dir():
        _raise(REVIEW_STATE_PERSISTENCE_INVALID)
    old = path.read_bytes() if os.path.lexists(path) and path.is_file() and not path.is_symlink() else None
    if os.path.lexists(path) and old is None:
        _raise(REVIEW_STATE_PERSISTENCE_INVALID)
    staging = path.with_name(f".{path.name}.staging-{secrets.token_hex(8)}")
    rollback = path.with_name(f".{path.name}.rollback-{secrets.token_hex(8)}")
    try:
        _write_file(staging, payload)
        os.replace(staging, path)
        if os.name != "nt":
            os.chmod(path, 0o600)
        _fsync_directory(path.parent)
        if path.read_bytes() != payload:
            _raise(REVIEW_STATE_PERSISTENCE_INVALID)
    except BaseException as original:
        cleanup_failed = False
        cleanup_cancellation: BaseException | None = None
        try:
            current = path.read_bytes() if os.path.lexists(path) and path.is_file() and not path.is_symlink() else None
            if current != old:
                if old is None:
                    if os.path.lexists(path):
                        os.unlink(path)
                else:
                    _write_file(rollback, old)
                    os.replace(rollback, path)
            for owned in (staging, rollback):
                if os.path.lexists(owned):
                    if owned.is_symlink() or not owned.is_file():
                        cleanup_failed = True
                    else:
                        os.unlink(owned)
            _fsync_directory(path.parent)
            restored = path.read_bytes() if os.path.lexists(path) and path.is_file() and not path.is_symlink() else None
            if restored != old:
                cleanup_failed = True
        except BaseException as error:
            cleanup_failed = True
            if not isinstance(error, Exception):
                cleanup_cancellation = error
        if not isinstance(original, Exception):
            raise
        if cleanup_cancellation is not None:
            raise cleanup_cancellation from None
        if cleanup_failed:
            raise ReviewStateError(REVIEW_STATE_PERSISTENCE_INVALID) from None
        raise ReviewStateError(REVIEW_STATE_PERSISTENCE_INVALID) from None


class ReviewStateStore:
    """Append-only authority for one monotonic HMAC-authenticated event chain.

    ``head.json`` is deliberately only a diagnostic cache.  Every authoritative
    read scans immutable no-clobber event objects and reconstructs the highest
    complete contiguous chain.  Restoring a historical cache therefore cannot
    erase a later terminal event.
    """

    def __init__(self, authority_root: Path, service: trust.NarrativeTrustService):
        if type(service) is not trust.NarrativeTrustService:
            _raise(REVIEW_STATE_INVALID)
        original = Path(authority_root)
        try:
            cursor = original
            while True:
                if os.path.lexists(cursor) and cursor.is_symlink():
                    _raise(REVIEW_STATE_INVALID)
                if cursor == cursor.parent:
                    break
                cursor = cursor.parent
            resolved = original.resolve(strict=False)
        except (OSError, RuntimeError):
            raise ReviewStateError(REVIEW_STATE_INVALID) from None
        self.authority_root = resolved
        self.root = resolved
        self.locks = resolved / ".locks"
        self.service = service

    def source_root_for(self, source_identity: str) -> Path:
        return self.root / _hex(source_identity)

    def events_path_for(self, source_identity: str) -> Path:
        return self.source_root_for(source_identity) / "events"

    def path_for(self, source_identity: str) -> Path:
        """Return the non-authoritative mutable head-cache path."""
        return self.source_root_for(source_identity) / "head.json"

    def event_path_for(self, event: ReviewEvent) -> Path:
        if type(event) is not ReviewEvent:
            _raise(REVIEW_STATE_INVALID)
        return self.events_path_for(event.source_identity) / (
            f"{event.revision:08d}-{event.event_digest}.json"
        )

    def _lock_for(self, source_identity: str) -> _LedgerLock:
        return _LedgerLock(self.locks / f"{_hex(source_identity)}.lock")

    @staticmethod
    def _directory(path: Path, *, create: bool) -> None:
        try:
            if create:
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.is_symlink() or not path.is_dir():
                _raise(REVIEW_STATE_PERSISTENCE_INVALID)
            if os.name != "nt":
                os.chmod(path, 0o700)
        except ReviewStateError:
            raise
        except OSError:
            raise ReviewStateError(REVIEW_STATE_PERSISTENCE_INVALID) from None

    def _ensure_write_layout(self, source_identity: str) -> Path:
        self._directory(self.root, create=True)
        self._directory(self.locks, create=True)
        source_root = self.source_root_for(source_identity)
        self._directory(source_root, create=True)
        events = self.events_path_for(source_identity)
        self._directory(events, create=True)
        return events

    def _read_event(self, path: Path) -> ReviewEvent:
        try:
            if path.is_symlink() or not path.is_file():
                _raise(REVIEW_STATE_INVALID)
            encoded = path.read_bytes()
            value = json.loads(encoded.decode("utf-8"))
            if encoded != _canonical(value) + b"\n":
                _raise(REVIEW_STATE_INVALID)
            return review_event_from_payload(value, self.service)
        except ReviewStateError:
            raise
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise ReviewStateError(REVIEW_STATE_INVALID) from None

    def _read_unlocked(self, source_identity: str) -> ReviewLedger:
        identity = _hex(source_identity)
        source_root = self.source_root_for(identity)
        events_root = self.events_path_for(identity)
        if not os.path.lexists(events_root):
            _raise(REVIEW_STATE_MISSING)
        try:
            if (
                self.root.is_symlink()
                or not self.root.is_dir()
                or source_root.is_symlink()
                or not source_root.is_dir()
                or events_root.is_symlink()
                or not events_root.is_dir()
            ):
                _raise(REVIEW_STATE_INVALID)
            indexed: list[tuple[int, str, Path]] = []
            for path in events_root.iterdir():
                if _EVENT_STAGING.fullmatch(path.name) is not None:
                    # An interrupted pre-promotion staging object is never an
                    # event and cannot advance the authoritative head.
                    continue
                matched = _EVENT_FILE.fullmatch(path.name)
                if matched is None:
                    _raise(REVIEW_STATE_INVALID)
                indexed.append((int(matched.group("revision")), matched.group("digest"), path))
            if not indexed:
                _raise(REVIEW_STATE_MISSING)
            indexed.sort(key=lambda item: (item[0], item[1]))
            revisions = [item[0] for item in indexed]
            if revisions != list(range(1, len(indexed) + 1)):
                # Includes gaps, duplicate revisions and forks.
                _raise(REVIEW_STATE_INVALID)
            parsed: list[ReviewEvent] = []
            draft_identity: str | None = None
            for revision, digest, path in indexed:
                event = self._read_event(path)
                if (
                    event.revision != revision
                    or event.event_digest != digest
                    or event.source_identity != identity
                ):
                    _raise(REVIEW_STATE_INVALID)
                if draft_identity is None:
                    draft_identity = event.draft_identity
                if event.draft_identity != draft_identity:
                    _raise(REVIEW_STATE_INVALID)
                if parsed and (
                    event.previous_revision != parsed[-1].revision
                    or event.previous_event_digest != parsed[-1].event_digest
                    or not _transition_allowed(parsed[-1].state, event.state)
                ):
                    _raise(REVIEW_STATE_INVALID)
                parsed.append(event)
            assert draft_identity is not None
            return ReviewLedger(identity, draft_identity, tuple(parsed))
        except ReviewStateError:
            raise
        except OSError:
            raise ReviewStateError(REVIEW_STATE_INVALID) from None

    def _update_head_cache(self, ledger: ReviewLedger) -> None:
        try:
            _atomic_replace(self.path_for(ledger.source_identity), _canonical(ledger.to_payload()) + b"\n")
        except ReviewStateError:
            # The cache has no authority.  A failed cache update cannot roll
            # back or invalidate an already durable immutable event.
            return

    def _append_event_unlocked(self, event: ReviewEvent) -> bool:
        events_root = self._ensure_write_layout(event.source_identity)
        encoded = _canonical(event.to_payload()) + b"\n"
        final = self.event_path_for(event)
        staging = events_root / f".event-staging-{secrets.token_hex(8)}"
        result: bool | None = None
        failure: BaseException | None = None
        try:
            _write_file(staging, encoded)
            if self._read_event(staging) != event or staging.read_bytes() != encoded:
                _raise(REVIEW_STATE_PERSISTENCE_INVALID)
            if os.path.lexists(final):
                if final.is_symlink() or not final.is_file() or final.read_bytes() != encoded:
                    _raise(REVIEW_STATE_CONFLICT)
                result = True
            else:
                try:
                    os.link(staging, final)
                except FileExistsError:
                    if final.is_symlink() or not final.is_file() or final.read_bytes() != encoded:
                        _raise(REVIEW_STATE_CONFLICT)
                    result = True
                else:
                    if os.name != "nt":
                        os.chmod(final, 0o600)
                    _fsync_directory(events_root)
                    if self._read_event(final) != event or final.read_bytes() != encoded:
                        _raise(REVIEW_STATE_PERSISTENCE_INVALID)
                    result = False
        except BaseException as error:
            failure = error

        cleanup_failure = False
        cleanup_cancellation: BaseException | None = None
        try:
            if os.path.lexists(staging):
                if staging.is_symlink() or not staging.is_file():
                    cleanup_failure = True
                else:
                    os.unlink(staging)
                    _fsync_directory(events_root)
            if os.path.lexists(staging):
                cleanup_failure = True
        except BaseException as error:
            cleanup_failure = True
            if not isinstance(error, Exception):
                cleanup_cancellation = error

        if failure is not None and not isinstance(failure, Exception):
            raise failure
        if cleanup_cancellation is not None:
            raise cleanup_cancellation
        if cleanup_failure:
            raise ReviewStateError(REVIEW_STATE_PERSISTENCE_INVALID) from None
        if failure is not None:
            if isinstance(failure, ReviewStateError):
                raise failure
            raise ReviewStateError(REVIEW_STATE_PERSISTENCE_INVALID) from None
        if result is None:
            _raise(REVIEW_STATE_PERSISTENCE_INVALID)
        return result

    @_privacy_boundary(REVIEW_STATE_INVALID)
    def read(self, source_identity: str, *, expected_draft_identity: str | None = None) -> ReviewLedger:
        ledger = self._read_unlocked(source_identity)
        if expected_draft_identity is not None and ledger.draft_identity != expected_draft_identity:
            _raise(REVIEW_STATE_CONFLICT)
        return ledger

    @_privacy_boundary(REVIEW_STATE_PERSISTENCE_INVALID)
    def initialize(
        self,
        *,
        source_identity: str,
        draft_identity: str,
        initial_state: str,
        reason_codes: Iterable[str],
        drafted_at: str,
        reviewed_at: str,
    ) -> ReviewLedger:
        if initial_state not in {STATE_PASSED, STATE_REJECTED}:
            _raise(REVIEW_STATE_TRANSITION_INVALID)
        source_identity = _hex(source_identity)
        draft_identity = _hex(draft_identity)
        first = build_review_event(
            self.service,
            revision=1,
            previous_revision=0,
            source_identity=source_identity,
            draft_identity=draft_identity,
            state=STATE_DRAFTED,
            operator_request_id=f"system-draft-{draft_identity[:16]}",
            reason_codes=(),
            timestamp=drafted_at,
            previous_event_digest="",
        )
        second = build_review_event(
            self.service,
            revision=2,
            previous_revision=1,
            source_identity=source_identity,
            draft_identity=draft_identity,
            state=initial_state,
            operator_request_id=f"system-review-{draft_identity[:16]}",
            reason_codes=tuple(reason_codes),
            timestamp=reviewed_at,
            previous_event_digest=first.event_digest,
        )
        candidate = ReviewLedger(source_identity, draft_identity, (first, second))
        self._ensure_write_layout(source_identity)
        with self._lock_for(source_identity):
            try:
                existing = self._read_unlocked(source_identity)
            except ReviewStateError as error:
                if error.reason_code != REVIEW_STATE_MISSING:
                    raise
                existing = None
            if existing is not None:
                if existing == candidate:
                    self._update_head_cache(existing)
                    return existing
                if existing.events != (first,):
                    _raise(REVIEW_STATE_CONFLICT)
            if existing is None:
                self._append_event_unlocked(first)
            self._append_event_unlocked(second)
            final = self._read_unlocked(source_identity)
            if final != candidate:
                _raise(REVIEW_STATE_CONFLICT)
            self._update_head_cache(final)
            return final

    @_privacy_boundary(REVIEW_STATE_INVALID)
    def prepare_transition(
        self,
        *,
        source_identity: str,
        draft_identity: str,
        new_state: str,
        operator_request_id: str,
        reason_codes: Iterable[str],
        timestamp: str,
        action_digest: str | None = None,
    ) -> tuple[ReviewEvent, bool]:
        ledger = self.read(source_identity, expected_draft_identity=draft_identity)
        reasons = _reason_codes(tuple(reason_codes))
        for event in ledger.events:
            if event.operator_request_id != operator_request_id:
                continue
            expected_action_digest = action_digest or _digest({
                "state": new_state,
                "operator_request_id": operator_request_id,
                "reason_codes": list(reasons),
            })
            if (
                event.state == new_state
                and event.reason_codes == reasons
                and event.action_digest == expected_action_digest
            ):
                return event, True
            _raise(REVIEW_STATE_CONFLICT)
        if not _transition_allowed(ledger.latest.state, new_state):
            _raise(REVIEW_STATE_TRANSITION_INVALID)
        event = build_review_event(
            self.service,
            revision=ledger.latest.revision + 1,
            previous_revision=ledger.latest.revision,
            source_identity=source_identity,
            draft_identity=draft_identity,
            state=new_state,
            operator_request_id=operator_request_id,
            reason_codes=reasons,
            action_digest=action_digest,
            timestamp=timestamp,
            previous_event_digest=ledger.latest.event_digest,
        )
        return event, False

    @_privacy_boundary(REVIEW_STATE_PERSISTENCE_INVALID)
    def commit_prepared(self, event: ReviewEvent) -> tuple[ReviewLedger, bool]:
        if type(event) is not ReviewEvent:
            _raise(REVIEW_STATE_INVALID)
        with self._lock_for(event.source_identity):
            ledger = self._read_unlocked(event.source_identity)
            for existing in ledger.events:
                if existing.operator_request_id != event.operator_request_id:
                    continue
                if existing == event:
                    return ledger, True
                if (
                    existing.state == event.state
                    and existing.reason_codes == event.reason_codes
                    and existing.action_digest == event.action_digest
                ):
                    # Two callers may prepare the same semantic request with
                    # different timestamps before either wins the append CAS.
                    # The immutable winning event remains the sole history;
                    # the loser observes a byte-idempotent semantic replay.
                    return ledger, True
                _raise(REVIEW_STATE_CONFLICT)
            if (
                ledger.draft_identity != event.draft_identity
                or event.revision != ledger.latest.revision + 1
                or event.previous_revision != ledger.latest.revision
                or event.previous_event_digest != ledger.latest.event_digest
                or not _transition_allowed(ledger.latest.state, event.state)
            ):
                _raise(REVIEW_STATE_CONFLICT)
            updated = ReviewLedger(ledger.source_identity, ledger.draft_identity, (*ledger.events, event))
            event_idempotent = self._append_event_unlocked(event)
            final = self._read_unlocked(event.source_identity)
            if final != updated:
                _raise(REVIEW_STATE_PERSISTENCE_INVALID)
            self._update_head_cache(final)
            return final, event_idempotent

    @_privacy_boundary(REVIEW_STATE_PERSISTENCE_INVALID)
    def transition(self, **kwargs: object) -> tuple[ReviewLedger, bool]:
        event, idempotent = self.prepare_transition(**kwargs)
        if idempotent:
            return self.read(event.source_identity, expected_draft_identity=event.draft_identity), True
        return self.commit_prepared(event)


@dataclass(frozen=True, slots=True)
class ApprovalAttestation:
    source_identity: str
    source_ref: str
    source_digest: str
    draft_identity: str
    package_digest: str
    narrative_ready_manifest_digest: str
    story_markdown_digest: str
    draft_manifest_digest: str
    review_digest: str
    completed_claim_digest: str
    artifact_binding_digest: str
    review_revision: int
    review_event_digest: str
    approval_request_id: str
    contract_versions: tuple[tuple[str, str], ...]
    key_id: str
    trust_receipt: trust.TrustReceipt

    def core_payload(self) -> dict[str, object]:
        return {
            "schema_version": APPROVAL_ATTESTATION_SCHEMA_VERSION,
            "source_identity": self.source_identity,
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "draft_identity": self.draft_identity,
            "package_digest": self.package_digest,
            "narrative_ready_manifest_digest": self.narrative_ready_manifest_digest,
            "story_markdown_digest": self.story_markdown_digest,
            "draft_manifest_digest": self.draft_manifest_digest,
            "review_digest": self.review_digest,
            "completed_claim_digest": self.completed_claim_digest,
            "artifact_binding_digest": self.artifact_binding_digest,
            "review_revision": self.review_revision,
            "review_event_digest": self.review_event_digest,
            "approval_request_id": self.approval_request_id,
            "contract_versions": dict(self.contract_versions),
            "key_id": self.key_id,
        }

    def to_payload(self) -> dict[str, object]:
        return dict(self.core_payload(), trust_receipt=trust.receipt_to_payload(self.trust_receipt))


def approval_contract_versions(*, ready_manifest: str, source: str) -> tuple[tuple[str, str], ...]:
    return tuple(sorted({
        "attestation": APPROVAL_ATTESTATION_POLICY_VERSION,
        "review_state": REVIEW_STATE_POLICY_VERSION,
        "ready_manifest": _plain(ready_manifest),
        "source": _plain(source),
        "trust": trust.TRUST_RECEIPT_SCHEMA_VERSION,
    }.items()))


def build_approval_attestation(
    service: trust.NarrativeTrustService,
    *,
    source_identity: str,
    source_ref: str,
    source_digest: str,
    draft_identity: str,
    package_digest: str,
    ready_manifest_digest: str,
    story_markdown_digest: str,
    draft_manifest_digest: str,
    review_digest: str,
    completed_claim_digest: str,
    artifact_binding_digest: str,
    approved_event: ReviewEvent,
    ready_manifest_contract: str,
    source_contract: str,
) -> ApprovalAttestation:
    if type(service) is not trust.NarrativeTrustService or approved_event.state != STATE_APPROVED:
        _raise(APPROVAL_ATTESTATION_INVALID)
    versions = approval_contract_versions(ready_manifest=ready_manifest_contract, source=source_contract)
    placeholder = ApprovalAttestation(
        _hex(source_identity), _plain(source_ref), _hex(source_digest), _hex(draft_identity),
        _hex(package_digest), _hex(ready_manifest_digest), _hex(story_markdown_digest),
        _hex(draft_manifest_digest), _hex(review_digest), _hex(completed_claim_digest),
        _hex(artifact_binding_digest), approved_event.revision,
        _hex(approved_event.event_digest), _safe(approved_event.operator_request_id),
        versions, service.key_id,
        service.sign(trust.TRUST_DOMAIN_APPROVAL_ATTESTATION, {}),
    )
    try:
        receipt = service.sign(trust.TRUST_DOMAIN_APPROVAL_ATTESTATION, placeholder.core_payload())
    except trust.TrustError:
        _raise(APPROVAL_ATTESTATION_INVALID)
    return ApprovalAttestation(
        placeholder.source_identity, placeholder.source_ref, placeholder.source_digest,
        placeholder.draft_identity, placeholder.package_digest,
        placeholder.narrative_ready_manifest_digest, placeholder.story_markdown_digest,
        placeholder.draft_manifest_digest, placeholder.review_digest,
        placeholder.completed_claim_digest, placeholder.artifact_binding_digest,
        placeholder.review_revision,
        placeholder.review_event_digest, placeholder.approval_request_id,
        placeholder.contract_versions, placeholder.key_id, receipt,
    )


def approval_attestation_from_payload(
    value: object,
    service: trust.NarrativeTrustService,
    *,
    ready_manifest_contract: str,
    source_contract: str,
) -> ApprovalAttestation:
    try:
        if type(value) is not dict or frozenset(value) != _ATTESTATION_KEYS:
            _raise(APPROVAL_ATTESTATION_INVALID)
        versions = value["contract_versions"]
        if type(versions) is not dict or frozenset(versions) != _ATTESTATION_CONTRACT_KEYS:
            _raise(APPROVAL_ATTESTATION_INVALID)
        expected_versions = approval_contract_versions(
            ready_manifest=ready_manifest_contract,
            source=source_contract,
        )
        if tuple(sorted(versions.items())) != expected_versions or any(
            type(key) is not str or type(item) is not str for key, item in versions.items()
        ):
            _raise(APPROVAL_ATTESTATION_INVALID)
        receipt = trust.receipt_from_payload(value["trust_receipt"])
        attestation = ApprovalAttestation(
            _hex(value["source_identity"]), _plain(value["source_ref"]),
            _hex(value["source_digest"]), _hex(value["draft_identity"]),
            _hex(value["package_digest"]), _hex(value["narrative_ready_manifest_digest"]),
            _hex(value["story_markdown_digest"]), _hex(value["draft_manifest_digest"]),
            _hex(value["review_digest"]), _hex(value["completed_claim_digest"]),
            _hex(value["artifact_binding_digest"]),
            value["review_revision"], _hex(value["review_event_digest"]),
            _safe(value["approval_request_id"]), expected_versions,
            _hex(value["key_id"], short=True), receipt,
        )
        if type(attestation.review_revision) is not int or attestation.review_revision < 3:
            _raise(APPROVAL_ATTESTATION_INVALID)
        if attestation.key_id != service.key_id or attestation.trust_receipt.key_id != service.key_id:
            _raise(APPROVAL_ATTESTATION_INVALID)
        service.require_valid(
            trust.TRUST_DOMAIN_APPROVAL_ATTESTATION,
            attestation.core_payload(),
            attestation.trust_receipt,
        )
        return attestation
    except ReviewStateError as error:
        if error.reason_code == APPROVAL_ATTESTATION_INVALID:
            raise
        raise ReviewStateError(APPROVAL_ATTESTATION_INVALID) from None
    except (KeyError, TypeError, ValueError, trust.TrustError):
        raise ReviewStateError(APPROVAL_ATTESTATION_INVALID) from None


__all__ = (
    "APPROVAL_ATTESTATION_INVALID",
    "APPROVAL_ATTESTATION_POLICY_VERSION",
    "APPROVAL_ATTESTATION_SCHEMA_VERSION",
    "ApprovalAttestation",
    "REVIEW_EVENT_SCHEMA_VERSION",
    "REVIEW_LEDGER_SCHEMA_VERSION",
    "REVIEW_STATE_CONFLICT",
    "REVIEW_STATE_INVALID",
    "REVIEW_STATE_MISSING",
    "REVIEW_STATE_PERSISTENCE_INVALID",
    "REVIEW_STATE_POLICY_VERSION",
    "REVIEW_STATE_TRANSITION_INVALID",
    "REVIEW_STATES",
    "ReviewEvent",
    "ReviewLedger",
    "ReviewStateError",
    "ReviewStateStore",
    "STATE_APPROVED",
    "STATE_DRAFTED",
    "STATE_PASSED",
    "STATE_REJECTED",
    "STATE_SUPERSEDED",
    "approval_attestation_from_payload",
    "approval_contract_versions",
    "build_approval_attestation",
    "build_review_event",
    "review_event_from_payload",
    "review_ledger_from_payload",
)
