"""Emergency Reels eligibility, quarantine, and notification state machine.

The raw inbox is immutable.  All mutable state lives in a strict registry outside
the inbox and the narrative outbox.  Public errors are stable reason codes only.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import secrets
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

import narrative_normalizer_review_state as normalizer_review_state
import narrative_normalizer_trust as normalizer_trust
import narrative_review_authority_client as review_authority_client_module


MANIFEST_SCHEMA_VERSION = "naz-narrative-ready-v1"
NARRATIVE_SOURCE_CONTRACT_VERSION = "agent-content-source-v1"
REGISTRY_SCHEMA_VERSION = "reels-material-registry-v1"
ELIGIBILITY_CONTRACT_VERSION = "reels-eligibility-v2"
AGGREGATE_POLICY_VERSION = "raw-backlog-aggregate-v1"
NOTIFICATION_CLAIM_STALE_SECONDS = 300
MATERIAL_CLAIM_STALE_SECONDS = 3600

CLASS_RAW = "raw_agent_material"
CLASS_READY = "narrative_ready"
CLASSIFICATIONS = frozenset({CLASS_RAW, CLASS_READY})

_LAYOUT_LEGACY_SOURCE_SIDE = "legacy_source_side"
_LAYOUT_LEGACY_DIGEST_ONLY = "legacy_digest_only"
_LAYOUT_NORMALIZER_IDENTITY_V2 = "normalizer_identity_layout_v2"
_NARRATIVE_READY_LAYOUTS = frozenset({
    _LAYOUT_LEGACY_SOURCE_SIDE,
    _LAYOUT_LEGACY_DIGEST_ONLY,
    _LAYOUT_NORMALIZER_IDENTITY_V2,
})
_V2_IDENTITY_ARTIFACT_NAMES = frozenset({
    "story.json",
    "story.md",
    "draft-manifest.json",
    "review.json",
    "approval-attestation.json",
    "narrative_ready.json",
})

STATUS_NEEDS_NARRATIVE = "needs_narrative"
STATUS_READY = "ready"
STATUS_PROCESSING = "processing"
STATUS_BLOCKED = "blocked"
STATUS_CONSUMED = "consumed"
MATERIAL_STATUSES = frozenset({
    STATUS_NEEDS_NARRATIVE,
    STATUS_READY,
    STATUS_PROCESSING,
    STATUS_BLOCKED,
    STATUS_CONSUMED,
})

NOTIFICATION_PENDING = "pending"
NOTIFICATION_CLAIMED = "claimed"
NOTIFICATION_SENT = "sent"
NOTIFICATION_FAILED = "failed"
NOTIFICATION_UNCERTAIN = "uncertain"
NOTIFICATION_STATES = frozenset({
    NOTIFICATION_PENDING,
    NOTIFICATION_CLAIMED,
    NOTIFICATION_SENT,
    NOTIFICATION_FAILED,
    NOTIFICATION_UNCERTAIN,
})

RAW_AGGREGATE_KIND = "raw_backlog"
DIRECTOR_REJECTION_KIND = "director_rejection"
NOTIFICATION_KINDS = frozenset({RAW_AGGREGATE_KIND, DIRECTOR_REJECTION_KIND})

RAW_SUMMARY_CODE = "raw_backlog_needs_narrative"
DIRECTOR_SUMMARY_CODE = "reels_director_rejected"
SUMMARY_CODES = frozenset({RAW_SUMMARY_CODE, DIRECTOR_SUMMARY_CODE})

SAFE_REASON_CODES = frozenset({
    "reels_director_rejected",
    "director_provider_error",
    "director_response_invalid",
    "director_contract_invalid",
    "director_scene_count_invalid",
    "director_story_arc_invalid",
    "director_role_invalid",
    "director_camera_invalid",
    "story_manifest_contract_stale",
    "story_pack_invalid",
    "processing_lease_expired",
})

CONTRACT_VERSION_KEYS = frozenset({"director", "narrative"})
MANIFEST_KEYS = frozenset({
    "schema_version",
    "source_ref",
    "source_digest",
    "narrative_package_ref",
    "narrative_package_digest",
    "status",
    "contract_versions",
})
REGISTRY_KEYS = frozenset({
    "schema_version", "registry_revision", "records", "notifications", "aggregates",
})
AGGREGATE_KEYS = frozenset({"current_raw_fingerprint"})
RECORD_KEYS = frozenset({
    "source_ref",
    "source_digest",
    "classification",
    "status",
    "narrative_package_ref",
    "narrative_package_digest",
    "first_observed_at",
    "last_observed_at",
    "attempt_count",
    "active_attempt_id",
    "failure_fingerprint",
    "safe_reason_codes",
    "director_contract_version",
    "narrative_contract_version",
    "eligibility_contract_version",
    "notification_ids",
    "manual_retry_ids",
    "history",
})
HISTORY_KEYS = frozenset({
    "event", "at", "attempt_id", "failure_fingerprint", "reason_codes", "operator_request_id",
})
NOTIFICATION_KEYS = frozenset({
    "notification_id",
    "kind",
    "aggregate_fingerprint",
    "state",
    "created_at",
    "claimed_at",
    "completed_at",
    "attempt_count",
    "safe_summary_code",
    "operator_request_ids",
})

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_SAFE_OPERATOR_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HISTORY_EVENTS = frozenset({
    "discovered_raw", "discovered_ready", "identity_changed", "attempt_claimed",
    "attempt_released", "attempt_expired", "director_blocked", "consumed",
    "manual_retry",
})
_MAX_HISTORY = 32
_MAX_RETRY_IDS = 32
_MAX_CAS_RETRIES = 3

_BROKER_ATTESTATION_KEYS = frozenset({
    "schema_version", "source_identity", "source_ref", "source_digest",
    "draft_identity", "draft_package_digest", "narrative_package_digest",
    "narrative_ready_manifest_digest", "story_markdown_digest",
    "draft_manifest_digest", "review_digest", "completed_claim_digest",
    "artifact_binding_digest", "review_revision", "review_event_digest",
    "approval_request_id", "contract_versions", "key_id", "trust_receipt",
})
_BROKER_STORAGE_ADAPTER_VERSION = "narrative-review-authority-state-adapter-v2"
_BROKER_READY_VERDICT_VERSION = "narrative-review-authority-ready-verdict-v2"
_BROKER_DRAFT_CONTRACT_VERSION = "normalizer-draft-identity-v1"


class QuarantineError(RuntimeError):
    """Privacy-safe domain failure containing one stable reason code."""

    def __init__(self, reason_code: str) -> None:
        if type(reason_code) is not str or not re.fullmatch(r"[a-z0-9_]{3,96}", reason_code):
            reason_code = "quarantine_error"
        self.reason_code = reason_code
        super().__init__(reason_code)


class EligibilityError(QuarantineError):
    pass


class RegistryError(QuarantineError):
    pass


def _exact_dict(value: object, keys: frozenset[str], reason: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys or any(type(key) is not str for key in value):
        raise EligibilityError(reason)
    return value


def _plain_str(value: object, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise EligibilityError(f"{name}_invalid")
    return value


def _plain_hex64(value: object, name: str, *, allow_empty: bool = False) -> str:
    text = _plain_str(value, name, allow_empty=allow_empty)
    if text == "" and allow_empty:
        return text
    if _HEX64.fullmatch(text) is None:
        raise EligibilityError(f"{name}_invalid")
    return text


def _plain_int(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise RegistryError(f"{name}_invalid")
    return value


def _timestamp(value: object, name: str, *, allow_empty: bool = False) -> str:
    text = _plain_str(value, name, allow_empty=allow_empty)
    if text == "" and allow_empty:
        return text
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RegistryError(f"{name}_invalid") from exc
    if parsed.tzinfo is None:
        raise RegistryError(f"{name}_invalid")
    return text


def _now_text(now: datetime | None = None) -> str:
    value = datetime.now(timezone.utc) if now is None else now
    if type(value) is not datetime or value.tzinfo is None:
        raise RegistryError("quarantine_time_invalid")
    return value.astimezone(timezone.utc).isoformat()


def _notification_claim_is_stale(item: "NotificationRecord", now_text: str) -> bool:
    if item.state != NOTIFICATION_CLAIMED or not item.claimed_at:
        return False
    claimed_at = datetime.fromisoformat(item.claimed_at).astimezone(timezone.utc)
    observed_at = datetime.fromisoformat(now_text).astimezone(timezone.utc)
    return (observed_at - claimed_at).total_seconds() >= NOTIFICATION_CLAIM_STALE_SECONDS


def _material_claim_is_stale(item: "MaterialRecord", now_text: str) -> bool:
    if item.status != STATUS_PROCESSING or not item.active_attempt_id:
        return False
    claimed_at = datetime.fromisoformat(item.last_observed_at).astimezone(timezone.utc)
    observed_at = datetime.fromisoformat(now_text).astimezone(timezone.utc)
    return (observed_at - claimed_at).total_seconds() >= MATERIAL_CLAIM_STALE_SECONDS


def _safe_logical_ref(value: object, name: str) -> str:
    text = _plain_str(value, name)
    if (
        "\0" in text
        or "\\" in text
        or ":" in text
        or "://" in text
        or text.startswith(("/", "//"))
        or len(text) > 512
    ):
        raise EligibilityError(f"{name}_invalid")
    path = PurePosixPath(text)
    parts = path.parts
    if not parts or path.is_absolute() or path.as_posix() != text:
        raise EligibilityError(f"{name}_invalid")
    if any(part in {"", ".", ".."} or _SAFE_COMPONENT.fullmatch(part) is None for part in parts):
        raise EligibilityError(f"{name}_invalid")
    return text


def _safe_version(value: object, name: str) -> str:
    text = _plain_str(value, name)
    if _SAFE_VERSION.fullmatch(text) is None:
        raise EligibilityError(f"{name}_invalid")
    return text


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _overlaps(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def _resolved(path: Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _has_existing_symlink_component(path: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            return True
    return False


def _reject_symlink_chain(root: Path, target: Path, reason: str) -> None:
    root = root.resolve(strict=True)
    if root.is_symlink() or target.is_symlink():
        raise EligibilityError(reason)
    relative = target.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise EligibilityError(reason)


@dataclass(frozen=True, slots=True)
class QuarantinePathPolicy:
    inbox_root: Path
    registry_path: Path
    narrative_outbox_root: Path
    narrative_trust_service: normalizer_trust.NarrativeTrustService | None = None
    narrative_review_authority_root: Path | None = None
    review_authority_client: object | None = None

    def __post_init__(self) -> None:
        original_inbox = Path(self.inbox_root)
        original_registry = Path(self.registry_path)
        original_outbox = Path(self.narrative_outbox_root)
        original_authority = (
            None
            if self.narrative_review_authority_root is None
            else Path(self.narrative_review_authority_root)
        )
        if (
            self.narrative_trust_service is not None
            and type(self.narrative_trust_service) is not normalizer_trust.NarrativeTrustService
        ):
            raise RegistryError("quarantine_narrative_trust_invalid")
        if self.review_authority_client is not None and any(
            not callable(getattr(self.review_authority_client, name, None))
            for name in ("latest_state", "verify_ready")
        ):
            raise RegistryError("quarantine_review_authority_invalid")
        if _has_existing_symlink_component(original_inbox):
            raise RegistryError("quarantine_inbox_path_invalid")
        if _has_existing_symlink_component(original_outbox):
            raise RegistryError("quarantine_narrative_outbox_invalid")
        if original_authority is not None and _has_existing_symlink_component(original_authority):
            raise RegistryError("quarantine_review_authority_invalid")
        if _has_existing_symlink_component(original_registry.parent):
            raise RegistryError("quarantine_registry_path_invalid")
        inbox = _resolved(original_inbox)
        registry = _resolved(original_registry)
        outbox = _resolved(original_outbox)
        authority = None if original_authority is None else _resolved(original_authority)
        registry_root = registry.parent
        if registry.name in {"", ".", ".."} or registry.suffix.lower() != ".json":
            raise RegistryError("quarantine_registry_path_invalid")
        if _overlaps(registry_root, inbox) or _overlaps(registry_root, outbox) or _overlaps(inbox, outbox):
            raise RegistryError("quarantine_registry_path_invalid")
        if authority is not None:
            code_root = Path(__file__).resolve().parent
            if (
                _overlaps(authority, inbox)
                or _overlaps(authority, outbox)
                or _overlaps(authority, registry_root)
                or _overlaps(authority, code_root)
            ):
                raise RegistryError("quarantine_review_authority_invalid")
        if inbox.exists() and (not inbox.is_dir() or inbox.is_symlink()):
            raise RegistryError("quarantine_inbox_path_invalid")
        if outbox.exists() and (not outbox.is_dir() or outbox.is_symlink()):
            raise RegistryError("quarantine_narrative_outbox_invalid")
        if authority is not None and authority.exists() and (
            not authority.is_dir() or authority.is_symlink()
        ):
            raise RegistryError("quarantine_review_authority_invalid")
        object.__setattr__(self, "inbox_root", inbox)
        object.__setattr__(self, "registry_path", registry)
        object.__setattr__(self, "narrative_outbox_root", outbox)
        object.__setattr__(self, "narrative_review_authority_root", authority)


@dataclass(frozen=True, slots=True)
class NarrativeReadyManifest:
    schema_version: str
    source_ref: str
    source_digest: str
    narrative_package_ref: str
    narrative_package_digest: str
    status: str
    contract_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _plain_str(self.schema_version, "narrative_manifest_schema")
        _plain_str(self.status, "narrative_manifest_status")
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise EligibilityError("narrative_manifest_schema_invalid")
        if self.status != CLASS_READY:
            raise EligibilityError("narrative_manifest_status_invalid")
        _safe_logical_ref(self.source_ref, "narrative_source_ref")
        _plain_hex64(self.source_digest, "narrative_source_digest")
        _safe_logical_ref(self.narrative_package_ref, "narrative_package_ref")
        _plain_hex64(self.narrative_package_digest, "narrative_package_digest")
        if type(self.contract_versions) is not tuple or len(self.contract_versions) != len(CONTRACT_VERSION_KEYS):
            raise EligibilityError("narrative_contract_versions_invalid")
        pairs: list[tuple[str, str]] = []
        for pair in self.contract_versions:
            if type(pair) is not tuple or len(pair) != 2:
                raise EligibilityError("narrative_contract_versions_invalid")
            key, value = pair
            if type(key) is not str or key not in CONTRACT_VERSION_KEYS:
                raise EligibilityError("narrative_contract_versions_invalid")
            pairs.append((key, _safe_version(value, "narrative_contract_version")))
        if frozenset(key for key, _ in pairs) != CONTRACT_VERSION_KEYS:
            raise EligibilityError("narrative_contract_versions_invalid")
        canonical = tuple(sorted(pairs))
        if canonical != self.contract_versions:
            raise EligibilityError("narrative_contract_versions_invalid")

    @classmethod
    def from_mapping(cls, value: object) -> "NarrativeReadyManifest":
        payload = _exact_dict(value, MANIFEST_KEYS, "narrative_manifest_shape_invalid")
        versions = payload["contract_versions"]
        if type(versions) is not dict or frozenset(versions) != CONTRACT_VERSION_KEYS:
            raise EligibilityError("narrative_contract_versions_invalid")
        if any(type(key) is not str or type(item) is not str for key, item in versions.items()):
            raise EligibilityError("narrative_contract_versions_invalid")
        return cls(
            schema_version=_plain_str(payload["schema_version"], "narrative_manifest_schema"),
            source_ref=_plain_str(payload["source_ref"], "narrative_source_ref"),
            source_digest=_plain_str(payload["source_digest"], "narrative_source_digest"),
            narrative_package_ref=_plain_str(payload["narrative_package_ref"], "narrative_package_ref"),
            narrative_package_digest=_plain_str(payload["narrative_package_digest"], "narrative_package_digest"),
            status=_plain_str(payload["status"], "narrative_manifest_status"),
            contract_versions=tuple(sorted((key, item) for key, item in versions.items())),
        )

    def contract_version(self, name: str) -> str:
        return dict(self.contract_versions)[name]

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "narrative_package_ref": self.narrative_package_ref,
            "narrative_package_digest": self.narrative_package_digest,
            "status": self.status,
            "contract_versions": dict(self.contract_versions),
        }


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    event: str
    at: str
    attempt_id: str
    failure_fingerprint: str
    reason_codes: tuple[str, ...]
    operator_request_id: str

    def __post_init__(self) -> None:
        if type(self.event) is not str or self.event not in _HISTORY_EVENTS:
            raise RegistryError("quarantine_history_invalid")
        _timestamp(self.at, "quarantine_history_time")
        if type(self.attempt_id) is not str or (self.attempt_id and _HEX32.fullmatch(self.attempt_id) is None):
            raise RegistryError("quarantine_history_invalid")
        if type(self.failure_fingerprint) is not str or (
            self.failure_fingerprint and _HEX64.fullmatch(self.failure_fingerprint) is None
        ):
            raise RegistryError("quarantine_history_invalid")
        if type(self.reason_codes) is not tuple or (
            self.reason_codes and canonical_reason_codes(self.reason_codes) != self.reason_codes
        ):
            raise RegistryError("quarantine_history_invalid")
        if type(self.operator_request_id) is not str or (
            self.operator_request_id and _SAFE_OPERATOR_ID.fullmatch(self.operator_request_id) is None
        ):
            raise RegistryError("quarantine_history_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "event": self.event,
            "at": self.at,
            "attempt_id": self.attempt_id,
            "failure_fingerprint": self.failure_fingerprint,
            "reason_codes": list(self.reason_codes),
            "operator_request_id": self.operator_request_id,
        }

    @classmethod
    def from_dict(cls, value: object) -> "HistoryEvent":
        payload = _registry_exact_dict(value, HISTORY_KEYS, "quarantine_history_invalid")
        reasons = _strict_string_list(payload["reason_codes"], "quarantine_history_invalid")
        return cls(
            event=_registry_str(payload["event"], "quarantine_history_invalid"),
            at=_registry_str(payload["at"], "quarantine_history_invalid"),
            attempt_id=_registry_str(payload["attempt_id"], "quarantine_history_invalid", allow_empty=True),
            failure_fingerprint=_registry_str(payload["failure_fingerprint"], "quarantine_history_invalid", allow_empty=True),
            reason_codes=reasons,
            operator_request_id=_registry_str(payload["operator_request_id"], "quarantine_history_invalid", allow_empty=True),
        )


def _registry_exact_dict(value: object, keys: frozenset[str], reason: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys or any(type(key) is not str for key in value):
        raise RegistryError(reason)
    return value


def _registry_str(value: object, reason: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise RegistryError(reason)
    return value


def _strict_string_list(value: object, reason: str, *, allow_empty_items: bool = False) -> tuple[str, ...]:
    if type(value) is not list:
        raise RegistryError(reason)
    rows: list[str] = []
    for item in value:
        if type(item) is not str or (not allow_empty_items and not item):
            raise RegistryError(reason)
        rows.append(item)
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class MaterialRecord:
    source_ref: str
    source_digest: str
    classification: str
    status: str
    narrative_package_ref: str
    narrative_package_digest: str
    first_observed_at: str
    last_observed_at: str
    attempt_count: int
    active_attempt_id: str
    failure_fingerprint: str
    safe_reason_codes: tuple[str, ...]
    director_contract_version: str
    narrative_contract_version: str
    eligibility_contract_version: str
    notification_ids: tuple[str, ...]
    manual_retry_ids: tuple[str, ...]
    history: tuple[HistoryEvent, ...]

    def __post_init__(self) -> None:
        _safe_logical_ref(self.source_ref, "quarantine_source_ref")
        _plain_hex64(self.source_digest, "quarantine_source_digest")
        if type(self.classification) is not str or self.classification not in CLASSIFICATIONS:
            raise RegistryError("quarantine_classification_invalid")
        if type(self.status) is not str or self.status not in MATERIAL_STATUSES:
            raise RegistryError("quarantine_status_invalid")
        if self.classification == CLASS_RAW:
            if self.narrative_package_ref or self.narrative_package_digest or self.status != STATUS_NEEDS_NARRATIVE:
                raise RegistryError("quarantine_raw_record_invalid")
        else:
            _safe_logical_ref(self.narrative_package_ref, "quarantine_package_ref")
            _plain_hex64(self.narrative_package_digest, "quarantine_package_digest")
            if self.status == STATUS_NEEDS_NARRATIVE:
                raise RegistryError("quarantine_ready_record_invalid")
        _timestamp(self.first_observed_at, "quarantine_first_observed")
        _timestamp(self.last_observed_at, "quarantine_last_observed")
        if type(self.attempt_count) is not int or self.attempt_count < 0:
            raise RegistryError("quarantine_attempt_count_invalid")
        if type(self.active_attempt_id) is not str or (
            self.active_attempt_id and _HEX32.fullmatch(self.active_attempt_id) is None
        ):
            raise RegistryError("quarantine_attempt_id_invalid")
        if (self.status == STATUS_PROCESSING) != bool(self.active_attempt_id):
            raise RegistryError("quarantine_attempt_state_invalid")
        if type(self.failure_fingerprint) is not str or (
            self.failure_fingerprint and _HEX64.fullmatch(self.failure_fingerprint) is None
        ):
            raise RegistryError("quarantine_failure_fingerprint_invalid")
        if type(self.safe_reason_codes) is not tuple or (
            self.safe_reason_codes
            and canonical_reason_codes(self.safe_reason_codes) != self.safe_reason_codes
        ):
            raise RegistryError("quarantine_reason_codes_invalid")
        if self.status == STATUS_BLOCKED and not self.failure_fingerprint:
            raise RegistryError("quarantine_blocked_record_invalid")
        if self.status != STATUS_BLOCKED and (self.failure_fingerprint or self.safe_reason_codes):
            raise RegistryError("quarantine_failure_state_invalid")
        _safe_version(self.director_contract_version, "quarantine_director_version")
        _safe_version(self.narrative_contract_version, "quarantine_narrative_version")
        _safe_version(self.eligibility_contract_version, "quarantine_eligibility_version")
        if type(self.notification_ids) is not tuple or any(
            type(item) is not str or _HEX32.fullmatch(item) is None for item in self.notification_ids
        ) or len(set(self.notification_ids)) != len(self.notification_ids):
            raise RegistryError("quarantine_notification_ids_invalid")
        if type(self.manual_retry_ids) is not tuple or any(
            type(item) is not str or _SAFE_OPERATOR_ID.fullmatch(item) is None for item in self.manual_retry_ids
        ) or len(set(self.manual_retry_ids)) != len(self.manual_retry_ids):
            raise RegistryError("quarantine_manual_retry_ids_invalid")
        if len(self.manual_retry_ids) > _MAX_RETRY_IDS:
            raise RegistryError("quarantine_manual_retry_ids_invalid")
        if type(self.history) is not tuple or any(type(item) is not HistoryEvent for item in self.history):
            raise RegistryError("quarantine_history_invalid")
        if len(self.history) > _MAX_HISTORY:
            raise RegistryError("quarantine_history_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_ref": self.source_ref,
            "source_digest": self.source_digest,
            "classification": self.classification,
            "status": self.status,
            "narrative_package_ref": self.narrative_package_ref,
            "narrative_package_digest": self.narrative_package_digest,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            "attempt_count": self.attempt_count,
            "active_attempt_id": self.active_attempt_id,
            "failure_fingerprint": self.failure_fingerprint,
            "safe_reason_codes": list(self.safe_reason_codes),
            "director_contract_version": self.director_contract_version,
            "narrative_contract_version": self.narrative_contract_version,
            "eligibility_contract_version": self.eligibility_contract_version,
            "notification_ids": list(self.notification_ids),
            "manual_retry_ids": list(self.manual_retry_ids),
            "history": [item.to_dict() for item in self.history],
        }

    @classmethod
    def from_dict(cls, value: object) -> "MaterialRecord":
        payload = _registry_exact_dict(value, RECORD_KEYS, "quarantine_record_shape_invalid")
        history = payload["history"]
        if type(history) is not list:
            raise RegistryError("quarantine_history_invalid")
        return cls(
            source_ref=_registry_str(payload["source_ref"], "quarantine_source_ref_invalid"),
            source_digest=_registry_str(payload["source_digest"], "quarantine_source_digest_invalid"),
            classification=_registry_str(payload["classification"], "quarantine_classification_invalid"),
            status=_registry_str(payload["status"], "quarantine_status_invalid"),
            narrative_package_ref=_registry_str(payload["narrative_package_ref"], "quarantine_package_ref_invalid", allow_empty=True),
            narrative_package_digest=_registry_str(payload["narrative_package_digest"], "quarantine_package_digest_invalid", allow_empty=True),
            first_observed_at=_registry_str(payload["first_observed_at"], "quarantine_first_observed_invalid"),
            last_observed_at=_registry_str(payload["last_observed_at"], "quarantine_last_observed_invalid"),
            attempt_count=_plain_int(payload["attempt_count"], "quarantine_attempt_count"),
            active_attempt_id=_registry_str(payload["active_attempt_id"], "quarantine_attempt_id_invalid", allow_empty=True),
            failure_fingerprint=_registry_str(payload["failure_fingerprint"], "quarantine_failure_fingerprint_invalid", allow_empty=True),
            safe_reason_codes=_strict_string_list(payload["safe_reason_codes"], "quarantine_reason_codes_invalid"),
            director_contract_version=_registry_str(payload["director_contract_version"], "quarantine_director_version_invalid"),
            narrative_contract_version=_registry_str(payload["narrative_contract_version"], "quarantine_narrative_version_invalid"),
            eligibility_contract_version=_registry_str(payload["eligibility_contract_version"], "quarantine_eligibility_version_invalid"),
            notification_ids=_strict_string_list(payload["notification_ids"], "quarantine_notification_ids_invalid"),
            manual_retry_ids=_strict_string_list(payload["manual_retry_ids"], "quarantine_manual_retry_ids_invalid"),
            history=tuple(HistoryEvent.from_dict(item) for item in history),
        )


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    notification_id: str
    kind: str
    aggregate_fingerprint: str
    state: str
    created_at: str
    claimed_at: str
    completed_at: str
    attempt_count: int
    safe_summary_code: str
    operator_request_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.notification_id) is not str or _HEX32.fullmatch(self.notification_id) is None:
            raise RegistryError("quarantine_notification_id_invalid")
        if type(self.kind) is not str or self.kind not in NOTIFICATION_KINDS:
            raise RegistryError("quarantine_notification_kind_invalid")
        if type(self.aggregate_fingerprint) is not str or _HEX64.fullmatch(self.aggregate_fingerprint) is None:
            raise RegistryError("quarantine_notification_fingerprint_invalid")
        if type(self.state) is not str or self.state not in NOTIFICATION_STATES:
            raise RegistryError("quarantine_notification_state_invalid")
        _timestamp(self.created_at, "quarantine_notification_created")
        _timestamp(self.claimed_at, "quarantine_notification_claimed", allow_empty=True)
        _timestamp(self.completed_at, "quarantine_notification_completed", allow_empty=True)
        if type(self.attempt_count) is not int or self.attempt_count < 0:
            raise RegistryError("quarantine_notification_attempt_invalid")
        if self.state == NOTIFICATION_PENDING and (self.claimed_at or self.completed_at):
            raise RegistryError("quarantine_notification_state_invalid")
        if self.state == NOTIFICATION_CLAIMED and (not self.claimed_at or self.completed_at):
            raise RegistryError("quarantine_notification_state_invalid")
        if self.state in {NOTIFICATION_SENT, NOTIFICATION_FAILED, NOTIFICATION_UNCERTAIN} and (
            not self.claimed_at or not self.completed_at
        ):
            raise RegistryError("quarantine_notification_state_invalid")
        if self.state == NOTIFICATION_PENDING and self.attempt_count != 0:
            raise RegistryError("quarantine_notification_attempt_invalid")
        if self.state != NOTIFICATION_PENDING and self.attempt_count < 1:
            raise RegistryError("quarantine_notification_attempt_invalid")
        if type(self.safe_summary_code) is not str or self.safe_summary_code not in SUMMARY_CODES:
            raise RegistryError("quarantine_notification_summary_invalid")
        if type(self.operator_request_ids) is not tuple or any(
            type(item) is not str or _SAFE_OPERATOR_ID.fullmatch(item) is None for item in self.operator_request_ids
        ) or len(set(self.operator_request_ids)) != len(self.operator_request_ids):
            raise RegistryError("quarantine_notification_retry_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "notification_id": self.notification_id,
            "kind": self.kind,
            "aggregate_fingerprint": self.aggregate_fingerprint,
            "state": self.state,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "completed_at": self.completed_at,
            "attempt_count": self.attempt_count,
            "safe_summary_code": self.safe_summary_code,
            "operator_request_ids": list(self.operator_request_ids),
        }

    @classmethod
    def from_dict(cls, value: object) -> "NotificationRecord":
        payload = _registry_exact_dict(value, NOTIFICATION_KEYS, "quarantine_notification_shape_invalid")
        return cls(
            notification_id=_registry_str(payload["notification_id"], "quarantine_notification_id_invalid"),
            kind=_registry_str(payload["kind"], "quarantine_notification_kind_invalid"),
            aggregate_fingerprint=_registry_str(payload["aggregate_fingerprint"], "quarantine_notification_fingerprint_invalid"),
            state=_registry_str(payload["state"], "quarantine_notification_state_invalid"),
            created_at=_registry_str(payload["created_at"], "quarantine_notification_created_invalid"),
            claimed_at=_registry_str(payload["claimed_at"], "quarantine_notification_claimed_invalid", allow_empty=True),
            completed_at=_registry_str(payload["completed_at"], "quarantine_notification_completed_invalid", allow_empty=True),
            attempt_count=_plain_int(payload["attempt_count"], "quarantine_notification_attempt"),
            safe_summary_code=_registry_str(payload["safe_summary_code"], "quarantine_notification_summary_invalid"),
            operator_request_ids=_strict_string_list(payload["operator_request_ids"], "quarantine_notification_retry_invalid"),
        )


@dataclass(frozen=True, slots=True)
class MaterialRegistry:
    registry_revision: int
    records: tuple[MaterialRecord, ...]
    notifications: tuple[NotificationRecord, ...]
    current_raw_fingerprint: str

    def __post_init__(self) -> None:
        if type(self.registry_revision) is not int or self.registry_revision < 0:
            raise RegistryError("quarantine_registry_revision_invalid")
        if type(self.records) is not tuple or any(type(item) is not MaterialRecord for item in self.records):
            raise RegistryError("quarantine_records_invalid")
        if tuple(sorted(self.records, key=lambda item: item.source_ref)) != self.records:
            raise RegistryError("quarantine_records_order_invalid")
        refs = tuple(item.source_ref for item in self.records)
        if len(refs) != len(set(refs)):
            raise RegistryError("quarantine_duplicate_source_ref")
        if type(self.notifications) is not tuple or any(type(item) is not NotificationRecord for item in self.notifications):
            raise RegistryError("quarantine_notifications_invalid")
        if tuple(sorted(self.notifications, key=lambda item: item.notification_id)) != self.notifications:
            raise RegistryError("quarantine_notifications_order_invalid")
        ids = tuple(item.notification_id for item in self.notifications)
        if len(ids) != len(set(ids)):
            raise RegistryError("quarantine_duplicate_notification_id")
        notifications_by_id = {item.notification_id: item for item in self.notifications}
        for record in self.records:
            if any(item not in notifications_by_id for item in record.notification_ids):
                raise RegistryError("quarantine_notification_reference_invalid")
            if record.status == STATUS_BLOCKED:
                matching = [notifications_by_id[item] for item in record.notification_ids]
                if not any(
                    item.kind == DIRECTOR_REJECTION_KIND
                    and item.aggregate_fingerprint == record.failure_fingerprint
                    for item in matching
                ):
                    raise RegistryError("quarantine_blocked_record_invalid")
        if type(self.current_raw_fingerprint) is not str or (
            self.current_raw_fingerprint and _HEX64.fullmatch(self.current_raw_fingerprint) is None
        ):
            raise RegistryError("quarantine_aggregate_invalid")

    @classmethod
    def empty(cls) -> "MaterialRegistry":
        return cls(0, (), (), "")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "registry_revision": self.registry_revision,
            "records": [item.to_dict() for item in self.records],
            "notifications": [item.to_dict() for item in self.notifications],
            "aggregates": {"current_raw_fingerprint": self.current_raw_fingerprint},
        }

    @classmethod
    def from_dict(cls, value: object) -> "MaterialRegistry":
        payload = _registry_exact_dict(value, REGISTRY_KEYS, "quarantine_registry_shape_invalid")
        if type(payload["schema_version"]) is not str or payload["schema_version"] != REGISTRY_SCHEMA_VERSION:
            raise RegistryError("quarantine_registry_schema_invalid")
        records = payload["records"]
        notifications = payload["notifications"]
        aggregates = _registry_exact_dict(payload["aggregates"], AGGREGATE_KEYS, "quarantine_aggregate_invalid")
        if type(records) is not list or type(notifications) is not list:
            raise RegistryError("quarantine_registry_shape_invalid")
        return cls(
            registry_revision=_plain_int(payload["registry_revision"], "quarantine_registry_revision"),
            records=tuple(MaterialRecord.from_dict(item) for item in records),
            notifications=tuple(NotificationRecord.from_dict(item) for item in notifications),
            current_raw_fingerprint=_registry_str(aggregates["current_raw_fingerprint"], "quarantine_aggregate_invalid", allow_empty=True),
        )


@dataclass(frozen=True, slots=True)
class DiscoveredSource:
    source_ref: str
    source_path: Path
    source_digest: str
    classification: str
    manifest: NarrativeReadyManifest | None


@dataclass(frozen=True, slots=True)
class EligibleCandidate:
    source_ref: str
    source_digest: str
    date_text: str
    narrative_package_ref: str
    narrative_package_digest: str
    director_contract_version: str
    narrative_contract_version: str
    eligibility_contract_version: str


@dataclass(frozen=True, slots=True)
class AttemptClaim:
    candidate: EligibleCandidate
    attempt_id: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class NotificationClaim:
    notification_id: str
    kind: str
    aggregate_fingerprint: str
    safe_summary_code: str
    attempt_count: int


@dataclass(frozen=True, slots=True)
class QuarantineStatusItem:
    material_id: str
    classification: str
    status: str
    attempt_count: int
    failure_fingerprint_id: str
    safe_reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BacklogReconciliationResult:
    discovered_count: int
    raw_count: int
    narrative_ready_count: int
    blocked_count: int
    consumed_count: int
    newly_registered_count: int
    changed_count: int
    aggregate_fingerprint: str
    aggregate_notification_claim: NotificationClaim | None


@dataclass(frozen=True, slots=True)
class _RegistrySnapshot:
    registry: MaterialRegistry
    exists: bool
    raw_digest: str


def _canonical_json(value: object) -> bytes:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise RegistryError("quarantine_registry_serialize_failed") from exc
    return (text + "\n").encode("utf-8")


def _broker_payload_digest(value: object) -> str:
    """Match the Broker's canonical IPC payload digest (no file terminator)."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise EligibilityError("narrative_approval_attestation_invalid") from exc
    return hashlib.sha256(encoded).hexdigest()


def _read_registry_snapshot(path: Path) -> _RegistrySnapshot:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _RegistrySnapshot(MaterialRegistry.empty(), False, "")
    except OSError as exc:
        raise RegistryError("quarantine_registry_read_failed") from exc
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError("quarantine_registry_corrupt") from exc
    try:
        registry = MaterialRegistry.from_dict(payload)
    except EligibilityError as exc:
        raise RegistryError("quarantine_registry_invalid") from exc
    return _RegistrySnapshot(registry, True, hashlib.sha256(raw).hexdigest())


def read_registry(path: Path) -> MaterialRegistry:
    return _read_registry_snapshot(Path(path)).registry


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        flush_file_buffers = kernel32.FlushFileBuffers
        flush_file_buffers.argtypes = (wintypes.HANDLE,)
        flush_file_buffers.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(str(path), 0x40000000, 0x00000007, None, 3, 0x02000000, None)
        invalid = wintypes.HANDLE(-1).value
        if handle in (None, invalid):
            raise RegistryError("quarantine_registry_directory_fsync_failed")
        try:
            if not flush_file_buffers(handle):
                error = ctypes.get_last_error()
                # Some Windows filesystems reject directory flush.  Atomic replace
                # plus file flush remains the documented durability strategy there.
                if error not in {1, 5, 6, 87}:
                    raise RegistryError("quarantine_registry_directory_fsync_failed")
        finally:
            close_handle(handle)
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _InterProcessLock:
    """Kernel lock with no lock-file artifact."""

    def __init__(self, registry_root: Path) -> None:
        self.registry_root = registry_root
        self._handle: int | None = None
        self._fd: int | None = None

    def __enter__(self) -> "_InterProcessLock":
        self.registry_root.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            name = "Local\\NazReelsQuarantine-" + hashlib.sha256(
                str(self.registry_root).casefold().encode("utf-8")
            ).hexdigest()
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
            create_mutex.restype = wintypes.HANDLE
            wait_for_single_object = kernel32.WaitForSingleObject
            wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            wait_for_single_object.restype = wintypes.DWORD
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            handle = create_mutex(None, False, name)
            if not handle:
                raise RegistryError("quarantine_registry_lock_failed")
            outcome = wait_for_single_object(handle, 0xFFFFFFFF)
            if outcome not in {0x00000000, 0x00000080}:
                close_handle(handle)
                raise RegistryError("quarantine_registry_lock_failed")
            self._handle = int(handle)
            return self
        import fcntl
        fd = os.open(self.registry_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if os.name == "nt" and self._handle is not None:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            release_mutex = kernel32.ReleaseMutex
            release_mutex.argtypes = (wintypes.HANDLE,)
            release_mutex.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            handle = wintypes.HANDLE(self._handle)
            try:
                release_mutex(handle)
            finally:
                close_handle(handle)
                self._handle = None
        elif self._fd is not None:
            import fcntl
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def _atomic_write_registry(path: Path, registry: MaterialRegistry, expected: _RegistrySnapshot) -> None:
    payload = _canonical_json(registry.to_dict())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = -1
    temp_path: Path | None = None
    previous_raw = b""
    promoted = False
    operation_error: BaseException | None = None
    try:
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        staged = _read_registry_snapshot(temp_path)
        if not staged.exists or staged.registry != registry:
            raise RegistryError("quarantine_registry_staging_invalid")
        current = _read_registry_snapshot(path)
        if (
            current.exists != expected.exists
            or current.registry.registry_revision != expected.registry.registry_revision
            or current.raw_digest != expected.raw_digest
        ):
            raise RegistryError("quarantine_registry_conflict")
        if current.exists:
            try:
                previous_raw = path.read_bytes()
            except OSError as exc:
                raise RegistryError("quarantine_registry_read_failed") from exc
            if hashlib.sha256(previous_raw).hexdigest() != current.raw_digest:
                raise RegistryError("quarantine_registry_conflict")
        # Make creation of the staged directory entry durable before promotion,
        # then make the atomic promotion durable with a second directory fsync.
        _fsync_directory(path.parent)
        os.replace(temp_path, path)
        temp_path = None
        promoted = True
        _fsync_directory(path.parent)
        final = _read_registry_snapshot(path)
        if final.registry != registry or final.raw_digest != hashlib.sha256(payload).hexdigest():
            raise RegistryError("quarantine_registry_final_invalid")
    except RegistryError as exc:
        operation_error = exc
    except OSError as exc:
        operation_error = RegistryError("quarantine_registry_write_failed")
        operation_error.__cause__ = exc
    except BaseException as exc:
        operation_error = exc
    finally:
        if operation_error is not None and promoted:
            rollback_path: Path | None = None
            try:
                if expected.exists:
                    rollback_fd, rollback_name = tempfile.mkstemp(
                        prefix=f".{path.name}.", suffix=".rollback.tmp", dir=str(path.parent)
                    )
                    rollback_path = Path(rollback_name)
                    try:
                        os.fchmod(rollback_fd, 0o600)
                    except (AttributeError, OSError):
                        pass
                    with os.fdopen(rollback_fd, "wb", closefd=True) as rollback_handle:
                        rollback_handle.write(previous_raw)
                        rollback_handle.flush()
                        os.fsync(rollback_handle.fileno())
                    os.replace(rollback_path, path)
                    rollback_path = None
                elif path.exists():
                    path.unlink()
            except BaseException as exc:
                operation_error = RegistryError("quarantine_registry_rollback_failed")
                operation_error.__cause__ = exc
            finally:
                if rollback_path is not None and rollback_path.exists():
                    try:
                        rollback_path.unlink()
                    except OSError:
                        operation_error = RegistryError("quarantine_registry_cleanup_failed")
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError as exc:
                operation_error = RegistryError("quarantine_registry_cleanup_failed")
                operation_error.__cause__ = exc
            if temp_path.exists():
                operation_error = RegistryError("quarantine_registry_cleanup_failed")
    if operation_error is not None:
        raise operation_error


def _mutate_registry(policy: QuarantinePathPolicy, mutate: Callable[[MaterialRegistry], tuple[MaterialRegistry, object]]) -> object:
    with _InterProcessLock(policy.registry_path.parent):
        for attempt in range(_MAX_CAS_RETRIES):
            snapshot = _read_registry_snapshot(policy.registry_path)
            updated, result = mutate(snapshot.registry)
            if type(updated) is not MaterialRegistry:
                raise RegistryError("quarantine_registry_mutation_invalid")
            if updated == snapshot.registry:
                return result
            if updated.registry_revision != snapshot.registry.registry_revision + 1:
                raise RegistryError("quarantine_registry_revision_invalid")
            try:
                _atomic_write_registry(policy.registry_path, updated, snapshot)
            except RegistryError as exc:
                if (
                    exc.reason_code == "quarantine_registry_conflict"
                    and attempt + 1 < _MAX_CAS_RETRIES
                ):
                    continue
                raise
            return result
    raise RegistryError("quarantine_registry_conflict")


def _tree_digest(path: Path, *, exclude_manifest: bool, require_file: bool, reason: str) -> str:
    try:
        root = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise EligibilityError(reason) from exc
    if root.is_symlink() or not (root.is_file() or root.is_dir()):
        raise EligibilityError(reason)
    rows: list[tuple[str, str, Path]] = []
    if root.is_file():
        if root.is_symlink():
            raise EligibilityError(reason)
        rows.append((root.name, "file", root))
    else:
        for item in root.rglob("*"):
            relative = item.relative_to(root).as_posix()
            if exclude_manifest and relative == "narrative_ready.json":
                continue
            if item.is_symlink():
                raise EligibilityError(reason)
            if item.is_dir():
                kind = "directory"
            elif item.is_file():
                kind = "file"
            else:
                raise EligibilityError(reason)
            rows.append((relative, kind, item))
    rows.sort(key=lambda row: (row[0], row[1]))
    file_count = sum(kind == "file" for _, kind, _ in rows)
    if require_file and file_count == 0:
        raise EligibilityError(reason)
    digest = hashlib.sha256()
    for relative, kind, item in rows:
        digest.update(kind.encode("ascii"))
        digest.update(b"\0")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if kind == "file":
            try:
                digest.update(item.read_bytes())
            except OSError as exc:
                raise EligibilityError(reason) from exc
        digest.update(b"\0")
    return digest.hexdigest()


def source_digest(path: Path) -> str:
    return _tree_digest(Path(path), exclude_manifest=True, require_file=True, reason="narrative_source_invalid")


def narrative_package_digest(path: Path) -> str:
    """Historical legacy tree-envelope digest; frozen for compatibility."""
    return _tree_digest(Path(path), exclude_manifest=False, require_file=True, reason="narrative_package_invalid")


def _resolve_ref(root: Path, value: str, *, require_directory: bool, reason: str) -> Path:
    ref = _safe_logical_ref(value, reason)
    candidate = root.joinpath(*PurePosixPath(ref).parts)
    try:
        resolved = candidate.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise EligibilityError(reason) from exc
    if not _is_within(resolved, root_resolved):
        raise EligibilityError(reason)
    _reject_symlink_chain(root_resolved, candidate, reason)
    if require_directory and not resolved.is_dir():
        raise EligibilityError(reason)
    if not require_directory and not (resolved.is_file() or resolved.is_dir()):
        raise EligibilityError(reason)
    return resolved


def _load_manifest(path: Path) -> NarrativeReadyManifest:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EligibilityError("narrative_manifest_invalid") from exc
    return NarrativeReadyManifest.from_mapping(payload)


def narrative_source_identity(
    source_ref: str,
    source_digest_value: str,
    source_contract_version: str = NARRATIVE_SOURCE_CONTRACT_VERSION,
) -> str:
    """Versioned identity shared by production discovery and the normalizer."""
    ref = _safe_logical_ref(source_ref, "narrative_source_ref")
    digest = _plain_hex64(source_digest_value, "narrative_source_digest")
    version = _safe_version(source_contract_version, "narrative_source_contract_version")
    raw = ref.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\0" + version.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _enumerate_source_paths(inbox: Path) -> tuple[Path, ...]:
    if not inbox.exists():
        return ()
    if inbox.is_symlink() or not inbox.is_dir():
        raise EligibilityError("quarantine_inbox_invalid")
    try:
        first_level = sorted(inbox.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise EligibilityError("quarantine_inbox_unavailable") from exc
    result: list[Path] = []
    for first in first_level:
        if first.is_symlink():
            raise EligibilityError("quarantine_source_symlink_invalid")
        if not first.is_dir():
            continue
        if _DATE.fullmatch(first.name):
            result.append(first)
            continue
        if _SAFE_COMPONENT.fullmatch(first.name) is None:
            continue
        try:
            second_level = sorted(first.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise EligibilityError("quarantine_inbox_unavailable") from exc
        for second in second_level:
            if second.is_symlink():
                raise EligibilityError("quarantine_source_symlink_invalid")
            if second.is_dir() and _DATE.fullmatch(second.name):
                result.append(second)
    return tuple(sorted(set(result), key=lambda item: item.relative_to(inbox).as_posix().casefold()))


def _legacy_digest_is_unambiguous(policy: QuarantinePathPolicy, source_digest_value: str) -> bool:
    matches = 0
    for candidate in _enumerate_source_paths(policy.inbox_root):
        if source_digest(candidate) == source_digest_value:
            matches += 1
            if matches > 1:
                return False
    return matches == 1


@dataclass(frozen=True, slots=True)
class _NarrativeReadyArtifact:
    path: Path
    layout: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or self.layout not in _NARRATIVE_READY_LAYOUTS:
            raise EligibilityError("narrative_manifest_invalid")


def _narrative_ready_artifact(
    policy: QuarantinePathPolicy,
    source_path: Path,
    source_ref: str,
    source_digest_value: str,
) -> _NarrativeReadyArtifact | None:
    """Classify exactly one manifest layout before any package digest check.

    Layout is derived only from code-owned discovery paths.  No manifest field or
    caller argument can select a weaker validation path.
    """
    _plain_hex64(source_digest_value, "narrative_source_digest")
    legacy = source_path / "narrative_ready.json"
    identity = narrative_source_identity(source_ref, source_digest_value)
    external = policy.narrative_outbox_root / identity / "narrative_ready.json"
    digest_legacy = policy.narrative_outbox_root / source_digest_value / "narrative_ready.json"
    for parent in (external.parent, digest_legacy.parent):
        if not os.path.lexists(parent):
            continue
        if parent.is_symlink() or not parent.is_dir():
            raise EligibilityError("narrative_manifest_invalid")
        try:
            outbox = policy.narrative_outbox_root.resolve(strict=True)
            resolved_parent = parent.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise EligibilityError("narrative_manifest_invalid") from exc
        if not _is_within(resolved_parent, outbox):
            raise EligibilityError("narrative_manifest_invalid")
        _reject_symlink_chain(outbox, parent, "narrative_manifest_invalid")
    candidates = (
        (legacy, _LAYOUT_LEGACY_SOURCE_SIDE),
        (external, _LAYOUT_NORMALIZER_IDENTITY_V2),
        (digest_legacy, _LAYOUT_LEGACY_DIGEST_ONLY),
    )
    present: list[_NarrativeReadyArtifact] = []
    for candidate, layout in candidates:
        if os.path.lexists(candidate):
            if candidate.is_symlink() or not candidate.is_file():
                raise EligibilityError("narrative_manifest_invalid")
            if candidate in {external, digest_legacy}:
                try:
                    outbox = policy.narrative_outbox_root.resolve(strict=True)
                    resolved = candidate.resolve(strict=True)
                except (FileNotFoundError, OSError) as exc:
                    raise EligibilityError("narrative_manifest_invalid") from exc
                if not _is_within(resolved, outbox):
                    raise EligibilityError("narrative_manifest_invalid")
                _reject_symlink_chain(outbox, candidate, "narrative_manifest_invalid")
            if candidate == digest_legacy and not _legacy_digest_is_unambiguous(policy, source_digest_value):
                raise EligibilityError("narrative_manifest_ambiguous")
            present.append(_NarrativeReadyArtifact(candidate, layout))
    if len(present) > 1:
        raise EligibilityError("narrative_manifest_ambiguous")
    if not present:
        return None

    artifact = present[0]
    if artifact.layout != _LAYOUT_NORMALIZER_IDENTITY_V2:
        try:
            identity_names = (
                frozenset(item.name for item in external.parent.iterdir())
                if external.parent.exists()
                else frozenset()
            )
        except OSError as exc:
            raise EligibilityError("narrative_manifest_invalid") from exc
        if identity_names & _V2_IDENTITY_ARTIFACT_NAMES:
            raise EligibilityError("narrative_manifest_ambiguous")
        if artifact.layout == _LAYOUT_LEGACY_SOURCE_SIDE and os.path.lexists(
            source_path / "approval-attestation.json"
        ):
            raise EligibilityError("narrative_manifest_ambiguous")
    return artifact


def _narrative_ready_manifest_path(
    policy: QuarantinePathPolicy,
    source_path: Path,
    source_ref: str,
    source_digest_value: str,
) -> Path | None:
    """Compatibility wrapper for internal callers that only need the path."""
    artifact = _narrative_ready_artifact(
        policy,
        source_path,
        source_ref,
        source_digest_value,
    )
    return None if artifact is None else artifact.path


def validate_narrative_ready_manifest(
    policy: QuarantinePathPolicy,
    source_ref: str,
    *,
    trust_service: normalizer_trust.NarrativeTrustService | None = None,
    expected_review_event: normalizer_review_state.ReviewEvent | None = None,
) -> NarrativeReadyManifest:
    source_path = _resolve_ref(
        policy.inbox_root, source_ref, require_directory=True, reason="narrative_source_ref_invalid"
    )
    actual_source_digest = source_digest(source_path)
    artifact = _narrative_ready_artifact(
        policy,
        source_path,
        source_ref,
        actual_source_digest,
    )
    if artifact is None:
        raise EligibilityError("narrative_manifest_missing")
    manifest_path = artifact.path
    manifest = _load_manifest(manifest_path)
    if artifact.layout == _LAYOUT_NORMALIZER_IDENTITY_V2:
        validated = _validate_identity_v2_ready_payload(
            policy,
            source_ref,
            manifest.to_mapping(),
        )
        _validate_normalizer_approval_pair(
            policy,
            source_ref,
            validated,
            manifest_path,
            trust_service=trust_service,
            expected_review_event=expected_review_event,
        )
    else:
        validated = _validate_legacy_ready_payload(
            policy,
            source_ref,
            manifest.to_mapping(),
        )
    return validated


def _hash_regular_file(path: Path) -> str:
    if not os.path.lexists(path) or path.is_symlink() or not path.is_file():
        raise EligibilityError("narrative_approval_attestation_invalid")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _broker_call(
    policy: QuarantinePathPolicy,
    method: str,
    payload: dict[str, object],
) -> dict[str, object]:
    client = policy.review_authority_client
    if client is None:
        raise EligibilityError("narrative_approval_authority_unavailable")
    try:
        result = getattr(client, method)(
            f"{method.replace('_', '-')}-{secrets.token_hex(16)}",
            payload,
        )
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        raise EligibilityError("narrative_approval_authority_unavailable") from None
    if type(result) is not dict:
        raise EligibilityError("narrative_approval_authority_invalid")
    return result


def _validate_broker_approval_pair(
    policy: QuarantinePathPolicy,
    source_ref: str,
    manifest: NarrativeReadyManifest,
    manifest_path: Path,
    raw_attestation: object,
) -> None:
    if type(raw_attestation) is not dict or frozenset(raw_attestation) != _BROKER_ATTESTATION_KEYS:
        raise EligibilityError("narrative_approval_attestation_invalid")
    if (
        raw_attestation.get("schema_version")
        != normalizer_review_state.DUAL_DIGEST_APPROVAL_ATTESTATION_SCHEMA_VERSION
    ):
        raise EligibilityError("narrative_approval_attestation_invalid")
    identity = narrative_source_identity(source_ref, manifest.source_digest)
    draft = policy.narrative_outbox_root / identity
    story_path = draft / "story.json"
    draft_package_digest = _plain_hex64(
        raw_attestation.get("draft_package_digest"),
        "narrative_draft_package_digest",
    )
    expected_draft_identity = _broker_payload_digest({
        "version": _BROKER_DRAFT_CONTRACT_VERSION,
        "source_identity": identity,
        "package_digest": draft_package_digest,
    })
    attestation_digest = _broker_payload_digest(raw_attestation)
    versions = raw_attestation.get("contract_versions")
    if (
        type(versions) is not dict
        or versions.get("draft") != _BROKER_DRAFT_CONTRACT_VERSION
        or versions.get("source") != NARRATIVE_SOURCE_CONTRACT_VERSION
        or versions.get("ready_manifest") != MANIFEST_SCHEMA_VERSION
        or raw_attestation.get("source_identity") != identity
        or raw_attestation.get("source_ref") != source_ref
        or raw_attestation.get("source_digest") != manifest.source_digest
        or raw_attestation.get("draft_identity") != expected_draft_identity
        or raw_attestation.get("narrative_package_digest")
        != _hash_regular_file(story_path)
        or raw_attestation.get("narrative_package_digest")
        != manifest.narrative_package_digest
        or raw_attestation.get("narrative_ready_manifest_digest")
        != _hash_regular_file(manifest_path)
        or raw_attestation.get("story_markdown_digest")
        != _hash_regular_file(draft / "story.md")
        or raw_attestation.get("draft_manifest_digest")
        != _hash_regular_file(draft / "draft-manifest.json")
        or raw_attestation.get("review_digest")
        != _hash_regular_file(draft / "review.json")
        or raw_attestation.get("completed_claim_digest")
        != _hash_regular_file(
            policy.narrative_outbox_root
            / ".normalizer-state"
            / "claims"
            / f"{identity}.json"
        )
        or type(raw_attestation.get("review_revision")) is not int
        or int(raw_attestation["review_revision"]) < 3
        or _HEX64.fullmatch(str(raw_attestation.get("review_event_digest", ""))) is None
        or manifest.narrative_package_ref != f"{identity}/story.json"
    ):
        raise EligibilityError("narrative_approval_attestation_invalid")

    latest = _broker_call(
        policy,
        "latest_state",
        {"source_identity": identity, "draft_identity": expected_draft_identity},
    )
    latest_keys = frozenset({
        "source_identity", "draft_identity", "draft_package_digest",
        "revision", "state", "event_digest", "event", "idempotent",
        "storage_adapter_version",
    })
    if (
        frozenset(latest) != latest_keys
        or latest.get("source_identity") != identity
        or latest.get("draft_identity") != expected_draft_identity
        or latest.get("draft_package_digest") != draft_package_digest
        or latest.get("state") != normalizer_review_state.STATE_APPROVED
        or latest.get("revision") != raw_attestation["review_revision"]
        or latest.get("event_digest") != raw_attestation["review_event_digest"]
        or latest.get("storage_adapter_version") != _BROKER_STORAGE_ADAPTER_VERSION
    ):
        raise EligibilityError("narrative_approval_authority_invalid")
    verdict = _broker_call(
        policy,
        "verify_ready",
        {
            "ready_manifest": manifest.to_mapping(),
            "attestation": raw_attestation,
        },
    )
    if (
        frozenset(verdict) != {
            "ready", "verdict_version", "source_identity", "draft_identity",
            "review_revision", "review_event_digest", "attestation_digest",
        }
        or verdict.get("ready") is not True
        or verdict.get("verdict_version") != _BROKER_READY_VERDICT_VERSION
        or verdict.get("source_identity") != identity
        or verdict.get("draft_identity") != expected_draft_identity
        or verdict.get("review_revision") != raw_attestation["review_revision"]
        or verdict.get("review_event_digest") != raw_attestation["review_event_digest"]
        or verdict.get("attestation_digest") != attestation_digest
    ):
        raise EligibilityError("narrative_approval_authority_invalid")


def _validate_normalizer_approval_pair(
    policy: QuarantinePathPolicy,
    source_ref: str,
    manifest: NarrativeReadyManifest,
    manifest_path: Path,
    *,
    trust_service: normalizer_trust.NarrativeTrustService | None,
    expected_review_event: normalizer_review_state.ReviewEvent | None,
) -> None:
    identity = narrative_source_identity(source_ref, manifest.source_digest)
    draft = policy.narrative_outbox_root / identity
    attestation_path = draft / "approval-attestation.json"
    service = trust_service if trust_service is not None else policy.narrative_trust_service
    if (
        type(service) is not normalizer_trust.NarrativeTrustService
        and not os.path.lexists(attestation_path)
    ):
        raise EligibilityError("narrative_approval_trust_missing")
    try:
        if draft.is_symlink() or not draft.is_dir():
            raise EligibilityError("narrative_approval_attestation_invalid")
        if not os.path.lexists(attestation_path) or attestation_path.is_symlink() or not attestation_path.is_file():
            raise EligibilityError("narrative_approval_attestation_invalid")
        raw = json.loads(attestation_path.read_bytes().decode("utf-8"))
        if (
            type(raw) is dict
            and raw.get("schema_version")
            == normalizer_review_state.DUAL_DIGEST_APPROVAL_ATTESTATION_SCHEMA_VERSION
        ):
            _validate_broker_approval_pair(
                policy,
                source_ref,
                manifest,
                manifest_path,
                raw,
            )
            return
        if type(service) is not normalizer_trust.NarrativeTrustService:
            raise EligibilityError("narrative_approval_trust_missing")
        attestation = normalizer_review_state.approval_attestation_from_payload(
            raw,
            service,
            ready_manifest_contract=MANIFEST_SCHEMA_VERSION,
            source_contract=NARRATIVE_SOURCE_CONTRACT_VERSION,
        )
        claim_path = policy.narrative_outbox_root / ".normalizer-state" / "claims" / f"{identity}.json"
        claim_payload = json.loads(claim_path.read_bytes().decode("utf-8"))
        if type(claim_payload) is not dict:
            raise EligibilityError("narrative_approval_attestation_invalid")
        authority_root = policy.narrative_review_authority_root
        if authority_root is None:
            raise EligibilityError("narrative_approval_attestation_invalid")
        ledger = normalizer_review_state.ReviewStateStore(
            authority_root,
            service,
        ).read(identity, expected_draft_identity=attestation.draft_identity)
    except EligibilityError:
        raise
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
        normalizer_review_state.ReviewStateError,
    ):
        raise EligibilityError("narrative_approval_attestation_invalid") from None
    latest = ledger.latest
    if expected_review_event is not None:
        if (
            type(expected_review_event) is not normalizer_review_state.ReviewEvent
            or expected_review_event.source_identity != identity
            or expected_review_event.draft_identity != attestation.draft_identity
            or expected_review_event.previous_revision != latest.revision
            or expected_review_event.previous_event_digest != latest.event_digest
            or expected_review_event.state != normalizer_review_state.STATE_APPROVED
        ):
            raise EligibilityError("narrative_approval_attestation_invalid")
        latest = expected_review_event
    if (
        attestation.source_identity != identity
        or attestation.source_ref != source_ref
        or attestation.source_digest != manifest.source_digest
        or attestation.package_digest != manifest.narrative_package_digest
        or attestation.narrative_ready_manifest_digest != _hash_regular_file(manifest_path)
        or attestation.story_markdown_digest != _hash_regular_file(draft / "story.md")
        or attestation.draft_manifest_digest != _hash_regular_file(draft / "draft-manifest.json")
        or attestation.review_digest != _hash_regular_file(draft / "review.json")
        or attestation.completed_claim_digest != _hash_regular_file(claim_path)
        or claim_payload.get("state") != "completed"
        or claim_payload.get("source_identity") != identity
        or claim_payload.get("draft_identity") != attestation.draft_identity
        or claim_payload.get("artifact_binding_digest") != attestation.artifact_binding_digest
        or claim_payload.get("key_id") != service.key_id
        or latest.state != normalizer_review_state.STATE_APPROVED
        or latest.revision != attestation.review_revision
        or latest.event_digest != attestation.review_event_digest
        or latest.operator_request_id != attestation.approval_request_id
    ):
        raise EligibilityError("narrative_approval_attestation_invalid")


def _validate_ready_payload_binding(
    policy: QuarantinePathPolicy,
    source_ref: str,
    payload: object,
    *,
    identity_layout_v2: bool,
) -> NarrativeReadyManifest:
    if type(policy) is not QuarantinePathPolicy:
        raise EligibilityError("narrative_manifest_invalid")
    source_path = _resolve_ref(
        policy.inbox_root, source_ref, require_directory=True, reason="narrative_source_ref_invalid"
    )
    actual_source_digest = source_digest(source_path)
    try:
        manifest = NarrativeReadyManifest.from_mapping(payload)
    except QuarantineError:
        raise
    if manifest.source_ref != source_ref:
        raise EligibilityError("narrative_manifest_source_ref_mismatch")
    if manifest.source_digest != actual_source_digest:
        raise EligibilityError("narrative_manifest_source_digest_mismatch")
    identity = narrative_source_identity(source_ref, actual_source_digest)
    identity_package_ref = f"{identity}/story.json"
    if identity_layout_v2:
        if manifest.narrative_package_ref != identity_package_ref:
            raise EligibilityError("narrative_manifest_layout_mismatch")
    elif manifest.narrative_package_ref == identity_package_ref:
        raise EligibilityError("narrative_manifest_layout_mismatch")
    package_path = _resolve_ref(
        policy.narrative_outbox_root,
        manifest.narrative_package_ref,
        require_directory=False,
        reason="narrative_package_ref_invalid",
    )
    if identity_layout_v2:
        try:
            actual_package_digest = hashlib.sha256(package_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise EligibilityError("narrative_package_invalid") from exc
    else:
        actual_package_digest = narrative_package_digest(package_path)
    if manifest.narrative_package_digest != actual_package_digest:
        raise EligibilityError("narrative_manifest_package_digest_mismatch")
    return manifest


def _validate_legacy_ready_payload(
    policy: QuarantinePathPolicy,
    source_ref: str,
    payload: object,
) -> NarrativeReadyManifest:
    return _validate_ready_payload_binding(
        policy,
        source_ref,
        payload,
        identity_layout_v2=False,
    )


def _validate_identity_v2_ready_payload(
    policy: QuarantinePathPolicy,
    source_ref: str,
    payload: object,
) -> NarrativeReadyManifest:
    return _validate_ready_payload_binding(
        policy,
        source_ref,
        payload,
        identity_layout_v2=True,
    )


def validate_narrative_ready_payload(
    policy: QuarantinePathPolicy,
    source_ref: str,
    payload: object,
) -> NarrativeReadyManifest:
    """Validate a staged payload using its exact source-identity package contract.

    Production discovery classifies the artifact path first and calls one of the
    closed validators above; no caller-supplied mode selects the digest contract.
    """
    if type(payload) is not dict:
        raise EligibilityError("narrative_manifest_invalid")
    manifest = NarrativeReadyManifest.from_mapping(payload)
    source_path = _resolve_ref(
        policy.inbox_root,
        source_ref,
        require_directory=True,
        reason="narrative_source_ref_invalid",
    )
    identity = narrative_source_identity(source_ref, source_digest(source_path))
    if manifest.narrative_package_ref == f"{identity}/story.json":
        return _validate_identity_v2_ready_payload(policy, source_ref, payload)
    return _validate_legacy_ready_payload(policy, source_ref, payload)


def _source_ref(inbox_root: Path, source_path: Path) -> str:
    try:
        relative = source_path.resolve(strict=True).relative_to(inbox_root.resolve(strict=True)).as_posix()
    except (ValueError, FileNotFoundError, OSError) as exc:
        raise EligibilityError("narrative_source_ref_invalid") from exc
    return _safe_logical_ref(relative, "narrative_source_ref")


def discover_all_agent_material_sources(
    policy: QuarantinePathPolicy | Path,
) -> tuple[DiscoveredSource, ...]:
    if type(policy) is not QuarantinePathPolicy:
        try:
            policy = default_path_policy(Path(policy))
        except (TypeError, ValueError, OSError) as exc:
            raise EligibilityError("quarantine_inbox_invalid") from exc
    inbox = policy.inbox_root
    if not inbox.exists():
        return ()
    if inbox.is_symlink() or not inbox.is_dir():
        raise EligibilityError("quarantine_inbox_invalid")
    source_paths: list[Path] = []
    try:
        first_level = sorted(inbox.iterdir(), key=lambda item: item.name.casefold())
    except OSError as exc:
        raise EligibilityError("quarantine_inbox_unavailable") from exc
    for first in first_level:
        if first.is_symlink():
            raise EligibilityError("quarantine_source_symlink_invalid")
        if not first.is_dir():
            continue
        if _DATE.fullmatch(first.name):
            source_paths.append(first)
            continue
        if _SAFE_COMPONENT.fullmatch(first.name) is None:
            continue
        try:
            second_level = sorted(first.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise EligibilityError("quarantine_inbox_unavailable") from exc
        for second in second_level:
            if second.is_symlink():
                raise EligibilityError("quarantine_source_symlink_invalid")
            if second.is_dir() and _DATE.fullmatch(second.name):
                source_paths.append(second)
    result: list[DiscoveredSource] = []
    for source_path in sorted(set(source_paths), key=lambda item: item.relative_to(inbox).as_posix().casefold()):
        ref = _source_ref(inbox, source_path)
        digest = source_digest(source_path)
        artifact = _narrative_ready_artifact(
            policy,
            source_path,
            ref,
            digest,
        )
        if artifact is not None:
            try:
                manifest = validate_narrative_ready_manifest(policy, ref)
                classification = CLASS_READY
            except EligibilityError:
                # A Normalizer identity-layout candidate is only an eligibility
                # hint until its HMAC attestation and current review event pass.
                # Invalid/missing trust must keep this one source raw without
                # aborting reconciliation for the rest of the inbox.  Legacy
                # source-side/digest-only failures retain their prior behavior.
                if artifact.layout != _LAYOUT_NORMALIZER_IDENTITY_V2:
                    raise
                manifest = None
                classification = CLASS_RAW
        else:
            manifest = None
            classification = CLASS_RAW
        result.append(DiscoveredSource(ref, source_path, digest, classification, manifest))
    return tuple(result)


def canonical_reason_codes(reason_codes: Iterable[str]) -> tuple[str, ...]:
    try:
        values = tuple(reason_codes)
    except TypeError:
        return (DIRECTOR_SUMMARY_CODE,)
    safe: set[str] = set()
    for value in values:
        if type(value) is str and value in SAFE_REASON_CODES:
            safe.add(value)
        else:
            safe.add(DIRECTOR_SUMMARY_CODE)
    if not safe:
        safe.add(DIRECTOR_SUMMARY_CODE)
    return tuple(sorted(safe))


def failure_fingerprint(
    source_digest_value: str,
    narrative_package_digest_value: str,
    reason_codes: Iterable[str],
    director_contract_version: str,
    narrative_contract_version: str,
    eligibility_contract_version: str = ELIGIBILITY_CONTRACT_VERSION,
) -> str:
    _plain_hex64(source_digest_value, "quarantine_source_digest")
    _plain_hex64(narrative_package_digest_value, "quarantine_package_digest")
    reasons = canonical_reason_codes(reason_codes)
    payload = {
        "director_contract_version": _safe_version(director_contract_version, "quarantine_director_version"),
        "eligibility_contract_version": _safe_version(eligibility_contract_version, "quarantine_eligibility_version"),
        "narrative_contract_version": _safe_version(narrative_contract_version, "quarantine_narrative_version"),
        "narrative_package_digest": narrative_package_digest_value,
        "safe_reason_codes": list(reasons),
        "source_digest": source_digest_value,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def aggregate_fingerprint(records: Iterable[tuple[str, str]]) -> str:
    rows = sorted(set(records))
    for source_ref, digest in rows:
        _safe_logical_ref(source_ref, "quarantine_source_ref")
        _plain_hex64(digest, "quarantine_source_digest")
    payload = {
        "aggregate_policy_version": AGGREGATE_POLICY_VERSION,
        "eligibility_contract_version": ELIGIBILITY_CONTRACT_VERSION,
        "unresolved_raw": [[ref, digest] for ref, digest in rows],
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _history(
    record: MaterialRecord | None,
    event: str,
    now_text: str,
    *,
    attempt_id: str = "",
    fingerprint: str = "",
    reasons: tuple[str, ...] = (),
    operator_request_id: str = "",
) -> tuple[HistoryEvent, ...]:
    prior = () if record is None else record.history
    rows = (*prior, HistoryEvent(event, now_text, attempt_id, fingerprint, reasons, operator_request_id))
    return tuple(rows[-_MAX_HISTORY:])


def _notification_id(kind: str, fingerprint: str, discriminator: str = "0") -> str:
    return hashlib.sha256(f"{kind}\0{fingerprint}\0{discriminator}".encode("utf-8")).hexdigest()[:32]


def _pending_notification(kind: str, fingerprint: str, now_text: str, *, discriminator: str = "0", operator_ids: tuple[str, ...] = ()) -> NotificationRecord:
    summary = RAW_SUMMARY_CODE if kind == RAW_AGGREGATE_KIND else DIRECTOR_SUMMARY_CODE
    return NotificationRecord(
        _notification_id(kind, fingerprint, discriminator),
        kind,
        fingerprint,
        NOTIFICATION_PENDING,
        now_text,
        "",
        "",
        0,
        summary,
        operator_ids,
    )


def _record_from_source(source: DiscoveredSource, now_text: str) -> MaterialRecord:
    if source.classification == CLASS_RAW:
        package_ref = ""
        package_digest_value = ""
        director_version = "unknown"
        narrative_version = "unknown"
        status = STATUS_NEEDS_NARRATIVE
        event = "discovered_raw"
    else:
        assert source.manifest is not None
        package_ref = source.manifest.narrative_package_ref
        package_digest_value = source.manifest.narrative_package_digest
        director_version = source.manifest.contract_version("director")
        narrative_version = source.manifest.contract_version("narrative")
        status = STATUS_READY
        event = "discovered_ready"
    return MaterialRecord(
        source.source_ref,
        source.source_digest,
        source.classification,
        status,
        package_ref,
        package_digest_value,
        now_text,
        now_text,
        0,
        "",
        "",
        (),
        director_version,
        narrative_version,
        ELIGIBILITY_CONTRACT_VERSION,
        (),
        (),
        _history(None, event, now_text),
    )


def _identity(record: MaterialRecord) -> tuple[str, ...]:
    return (
        record.source_digest,
        record.classification,
        record.narrative_package_ref,
        record.narrative_package_digest,
        record.director_contract_version,
        record.narrative_contract_version,
        record.eligibility_contract_version,
    )


def reconcile_complete_backlog(
    policy: QuarantinePathPolicy,
    *,
    now: datetime | None = None,
) -> BacklogReconciliationResult:
    if type(policy) is not QuarantinePathPolicy:
        raise RegistryError("quarantine_path_policy_invalid")
    now_text = _now_text(now)
    discovered = discover_all_agent_material_sources(policy)

    def mutate(registry: MaterialRegistry) -> tuple[MaterialRegistry, BacklogReconciliationResult]:
        records = {item.source_ref: item for item in registry.records}
        notifications = list(registry.notifications)
        changed = 0
        newly = 0
        state_changed = False
        # A previous process may have died after claim.  A grace period keeps a
        # concurrent live sender from being reclassified by another scanner;
        # stale claims become uncertain and are never auto-resent.
        recovered: list[NotificationRecord] = []
        for item in notifications:
            if _notification_claim_is_stale(item, now_text):
                recovered.append(replace(item, state=NOTIFICATION_UNCERTAIN, completed_at=now_text))
                state_changed = True
            else:
                recovered.append(item)
        notifications = recovered

        current_records: list[MaterialRecord] = []
        for source in discovered:
            candidate = _record_from_source(source, now_text)
            existing = records.get(source.source_ref)
            if existing is None:
                records[source.source_ref] = candidate
                newly += 1
                state_changed = True
            elif _identity(existing) != _identity(candidate):
                records[source.source_ref] = replace(
                    candidate,
                    first_observed_at=existing.first_observed_at,
                    attempt_count=existing.attempt_count,
                    notification_ids=existing.notification_ids,
                    manual_retry_ids=existing.manual_retry_ids,
                    history=_history(existing, "identity_changed", now_text),
                )
                changed += 1
                state_changed = True
            elif _material_claim_is_stale(existing, now_text):
                reasons = ("processing_lease_expired",)
                fingerprint = failure_fingerprint(
                    existing.source_digest,
                    existing.narrative_package_digest,
                    reasons,
                    existing.director_contract_version,
                    existing.narrative_contract_version,
                    existing.eligibility_contract_version,
                )
                notification = next((
                    item for item in notifications
                    if item.kind == DIRECTOR_REJECTION_KIND
                    and item.aggregate_fingerprint == fingerprint
                ), None)
                if notification is None:
                    notification = _pending_notification(
                        DIRECTOR_REJECTION_KIND,
                        fingerprint,
                        now_text,
                    )
                    notifications.append(notification)
                records[source.source_ref] = replace(
                    existing,
                    status=STATUS_BLOCKED,
                    active_attempt_id="",
                    failure_fingerprint=fingerprint,
                    safe_reason_codes=reasons,
                    last_observed_at=now_text,
                    notification_ids=tuple(
                        dict.fromkeys(
                            (*existing.notification_ids, notification.notification_id)
                        )
                    ),
                    history=_history(
                        existing,
                        "attempt_expired",
                        now_text,
                        attempt_id=existing.active_attempt_id,
                        fingerprint=fingerprint,
                        reasons=reasons,
                    ),
                )
                changed += 1
                state_changed = True
            current_records.append(records[source.source_ref])

        raw_rows = tuple(
            (item.source_ref, item.source_digest)
            for item in current_records
            if item.classification == CLASS_RAW and item.status == STATUS_NEEDS_NARRATIVE
        )
        fingerprint = aggregate_fingerprint(raw_rows) if raw_rows else ""
        if fingerprint != registry.current_raw_fingerprint:
            state_changed = True
        if fingerprint and not any(
            item.kind == RAW_AGGREGATE_KIND and item.aggregate_fingerprint == fingerprint
            for item in notifications
        ):
            notifications.append(_pending_notification(RAW_AGGREGATE_KIND, fingerprint, now_text))
            state_changed = True

        next_registry = MaterialRegistry(
            registry.registry_revision + (1 if state_changed else 0),
            tuple(sorted(records.values(), key=lambda item: item.source_ref)),
            tuple(sorted(notifications, key=lambda item: item.notification_id)),
            fingerprint,
        )
        result = BacklogReconciliationResult(
            discovered_count=len(discovered),
            raw_count=sum(item.classification == CLASS_RAW for item in current_records),
            narrative_ready_count=sum(item.classification == CLASS_READY for item in current_records),
            blocked_count=sum(item.status == STATUS_BLOCKED for item in current_records),
            consumed_count=sum(item.status == STATUS_CONSUMED for item in current_records),
            newly_registered_count=newly,
            changed_count=changed,
            aggregate_fingerprint=fingerprint,
            aggregate_notification_claim=None,
        )
        return next_registry, result

    return _mutate_registry(policy, mutate)  # type: ignore[return-value]


def claim_notification(
    policy: QuarantinePathPolicy,
    *,
    notification_id: str | None = None,
    kind: str | None = None,
    now: datetime | None = None,
) -> NotificationClaim | None:
    if notification_id is not None and (type(notification_id) is not str or _HEX32.fullmatch(notification_id) is None):
        raise RegistryError("quarantine_notification_id_invalid")
    if kind is not None and (type(kind) is not str or kind not in NOTIFICATION_KINDS):
        raise RegistryError("quarantine_notification_kind_invalid")
    now_text = _now_text(now)

    def mutate(registry: MaterialRegistry) -> tuple[MaterialRegistry, NotificationClaim | None]:
        rows = list(registry.notifications)
        candidates = [
            (index, item) for index, item in enumerate(rows)
            if item.state == NOTIFICATION_PENDING
            and (notification_id is None or item.notification_id == notification_id)
            and (kind is None or item.kind == kind)
        ]
        if not candidates:
            return registry, None
        index, item = sorted(candidates, key=lambda pair: (pair[1].created_at, pair[1].notification_id))[0]
        claimed = replace(
            item,
            state=NOTIFICATION_CLAIMED,
            claimed_at=now_text,
            completed_at="",
            attempt_count=item.attempt_count + 1,
        )
        rows[index] = claimed
        updated = MaterialRegistry(
            registry.registry_revision + 1,
            registry.records,
            tuple(sorted(rows, key=lambda row: row.notification_id)),
            registry.current_raw_fingerprint,
        )
        return updated, NotificationClaim(
            claimed.notification_id,
            claimed.kind,
            claimed.aggregate_fingerprint,
            claimed.safe_summary_code,
            claimed.attempt_count,
        )

    return _mutate_registry(policy, mutate)  # type: ignore[return-value]


def finalize_notification(
    policy: QuarantinePathPolicy,
    notification_id: str,
    outcome: str,
    *,
    now: datetime | None = None,
) -> NotificationRecord:
    if type(outcome) is not str or outcome not in {
        NOTIFICATION_SENT, NOTIFICATION_FAILED, NOTIFICATION_UNCERTAIN,
    }:
        raise RegistryError("quarantine_notification_outcome_invalid")
    now_text = _now_text(now)

    def mutate(registry: MaterialRegistry) -> tuple[MaterialRegistry, NotificationRecord]:
        rows = list(registry.notifications)
        for index, item in enumerate(rows):
            if item.notification_id != notification_id:
                continue
            if item.state != NOTIFICATION_CLAIMED:
                raise RegistryError("quarantine_notification_transition_invalid")
            completed = replace(item, state=outcome, completed_at=now_text)
            rows[index] = completed
            updated = MaterialRegistry(
                registry.registry_revision + 1,
                registry.records,
                tuple(sorted(rows, key=lambda row: row.notification_id)),
                registry.current_raw_fingerprint,
            )
            return updated, completed
        raise RegistryError("quarantine_notification_missing")

    return _mutate_registry(policy, mutate)  # type: ignore[return-value]


def select_ready_candidate(
    policy: QuarantinePathPolicy,
    *,
    project_name: str = "",
    date_text: str = "",
) -> EligibleCandidate | None:
    if type(project_name) is not str or (project_name and _SAFE_COMPONENT.fullmatch(project_name) is None):
        raise RegistryError("quarantine_project_invalid")
    if type(date_text) is not str or (date_text and _DATE.fullmatch(date_text) is None):
        raise RegistryError("quarantine_date_invalid")
    with _InterProcessLock(policy.registry_path.parent):
        registry = _read_registry_snapshot(policy.registry_path).registry
        rows = [item for item in registry.records if item.classification == CLASS_READY and item.status == STATUS_READY]
        if date_text:
            rows = [item for item in rows if PurePosixPath(item.source_ref).name == date_text]
        if project_name:
            project_rows = [item for item in rows if len(PurePosixPath(item.source_ref).parts) == 2 and PurePosixPath(item.source_ref).parts[0].casefold() == project_name.casefold()]
            legacy_rows = [item for item in rows if len(PurePosixPath(item.source_ref).parts) == 1]
            rows = project_rows or legacy_rows
        if not rows:
            return None
        item = sorted(rows, key=lambda row: row.source_ref)[0]
        manifest = validate_narrative_ready_manifest(policy, item.source_ref)
        if (
            manifest.source_digest != item.source_digest
            or manifest.narrative_package_ref != item.narrative_package_ref
            or manifest.narrative_package_digest != item.narrative_package_digest
            or manifest.contract_version("director") != item.director_contract_version
            or manifest.contract_version("narrative") != item.narrative_contract_version
        ):
            raise RegistryError("quarantine_candidate_stale")
        return EligibleCandidate(
            item.source_ref,
            item.source_digest,
            PurePosixPath(item.source_ref).name,
            item.narrative_package_ref,
            item.narrative_package_digest,
            item.director_contract_version,
            item.narrative_contract_version,
            item.eligibility_contract_version,
        )


def select_idempotency_candidate(
    policy: QuarantinePathPolicy,
    *,
    project_name: str,
    date_text: str,
) -> EligibleCandidate | None:
    """Return a strictly bound candidate for a read-only idempotency check.

    Standard-only maintenance must be able to classify a durable partial or
    consumed result before attempting another processing claim.  This selector
    never changes registry state and deliberately excludes raw and blocked
    material; a caller must still win ``claim_ready_candidate`` before starting
    any new generation.
    """
    if type(project_name) is not str or _SAFE_COMPONENT.fullmatch(project_name) is None:
        raise RegistryError("quarantine_project_invalid")
    if type(date_text) is not str or _DATE.fullmatch(date_text) is None:
        raise RegistryError("quarantine_date_invalid")
    with _InterProcessLock(policy.registry_path.parent):
        registry = _read_registry_snapshot(policy.registry_path).registry
        rows = [
            item
            for item in registry.records
            if item.classification == CLASS_READY
            and item.status in {STATUS_READY, STATUS_PROCESSING, STATUS_CONSUMED}
            and PurePosixPath(item.source_ref).name == date_text
        ]
        project_rows = [
            item
            for item in rows
            if len(PurePosixPath(item.source_ref).parts) == 2
            and PurePosixPath(item.source_ref).parts[0].casefold()
            == project_name.casefold()
        ]
        legacy_rows = [
            item for item in rows if len(PurePosixPath(item.source_ref).parts) == 1
        ]
        rows = project_rows or legacy_rows
        if not rows:
            return None
        item = sorted(rows, key=lambda row: row.source_ref)[0]
        manifest = validate_narrative_ready_manifest(policy, item.source_ref)
        if (
            manifest.source_digest != item.source_digest
            or manifest.narrative_package_ref != item.narrative_package_ref
            or manifest.narrative_package_digest != item.narrative_package_digest
            or manifest.contract_version("director") != item.director_contract_version
            or manifest.contract_version("narrative") != item.narrative_contract_version
        ):
            raise RegistryError("quarantine_candidate_stale")
        return _candidate_from_record(item)


def _candidate_from_record(record: MaterialRecord) -> EligibleCandidate:
    return EligibleCandidate(
        record.source_ref,
        record.source_digest,
        PurePosixPath(record.source_ref).name,
        record.narrative_package_ref,
        record.narrative_package_digest,
        record.director_contract_version,
        record.narrative_contract_version,
        record.eligibility_contract_version,
    )


def claim_ready_candidate(
    policy: QuarantinePathPolicy,
    candidate: EligibleCandidate,
    *,
    now: datetime | None = None,
) -> AttemptClaim | None:
    if type(candidate) is not EligibleCandidate:
        raise RegistryError("quarantine_candidate_invalid")
    now_text = _now_text(now)

    def mutate(registry: MaterialRegistry) -> tuple[MaterialRegistry, AttemptClaim | None]:
        records = list(registry.records)
        for index, record in enumerate(records):
            if record.source_ref != candidate.source_ref:
                continue
            if record.status != STATUS_READY or _candidate_from_record(record) != candidate:
                return registry, None
            manifest = validate_narrative_ready_manifest(policy, record.source_ref)
            if (
                manifest.source_digest != record.source_digest
                or manifest.narrative_package_digest != record.narrative_package_digest
                or manifest.narrative_package_ref != record.narrative_package_ref
                or manifest.contract_version("director") != record.director_contract_version
                or manifest.contract_version("narrative") != record.narrative_contract_version
            ):
                raise RegistryError("quarantine_candidate_stale")
            attempt_id = secrets.token_hex(16)
            claimed = replace(
                record,
                status=STATUS_PROCESSING,
                active_attempt_id=attempt_id,
                attempt_count=record.attempt_count + 1,
                last_observed_at=now_text,
                history=_history(record, "attempt_claimed", now_text, attempt_id=attempt_id),
            )
            records[index] = claimed
            updated = MaterialRegistry(
                registry.registry_revision + 1,
                tuple(sorted(records, key=lambda row: row.source_ref)),
                registry.notifications,
                registry.current_raw_fingerprint,
            )
            return updated, AttemptClaim(_candidate_from_record(claimed), attempt_id, claimed.attempt_count)
        return registry, None

    return _mutate_registry(policy, mutate)  # type: ignore[return-value]


def finalize_attempt(
    policy: QuarantinePathPolicy,
    claim: AttemptClaim,
    outcome: str,
    reason_codes: Iterable[str] = (),
    *,
    now: datetime | None = None,
) -> tuple[MaterialRecord, str]:
    """Atomically hand an active processing lease to one durable state."""
    if type(claim) is not AttemptClaim:
        raise RegistryError("quarantine_attempt_claim_invalid")
    if outcome not in {STATUS_READY, STATUS_BLOCKED, STATUS_CONSUMED}:
        raise RegistryError("quarantine_attempt_outcome_invalid")
    now_text = _now_text(now)
    reasons = canonical_reason_codes(reason_codes) if outcome == STATUS_BLOCKED else ()
    if outcome == STATUS_BLOCKED and not reasons:
        raise RegistryError("quarantine_attempt_reasons_invalid")
    fingerprint = (
        failure_fingerprint(
            claim.candidate.source_digest,
            claim.candidate.narrative_package_digest,
            reasons,
            claim.candidate.director_contract_version,
            claim.candidate.narrative_contract_version,
            claim.candidate.eligibility_contract_version,
        )
        if outcome == STATUS_BLOCKED
        else ""
    )

    def mutate(
        registry: MaterialRegistry,
    ) -> tuple[MaterialRegistry, tuple[MaterialRecord, str]]:
        records = list(registry.records)
        notifications = list(registry.notifications)
        for index, record in enumerate(records):
            if record.source_ref != claim.candidate.source_ref:
                continue
            if record.status != STATUS_PROCESSING or record.active_attempt_id != claim.attempt_id:
                raise RegistryError("quarantine_attempt_transition_invalid")
            notification_id = ""
            notification_ids = record.notification_ids
            if outcome == STATUS_BLOCKED:
                notification = next((
                    item for item in notifications
                    if item.kind == DIRECTOR_REJECTION_KIND
                    and item.aggregate_fingerprint == fingerprint
                ), None)
                if notification is None:
                    notification = _pending_notification(
                        DIRECTOR_REJECTION_KIND,
                        fingerprint,
                        now_text,
                    )
                    notifications.append(notification)
                notification_id = notification.notification_id
                notification_ids = tuple(
                    dict.fromkeys((*record.notification_ids, notification_id))
                )
            event = {
                STATUS_READY: "attempt_released",
                STATUS_BLOCKED: "director_blocked",
                STATUS_CONSUMED: "consumed",
            }[outcome]
            finalized = replace(
                record,
                status=outcome,
                active_attempt_id="",
                failure_fingerprint=fingerprint,
                safe_reason_codes=reasons,
                last_observed_at=now_text,
                notification_ids=notification_ids,
                history=_history(
                    record,
                    event,
                    now_text,
                    attempt_id=claim.attempt_id,
                    fingerprint=fingerprint,
                    reasons=reasons,
                ),
            )
            records[index] = finalized
            updated = MaterialRegistry(
                registry.registry_revision + 1,
                tuple(sorted(records, key=lambda row: row.source_ref)),
                tuple(sorted(notifications, key=lambda row: row.notification_id)),
                registry.current_raw_fingerprint,
            )
            return updated, (finalized, notification_id)
        raise RegistryError("quarantine_attempt_missing")

    return _mutate_registry(policy, mutate)  # type: ignore[return-value]


def release_attempt(
    policy: QuarantinePathPolicy,
    claim: AttemptClaim,
    *,
    now: datetime | None = None,
) -> MaterialRecord:
    record, _ = finalize_attempt(policy, claim, STATUS_READY, now=now)
    return record


def finalize_cancelled_attempt(
    policy: QuarantinePathPolicy,
    claim: AttemptClaim,
    *,
    now: datetime | None = None,
) -> MaterialRecord:
    """Release only this exact active lease; terminal/stale attempts are no-ops."""
    if type(claim) is not AttemptClaim:
        raise RegistryError("quarantine_attempt_claim_invalid")
    now_text = _now_text(now)

    def mutate(registry: MaterialRegistry) -> tuple[MaterialRegistry, MaterialRecord]:
        records = list(registry.records)
        for index, record in enumerate(records):
            if record.source_ref != claim.candidate.source_ref:
                continue
            if _candidate_from_record(record) != claim.candidate:
                raise RegistryError("quarantine_attempt_identity_conflict")
            if (
                record.status != STATUS_PROCESSING
                or record.active_attempt_id != claim.attempt_id
            ):
                return registry, record
            released = replace(
                record,
                status=STATUS_READY,
                active_attempt_id="",
                failure_fingerprint="",
                safe_reason_codes=(),
                last_observed_at=now_text,
                history=_history(
                    record,
                    "attempt_released",
                    now_text,
                    attempt_id=claim.attempt_id,
                ),
            )
            records[index] = released
            updated = MaterialRegistry(
                registry.registry_revision + 1,
                tuple(sorted(records, key=lambda row: row.source_ref)),
                registry.notifications,
                registry.current_raw_fingerprint,
            )
            return updated, released
        raise RegistryError("quarantine_attempt_missing")

    return _mutate_registry(policy, mutate)  # type: ignore[return-value]


def mark_attempt_blocked(
    policy: QuarantinePathPolicy,
    claim: AttemptClaim,
    reason_codes: Iterable[str],
    *,
    now: datetime | None = None,
) -> str:
    _, notification_id = finalize_attempt(
        policy,
        claim,
        STATUS_BLOCKED,
        reason_codes,
        now=now,
    )
    return notification_id


def mark_attempt_consumed(
    policy: QuarantinePathPolicy,
    claim: AttemptClaim,
    *,
    now: datetime | None = None,
) -> MaterialRecord:
    record, _ = finalize_attempt(policy, claim, STATUS_CONSUMED, now=now)
    return record


def request_manual_retry(
    policy: QuarantinePathPolicy,
    source_ref: str,
    expected_source_digest: str,
    expected_package_digest: str,
    operator_request_id: str,
    *,
    now: datetime | None = None,
) -> MaterialRecord:
    source_ref = _safe_logical_ref(source_ref, "quarantine_source_ref")
    _plain_hex64(expected_source_digest, "quarantine_source_digest")
    _plain_hex64(expected_package_digest, "quarantine_package_digest")
    if type(operator_request_id) is not str or _SAFE_OPERATOR_ID.fullmatch(operator_request_id) is None:
        raise RegistryError("quarantine_operator_request_invalid")
    now_text = _now_text(now)

    def mutate(registry: MaterialRegistry) -> tuple[MaterialRegistry, MaterialRecord]:
        records = list(registry.records)
        for index, record in enumerate(records):
            if record.source_ref != source_ref:
                continue
            if operator_request_id in record.manual_retry_ids:
                return registry, record
            if (
                record.status != STATUS_BLOCKED
                or record.source_digest != expected_source_digest
                or record.narrative_package_digest != expected_package_digest
            ):
                raise RegistryError("quarantine_manual_retry_conflict")
            manifest = validate_narrative_ready_manifest(policy, source_ref)
            if manifest.source_digest != expected_source_digest or manifest.narrative_package_digest != expected_package_digest:
                raise RegistryError("quarantine_manual_retry_stale")
            ready = replace(
                record,
                status=STATUS_READY,
                active_attempt_id="",
                failure_fingerprint="",
                safe_reason_codes=(),
                last_observed_at=now_text,
                manual_retry_ids=tuple((*record.manual_retry_ids, operator_request_id)[-_MAX_RETRY_IDS:]),
                history=_history(record, "manual_retry", now_text, operator_request_id=operator_request_id),
            )
            records[index] = ready
            updated = MaterialRegistry(
                registry.registry_revision + 1,
                tuple(sorted(records, key=lambda row: row.source_ref)),
                registry.notifications,
                registry.current_raw_fingerprint,
            )
            return updated, ready
        raise RegistryError("quarantine_source_missing")

    return _mutate_registry(policy, mutate)  # type: ignore[return-value]


def request_notification_retry(
    policy: QuarantinePathPolicy,
    notification_id: str,
    operator_request_id: str,
    *,
    now: datetime | None = None,
) -> NotificationRecord:
    if type(notification_id) is not str or _HEX32.fullmatch(notification_id) is None:
        raise RegistryError("quarantine_notification_id_invalid")
    if type(operator_request_id) is not str or _SAFE_OPERATOR_ID.fullmatch(operator_request_id) is None:
        raise RegistryError("quarantine_operator_request_invalid")
    now_text = _now_text(now)

    def mutate(registry: MaterialRegistry) -> tuple[MaterialRegistry, NotificationRecord]:
        original = next((item for item in registry.notifications if item.notification_id == notification_id), None)
        if original is None:
            raise RegistryError("quarantine_notification_missing")
        existing = next((
            item for item in registry.notifications if operator_request_id in item.operator_request_ids
        ), None)
        if existing is not None:
            if existing.kind != original.kind or existing.aggregate_fingerprint != original.aggregate_fingerprint:
                raise RegistryError("quarantine_notification_retry_conflict")
            return registry, existing
        if original.state not in {NOTIFICATION_FAILED, NOTIFICATION_UNCERTAIN}:
            raise RegistryError("quarantine_notification_retry_invalid")
        retry = _pending_notification(
            original.kind,
            original.aggregate_fingerprint,
            now_text,
            discriminator=operator_request_id,
            operator_ids=(operator_request_id,),
        )
        updated = MaterialRegistry(
            registry.registry_revision + 1,
            registry.records,
            tuple(sorted((*registry.notifications, retry), key=lambda row: row.notification_id)),
            registry.current_raw_fingerprint,
        )
        return updated, retry

    return _mutate_registry(policy, mutate)  # type: ignore[return-value]


def list_quarantine_status(policy: QuarantinePathPolicy) -> tuple[QuarantineStatusItem, ...]:
    """Return local operator status rows without source or package references."""
    with _InterProcessLock(policy.registry_path.parent):
        records = _read_registry_snapshot(policy.registry_path).registry.records
    return tuple(
        QuarantineStatusItem(
            material_id=hashlib.sha256(item.source_ref.encode("utf-8")).hexdigest()[:12],
            classification=item.classification,
            status=item.status,
            attempt_count=item.attempt_count,
            failure_fingerprint_id=item.failure_fingerprint[:12],
            safe_reason_codes=item.safe_reason_codes,
        )
        for item in records
    )


def summarize_quarantine_notifications(policy: QuarantinePathPolicy) -> dict[str, object]:
    """Return the privacy-safe local summary for failed/uncertain alert audit."""
    with _InterProcessLock(policy.registry_path.parent):
        registry = _read_registry_snapshot(policy.registry_path).registry
    counts = {state: 0 for state in NOTIFICATION_STATES}
    for item in registry.notifications:
        counts[item.state] += 1
    return {
        "needs_narrative_count": sum(item.status == STATUS_NEEDS_NARRATIVE for item in registry.records),
        "blocked_count": sum(item.status == STATUS_BLOCKED for item in registry.records),
        "notification_counts": {key: counts[key] for key in sorted(counts)},
        "aggregate_fingerprint_id": registry.current_raw_fingerprint[:12],
    }


def default_registry_path() -> Path:
    return Path(os.getenv(
        "REELS_QUARANTINE_REGISTRY",
        ".local_artifacts/reels_failure_quarantine/registry.json",
    ))


def default_narrative_outbox_path() -> Path:
    return Path(os.getenv(
        "REELS_NARRATIVE_OUTBOX",
        ".local_artifacts/narrative_outbox",
    ))


def default_review_authority_path() -> Path | None:
    value = os.getenv("NARRATIVE_NORMALIZER_REVIEW_AUTHORITY_ROOT")
    if value is None or not value.strip():
        return None
    return Path(value)


def default_review_authority_client() -> object | None:
    socket_value = os.getenv("NARRATIVE_REVIEW_AUTHORITY_SOCKET")
    if socket_value is None or not socket_value.strip():
        return None
    uid_value = os.getenv("NARRATIVE_REVIEW_AUTHORITY_OWNER_UID")
    gid_value = os.getenv("NARRATIVE_REVIEW_AUTHORITY_OWNER_GID")
    mode_value = os.getenv("NARRATIVE_REVIEW_AUTHORITY_SOCKET_MODE", "0660")
    timeout_value = os.getenv("NARRATIVE_REVIEW_AUTHORITY_TIMEOUT", "10")
    try:
        if uid_value is None or gid_value is None:
            raise ValueError
        owner_uid = int(uid_value, 10)
        owner_gid = int(gid_value, 10)
        mode = int(mode_value, 8)
        timeout = float(timeout_value)
        if owner_uid < 0 or owner_gid < 0:
            raise ValueError
        return review_authority_client_module.ReviewAuthorityClient(
            socket_value,
            owner_uid=owner_uid,
            owner_gid=owner_gid,
            mode=mode,
            timeout=timeout,
        )
    except (TypeError, ValueError, review_authority_client_module.ClientError):
        raise RegistryError("quarantine_review_authority_invalid") from None


def default_path_policy(inbox_root: Path) -> QuarantinePathPolicy:
    return QuarantinePathPolicy(
        Path(inbox_root),
        default_registry_path(),
        default_narrative_outbox_path(),
        narrative_review_authority_root=default_review_authority_path(),
        review_authority_client=default_review_authority_client(),
    )
