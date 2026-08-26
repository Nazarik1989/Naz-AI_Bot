"""Review-only Agent Content narrative normalization.

This module is deliberately standalone.  It reads immutable source units,
delegates bounded narrative generation through the reviewed CP2 interface,
persists review drafts outside the inbox, and creates a Reels-compatible ready
manifest only after an explicit approval call.  Importing it starts nothing.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import asdict, dataclass, is_dataclass, replace
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Mapping, Protocol, Sequence

import narrative_generation as generation
import narrative_translator as translator
import reels_failure_quarantine as quarantine
import narrative_normalizer_evidence as evidence
import narrative_normalizer_review_state as review_state
import narrative_normalizer_trust as trust
import narrative_outbox_permissions as outbox_permissions
import narrative_review_authority_client as review_authority_client


SOURCE_CONTRACT_VERSION = "agent-content-source-v1"
NORMALIZATION_POLICY_VERSION = "naz-narrative-normalizer-v1"
OPEN_DOMAIN_POLICY_VERSION = "naz-narrative-open-domain-v1"
PLAIN_LANGUAGE_POLICY_VERSION = "plain-russian-reader-v1"
FACTUALITY_POLICY_VERSION = "naz-claim-factuality-v1"
MEANING_POLICY_VERSION = "naz-meaning-preservation-v1"
CP2_ADJUDICATION_EVIDENCE_VERSION = "normalizer-cp2-adjudication-evidence-v1"
SOURCE_IDENTITY_VERSION = "normalizer-source-identity-v1"
DRAFT_IDENTITY_VERSION = "normalizer-draft-identity-v1"
DRAFT_SCHEMA_VERSION = "naz-narrative-draft-v3"
DRAFT_MANIFEST_SCHEMA_VERSION = "naz-narrative-draft-manifest-v3"
REVIEW_SCHEMA_VERSION = "naz-narrative-review-v3"
CLAIM_SCHEMA_VERSION = "naz-narrative-normalization-claim-v3"
REVIEWER_VERSION = "deterministic-normalizer-review-v2"
IDEMPOTENCY_VERSION = "normalizer-source-identity-v2"
REVIEW_ACTION_VERSION = "normalizer-review-action-v1"
DEFAULT_CLAIM_LEASE_SECONDS = 3600
DEFAULT_MAX_WORKERS = 1
MAX_WORKERS = 8
MIN_SOURCE_FACTS = 2
MAX_SOURCE_FILE_BYTES = 2_000_000

REVIEW_PASSED = "passed"
REVIEW_REJECTED = "rejected"
REVIEW_SUPERSEDED = "superseded"
REVIEW_STATES = frozenset({REVIEW_PASSED, REVIEW_REJECTED, REVIEW_SUPERSEDED})

OUTCOME_DRAFT_READY_FOR_REVIEW = "draft_ready_for_review"
OUTCOME_SOURCE_INSUFFICIENT = "source_insufficient"
OUTCOME_MANUAL_ATTENTION = "manual_attention"
OUTCOME_SENSITIVE_REJECTED = "sensitive_rejected"
OUTCOME_EXISTING_DRAFT = "existing_draft"
OUTCOME_PROCESSING = "processing"
OUTCOME_FAILED = "failed"
OUTCOME_UNCERTAIN = "uncertain"
OUTCOME_DRY_RUN = "dry_run"
# Compatibility aliases for the already-reviewed closed-domain test contract.
OUTCOME_CREATED = OUTCOME_DRAFT_READY_FOR_REVIEW
OUTCOME_EXISTING = OUTCOME_EXISTING_DRAFT
PUBLIC_OUTCOMES = frozenset({
    OUTCOME_DRAFT_READY_FOR_REVIEW,
    OUTCOME_SOURCE_INSUFFICIENT,
    OUTCOME_MANUAL_ATTENTION,
    OUTCOME_SENSITIVE_REJECTED,
    OUTCOME_EXISTING_DRAFT,
    OUTCOME_PROCESSING,
    OUTCOME_FAILED,
    OUTCOME_UNCERTAIN,
})

CLAIM_PROCESSING = "processing"
CLAIM_COMPLETED = "completed"
CLAIM_FAILED = "failed"
CLAIM_UNCERTAIN = "uncertain"
CLAIM_STATES = frozenset({CLAIM_PROCESSING, CLAIM_COMPLETED, CLAIM_FAILED, CLAIM_UNCERTAIN})

REASON_CODES = frozenset({
    "narrative_normalizer_source_invalid",
    "narrative_normalizer_source_changed",
    "narrative_normalizer_source_insufficient",
    "narrative_normalizer_source_sensitive",
    "narrative_normalizer_registry_invalid",
    "narrative_normalizer_context_invalid",
    "narrative_normalizer_generation_failed",
    "narrative_normalizer_fact_coverage_invalid",
    "narrative_normalizer_model_budget_exceeded",
    "narrative_normalizer_plain_language_invalid",
    "narrative_normalizer_factuality_invalid",
    "narrative_normalizer_meaning_invalid",
    "narrative_normalizer_persistence_invalid",
    "narrative_normalizer_persistence_conflict",
    "narrative_normalizer_claim_invalid",
    "narrative_normalizer_claim_uncertain",
    "narrative_normalizer_review_not_passed",
    "narrative_normalizer_draft_invalid",
    "narrative_normalizer_approval_conflict",
    "narrative_normalizer_review_identity_conflict",
    "narrative_normalizer_supersede_identity_invalid",
    "narrative_normalizer_manifest_invalid",
    "narrative_normalizer_evidence_invalid",
    "narrative_normalizer_evidence_incomplete",
    "narrative_normalizer_evidence_ambiguous",
    "narrative_normalizer_review_authority_unavailable",
    "narrative_normalizer_trust_unavailable",
    "narrative_normalizer_trust_invalid",
    "narrative_normalizer_cli_invalid",
    "narrative_normalizer_internal_error",
})

BROKER_DRAFT_CONTRACT_VERSION = "normalizer-draft-identity-v1"
BROKER_REGISTER_BINDING_VERSION = "narrative-review-authority-draft-binding-v1"
BROKER_PREPARED_APPROVAL_VERSION = "narrative-review-authority-prepared-approval-v2"
BROKER_READY_VERDICT_VERSION = "narrative-review-authority-ready-verdict-v2"
BROKER_STORAGE_ADAPTER_VERSION = "narrative-review-authority-state-adapter-v2"

_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_ABSOLUTE_PATH = re.compile(
    r"(?:[A-Za-z]:[\\/]|(?<![A-Za-z0-9._-])/(?!/)[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*)",
    re.IGNORECASE,
)
_SECRET = re.compile(
    r"(?:api[_-]?key|token|secret|password|authorization|credential)\s*[:=]\s*\S+|"
    r"(?:sk|ghp|xox[baprs])[-_A-Za-z0-9]{12,}",
    re.IGNORECASE,
)
_INTERNAL_IDENTIFIER = re.compile(
    r"\b[A-Za-z_][A-Za-z0-9_]*\([^)]*\)|\b[A-Za-z]+(?:_[A-Za-z0-9]+){1,}\b|"
    r"(?i:\bnarrative_[a-z0-9_]+\b)|\b(?:[A-Z][a-z0-9]+){2,}\b",
)
_ACRONYM = re.compile(r"(?<![A-Za-zА-Яа-яЁё])[A-ZА-ЯЁ]{2,}(?![A-Za-zА-Яа-яЁё])")
_CYRILLIC_WORD = re.compile(r"\b[А-Яа-яЁё]{2,}\b")
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
_TEST_REPORT = re.compile(
    r"\b(?:pytest|assertion|fixture)\b|\b\d+\s+(?:tests?\s+)?(?:passed|failed|skipped)\b",
    re.IGNORECASE,
)

_JARGON = (
    "provider", "runtime", "deploy", "pipeline", "manifest", "digest", "schema",
    "callback", "binding", "mismatch", "cleanup", "fail-closed", "async", "llm",
    "api", "webhook", "stack trace", "traceback", "payload", "endpoint",
)

_SOURCE_FILE_SUFFIXES = frozenset({".md", ".txt", ".log", ".json", ".jsonl", ".yaml", ".yml"})
_STORY_KEYS = frozenset({
    "schema_version", "source_ref", "source_digest", "source_contract_version", "source_identity",
    "source_facts", "human_story_package", "claims", "title", "hook",
    "story", "ending", "primary_character", "secondary_character", "presence_mode",
    "factuality_receipt", "meaning_preservation_receipt", "plain_language_receipt",
    "cp2_adjudication_evidence",
    "evidence_mode", "source_document_digest", "source_evidence_bundle", "verified_fact_bindings",
    "generation_contract_version", "adjudication_contract_version",
    "normalization_policy_version", "selected_candidate_id", "human_story_package_digest", "package_digest",
})
_DRAFT_MANIFEST_KEYS = frozenset({
    "schema_version", "source_ref", "source_digest", "source_identity", "draft_identity",
    "package_digest", "status", "created_at", "model_policy_identity", "contract_versions",
    "initial_review_state", "idempotency_identity", "generation_run_id", "supersedes",
    "evidence_mode", "source_document_digest", "evidence_bundle_digest", "verified_fact_projection_digest",
    "trust_receipt",
})
_REVIEW_KEYS = frozenset({
    "schema_version", "draft_identity", "status", "reason_codes", "fact_coverage",
    "factuality_receipt", "meaning_preservation_receipt", "plain_language_receipt",
    "unsupported_claim_count", "reviewed_at", "reviewer_version", "operator_request_id",
    "action_digest", "supersede_binding", "trust_receipt",
})
_READY_KEYS = quarantine.MANIFEST_KEYS
_FACT_KEYS = frozenset({"fact_id", "exact_text", "source_ref", "order"})
_FACT_COVERAGE_KEYS = frozenset({
    "source_fact_count", "referenced_fact_count", "referenced_fact_ids", "coverage_complete",
})
_CONTRACT_VERSION_KEYS = frozenset({"generation", "adjudication", "translator", "normalizer", "source"})
_CLAIM_KEYS = frozenset({
    "claim_id", "claim_kind", "rendered_text", "ordered_source_fact_refs", "semantic_anchors",
    "numbers", "named_entities", "temporal_relation", "causal_relation", "interpretation_mode",
})
_FACTUALITY_KEYS = frozenset({
    "policy_version", "source_identity", "candidate_id", "package_digest", "ordered_claim_ids",
    "claim_digests", "statement_inference_kinds", "unsupported_claim_ids", "unsupported_claim_count",
    "adjudication_binding_digest", "passed",
})
_MEANING_KEYS = frozenset({
    "policy_version", "required_source_anchors", "covered_source_anchors", "omitted_anchors",
    "source_specificity_score", "distinct_story_identity", "significance_mode", "passed",
})
_SUPERSEDES_KEYS = frozenset({
    "old_source_ref", "old_source_digest", "old_source_identity", "old_draft_identity",
    "new_source_ref", "new_source_digest", "new_source_identity", "new_draft_identity",
})
_SUPERSEDE_BINDING_KEYS = _SUPERSEDES_KEYS | {"operator_request_id"}
_CLAIM_RECORD_KEYS = frozenset({
    "schema_version", "source_id", "source_ref", "source_digest", "source_identity",
    "attempt_id", "state", "started_at", "updated_at", "generation_run_id",
    "package_digest", "draft_identity", "selected_candidate_id", "human_story_package_digest",
    "factuality_binding_digest", "adjudication_evidence_digest", "ordered_claim_digests", "reason_code",
    "artifact_binding_digest", "key_id", "claim_seal",
})
_ADJUDICATION_EVIDENCE_KEYS = frozenset({
    "policy_version", "run_id", "generation_model", "adjudication_model", "repair_model",
    "model_call_count", "source_identity", "candidate_id", "candidate_rank", "package_digest",
    "authority_context_digest", "draft_digest", "candidate_adjudication_digest",
    "candidate_adjudication", "validation_context", "editorial_context", "statement_evidence",
    "overall_decision", "reason_codes", "evidence_digest",
})
_ADJUDICATED_STATEMENT_KEYS = frozenset({
    "statement_name", "statement_digest", "decision", "reason_codes", "claim_id",
    "claim_digest", "inference_kind", "ordered_source_fact_refs",
})
_HUMAN_PACKAGE_KEYS = frozenset({
    "schema", "plan_id", "source_ref", "source_facts", "hook", "human_problem", "tension",
    "turning_point", "resolution", "primary_interpretation", "secondary_interpretation",
    "character_states", "character_canons", "relationship_state", "duo_context", "visual_direction",
    "story_type", "confidence",
})
_GROUNDED_STATEMENT_KEYS = frozenset({
    "text", "inference_kind", "source_fact_refs", "editorial_refs", "canon_refs",
})

CLAIM_KINDS = frozenset({
    "fact_paraphrase", "fact_sequence", "source_supported_significance",
    "explicitly_marked_metaphor", "code_owned_bridge",
})
INTERPRETATION_MODES = frozenset({"literal", "marked_metaphor", "code_owned"})
SIGNIFICANCE_MODES = frozenset({"source_supported_significance", "significance_not_supported"})
CODE_OWNED_BRIDGE_TEXTS: tuple[str, ...] = ()
_SIGNIFICANCE_ANCHORS = frozenset({"manual_inspection_not_blind_trust", "same_next_action"})
_PUBLIC_CLAIM_FIELDS = (
    ("hook", "hook"),
    ("story-1", "human_problem"),
    ("story-2", "tension"),
    ("story-3", "turning_point"),
    ("ending", "resolution"),
)


class NarrativeNormalizerError(ValueError):
    """A privacy-safe domain error with one stable public reason."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code if reason_code in REASON_CODES else "narrative_normalizer_internal_error"
        super().__init__(self.reason_code)


def _raise(reason: str) -> None:
    raise NarrativeNormalizerError(reason) from None


def _privacy_boundary(fallback_reason: str):
    """Normalize every ordinary exception without retaining private context."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            reason: str | None = None
            try:
                return function(*args, **kwargs)
            except NarrativeNormalizerError as error:
                reason = error.reason_code
            except Exception:
                reason = fallback_reason
            if reason is not None:
                raise NarrativeNormalizerError(reason) from None
            raise AssertionError("unreachable")
        return wrapped
    return decorate


def _plain(value: object, field: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value.strip()):
        raise TypeError(field)
    return value


def _plain_int(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise TypeError(field)
    return value


def _canonical(value: object) -> bytes:
    if is_dataclass(value):
        value = asdict(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path, reason: str = "narrative_normalizer_draft_invalid") -> str:
    if not os.path.lexists(path) or path.is_symlink() or not path.is_file():
        _raise(reason)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _require_trust_service(
    value: trust.NarrativeTrustService | None,
) -> trust.NarrativeTrustService:
    if type(value) is trust.NarrativeTrustService:
        return value
    try:
        return trust.load_trust_service(os.environ, None)
    except trust.TrustError:
        _raise("narrative_normalizer_trust_unavailable")


def _trust_receipt(value: object) -> trust.TrustReceipt:
    try:
        return trust.receipt_from_payload(value)
    except trust.TrustError:
        _raise("narrative_normalizer_trust_invalid")


def source_identity(source_ref: str, source_digest: str, source_contract_version: str = SOURCE_CONTRACT_VERSION) -> str:
    """Return the exact versioned identity for one logical source occurrence."""
    ref = _safe_source_ref(source_ref)
    if type(source_digest) is not str or _HEX64.fullmatch(source_digest) is None:
        _raise("narrative_normalizer_source_invalid")
    version = _plain(source_contract_version, "source_contract_version")
    payload = ref.encode("utf-8") + b"\0" + source_digest.encode("ascii") + b"\0" + version.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def draft_identity(source_identity_value: str, package_digest: str) -> str:
    if type(source_identity_value) is not str or _HEX64.fullmatch(source_identity_value) is None:
        _raise("narrative_normalizer_draft_invalid")
    if type(package_digest) is not str or _HEX64.fullmatch(package_digest) is None:
        _raise("narrative_normalizer_draft_invalid")
    return _sha({
        "version": DRAFT_IDENTITY_VERSION,
        "source_identity": source_identity_value,
        "package_digest": package_digest,
    })


def _now(clock: Callable[[], datetime]) -> str:
    value = clock()
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError("clock")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str) -> datetime:
    invalid = False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        invalid = True
        parsed = None
    if invalid:
        _raise("narrative_normalizer_claim_invalid")
    assert parsed is not None
    if parsed.tzinfo is None:
        _raise("narrative_normalizer_claim_invalid")
    return parsed.astimezone(timezone.utc)


def _safe_source_ref(value: object) -> str:
    value = _plain(value, "source_ref")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or not path.parts:
        _raise("narrative_normalizer_source_invalid")
    if any(part in {"", ".", ".."} or _SAFE_COMPONENT.fullmatch(part) is None for part in path.parts):
        _raise("narrative_normalizer_source_invalid")
    return value


def _is_safe_source_ref(value: object) -> bool:
    try:
        _safe_source_ref(value)
    except (NarrativeNormalizerError, TypeError, ValueError):
        return False
    return True


def _is_timestamp(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _exact_mapping(value: object, keys: frozenset[str], reason: str) -> dict[str, object]:
    if type(value) is not dict or frozenset(value) != keys or any(type(key) is not str for key in value):
        _raise(reason)
    return value


def _json_read(path: Path, keys: frozenset[str], reason: str) -> dict[str, object]:
    invalid = False
    payload: object = None
    try:
        if path.is_symlink() or not path.is_file():
            _raise(reason)
        payload = json.loads(path.read_bytes().decode("utf-8"))
    except NarrativeNormalizerError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        invalid = True
    if invalid:
        _raise(reason)
    return _exact_mapping(payload, keys, reason)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.tmp-{secrets.token_hex(8)}")
    rollback = path.with_name(f".{path.name}.rollback-{secrets.token_hex(8)}")
    had_old = os.path.lexists(path)
    old_bytes: bytes | None = None
    if had_old:
        if path.is_symlink() or not path.is_file():
            _raise("narrative_normalizer_persistence_invalid")
        old_bytes = path.read_bytes()
    descriptor = -1
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        if os.name != "nt":
            os.chmod(path, mode)
        _fsync_directory(path.parent)
    except BaseException as original:
        cleanup_failure = False
        cleanup_cancellation: BaseException | None = None
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except BaseException as error:
                cleanup_failure = True
                if not isinstance(error, Exception):
                    cleanup_cancellation = error
        current_bytes: bytes | None = None
        try:
            if os.path.lexists(path):
                if path.is_symlink() or not path.is_file():
                    raise OSError("claim-target-invalid")
                current_bytes = path.read_bytes()
        except BaseException as error:
            cleanup_failure = True
            if not isinstance(error, Exception) and cleanup_cancellation is None:
                cleanup_cancellation = error
        if current_bytes != old_bytes:
            if had_old and old_bytes is not None:
                try:
                    _write_exclusive_file(rollback, old_bytes, mode=mode)
                    os.replace(rollback, path)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
            elif os.path.lexists(path):
                try:
                    _cleanup_owned_path(path)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
        if os.path.lexists(temp):
            try:
                _cleanup_owned_path(temp)
            except BaseException as error:
                cleanup_failure = True
                if not isinstance(error, Exception) and cleanup_cancellation is None:
                    cleanup_cancellation = error
        if os.path.lexists(rollback):
            try:
                _cleanup_owned_path(rollback)
            except BaseException as error:
                cleanup_failure = True
                if not isinstance(error, Exception) and cleanup_cancellation is None:
                    cleanup_cancellation = error
        try:
            if had_old:
                if path.is_symlink() or not path.is_file() or path.read_bytes() != old_bytes:
                    raise OSError("claim-rollback-invalid")
            elif os.path.lexists(path):
                raise OSError("claim-partial-target")
            if os.path.lexists(temp) or os.path.lexists(rollback):
                raise OSError("claim-temp-cleanup-invalid")
            _fsync_directory(path.parent)
        except BaseException as error:
            cleanup_failure = True
            if not isinstance(error, Exception) and cleanup_cancellation is None:
                cleanup_cancellation = error
        if not isinstance(original, Exception):
            raise
        if cleanup_cancellation is not None:
            raise cleanup_cancellation from None
        if cleanup_failure:
            _raise("narrative_normalizer_persistence_invalid")
        raise


class _FileLock:
    """Small cross-platform advisory lock; lock files contain no source data."""

    def __init__(
        self,
        path: Path,
        *,
        blocking: bool,
        mode: int = 0o600,
        expected_gid: int | None = None,
    ):
        self.path = path
        self.blocking = blocking
        self.mode = mode
        self.expected_gid = expected_gid
        self._stream = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = -1
        created = False
        try:
            if os.name == "nt":
                stream = self.path.open("a+b")
            else:
                flags = os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                try:
                    descriptor = os.open(
                        self.path,
                        flags | os.O_CREAT | os.O_EXCL,
                        self.mode,
                    )
                    created = True
                except FileExistsError:
                    descriptor = os.open(self.path, flags)
                stream = os.fdopen(descriptor, "a+b", closefd=True)
                descriptor = -1
                if created:
                    if self.expected_gid is not None:
                        os.fchown(stream.fileno(), -1, self.expected_gid)
                    os.fchmod(stream.fileno(), self.mode)
                metadata = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or stat.S_IMODE(metadata.st_mode) != self.mode
                    or (
                        self.expected_gid is not None
                        and metadata.st_gid != self.expected_gid
                    )
                ):
                    stream.close()
                    return False
        except OSError:
            if descriptor >= 0:
                os.close(descriptor)
            return False
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                mode = msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK
                msvcrt.locking(stream.fileno(), mode, 1)
            else:
                import fcntl
                flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
                fcntl.flock(stream.fileno(), flags)
        except (OSError, BlockingIOError):
            stream.close()
            return False
        self._stream = stream
        return True

    def release(self) -> None:
        if self._stream is None:
            return
        stream = self._stream
        self._stream = None
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> "_FileLock":
        if not self.acquire():
            raise BlockingIOError
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


@dataclass(frozen=True, slots=True)
class SourceFact:
    fact_id: str
    exact_text: str
    source_ref: str
    order: int

    def __post_init__(self) -> None:
        _plain(self.fact_id, "fact_id")
        _plain(self.exact_text, "exact_text")
        _safe_source_ref(self.source_ref)
        _plain_int(self.order, "order", minimum=1)

    def to_contract(self) -> translator.SourceFact:
        return translator.SourceFact(self.fact_id, self.exact_text)


@dataclass(frozen=True, slots=True)
class FactExtractionReceipt:
    source_contract_version: str
    source_file_count: int
    fact_count: int
    duplicate_count: int
    excluded_sensitive_count: int
    exact_order_preserved: bool


@dataclass(frozen=True, slots=True)
class SourceUnit:
    source_ref: str
    source_digest: str
    facts: tuple[SourceFact, ...]
    receipt: FactExtractionReceipt
    source_documents: evidence.SourceDocumentBundle | None = None
    verified_evidence: evidence.VerifiedEvidenceBundle | None = None
    verified_fact_bindings: tuple[evidence.VerifiedFactBinding, ...] = ()
    evidence_mode: str = "deterministic_fast_path"

    def __post_init__(self) -> None:
        _safe_source_ref(self.source_ref)
        if type(self.source_digest) is not str or _HEX64.fullmatch(self.source_digest) is None:
            raise TypeError("source_digest")
        if type(self.facts) is not tuple or any(type(item) is not SourceFact for item in self.facts):
            raise TypeError("facts")
        if tuple(item.order for item in self.facts) != tuple(range(1, len(self.facts) + 1)):
            raise ValueError("fact order")
        if any(item.source_ref != self.source_ref for item in self.facts):
            raise ValueError("fact source binding")
        if type(self.receipt) is not FactExtractionReceipt:
            raise TypeError("receipt")
        if self.source_documents is not None and type(self.source_documents) is not evidence.SourceDocumentBundle:
            raise TypeError("source_documents")
        if self.verified_evidence is not None and type(self.verified_evidence) is not evidence.VerifiedEvidenceBundle:
            raise TypeError("verified_evidence")
        if (
            type(self.verified_fact_bindings) is not tuple
            or any(type(item) is not evidence.VerifiedFactBinding for item in self.verified_fact_bindings)
        ):
            raise TypeError("verified_fact_bindings")
        if self.evidence_mode not in {"deterministic_fast_path", "generic"}:
            raise ValueError("evidence_mode")
        if self.evidence_mode == "generic" and len(self.verified_fact_bindings) != len(self.facts):
            raise ValueError("generic evidence binding")


@dataclass(frozen=True, slots=True)
class SentenceLengthSummary:
    sentence_count: int
    minimum_words: int
    maximum_words: int
    average_words_x100: int
    over_limit_count: int


@dataclass(frozen=True, slots=True)
class PlainLanguageReceipt:
    policy_version: str
    unexplained_jargon_count: int
    acronym_count: int
    sentence_length_summary: SentenceLengthSummary
    internal_identifier_count: int
    reader_questions_answered: tuple[str, ...]
    factuality_passed: bool
    meaning_preservation_passed: bool
    significance_mode: str
    passed: bool


@dataclass(frozen=True, slots=True)
class SupportedStoryClaim:
    claim_id: str
    claim_kind: str
    rendered_text: str
    ordered_source_fact_refs: tuple[str, ...]
    semantic_anchors: tuple[str, ...]
    numbers: tuple[str, ...]
    named_entities: tuple[str, ...]
    temporal_relation: str | None
    causal_relation: str | None
    interpretation_mode: str

    def __post_init__(self) -> None:
        if type(self.claim_id) is not str or re.fullmatch(r"claim-(?:hook|story-[123]|ending)", self.claim_id) is None:
            raise TypeError("claim_id")
        if self.claim_kind not in CLAIM_KINDS:
            raise ValueError("claim_kind")
        _plain(self.rendered_text, "rendered_text")
        for field_name in ("ordered_source_fact_refs", "semantic_anchors", "numbers", "named_entities"):
            value = getattr(self, field_name)
            if type(value) is not tuple or any(type(item) is not str or not item for item in value):
                raise TypeError(field_name)
            if len(value) != len(set(value)):
                raise ValueError(field_name)
        if self.temporal_relation is not None and self.temporal_relation not in {"before", "after", "sequence", "same_result"}:
            raise ValueError("temporal_relation")
        if self.causal_relation is not None and self.causal_relation not in {"because", "therefore", "caused"}:
            raise ValueError("causal_relation")
        if self.interpretation_mode not in INTERPRETATION_MODES:
            raise ValueError("interpretation_mode")
        if self.claim_kind == "code_owned_bridge" and self.interpretation_mode != "code_owned":
            raise ValueError("interpretation_mode")
        if self.claim_kind == "explicitly_marked_metaphor" and self.interpretation_mode != "marked_metaphor":
            raise ValueError("interpretation_mode")


@dataclass(frozen=True, slots=True)
class AdjudicatedStatementEvidence:
    statement_name: str
    statement_digest: str
    decision: str
    reason_codes: tuple[str, ...]
    claim_id: str
    claim_digest: str
    inference_kind: str
    ordered_source_fact_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CP2AdjudicationEvidence:
    policy_version: str
    run_id: str
    generation_model: str
    adjudication_model: str
    repair_model: str | None
    model_call_count: int
    source_identity: str
    candidate_id: str
    candidate_rank: int
    package_digest: str
    authority_context_digest: str
    draft_digest: str
    candidate_adjudication_digest: str
    candidate_adjudication: generation.CandidateAdjudication
    validation_context: translator.HumanStoryValidationContext
    editorial_context: generation.NarrativeEditorialContext
    statement_evidence: tuple[AdjudicatedStatementEvidence, ...]
    overall_decision: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CapturedCP2Evidence:
    drafts: tuple[generation.NarrativeDraft, ...]
    adjudications: generation.NarrativeAdjudicationBatch


@dataclass(frozen=True, slots=True)
class FactualityReceipt:
    policy_version: str
    source_identity: str
    candidate_id: str
    package_digest: str
    ordered_claim_ids: tuple[str, ...]
    claim_digests: tuple[str, ...]
    statement_inference_kinds: tuple[str, ...]
    unsupported_claim_ids: tuple[str, ...]
    unsupported_claim_count: int
    adjudication_binding_digest: str
    passed: bool


@dataclass(frozen=True, slots=True)
class MeaningPreservationReceipt:
    policy_version: str
    required_source_anchors: tuple[str, ...]
    covered_source_anchors: tuple[str, ...]
    omitted_anchors: tuple[str, ...]
    source_specificity_score: int
    distinct_story_identity: str
    significance_mode: str
    passed: bool


@dataclass(frozen=True, slots=True)
class ReviewUpdateResult:
    source_identity: str
    draft_identity: str
    status: str
    operator_request_id: str
    idempotent: bool


@dataclass(frozen=True, slots=True)
class NormalizationInput:
    source_ref: str
    source_digest: str
    source_facts: tuple[SourceFact, ...]
    source_contract_version: str
    generation_input: generation.NarrativeGenerationInput
    quarantine_record_identity: str
    normalization_policy_version: str

    def __post_init__(self) -> None:
        _safe_source_ref(self.source_ref)
        if _HEX64.fullmatch(self.source_digest) is None:
            raise ValueError("source_digest")
        if type(self.source_facts) is not tuple or not self.source_facts:
            raise TypeError("source_facts")
        if type(self.generation_input) is not generation.NarrativeGenerationInput:
            raise TypeError("generation_input")
        if self.generation_input.source_ref != self.source_ref:
            raise ValueError("generation source binding")
        expected = tuple(item.to_contract() for item in self.source_facts)
        if self.generation_input.source_facts != expected:
            raise ValueError("generation fact binding")
        for name in ("source_contract_version", "quarantine_record_identity", "normalization_policy_version"):
            _plain(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class DraftArtifact:
    source_ref: str
    source_digest: str
    story_markdown: str
    story_payload: Mapping[str, object]
    draft_manifest: Mapping[str, object]
    review_payload: Mapping[str, object]
    package_digest: str
    review_status: str
    model_call_count: int
    artifact_binding_digest: str


@dataclass(frozen=True, slots=True)
class NormalizationOutcome:
    source_id: str
    source_digest: str
    status: str
    reason_codes: tuple[str, ...]
    model_call_count: int
    package_digest: str | None
    review_status: str | None
    evidence_path: str | None = None

    def __post_init__(self) -> None:
        if type(self.source_id) is not str or re.fullmatch(r"[0-9a-f]{12}", self.source_id) is None:
            raise TypeError("source_id")
        if type(self.source_digest) is not str or _HEX64.fullmatch(self.source_digest) is None:
            raise TypeError("source_digest")
        if self.status not in PUBLIC_OUTCOMES | {OUTCOME_DRY_RUN}:
            raise ValueError("status")
        if (
            type(self.reason_codes) is not tuple
            or any(type(item) is not str or item not in REASON_CODES for item in self.reason_codes)
            or len(self.reason_codes) != len(set(self.reason_codes))
        ):
            raise TypeError("reason_codes")
        if type(self.model_call_count) is not int or isinstance(self.model_call_count, bool) or not 0 <= self.model_call_count <= 5:
            raise TypeError("model_call_count")
        if self.package_digest is not None and (
            type(self.package_digest) is not str or _HEX64.fullmatch(self.package_digest) is None
        ):
            raise TypeError("package_digest")
        if self.review_status is not None and self.review_status not in REVIEW_STATES:
            raise ValueError("review_status")
        if self.evidence_path not in {None, "deterministic_fast_path", "generic"}:
            raise ValueError("evidence_path")


@dataclass(frozen=True, slots=True)
class BatchResult:
    requested_count: int
    outcomes: tuple[NormalizationOutcome, ...]

    def __post_init__(self) -> None:
        if (
            type(self.requested_count) is not int
            or isinstance(self.requested_count, bool)
            or self.requested_count < 0
            or type(self.outcomes) is not tuple
            or any(type(item) is not NormalizationOutcome for item in self.outcomes)
            or self.requested_count != len(self.outcomes)
        ):
            raise TypeError("batch result")

    def safe_summary(self) -> dict[str, object]:
        if self.requested_count != len(self.outcomes):
            _raise("narrative_normalizer_internal_error")
        counts: dict[str, int] = {}
        for item in self.outcomes:
            counts[item.status] = counts.get(item.status, 0) + 1
        accounted_count = sum(counts.values())
        return {
            "requested_count": self.requested_count,
            "completed_count": len(self.outcomes),
            "accounted_count": accounted_count,
            "accounting_complete": accounted_count == self.requested_count,
            "status_counts": {key: counts[key] for key in sorted(counts)},
            "evidence_fast_path": sum(item.evidence_path == "deterministic_fast_path" for item in self.outcomes),
            "evidence_generic_path": sum(item.evidence_path == "generic" for item in self.outcomes),
            OUTCOME_DRAFT_READY_FOR_REVIEW: counts.get(OUTCOME_DRAFT_READY_FOR_REVIEW, 0),
            "source_insufficient": counts.get(OUTCOME_SOURCE_INSUFFICIENT, 0),
            "manual_attention": counts.get(OUTCOME_MANUAL_ATTENTION, 0),
            "sensitive_rejected": counts.get(OUTCOME_SENSITIVE_REJECTED, 0),
            OUTCOME_EXISTING_DRAFT: counts.get(OUTCOME_EXISTING_DRAFT, 0),
            OUTCOME_PROCESSING: counts.get(OUTCOME_PROCESSING, 0),
            OUTCOME_FAILED: counts.get(OUTCOME_FAILED, 0),
            OUTCOME_UNCERTAIN: counts.get(OUTCOME_UNCERTAIN, 0),
            "items": [
                {
                    "source_id": item.source_id,
                    "source_digest": item.source_digest,
                    "status": item.status,
                    "reason_codes": list(item.reason_codes),
                    "model_call_count": item.model_call_count,
                    "package_digest": item.package_digest,
                    "review_status": item.review_status,
                    "evidence_path": item.evidence_path,
                }
                for item in self.outcomes
            ],
        }


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    source_digest: str
    manifest_digest: str
    status: str
    idempotent: bool


class ReviewAuthorityTransport(Protocol):
    """State-free Broker proxy used by production-capable review operations."""

    def register_draft(self, request_id: str, payload: dict[str, object]) -> dict[str, object]: ...
    def latest_state(self, request_id: str, payload: dict[str, object]) -> dict[str, object]: ...
    def append_review(self, request_id: str, payload: dict[str, object]) -> dict[str, object]: ...
    def prepare_approval(self, request_id: str, payload: dict[str, object]) -> dict[str, object]: ...
    def commit_approval(self, request_id: str, payload: dict[str, object]) -> dict[str, object]: ...


class NarrativeContextProvider(Protocol):
    def build(self, source: SourceUnit) -> generation.NarrativeGenerationInput:
        """Return immutable CP2 context bound to exactly this source."""


@dataclass(frozen=True, slots=True)
class TemplateNarrativeContextProvider:
    """Bind reviewed immutable receipts from a supplied template to a source."""

    template: generation.NarrativeGenerationInput

    def __post_init__(self) -> None:
        if type(self.template) is not generation.NarrativeGenerationInput:
            raise TypeError("template")

    def build(self, source: SourceUnit) -> generation.NarrativeGenerationInput:
        facts = tuple(item.to_contract() for item in source.facts)
        identity = source_identity(source.source_ref, source.source_digest, source.receipt.source_contract_version)
        plan = replace(
            self.template.editorial_plan,
            plan_id=f"normalizer-{identity[:24]}",
            source_ref=source.source_ref,
        )
        return replace(self.template, source_ref=source.source_ref, source_facts=facts, editorial_plan=plan)


@_privacy_boundary("narrative_normalizer_context_invalid")
def build_normalization_input(
    source: SourceUnit,
    context_provider: NarrativeContextProvider,
) -> NormalizationInput:
    invalid = False
    result: NormalizationInput | None = None
    try:
        context = context_provider.build(source)
        if type(context) is not generation.NarrativeGenerationInput or any(
            _is_sensitive(item) for item in _context_strings(context)
        ):
            _raise("narrative_normalizer_context_invalid")
        result = NormalizationInput(
            source_ref=source.source_ref,
            source_digest=source.source_digest,
            source_facts=source.facts,
            source_contract_version=SOURCE_CONTRACT_VERSION,
            generation_input=context,
            quarantine_record_identity=source_identity(
                source.source_ref, source.source_digest, source.receipt.source_contract_version
            ),
            normalization_policy_version=NORMALIZATION_POLICY_VERSION,
        )
    except NarrativeNormalizerError:
        raise
    except Exception:
        invalid = True
    if invalid or result is None:
        _raise("narrative_normalizer_context_invalid")
    return result


def _is_sensitive(text: str) -> bool:
    return bool(_ABSOLUTE_PATH.search(text) or _SECRET.search(text))


def _context_strings(value: object) -> Iterable[str]:
    """Walk immutable context data only to enforce the provider privacy gate."""
    if is_dataclass(value):
        yield from _context_strings(asdict(value))
    elif type(value) is dict:
        for key, item in value.items():
            if type(key) is str:
                yield key
            yield from _context_strings(item)
    elif type(value) in (list, tuple):
        for item in value:
            yield from _context_strings(item)
    elif type(value) is str:
        yield value


def _json_fact_strings(value: object, *, parent_key: str = "") -> Iterable[str]:
    if type(value) is dict:
        fact_keys = {"facts", "source_facts", "confirmed_facts", "observations", "events"}
        selected = tuple((key, item) for key, item in value.items() if type(key) is str and key.casefold() in fact_keys)
        for key, item in selected:
            if type(key) is not str:
                continue
            yield from _json_fact_strings(item, parent_key=key)
    elif type(value) in (list, tuple):
        for item in value:
            yield from _json_fact_strings(item, parent_key=parent_key)
    elif type(value) is str and value.strip():
        yield value
    elif type(value) in (int, float) and type(value) is not bool:
        yield str(value)


def _text_fact_strings(text: str) -> Iterable[str]:
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith(("```", "#")):
            continue
        yield line


def _source_path(policy: quarantine.QuarantinePathPolicy, source_ref: str) -> Path:
    source_ref = _safe_source_ref(source_ref)
    candidate = policy.inbox_root.joinpath(*PurePosixPath(source_ref).parts)
    unavailable = False
    try:
        root = policy.inbox_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError):
        unavailable = True
        root = resolved = policy.inbox_root
    if unavailable:
        _raise("narrative_normalizer_source_invalid")
    escaped = False
    try:
        resolved.relative_to(root)
    except ValueError:
        escaped = True
    if escaped:
        _raise("narrative_normalizer_source_invalid")
    cursor = root
    for part in resolved.relative_to(root).parts:
        cursor = cursor / part
        if cursor.is_symlink():
            _raise("narrative_normalizer_source_invalid")
    if not resolved.is_dir():
        _raise("narrative_normalizer_source_invalid")
    return resolved


@_privacy_boundary("narrative_normalizer_source_invalid")
def read_source_unit(
    policy: quarantine.QuarantinePathPolicy,
    source_ref: str,
    *,
    expected_digest: str | None = None,
    allow_insufficient: bool = False,
) -> SourceUnit:
    """Read one complete unit without mutating or truncating the source."""
    invalid = False
    result: SourceUnit | None = None
    try:
        source_path = _source_path(policy, source_ref)
        source_digest = quarantine.source_digest(source_path)
        if expected_digest is not None and source_digest != expected_digest:
            _raise("narrative_normalizer_source_changed")
        candidates: list[str] = []
        sensitive_count = 0
        source_files = 0
        paths = sorted(source_path.rglob("*"), key=lambda item: item.relative_to(source_path).as_posix())
        for path in paths:
            if path.is_symlink():
                _raise("narrative_normalizer_source_invalid")
            if not path.is_file() or path.name == "narrative_ready.json" or path.suffix.casefold() not in _SOURCE_FILE_SUFFIXES:
                continue
            source_files += 1
            raw = path.read_bytes()
            if len(raw) > MAX_SOURCE_FILE_BYTES:
                _raise("narrative_normalizer_source_invalid")
            text = raw.decode("utf-8")
            if path.suffix.casefold() in {".json", ".jsonl"}:
                try:
                    if path.suffix.casefold() == ".jsonl":
                        documents = tuple(
                            json.loads(line, parse_int=str, parse_float=str)
                            for line in text.splitlines()
                            if line.strip()
                        )
                    else:
                        documents = (json.loads(text, parse_int=str, parse_float=str),)
                except json.JSONDecodeError:
                    documents = ()
                values = tuple(item for document in documents for item in _json_fact_strings(document)) if documents else tuple(_text_fact_strings(text))
            else:
                values = tuple(_text_fact_strings(text))
            for value in values:
                if _is_sensitive(value):
                    sensitive_count += 1
                else:
                    candidates.append(value)
        seen: set[str] = set()
        facts: list[SourceFact] = []
        duplicate_count = 0
        for value in candidates:
            if value in seen:
                duplicate_count += 1
                continue
            seen.add(value)
            order = len(facts) + 1
            facts.append(SourceFact(f"fact-{order}", value, source_ref, order))
        if len(facts) < MIN_SOURCE_FACTS and not allow_insufficient:
            _raise("narrative_normalizer_source_insufficient")
        receipt = FactExtractionReceipt(
            SOURCE_CONTRACT_VERSION,
            source_files,
            len(facts),
            duplicate_count,
            sensitive_count,
            True,
        )
        result = SourceUnit(source_ref, source_digest, tuple(facts), receipt)
    except NarrativeNormalizerError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        invalid = True
    if invalid or result is None:
        _raise("narrative_normalizer_source_invalid")
    return result


@_privacy_boundary("narrative_normalizer_source_invalid")
def read_source_documents(
    policy: quarantine.QuarantinePathPolicy,
    source_ref: str,
    *,
    expected_digest: str,
) -> evidence.SourceDocumentBundle:
    """Build the immutable exact-byte/character source view without writes."""
    _source_path(policy, source_ref)
    return evidence.build_source_document_bundle(
        policy.inbox_root,
        source_ref,
        expected_digest,
        SOURCE_CONTRACT_VERSION,
    )


def _source_from_verified_evidence(
    raw_source: SourceUnit,
    documents: evidence.SourceDocumentBundle,
    verified: evidence.VerifiedEvidenceBundle,
) -> SourceUnit:
    """Project only code-verified evidence into the unchanged CP1/CP2 fact tuple."""
    bindings = evidence.build_verified_fact_bindings(documents, verified)
    if type(bindings) is not tuple or len(bindings) < MIN_SOURCE_FACTS:
        _raise("narrative_normalizer_source_insufficient")
    facts = tuple(
        SourceFact(
            binding.fact_id,
            binding.public_proposition,
            raw_source.source_ref,
            binding.order,
        )
        for binding in bindings
    )
    receipt = FactExtractionReceipt(
        SOURCE_CONTRACT_VERSION,
        len(documents.ordered_documents),
        len(facts),
        0,
        sum(item.disposition == "sensitive" for item in verified.extraction.ordered_segment_dispositions),
        True,
    )
    return SourceUnit(
        raw_source.source_ref,
        raw_source.source_digest,
        facts,
        receipt,
        documents,
        verified,
        bindings,
        "generic",
    )


def _replay_source_for_story(
    raw_source: SourceUnit,
    documents: evidence.SourceDocumentBundle,
    story: Mapping[str, object],
) -> SourceUnit:
    mode = story.get("evidence_mode")
    if mode == "deterministic_fast_path":
        return replace(raw_source, source_documents=documents)
    if mode != "generic" or story.get("source_document_digest") != documents.bundle_digest:
        _raise("narrative_normalizer_source_changed")
    try:
        verified = evidence.verified_bundle_from_payload(story.get("source_evidence_bundle"), documents)
        evidence.revalidate_verified_bundle(documents, verified)
    except evidence.EvidenceContractError:
        _raise("narrative_normalizer_evidence_invalid")
    replayed = _source_from_verified_evidence(raw_source, documents, verified)
    expected_bindings = [
        evidence.verified_fact_binding_to_payload(item)
        for item in replayed.verified_fact_bindings
    ]
    if story.get("verified_fact_bindings") != expected_bindings:
        _raise("narrative_normalizer_evidence_invalid")
    return replayed


@_privacy_boundary("narrative_normalizer_registry_invalid")
def scan_needs_narrative(policy: quarantine.QuarantinePathPolicy) -> tuple[tuple[str, str], ...]:
    """Read the quarantine registry and return deterministic raw work identities."""
    invalid = False
    rows: tuple[tuple[str, str], ...] = ()
    try:
        registry = quarantine.read_registry(policy.registry_path)
        rows = tuple(
            (item.source_ref, item.source_digest)
            for item in registry.records
            if item.classification == quarantine.CLASS_RAW
            and item.status == quarantine.STATUS_NEEDS_NARRATIVE
        )
        rows = tuple(sorted(rows))
    except Exception:
        invalid = True
    if invalid:
        _raise("narrative_normalizer_registry_invalid")
    return rows


@dataclass(frozen=True, slots=True)
class _SemanticAnchorRule:
    anchor_id: str
    source_pattern: str
    rendered_pattern: str


@dataclass(frozen=True, slots=True)
class _SemanticOperandRule:
    anchor_id: str
    source_pattern: str
    rendered_pattern: str


@dataclass(frozen=True, slots=True)
class _ClosedSourceFactRule:
    rule_id: str
    source_pattern: re.Pattern[str]
    required_semantic_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _SemanticRenderedRejection:
    anchor_id: str
    rendered_pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class _RenderedFactProjectionRule:
    source_rule_ids: frozenset[str]
    rendered_pattern: re.Pattern[str]


_SEMANTIC_ANCHOR_RULES = (
    _SemanticAnchorRule(
        "safe_atomic_replacement",
        r"(?:atomic|safe)\s+(?:file\s+)?replac|replace[^.]{0,32}safely",
        r"(?:безопасн|атомарн)[^.\n]{0,40}замен|замен[^.\n]{0,40}(?:безопасн|атомарн)",
    ),
    _SemanticAnchorRule(
        "utf8_without_bom",
        r"utf-?8[^.\n]{0,80}(?:without|no)[^.\n]{0,40}(?:byte-order mark|bom)",
        r"utf-?8[^.\n]{0,80}без[^.\n]{0,40}(?:метк|знак|сигнатур|bom)|без лишн[^.\n]{0,40}(?:метк|знак)[^.\n]{0,30}utf-?8",
    ),
    _SemanticAnchorRule(
        "working_directory",
        r"working (?:directory|folder)",
        r"рабоч[^.\n]{0,24}(?:папк|каталог|мест)",
    ),
    _SemanticAnchorRule(
        "parent_directory",
        r"parent (?:directory|folder)|directory[^.\n]{0,30}parent",
        r"родительск[^.\n]{0,24}(?:папк|каталог)|(?:папк|каталог)[^.\n]{0,32}уровн[^.\n]{0,12}выше",
    ),
    _SemanticAnchorRule(
        "repeated_same_result",
        r"(?:rerun|repeat)[^.\n]{0,60}(?:same|identical)[^.\n]{0,30}(?:result|digest|outcome)",
        r"повтор[^.\n]{0,60}(?:тот же|такой же|одинаков)[^.\n]{0,30}(?:результат|итог|отпечаток)|(?:тот же|одинаков)[^.\n]{0,30}(?:результат|итог)",
    ),
    _SemanticAnchorRule(
        "manual_inspection_not_blind_trust",
        r"manual inspection[^.\n]{0,80}(?:instead of|not)[^.\n]{0,30}blind trust",
        r"(?=[^.\n]{0,100}ручн)(?=[^.\n]{0,130}(?:не[^.\n]{0,30}довер|вместо[^.\n]{0,30}слеп|не[^.\n]{0,30}вслеп))[^.\n]+",
    ),
    _SemanticAnchorRule("file_object", r"\bfile\b", r"\b(?:file|файл\w*)\b"),
    _SemanticAnchorRule(
        "configuration_object",
        r"\b(?:config|configuration)\b|(?<!\w)\.env(?!\w)",
        r"\b(?:config\w*|configuration\w*|конфиг\w*|настро(?:ек|йк\w*))\b",
    ),
    _SemanticAnchorRule("notebook_near_keyboard", r"notebook[^.\n]{0,60}(?:beside|near)[^.\n]{0,30}keyboard", r"книжк[^.\n]{0,60}рядом[^.\n]{0,30}клавиатур"),
    _SemanticAnchorRule("three_observations", r"three short observations", r"три[^.\n]{0,30}(?:наблюден|заметк|запис)"),
    _SemanticAnchorRule("before_conclusion", r"before any conclusion", r"до[^.\n]{0,30}(?:вывод|заключен)"),
    _SemanticAnchorRule("notebook_closed", r"notebook was closed", r"книжк[^.\n]{0,30}закры\w*|закры\w*[^.\n]{0,30}книжк"),
    _SemanticAnchorRule("final_check", r"final check", r"последн[^.\n]{0,20}проверк|финальн[^.\n]{0,20}проверк"),
    _SemanticAnchorRule(
        "build_completed",
        r"build[^.\n]{0,24}(?:completed|finished)",
        r"сборк[^.\n]{0,24}(?:заверш|закончил)|(?:заверш|закончил)[^.\n]{0,24}сборк",
    ),
    _SemanticAnchorRule(
        "test_failed",
        r"test[^.\n]{0,24}failed",
        r"(?:тест|проверк)[^.\n]{0,24}(?:провал|не прош)|(?:провал|не прош)[^.\n]{0,24}(?:тест|проверк)",
    ),
    _SemanticAnchorRule("two_reviewers", r"two reviewers", r"два[^.\n]{0,20}(?:человек|рецензент|проверя)"),
    _SemanticAnchorRule("same_prototype", r"same prototype", r"один[^.\n]{0,20}(?:образец|прототип)|тот же[^.\n]{0,20}прототип"),
    _SemanticAnchorRule("different_desks", r"different desks", r"разн[^.\n]{0,30}(?:мест|стол)"),
    _SemanticAnchorRule("fragile_step", r"fragile step", r"хрупк[^.\n]{0,20}шаг"),
    _SemanticAnchorRule("another_test", r"another test", r"(?:нов|ещ[её])[^.\n]{0,20}проверк"),
    _SemanticAnchorRule("different_wording", r"different wording", r"(?:разн[^.\n]{0,20}слов|по-разному)"),
    _SemanticAnchorRule("same_next_action", r"same next action", r"одн[^.\n]{0,25}(?:следующ[^.\n]{0,15})?(?:действ|решен)"),
)

_SEMANTIC_OPERAND_RULES = (
    _SemanticOperandRule(
        "manual_inspection_not_blind_trust",
        r"manual inspection",
        r"ручн\w*\s+(?:проверк\w*|осмотр\w*|сверк\w*)|"
        r"(?:проверк\w*|осмотр\w*|сверк\w*)[^,.;]{0,20}ручн\w*",
    ),
)

_CLOSED_SOURCE_FACT_RULES = (
    _ClosedSourceFactRule(
        "env-atomic-file-replacement",
        re.compile(
            r"(?:the\s+)?\.env\s+file\s+was\s+created\s+with\s+an?\s+atomic\s+replacement\.?",
            re.IGNORECASE,
        ),
        frozenset({"safe_atomic_replacement", "file_object", "configuration_object"}),
    ),
    _ClosedSourceFactRule(
        "atomic-file-replacement",
        re.compile(
            r"(?:the\s+)?file\s+used\s+safe\s+atomic\s+replacement\.?",
            re.IGNORECASE,
        ),
        frozenset({"safe_atomic_replacement", "file_object"}),
    ),
    _ClosedSourceFactRule(
        "utf8-file-without-bom",
        re.compile(r"the\s+file\s+was\s+encoded\s+as\s+utf-?8\s+without\s+(?:a\s+)?(?:byte-order\s+mark|bom)\.?", re.IGNORECASE),
        frozenset({"utf8_without_bom", "file_object"}),
    ),
    _ClosedSourceFactRule(
        "working-and-parent-checked",
        re.compile(r"the\s+working\s+(?:directory|folder)\s+and\s+its\s+parent\s+were\s+both\s+checked\.?", re.IGNORECASE),
        frozenset({"working_directory", "parent_directory"}),
    ),
    _ClosedSourceFactRule(
        "rerun-same-result",
        re.compile(r"a\s+rerun\s+confirmed\s+the\s+same\s+result\.?", re.IGNORECASE),
        frozenset({"repeated_same_result"}),
    ),
    _ClosedSourceFactRule(
        "manual-not-blind",
        re.compile(r"a\s+manual\s+inspection\s+was\s+used\s+instead\s+of\s+blind\s+trust\.?", re.IGNORECASE),
        frozenset({"manual_inspection_not_blind_trust"}),
    ),
    _ClosedSourceFactRule(
        "notebook-near-keyboard",
        re.compile(r"a\s+worn\s+notebook\s+remained\s+beside\s+the\s+keyboard\s+throughout\s+the\s+work\.?", re.IGNORECASE),
        frozenset({"notebook_near_keyboard"}),
    ),
    _ClosedSourceFactRule(
        "observations-before-conclusion",
        re.compile(r"three\s+short\s+observations\s+were\s+written\s+down\s+before\s+any\s+conclusion\.?", re.IGNORECASE),
        frozenset({"three_observations", "before_conclusion"}),
    ),
    _ClosedSourceFactRule(
        "notebook-after-final-check",
        re.compile(r"the\s+notebook\s+was\s+closed\s+after\s+the\s+final\s+check\.?", re.IGNORECASE),
        frozenset({"notebook_closed", "final_check"}),
    ),
    _ClosedSourceFactRule(
        "reviewers-prototype-desks",
        re.compile(r"two\s+reviewers\s+examined\s+the\s+same\s+prototype\s+from\s+different\s+desks\.?", re.IGNORECASE),
        frozenset({"two_reviewers", "same_prototype", "different_desks"}),
    ),
    _ClosedSourceFactRule(
        "fragile-step-another-test",
        re.compile(r"they\s+marked\s+the\s+same\s+fragile\s+step\s+for\s+another\s+test\.?", re.IGNORECASE),
        frozenset({"fragile_step", "another_test"}),
    ),
    _ClosedSourceFactRule(
        "different-wording-same-action",
        re.compile(r"their\s+notes\s+used\s+different\s+wording\s+but\s+proposed\s+the\s+same\s+next\s+action\.?", re.IGNORECASE),
        frozenset({"different_wording", "same_next_action"}),
    ),
    _ClosedSourceFactRule(
        "safe-because-manual",
        re.compile(
            r"safe\s+atomic\s+replacement\s+happened\s+because\s+manual\s+inspection\s+was\s+used\s+instead\s+of\s+blind\s+trust\.?",
            re.IGNORECASE,
        ),
        frozenset({"safe_atomic_replacement", "manual_inspection_not_blind_trust"}),
    ),
    _ClosedSourceFactRule(
        "explicit-sequence",
        re.compile(r"first\s+[a-z][a-z0-9_]*\s+then\s+[a-z][a-z0-9_]*\.?", re.IGNORECASE),
        frozenset(),
    ),
)

_SEMANTIC_RENDERED_REJECTIONS = (
    _SemanticRenderedRejection(
        "safe_atomic_replacement",
        re.compile(r"\b(?:не\s*(?:безопасн|атомарн)|небезопасн|неатомарн)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "utf8_without_bom",
        re.compile(r"\b(?:не\s+без|not\s+without)\b[^.\n]{0,80}(?:метк|знак|сигнатур|bom|byte-order\s+mark)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "manual_inspection_not_blind_trust",
        re.compile(r"(?:\bне\s+было[^.\n]{0,30}ручн|ручн[^.\n]{0,40}\bне\s+(?:прош|состоя|выполн|провед))", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "repeated_same_result",
        re.compile(r"\b(?:не\s+(?:тот\s+же|такой\s+же|одинаков)|not\s+(?:the\s+same|identical))", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "working_directory",
        re.compile(r"рабоч[^.\n]{0,35}\bне\s+провер", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "parent_directory",
        re.compile(r"(?:родительск|уровн[^.\n]{0,12}выше)[^.\n]{0,35}\bне\s+провер", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "notebook_closed",
        re.compile(r"(?:книжк[^.\n]{0,25}\bне\s+закры|\bне\s+закры[^.\n]{0,25}книжк)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "notebook_near_keyboard",
        re.compile(r"книжк[^.\n]{0,35}\bне\s+(?:леж|наход|остав)[^.\n]{0,35}рядом", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "three_observations",
        re.compile(r"(?:три[^.\n]{0,30}\bне\s+(?:внес|запис|сохран)|\bне\s+(?:внес|запис|сохран)[^.\n]{0,30}три)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "before_conclusion",
        re.compile(r"\bне\s+до[^.\n]{0,30}(?:вывод|заключен)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "final_check",
        re.compile(r"(?:\bбез[^.\n]{0,25}(?:финальн|последн)[^.\n]{0,15}проверк|(?:финальн|последн)[^.\n]{0,25}проверк[^.\n]{0,20}\bне\s+(?:был|пров))", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "two_reviewers",
        re.compile(r"\bне\s+два[^.\n]{0,20}(?:человек|рецензент|проверя)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "same_prototype",
        re.compile(r"\bне\s+(?:один|тот\s+же)[^.\n]{0,20}(?:образец|прототип)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "different_desks",
        re.compile(r"\bне\s+разн[^.\n]{0,30}(?:мест|стол)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "fragile_step",
        re.compile(r"\bне\s+хрупк[^.\n]{0,20}шаг", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "another_test",
        re.compile(r"(?:\bбез[^.\n]{0,20}(?:нов|ещ[её])[^.\n]{0,15}проверк|(?:нов|ещ[её])[^.\n]{0,20}проверк[^.\n]{0,15}\bне\s+)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "different_wording",
        re.compile(r"\bне\s+разн[^.\n]{0,25}слов", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "same_next_action",
        re.compile(r"\b(?:не\s+одно|разн\w*)[^.\n]{0,25}(?:действ|решен)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "build_completed",
        re.compile(r"сборк[^.\n]{0,24}\bне\s+(?:заверш|закончил)", re.IGNORECASE),
    ),
    _SemanticRenderedRejection(
        "test_failed",
        re.compile(r"(?:тест|проверк)[^.\n]{0,24}\bне\s+(?:провал|прош)", re.IGNORECASE),
    ),
)
_SEMANTIC_ASSERTION_NEGATION = re.compile(
    r"\b(?:не|нет|ни|никогда)\b|\b(?:isn't|aren't|wasn't|weren't|don't|doesn't|didn't|"
    r"hasn't|haven't|hadn't|can't|couldn't|won't|wouldn't|shouldn't|mustn't)\b",
    re.IGNORECASE,
)

_RENDERED_FACT_PROJECTION_RULES = (
    _RenderedFactProjectionRule(
        frozenset({"env-atomic-file-replacement"}),
        re.compile(
            r"(?:файл\s+настроек\s+безопасно\s+заменили|"
            r"файл\s+настроек\s+заменили\s+безопасно)\.?",
            re.IGNORECASE,
        ),
    ),
    _RenderedFactProjectionRule(
        frozenset({"atomic-file-replacement"}),
        re.compile(
            r"(?:файл\s+безопасно\s+заменили|файл\s+заменили\s+безопасно)\.?",
            re.IGNORECASE,
        ),
    ),
    _RenderedFactProjectionRule(
        frozenset({"utf8-file-without-bom"}),
        re.compile(
            r"файл\s+(?:записали|сохранили)\s+в\s+utf-?8\s+без\s+"
            r"(?:лишней\s+)?(?:служебной\s+)?(?:метки|знака|bom)(?:\s+в\s+начале)?\.?",
            re.IGNORECASE,
        ),
    ),
    _RenderedFactProjectionRule(
        frozenset({"working-and-parent-checked"}),
        re.compile(r"рабочую\s+папку\s+и\s+папку\s+уровнем\s+выше\s+проверили\.?", re.IGNORECASE),
    ),
    _RenderedFactProjectionRule(
        frozenset({"rerun-same-result"}),
        re.compile(r"повторная\s+проверка\s+дала\s+(?:тот\s+же|такой\s+же|одинаковый)\s+результат\.?", re.IGNORECASE),
    ),
    _RenderedFactProjectionRule(
        frozenset({"manual-not-blind"}),
        re.compile(r"ручн(?:ая|ой)\s+(?:проверка|сверка|осмотр)\s+прош(?:ла|[её]л)\s+вместо\s+слепого\s+доверия\.?", re.IGNORECASE),
    ),
    _RenderedFactProjectionRule(
        frozenset({"notebook-near-keyboard"}),
        re.compile(
            r"пот[её]ртая\s+записная\s+книжка\s+вс[её]\s+время\s+"
            r"лежала\s+рядом\s+с\s+клавиатурой\.?",
            re.IGNORECASE,
        ),
    ),
    _RenderedFactProjectionRule(
        frozenset({"observations-before-conclusion"}),
        re.compile(
            r"(?:до\s+любого\s+вывода\s+(?:внесли|записали)\s+"
            r"три\s+коротких\s+(?:наблюдения|заметки|записи)|"
            r"три\s+коротких\s+(?:наблюдения|заметки|записи)\s+(?:сохранили|записали)\s+"
            r"до\s+любого\s+вывода)\.?",
            re.IGNORECASE,
        ),
    ),
    _RenderedFactProjectionRule(
        frozenset({"notebook-after-final-check"}),
        re.compile(r"(?:после\s+последней\s+проверки\s+книжку\s+закрыли|книжку\s+закрыли\s+после\s+последней\s+проверки)\.?", re.IGNORECASE),
    ),
    _RenderedFactProjectionRule(
        frozenset({"reviewers-prototype-desks"}),
        re.compile(r"два\s+человека\s+изучили\s+один\s+образец\s+с\s+разных\s+рабочих\s+мест\.?", re.IGNORECASE),
    ),
    _RenderedFactProjectionRule(
        frozenset({"fragile-step-another-test"}),
        re.compile(r"они\s+отметили\s+один\s+и\s+тот\s+же\s+хрупкий\s+шаг\s+для\s+новой\s+проверки\.?", re.IGNORECASE),
    ),
    _RenderedFactProjectionRule(
        frozenset({"different-wording-same-action"}),
        re.compile(
            r"(?:разные\s+слова\s+(?:указали|описали)|"
            r"по-разному\s+написанные\s+записи\s+указали)\s+"
            r"одно(?:\s+и\s+то\s+же)?\s+следующее\s+действие\.?",
            re.IGNORECASE,
        ),
    ),
    _RenderedFactProjectionRule(
        frozenset({"safe-because-manual"}),
        re.compile(
            r"потому\s+что\s+(?:была\s+)?ручная\s+проверка\s+вместо\s+"
            r"слепого\s+доверия\s*,\s*замену\s+"
            r"(?:выполнили|провели)\s+безопасно\.?",
            re.IGNORECASE,
        ),
    ),
    _RenderedFactProjectionRule(
        frozenset({"explicit-sequence"}),
        re.compile(r"first\s+[a-z][a-z0-9_]*\s+then\s+[a-z][a-z0-9_]*\.?", re.IGNORECASE),
    ),
)

_STOPWORDS = frozenset({
    "about", "after", "again", "also", "another", "before", "both", "from", "into", "only",
    "same", "that", "their", "there", "these", "they", "this", "throughout", "used", "was", "were",
    "with", "without",
})
_NUMBER = re.compile(
    r"(?<![\w+\-−])(?:[$€₽]\s*)?[+\-−]?\d+(?:[.,]\d+)*(?:[eE][+\-−]?\d+)?(?:%|\s?(?:usd|eur|rub|₽|\$|€))?(?![\w-])",
    re.IGNORECASE,
)
_DATE_TOKEN = re.compile(r"(?<!\d)(?:\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{2,4})(?!\d)")
_TEXTUAL_DATE = re.compile(
    r"(?<!\w)(\d{1,2})\s+([A-Za-zА-Яа-яЁё]+)\s+(\d{4})(?!\d)",
    re.IGNORECASE,
)
_MONTH_NAMES = {
    "january": 1, "jan": 1, "январь": 1, "января": 1,
    "february": 2, "feb": 2, "февраль": 2, "февраля": 2,
    "march": 3, "mar": 3, "март": 3, "марта": 3,
    "april": 4, "apr": 4, "апрель": 4, "апреля": 4,
    "may": 5, "май": 5, "мая": 5,
    "june": 6, "jun": 6, "июнь": 6, "июня": 6,
    "july": 7, "jul": 7, "июль": 7, "июля": 7,
    "august": 8, "aug": 8, "август": 8, "августа": 8,
    "september": 9, "sep": 9, "сентябрь": 9, "сентября": 9,
    "october": 10, "oct": 10, "октябрь": 10, "октября": 10,
    "november": 11, "nov": 11, "ноябрь": 11, "ноября": 11,
    "december": 12, "dec": 12, "декабрь": 12, "декабря": 12,
}
_ENTITY = re.compile(
    r"\b(?:[A-ZА-ЯЁ][a-zа-яё]+\s+){1,3}[A-ZА-ЯЁ][a-zа-яё]+\b|"
    r"\b[A-Z][A-Za-z0-9&.-]+\s+(?:Inc|Corp|LLC|Ltd|Company)\b|"
    r"\b(?:ООО|АО)\s+[«\"]?[A-ZА-ЯЁ][^.,;!?\n\"]{1,50}[»\"]?",
)
_SINGLE_LATIN_ENTITY = re.compile(r"\b[A-Z][a-z]{2,}\b")
_ACRONYM_ENTITY = re.compile(r"\b[A-ZА-ЯЁ][A-ZА-ЯЁ0-9&.-]{1,}\b")
_LEADING_LOWER_ENTITY = re.compile(r"(?m)^\s*([a-z][a-z0-9._-]{2,})\b")
_CONTEXTUAL_LOWER_ENTITY = re.compile(
    r"\b(?:the\s+)?([a-z][a-z0-9._-]{2,})\s+(?:team|company|project|product)\b|"
    r"\b(?:team|company|project|product)\s+([a-z][a-z0-9._-]{2,})\b|"
    r"\b(?:команда|компания|проект|продукт)\s+([a-z][a-z0-9._-]{2,})\b",
    re.IGNORECASE,
)
_ENTITY_STOPWORDS = frozenset({
    "The", "One", "Two", "Three", "File", "After", "Before", "Then", "This", "That",
    "They", "Their", "Them", "He", "His", "She", "Her", "It", "Its", "We", "Our",
    "Safe", "Build", "Manual", "First",
})
_LOWER_ENTITY_STOPWORDS = frozenset({
    "the", "one", "two", "three", "after", "before", "then", "this", "that", "they",
    "their", "them", "he", "his", "she", "her", "its", "our", "we", "file", "config",
    "build", "release", "outage", "users", "clients",
})
_TEMPORAL_PATTERNS = (
    ("before", re.compile(r"\b(?:before|до)\b", re.IGNORECASE)),
    ("after", re.compile(r"\b(?:after|после|затем)\b", re.IGNORECASE)),
    ("same_result", re.compile(r"\b(?:rerun|repeat|повтор)[^.\n]{0,60}(?:same|тот же|одинаков)", re.IGNORECASE)),
    ("sequence", re.compile(r"\b(?:first.+then|сначала.+затем|по очереди)\b", re.IGNORECASE)),
)
_CAUSAL_PATTERNS = (
    ("because", re.compile(r"\b(?:because|потому что|так как)\b", re.IGNORECASE)),
    ("therefore", re.compile(r"\b(?:therefore|thus|поэтому|следовательно)\b", re.IGNORECASE)),
    ("caused", re.compile(r"\b(?:caused?|привел[аои]? к|вызвал[аои]?)\b", re.IGNORECASE)),
)
_MARKED_METAPHOR = re.compile(r"\b(?:metaphorically|as if|like a metaphor|метафорически|словно|как будто)\b", re.IGNORECASE)
_LITERAL_METAPHOR = re.compile(r"\b(?:storm|war|exploded|earthquake|буря|война|взорвал|землетрясение)\b", re.IGNORECASE)
_SENSITIVE_CLAIM_CATEGORIES = (
    ("outage", re.compile(r"\b(?:outage|downtime|сбой|простой)\b", re.IGNORECASE)),
    ("users", re.compile(r"\b(?:users?|пользовател\w*)\b", re.IGNORECASE)),
    ("clients", re.compile(r"\b(?:clients?|customers?|клиент[а-я]*)\b", re.IGNORECASE)),
    ("money", re.compile(r"(?:\b(?:money|revenue|cost|loss|денег|выручк|стоимост|убыт)\b|[$€₽])", re.IGNORECASE)),
    ("damage", re.compile(r"\b(?:damage|harm|ущерб|вред)\b", re.IGNORECASE)),
    ("production_incident", re.compile(r"\b(?:production incident|prod incident|инцидент[^.\n]{0,20}продакш|авария)\b", re.IGNORECASE)),
    ("emotion", re.compile(r"\b(?:afraid|angry|panic|sad|happy|\w*испуг\w*|\w*злост\w*|\w*паник\w*|\w*груст\w*|\w*счаст\w*)\b", re.IGNORECASE)),
    ("deadline", re.compile(r"\b(?:deadline|due date|дедлайн|срок)\b", re.IGNORECASE)),
    ("impact", re.compile(r"\b(?:impact|affected|consequence|влияни|затрон|последстви)\w*\b", re.IGNORECASE)),
)
_NEGATION = re.compile(r"\b(?:no|not|never|without|не|нет|никогда|без)\b", re.IGNORECASE)
_NEGATIVE_CONTRACTION = re.compile(
    r"\b(?:isn't|aren't|wasn't|weren't|don't|doesn't|didn't|hasn't|haven't|hadn't|"
    r"can't|couldn't|won't|wouldn't|shouldn't|mustn't)\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY = re.compile(
    r"(?:[;\n]+|[.!?]+|,\s*(?:and|but|or|yet|и|но|а|или|однако)\s+|\s+(?:but|yet|но|однако)\s+)",
    re.IGNORECASE,
)
_COORDINATING_CONJUNCTION = re.compile(
    r"\b(?:and|but|or|yet|и(?!\s+(?:тот|то|та|те)\s+же\b)|но|а|или|однако)\b",
    re.IGNORECASE,
)
_NOMINAL_COORDINATION_ALLOWLIST = (
    re.compile(
        r"(?:в\s+)?рабоч\w*\s+(?:папк\w*|каталог\w*)\s+и\s+"
        r"(?:в\s+)?(?:папк\w*|каталог\w*)\s+уровн\w*\s+выше",
        re.IGNORECASE,
    ),
    re.compile(
        r"working\s+(?:directory|folder)\s+and\s+(?:its\s+)?parent(?:\s+(?:directory|folder))?",
        re.IGNORECASE,
    ),
)
_RELATION_STOPWORDS = _STOPWORDS | frozenset({
    "before", "after", "because", "therefore", "thus", "caused", "cause", "happened",
    "до", "после", "затем", "потому", "что", "поэтому", "так", "как", "привел", "привела",
    "привело", "вызвал", "вызвала", "вызвало",
})
_SEQUENCE_SPLIT = (
    re.compile(r"\bfirst\b(?P<origin>.+?)\bthen\b(?P<result>.+)", re.IGNORECASE),
    re.compile(r"\bсначала\b(?P<origin>.+?)\bзатем\b(?P<result>.+)", re.IGNORECASE),
)


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _required_anchors_by_fact(source: SourceUnit) -> dict[str, tuple[str, ...]]:
    if source.evidence_mode == "generic":
        return {
            item.fact_id: tuple(f"evidence:{anchor}" for anchor in item.meaning_anchor_ids)
            for item in source.verified_fact_bindings
        }
    result: dict[str, tuple[str, ...]] = {}
    for fact in source.facts:
        known = [
            f"semantic:{fact.fact_id}:{rule.anchor_id}"
            for rule in _SEMANTIC_ANCHOR_RULES
            if re.search(rule.source_pattern, fact.exact_text, re.IGNORECASE)
        ]
        if not known:
            tokens = [
                token.casefold()
                for token in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", fact.exact_text)
                if token.casefold() not in _STOPWORDS
            ]
            known = [f"lexical:{fact.fact_id}:{token}" for token in _ordered_unique(tokens)]
        known.extend(f"number:{fact.fact_id}:{item}" for item in _extract_numbers(fact.exact_text))
        known.extend(f"date:{fact.fact_id}:{item}" for item in _extract_dates(fact.exact_text))
        known.extend(f"entity:{fact.fact_id}:{item}" for item in _extract_entities(fact.exact_text))
        for name, pattern in _SENSITIVE_CLAIM_CATEGORIES:
            for positive in sorted(_category_polarities(fact.exact_text, pattern)):
                known.append(f"category:{fact.fact_id}:{name}:{'positive' if positive else 'negative'}")
        temporal = _relation(fact.exact_text, _TEMPORAL_PATTERNS)
        causal = _relation(fact.exact_text, _CAUSAL_PATTERNS)
        if temporal is not None:
            known.append(f"relation:{fact.fact_id}:temporal:{temporal}")
        if causal is not None:
            known.append(f"relation:{fact.fact_id}:causal:{causal}")
        result[fact.fact_id] = tuple(known)
    return result


def required_source_anchors(source: SourceUnit) -> tuple[str, ...]:
    by_fact = _required_anchors_by_fact(source)
    return _ordered_unique(anchor for fact in source.facts for anchor in by_fact[fact.fact_id])


def _closed_source_rule(fact: SourceFact) -> _ClosedSourceFactRule | None:
    semantic_ids = frozenset(
        rule.anchor_id
        for rule in _SEMANTIC_ANCHOR_RULES
        if re.search(rule.source_pattern, fact.exact_text, re.IGNORECASE)
    )
    matches = tuple(
        rule
        for rule in _CLOSED_SOURCE_FACT_RULES
        if (
        rule.source_pattern.fullmatch(fact.exact_text.strip()) is not None
        and rule.required_semantic_ids.issubset(semantic_ids)
        )
    )
    return matches[0] if len(matches) == 1 else None


def _fact_semantically_closed(fact: SourceFact) -> bool:
    return _closed_source_rule(fact) is not None


def _source_semantically_closed(source: SourceUnit) -> bool:
    return bool(source.facts) and all(_fact_semantically_closed(fact) for fact in source.facts)


def _rendered_projection_supported(facts: tuple[SourceFact, ...], text: str) -> bool:
    rules = tuple(_closed_source_rule(fact) for fact in facts)
    if any(rule is None for rule in rules):
        return False
    rule_ids = frozenset(rule.rule_id for rule in rules if rule is not None)
    return any(
        projection.source_rule_ids == rule_ids
        and projection.rendered_pattern.fullmatch(text.strip()) is not None
        for projection in _RENDERED_FACT_PROJECTION_RULES
    )


def _semantic_rendering_rejected(anchor_id: str, text: str) -> bool:
    return _SEMANTIC_ASSERTION_NEGATION.search(text.replace("’", "'")) is not None or any(
        rule.anchor_id == anchor_id and rule.rendered_pattern.search(text) is not None
        for rule in _SEMANTIC_RENDERED_REJECTIONS
    )


def _anchor_rendered(anchor: str, text: str) -> bool:
    semantic_id = anchor
    if anchor.startswith("semantic:"):
        parts = anchor.split(":", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            return False
        semantic_id = parts[2]
    for rule in _SEMANTIC_ANCHOR_RULES:
        if rule.anchor_id == semantic_id:
            return (
                not _semantic_rendering_rejected(rule.anchor_id, text)
                and re.search(rule.rendered_pattern, text, re.IGNORECASE) is not None
            )
    if anchor.startswith("lexical:"):
        token = anchor.rsplit(":", 1)[-1]
        return re.search(rf"(?<!\w){re.escape(token)}(?!\w)", text, re.IGNORECASE) is not None
    if anchor.startswith("number:"):
        return anchor.split(":", 2)[-1] in _extract_numbers(text)
    if anchor.startswith("date:"):
        return anchor.split(":", 2)[-1] in _extract_dates(text)
    if anchor.startswith("entity:"):
        value = anchor.split(":", 2)[-1].casefold()
        return value in {item.casefold() for item in _extract_entities(text)}
    if anchor.startswith("category:"):
        parts = anchor.split(":", 3)
        if len(parts) != 4:
            return False
        name, polarity = parts[2], parts[3]
        pattern = dict(_SENSITIVE_CLAIM_CATEGORIES).get(name)
        expected = polarity == "positive"
        return pattern is not None and expected in _category_polarities(text, pattern)
    if anchor.startswith("relation:"):
        parts = anchor.split(":", 3)
        if len(parts) != 4:
            return False
        kind, relation = parts[2], parts[3]
        rules = _TEMPORAL_PATTERNS if kind == "temporal" else _CAUSAL_PATTERNS if kind == "causal" else ()
        return bool(rules) and _relation(text, rules) == relation
    return False


def _extract_numbers(text: str) -> tuple[str, ...]:
    return _ordered_unique(
        match.group(0).casefold().replace(" ", "").replace("−", "-")
        for match in _NUMBER.finditer(text)
    )


def _extract_dates(text: str) -> tuple[str, ...]:
    values = list(_DATE_TOKEN.findall(text))
    for match in _TEXTUAL_DATE.finditer(text):
        day, month_name, year = match.groups()
        month = _MONTH_NAMES.get(month_name.casefold())
        if month is not None and 1 <= int(day) <= 31:
            values.append(f"{int(year):04d}-{month:02d}-{int(day):02d}")
    return _ordered_unique(values)


def _extract_entities(text: str) -> tuple[str, ...]:
    values = [match.group(0).strip() for match in _ENTITY.finditer(text)]
    values.extend(match.group(0) for match in _SINGLE_LATIN_ENTITY.finditer(text) if match.group(0) not in _ENTITY_STOPWORDS)
    values.extend(match.group(0) for match in _ACRONYM_ENTITY.finditer(text))
    values.extend(
        match.group(1)
        for match in _LEADING_LOWER_ENTITY.finditer(text)
        if match.group(1).casefold() not in _LOWER_ENTITY_STOPWORDS
    )
    values.extend(
        value
        for match in _CONTEXTUAL_LOWER_ENTITY.finditer(text)
        for value in match.groups()
        if value is not None and value.casefold() not in _LOWER_ENTITY_STOPWORDS
    )
    return _ordered_unique(values)


def _relation(text: str, rules: Sequence[tuple[str, re.Pattern[str]]]) -> str | None:
    for name, pattern in rules:
        if pattern.search(text):
            return name
    return None


def _statement_count(text: str) -> int:
    return sum(1 for item in _SENTENCE.findall(text) if item.strip())


def _semantic_clauses(text: str) -> tuple[str, ...]:
    return tuple(item.strip(" \t\r\n,.;:!?—-") for item in _CLAUSE_BOUNDARY.split(text) if item.strip(" \t\r\n,.;:!?—-"))


def _contains_multiple_atomic_propositions(text: str) -> bool:
    """Reject coordinated independent predicates inside one public claim.

    Punctuation alone is not a proposition boundary: a model can append an
    unsupported event with ``and``/``и`` and repeat a grounded anchor in that
    event.  Every coordination therefore fails closed except a small
    code-owned nominal allowlist (the two directory objects) and fixed
    same-object idioms excluded by the conjunction pattern itself.
    """
    allowlisted_spans = tuple(
        (match.start(), match.end())
        for pattern in _NOMINAL_COORDINATION_ALLOWLIST
        for match in pattern.finditer(text)
    )
    for match in _COORDINATING_CONJUNCTION.finditer(text):
        if not any(start <= match.start() and match.end() <= end for start, end in allowlisted_spans):
            return True
    return False


def _category_polarity_sequence(text: str, pattern: re.Pattern[str]) -> tuple[bool, ...]:
    values: list[bool] = []
    for clause in _semantic_clauses(text):
        normalized_clause = _NEGATIVE_CONTRACTION.sub("not", clause.replace("’", "'"))
        for match in pattern.finditer(normalized_clause):
            prefix = normalized_clause[:match.start()]
            suffix = normalized_clause[match.end():]
            directly_before = re.search(
                r"\b(?:no\s+one|no|not|never|without|none|не|нет|никогда|без|"
                r"ни(?:\s+(?:один|одного|единого))?)\b"
                r"(?:\s+[A-Za-zА-Яа-яЁё-]+){0,4}\s*$",
                prefix,
                re.IGNORECASE,
            )
            predicate_negation = re.match(
                r"\s+(?:(?:did|does|do|was|were|is|are|has|have|had)\s+)?(?:not|never|не|никогда)\b",
                suffix,
                re.IGNORECASE,
            )
            values.append(directly_before is None and predicate_negation is None)
    return tuple(values)


def _category_polarities(text: str, pattern: re.Pattern[str]) -> frozenset[bool]:
    return frozenset(_category_polarity_sequence(text, pattern))


def _ordered_polarity_subset(values: tuple[bool, ...], reference: tuple[bool, ...]) -> bool:
    cursor = 0
    for value in values:
        try:
            cursor = reference.index(value, cursor) + 1
        except ValueError:
            return False
    return True


def _semantic_anchor_counts(anchors: Iterable[str], text: str) -> Counter[str]:
    required = Counter(
        anchor.split(":", 2)[2]
        for anchor in anchors
        if anchor.startswith("semantic:") and len(anchor.split(":", 2)) == 3
    )
    rendered: Counter[str] = Counter()
    for rule in _SEMANTIC_ANCHOR_RULES:
        if rule.anchor_id in required:
            rendered[rule.anchor_id] = 0 if _semantic_rendering_rejected(rule.anchor_id, text) else sum(
                1 for _ in re.finditer(rule.rendered_pattern, text, re.IGNORECASE)
            )
    return Counter({key: min(required[key], rendered[key]) for key in required})


def _relation_operand_sides(
    text: str,
    rules: Sequence[tuple[str, re.Pattern[str]]],
    relation: str,
) -> dict[str, str]:
    pattern = dict(rules).get(relation)
    match = None if pattern is None else pattern.search(text)
    if match is None:
        return {}

    def tokens(value: str) -> tuple[str, ...]:
        return _ordered_unique(
            token.casefold()
            for token in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", value)
            if token.casefold() not in _RELATION_STOPWORDS
        )

    result = {token: "left" for token in tokens(text[:match.start()])}
    for token in tokens(text[match.end():]):
        result[token] = "right" if token not in result else "both"
    return result


def _semantic_operand_positions(
    text: str,
    semantic_ids: frozenset[str],
    *,
    source_patterns: bool,
) -> tuple[tuple[str, int, int], ...]:
    positions: list[tuple[str, int, int]] = []
    operand_rules = {item.anchor_id: item for item in _SEMANTIC_OPERAND_RULES}
    for rule in _SEMANTIC_ANCHOR_RULES:
        if rule.anchor_id not in semantic_ids:
            continue
        operand_rule = operand_rules.get(rule.anchor_id)
        pattern = (
            operand_rule.source_pattern if source_patterns else operand_rule.rendered_pattern
        ) if operand_rule is not None else (
            rule.source_pattern if source_patterns else rule.rendered_pattern
        )
        matches = tuple(re.finditer(pattern, text, re.IGNORECASE))
        if len(matches) != 1:
            return ()
        positions.append((rule.anchor_id, matches[0].start(), matches[0].end()))
    return tuple(sorted(positions, key=lambda item: (item[1], item[2], item[0])))


def _canonical_semantic_relation_signature(
    text: str,
    rules: Sequence[tuple[str, re.Pattern[str]]],
    relation: str,
    semantic_ids: frozenset[str],
    *,
    source_patterns: bool,
) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Return direction-aware semantic operands for a binary relation.

    The tuple is canonical rather than surface ordered: temporal relations are
    represented as ``(earlier, later)`` and causal relations as
    ``(cause, effect)``.  Prefix and infix forms therefore compare across
    languages without treating mere concept presence as proof of direction.
    """
    pattern = dict(rules).get(relation)
    relation_match = None if pattern is None else pattern.search(text)
    operands = _semantic_operand_positions(
        text,
        semantic_ids,
        source_patterns=source_patterns,
    )
    if relation_match is None or len(operands) != 2:
        return None
    pre = tuple(item[0] for item in operands if item[2] <= relation_match.start())
    # A semantic span such as ``before any conclusion`` includes the relation
    # marker.  It belongs to the marker's following/head operand, never to the
    # surface prefix preceding it.
    post = tuple(item[0] for item in operands if item[2] > relation_match.start())
    if pre and post:
        if relation == "before":
            return pre, post
        if relation == "after":
            return post, pre
        if relation == "because":
            return post, pre
        if relation in {"therefore", "caused"}:
            return pre, post
        return None
    if pre or len(post) != 2:
        return None
    first, rest = (post[0],), (post[1],)
    if relation == "after":
        return first, rest
    if relation == "before":
        return rest, first
    if relation == "because":
        return first, rest
    return None


def _sequence_operand_sides(text: str) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    def tokens(value: str) -> tuple[str, ...]:
        return _ordered_unique(
            token.casefold()
            for token in re.findall(r"[A-Za-zА-Яа-яЁё]{3,}", value)
            if token.casefold() not in _RELATION_STOPWORDS
        )

    for pattern in _SEQUENCE_SPLIT:
        match = pattern.search(text)
        if match is not None:
            origin = tokens(match.group("origin"))
            result = tokens(match.group("result"))
            return (origin, result) if origin and result else None
    return None


def _relation_operands_preserved(
    source_text: str,
    rendered_text: str,
    rules: Sequence[tuple[str, re.Pattern[str]]],
    relation: str,
) -> bool:
    if relation == "sequence":
        source_sequence = _sequence_operand_sides(source_text)
        rendered_sequence = _sequence_operand_sides(rendered_text)
        if source_sequence is None or rendered_sequence is None:
            return False
        # This closed same-language projection must preserve every operand
        # token exactly.  A shared-token test lets an unsupported hyphenated
        # suffix (for example ``build-destroy``) ride beside the grounded
        # operand while keeping the same apparent direction.
        return source_sequence == rendered_sequence
    source_sides = _relation_operand_sides(source_text, rules, relation)
    rendered_sides = _relation_operand_sides(rendered_text, rules, relation)
    shared = set(source_sides).intersection(rendered_sides)
    if relation == "same_result":
        return True
    shared_semantics = frozenset({
        rule.anchor_id
        for rule in _SEMANTIC_ANCHOR_RULES
        if re.search(rule.source_pattern, source_text, re.IGNORECASE)
        and re.search(rule.rendered_pattern, rendered_text, re.IGNORECASE)
    })
    if len(shared_semantics) >= 2:
        if len(shared_semantics) != 2:
            return False
        source_signature = _canonical_semantic_relation_signature(
            source_text,
            rules,
            relation,
            shared_semantics,
            source_patterns=True,
        )
        rendered_signature = _canonical_semantic_relation_signature(
            rendered_text,
            rules,
            relation,
            shared_semantics,
            source_patterns=False,
        )
        return source_signature is not None and source_signature == rendered_signature
    return len(shared) >= 2 and all(source_sides[token] == rendered_sides[token] for token in shared)


def _ordered_subset(values: tuple[str, ...], reference: tuple[str, ...]) -> bool:
    positions: list[int] = []
    folded = tuple(item.casefold() for item in reference)
    for value in values:
        try:
            positions.append(folded.index(value.casefold()))
        except ValueError:
            return False
    return positions == sorted(positions) and len(positions) == len(set(positions))


def _claim_kind(text: str, refs: tuple[str, ...], *, is_resolution: bool, source_text: str) -> tuple[str, str]:
    marked = _MARKED_METAPHOR.search(text) is not None
    if marked:
        return "explicitly_marked_metaphor", "marked_metaphor"
    significance = bool(is_resolution and any(
        rule.anchor_id in _SIGNIFICANCE_ANCHORS
        and re.search(rule.source_pattern, source_text, re.IGNORECASE)
        and re.search(rule.rendered_pattern, text, re.IGNORECASE)
        for rule in _SEMANTIC_ANCHOR_RULES
    ))
    if significance:
        return "source_supported_significance", "literal"
    if len(refs) > 1 or _relation(text, _TEMPORAL_PATTERNS) is not None:
        return "fact_sequence", "literal"
    return "fact_paraphrase", "literal"


def build_supported_story_claims(source: SourceUnit, candidate: generation.NarrativeCandidateResult) -> tuple[SupportedStoryClaim, ...]:
    package = candidate.package
    if package is None:
        _raise("narrative_normalizer_generation_failed")
    fact_map = {item.fact_id: item for item in source.facts}
    anchors_by_fact = _required_anchors_by_fact(source) if source.evidence_mode == "deterministic_fast_path" else {
        item.fact_id: tuple(f"evidence:{anchor}" for anchor in item.meaning_anchor_ids)
        for item in source.verified_fact_bindings
    }
    claims: list[SupportedStoryClaim] = []
    for public_name, package_name in _PUBLIC_CLAIM_FIELDS:
        field = getattr(package, package_name)
        refs = tuple(field.source_fact_refs)
        source_text = "\n".join(fact_map[ref].exact_text for ref in refs if ref in fact_map)
        if source.evidence_mode == "generic":
            anchors = _ordered_unique(anchor for ref in refs for anchor in anchors_by_fact.get(ref, ()))
        else:
            anchors = _ordered_unique(
                anchor
                for ref in refs
                for anchor in anchors_by_fact.get(ref, ())
                if _anchor_rendered(anchor, field.text)
            )
        kind, interpretation = _claim_kind(
            field.text,
            refs,
            is_resolution=package_name == "resolution",
            source_text=source_text,
        )
        claims.append(SupportedStoryClaim(
            claim_id=f"claim-{public_name}",
            claim_kind=kind,
            rendered_text=field.text,
            ordered_source_fact_refs=refs,
            semantic_anchors=anchors,
            numbers=_extract_numbers(field.text),
            named_entities=_extract_entities(field.text),
            temporal_relation=_relation(field.text, _TEMPORAL_PATTERNS),
            causal_relation=_relation(field.text, _CAUSAL_PATTERNS),
            interpretation_mode=interpretation,
        ))
    return tuple(claims)


_GENERIC_FUNCTION_WORDS = frozenset({
    "a", "an", "and", "as", "at", "be", "because", "before", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "that", "the", "then", "this", "to", "was", "were", "with",
    "а", "без", "был", "была", "были", "в", "для", "до", "и", "из", "к", "как", "на", "но",
    "о", "от", "по", "после", "при", "с", "так", "то", "у", "что", "это",
})


def _generic_content_tokens(text: str) -> frozenset[str]:
    return frozenset(
        token.casefold()
        for token in re.findall(r"[A-Za-zА-Яа-яЁё0-9][A-Za-zА-Яа-яЁё0-9._-]*", text)
        if token.casefold() not in _GENERIC_FUNCTION_WORDS
    )


def _generic_claim_supported(source: SourceUnit, claim: SupportedStoryClaim) -> bool:
    binding_map = {item.fact_id: item for item in source.verified_fact_bindings}
    refs = claim.ordered_source_fact_refs
    if (
        not refs
        or len(refs) != len(set(refs))
        or any(ref not in binding_map for ref in refs)
        or tuple(binding_map[ref].order for ref in refs) != tuple(sorted(binding_map[ref].order for ref in refs))
        or _statement_count(claim.rendered_text) != 1
        or _is_sensitive(claim.rendered_text)
        or _INTERNAL_IDENTIFIER.search(claim.rendered_text)
    ):
        return False
    bindings = tuple(binding_map[ref] for ref in refs)
    expected_anchors = _ordered_unique(
        f"evidence:{anchor}"
        for binding in bindings
        for anchor in binding.meaning_anchor_ids
    )
    if claim.semantic_anchors != expected_anchors or not expected_anchors:
        return False
    # Open-domain evidence is safe only while the public assertion remains an
    # exact extractive proposition that code can replay against source spans.
    # Token-subset checks alone allow a model to omit the predicate, object, or
    # qualifier while retaining automatically attached anchor identifiers.
    # Cross-language/free paraphrase therefore fails closed until a stronger
    # structured semantic proof exists.
    if len(bindings) != 1 or claim.rendered_text.strip() != bindings[0].public_proposition.strip():
        return False
    allowed_text = "\n".join(
        (binding.public_proposition + " " + " ".join(binding.public_anchor_labels))
        for binding in bindings
    )
    if not _generic_content_tokens(claim.rendered_text).issubset(_generic_content_tokens(allowed_text)):
        return False
    source_numbers = _ordered_unique(item for binding in bindings for item in binding.numbers)
    source_entities = _ordered_unique(item for binding in bindings for item in binding.entities)
    source_dates = _ordered_unique(item for binding in bindings for item in binding.dates)
    if (
        claim.numbers != _extract_numbers(claim.rendered_text)
        or any(item not in source_numbers for item in claim.numbers)
        or claim.named_entities != _extract_entities(claim.rendered_text)
        or any(item not in source_entities for item in claim.named_entities)
        or any(item not in source_dates for item in _extract_dates(claim.rendered_text))
    ):
        return False
    temporal = _relation(claim.rendered_text, _TEMPORAL_PATTERNS)
    causal = _relation(claim.rendered_text, _CAUSAL_PATTERNS)
    if (
        claim.temporal_relation != temporal
        or claim.causal_relation != causal
        or (temporal is not None and temporal not in {item.temporal_relation for item in bindings})
        or (causal is not None and causal not in {item.causal_relation for item in bindings})
    ):
        return False
    negative = bool(re.search(r"(?i)(?<!\w)(?:no|not|never|without|не|нет|никогда|без)(?!\w)", claim.rendered_text))
    if negative and all(item.polarity == "positive" for item in bindings):
        return False
    uncertain = bool(re.search(r"(?i)(?:uncertain|unknown|possibly|maybe|неясн|возможн|вероятн)", claim.rendered_text))
    if uncertain and not any(item.uncertainty for item in bindings):
        return False
    return True


def _claim_supported(source: SourceUnit, claim: SupportedStoryClaim) -> bool:
    if source.evidence_mode == "generic":
        return _generic_claim_supported(source, claim)
    facts = {item.fact_id: item for item in source.facts}
    refs = claim.ordered_source_fact_refs
    if claim.claim_kind == "code_owned_bridge":
        return (
            claim.rendered_text in CODE_OWNED_BRIDGE_TEXTS
            and not refs
            and not claim.semantic_anchors
            and claim.interpretation_mode == "code_owned"
        )
    if (
        not refs
        or len(refs) != len(set(refs))
        or any(ref not in facts for ref in refs)
        or any(not _fact_semantically_closed(facts[ref]) for ref in refs)
    ):
        return False
    referenced_facts = tuple(facts[ref] for ref in refs)
    if not _rendered_projection_supported(referenced_facts, claim.rendered_text):
        return False
    if _statement_count(claim.rendered_text) != 1:
        return False
    orders = tuple(facts[ref].order for ref in refs)
    if orders != tuple(sorted(orders)):
        return False
    source_text = "\n".join(facts[ref].exact_text for ref in refs)
    anchors_by_fact = _required_anchors_by_fact(source)
    expected_anchors = _ordered_unique(
        anchor for ref in refs for anchor in anchors_by_fact[ref] if _anchor_rendered(anchor, claim.rendered_text)
    )
    if claim.semantic_anchors != expected_anchors or (claim.claim_kind != "code_owned_bridge" and not expected_anchors):
        return False
    required_semantics = Counter(
        anchor.split(":", 2)[2]
        for anchor in expected_anchors
        if anchor.startswith("semantic:") and len(anchor.split(":", 2)) == 3
    )
    if _semantic_anchor_counts(expected_anchors, claim.rendered_text) != required_semantics:
        return False
    if any(
        not all(_anchor_rendered(anchor, claim.rendered_text) for anchor in anchors_by_fact[ref])
        for ref in refs
    ):
        return False
    clauses = _semantic_clauses(claim.rendered_text)
    if (
        len(clauses) != 1
        or _contains_multiple_atomic_propositions(claim.rendered_text)
        or not any(_anchor_rendered(anchor, clauses[0]) for anchor in expected_anchors)
    ):
        return False
    source_numbers = _extract_numbers(source_text)
    if claim.numbers != _extract_numbers(claim.rendered_text) or any(number not in source_numbers for number in claim.numbers):
        return False
    source_dates = _extract_dates(source_text)
    claim_dates = _extract_dates(claim.rendered_text)
    if any(date not in source_dates for date in claim_dates):
        return False
    source_entities = _extract_entities(source_text)
    if (
        claim.named_entities != _extract_entities(claim.rendered_text)
        or not _ordered_subset(claim.named_entities, source_entities)
    ):
        return False
    for _, pattern in _SENSITIVE_CLAIM_CATEGORIES:
        claim_polarities = _category_polarity_sequence(claim.rendered_text, pattern)
        if claim_polarities and not _ordered_polarity_subset(
            claim_polarities,
            _category_polarity_sequence(source_text, pattern),
        ):
            return False
    temporal = _relation(claim.rendered_text, _TEMPORAL_PATTERNS)
    causal = _relation(claim.rendered_text, _CAUSAL_PATTERNS)
    if claim.temporal_relation != temporal or claim.causal_relation != causal:
        return False
    if temporal is not None:
        matching = dict(_TEMPORAL_PATTERNS)[temporal]
        if not matching.search(source_text) or not _relation_operands_preserved(
            source_text, claim.rendered_text, _TEMPORAL_PATTERNS, temporal
        ):
            return False
    if causal is not None:
        matching = dict(_CAUSAL_PATTERNS)[causal]
        if not matching.search(source_text) or not _relation_operands_preserved(
            source_text, claim.rendered_text, _CAUSAL_PATTERNS, causal
        ):
            return False
    if (temporal is not None or causal is not None) and (
        not _ordered_subset(claim.named_entities, source_entities)
        or not _ordered_subset(claim.numbers, source_numbers)
        or not _ordered_subset(claim_dates, source_dates)
    ):
        return False
    expected_kind, expected_interpretation = _claim_kind(
        claim.rendered_text,
        refs,
        is_resolution=claim.claim_id == "claim-ending",
        source_text=source_text,
    )
    if claim.claim_kind != expected_kind or claim.interpretation_mode != expected_interpretation:
        return False
    if _LITERAL_METAPHOR.search(claim.rendered_text) and not _LITERAL_METAPHOR.search(source_text) and claim.interpretation_mode != "marked_metaphor":
        return False
    if claim.interpretation_mode == "marked_metaphor":
        stripped = _MARKED_METAPHOR.sub("", claim.rendered_text)
        for _, pattern in _SENSITIVE_CLAIM_CATEGORIES:
            if pattern.search(stripped) and not pattern.search(source_text):
                return False
    return True


def build_factuality_receipt(
    source: SourceUnit,
    claims: tuple[SupportedStoryClaim, ...],
    *,
    candidate_id: str,
    package_digest: str,
    statement_inference_kinds: tuple[str, ...],
    adjudication_evidence_digest: str,
) -> FactualityReceipt:
    expected_ids = tuple(f"claim-{name}" for name, _ in _PUBLIC_CLAIM_FIELDS)
    claim_ids = tuple(item.claim_id for item in claims)
    valid_inference_kinds = (
        type(statement_inference_kinds) is tuple
        and len(statement_inference_kinds) == len(expected_ids)
        and all(item in {"observed", "bounded_interpretation"} for item in statement_inference_kinds)
    )
    valid_evidence_digest = type(adjudication_evidence_digest) is str and _HEX64.fullmatch(adjudication_evidence_digest) is not None
    unsupported = tuple(
        item.claim_id
        for item in claims
        if not _claim_supported(source, item)
    )
    if claim_ids != expected_ids or len(claim_ids) != len(set(claim_ids)):
        unsupported = _ordered_unique((*unsupported, *[item for item in expected_ids if item not in claim_ids], *claim_ids))
    if not valid_inference_kinds:
        unsupported = _ordered_unique((*unsupported, *expected_ids))
    if not valid_evidence_digest:
        unsupported = _ordered_unique((*unsupported, *expected_ids))
    digests = tuple(_sha(asdict(item)) for item in claims)
    identity = source_identity(source.source_ref, source.source_digest, source.receipt.source_contract_version)
    binding = _sha({
        "source_identity": identity,
        "candidate_id": candidate_id,
        "package_digest": package_digest,
        "ordered_claim_ids": claim_ids,
        "claim_digests": digests,
        "statement_inference_kinds": statement_inference_kinds,
        "ordered_source_fact_refs": [list(item.ordered_source_fact_refs) for item in claims],
        "adjudication_contract_version": generation.ADJUDICATION_SCHEMA,
        "cp2_adjudication_evidence_digest": adjudication_evidence_digest,
    })
    return FactualityReceipt(
        FACTUALITY_POLICY_VERSION,
        identity,
        candidate_id,
        package_digest,
        claim_ids,
        digests,
        statement_inference_kinds,
        unsupported,
        len(unsupported),
        binding,
        not unsupported and claim_ids == expected_ids and valid_inference_kinds and valid_evidence_digest,
    )


def build_meaning_preservation_receipt(
    source: SourceUnit,
    claims: tuple[SupportedStoryClaim, ...],
    factuality: FactualityReceipt,
) -> MeaningPreservationReceipt:
    required = required_source_anchors(source)
    claimed = frozenset(anchor for claim in claims for anchor in claim.semantic_anchors)
    covered = tuple(anchor for anchor in required if anchor in claimed)
    omitted = tuple(anchor for anchor in required if anchor not in claimed)
    score = 0 if not required else len(covered) * 100 // len(required)
    significance = (
        "source_supported_significance"
        if any(item.claim_kind == "source_supported_significance" and _claim_supported(source, item) for item in claims)
        else "significance_not_supported"
    )
    distinct = _sha({
        "ordered_claim_texts": [item.rendered_text for item in claims],
        "covered_source_anchors": covered,
    })
    return MeaningPreservationReceipt(
        MEANING_POLICY_VERSION,
        required,
        covered,
        omitted,
        score,
        distinct,
        significance,
        factuality.passed and bool(required) and not omitted,
    )


def _sentence_summary(text: str) -> SentenceLengthSummary:
    counts = [len(re.findall(r"\b[\wА-Яа-яЁё-]+\b", item, re.UNICODE)) for item in _SENTENCE.findall(text)]
    counts = [item for item in counts if item]
    if not counts:
        return SentenceLengthSummary(0, 0, 0, 0, 0)
    return SentenceLengthSummary(
        len(counts), min(counts), max(counts), math.floor(sum(counts) * 100 / len(counts)),
        sum(item > 30 for item in counts),
    )


def _unexplained_jargon_count(text: str) -> int:
    count = 0
    lowered = text.casefold()
    explanation = re.compile(r"^\s*(?:—|-|:|\(|,?\s*(?:это|то есть))\s*[a-zа-яё]", re.IGNORECASE)
    for term in _JARGON:
        for match in re.finditer(rf"(?<![\w-]){re.escape(term)}(?![\w-])", lowered):
            if not explanation.search(lowered[match.end():match.end() + 96]):
                count += 1
    return count


def build_plain_language_receipt(
    *,
    title: str,
    hook: str,
    story: str,
    ending: str,
    factuality_passed: bool = False,
    meaning_preservation_passed: bool = False,
    significance_mode: str = "significance_not_supported",
) -> PlainLanguageReceipt:
    for value in (title, hook, story, ending):
        _plain(value, "story_text")
    text = "\n".join((title, hook, story, ending))
    jargon = _unexplained_jargon_count(text)
    identifiers = len(_INTERNAL_IDENTIFIER.findall(text)) + len(_ABSOLUTE_PATH.findall(text))
    acronyms = len(_ACRONYM.findall(text))
    summary = _sentence_summary(text)
    answered = []
    if hook.strip() and story.strip():
        answered.append("what_happened")
    if significance_mode == "source_supported_significance":
        answered.append("why_it_matters")
    elif significance_mode == "significance_not_supported":
        answered.append("significance_not_supported")
    if ending.strip():
        answered.append("what_changed")
        answered.append("what_can_be_understood")
    if identifiers == 0:
        answered.append("internal_detail_avoided")
    if jargon == 0:
        answered.append("no_unexplained_term")
    passed = (
        "what_happened" in answered
        and ("why_it_matters" in answered or "significance_not_supported" in answered)
        and "what_changed" in answered
        and jargon == 0
        and identifiers == 0
        and acronyms <= 2
        and summary.sentence_count >= 3
        and summary.over_limit_count == 0
        and _CYRILLIC_WORD.search(text) is not None
        and not _TEST_REPORT.search(text)
        and factuality_passed is True
        and meaning_preservation_passed is True
        and significance_mode in SIGNIFICANCE_MODES
    )
    return PlainLanguageReceipt(
        PLAIN_LANGUAGE_POLICY_VERSION,
        jargon,
        acronyms,
        summary,
        identifiers,
        tuple(answered),
        factuality_passed,
        meaning_preservation_passed,
        significance_mode,
        passed,
    )


def _selected(result: generation.NarrativeGenerationResult) -> generation.NarrativeCandidateResult:
    if result.selected_candidate_id is None:
        _raise("narrative_normalizer_generation_failed")
    for candidate in result.candidates:
        if candidate.candidate_id == result.selected_candidate_id and candidate.accepted and candidate.package is not None:
            return candidate
    _raise("narrative_normalizer_generation_failed")


def _adjudication_evidence_payload(evidence: CP2AdjudicationEvidence) -> dict[str, object]:
    payload = asdict(evidence)
    return dict(payload, evidence_digest=_sha(payload))


def _adjudication_is_fully_supported(item: generation.CandidateAdjudication) -> bool:
    decisions = [
        *item.statement_decisions,
        item.primary_continuity,
        item.visual_grounding,
        item.editorial_alignment,
    ]
    if item.secondary_continuity is not None:
        decisions.append(item.secondary_continuity)
    if item.relationship_continuity is not None:
        decisions.append(item.relationship_continuity)
    return (
        item.overall_decision == "supported"
        and not item.reason_codes
        and all(value.decision == "supported" and not value.reason_codes for value in decisions)
    )


def _build_cp2_adjudication_evidence(
    source: SourceUnit,
    normalization_input: NormalizationInput,
    result: generation.NarrativeGenerationResult,
    captured: _CapturedCP2Evidence,
    claims: tuple[SupportedStoryClaim, ...],
    service: generation.NarrativeGenerationService,
) -> CP2AdjudicationEvidence:
    candidate = _selected(result)
    draft_by_id = {item.candidate_id: item for item in captured.drafts}
    adjudication_by_id = {item.candidate_id: item for item in captured.adjudications.candidates}
    if (
        set(draft_by_id) != {item.candidate_id for item in result.candidates}
        or set(adjudication_by_id) != set(draft_by_id)
        or candidate.candidate_id not in draft_by_id
    ):
        _raise("narrative_normalizer_generation_failed")
    draft = draft_by_id[candidate.candidate_id]
    adjudication = adjudication_by_id[candidate.candidate_id]
    package = generation.assemble_human_story_package(draft, normalization_input.generation_input)
    if (
        candidate.package is None
        or candidate.package_digest is None
        or translator.canonical_payload(package) != translator.canonical_payload(candidate.package)
    ):
        _raise("narrative_normalizer_generation_failed")
    authority = generation.build_authority_context_binding(normalization_input.generation_input)
    binding = generation.build_draft_bindings(draft, normalization_input.generation_input, authority)
    if (
        adjudication.authority_context_digest != binding.authority_context_digest
        or adjudication.draft_digest != binding.draft_digest
        or not _adjudication_is_fully_supported(adjudication)
    ):
        _raise("narrative_normalizer_generation_failed")
    validation_context = generation.build_validation_context(
        package,
        normalization_input.generation_input,
        adjudication,
    )
    try:
        validated = translator.validate_human_story_package(package, validation_context)
    except translator.HumanStoryValidationError:
        _raise("narrative_normalizer_generation_failed")
    if validated.package_digest != candidate.package_digest:
        _raise("narrative_normalizer_generation_failed")
    statement_bindings = {item.statement_name: item for item in binding.statement_bindings}
    statement_decisions = {item.statement_name: item for item in adjudication.statement_decisions}
    if set(statement_bindings) != set(generation.STORY_FIELDS) or set(statement_decisions) != set(generation.STORY_FIELDS):
        _raise("narrative_normalizer_generation_failed")
    statement_evidence: list[AdjudicatedStatementEvidence] = []
    for claim, (_, statement_name) in zip(claims, _PUBLIC_CLAIM_FIELDS, strict=True):
        statement = getattr(draft, statement_name)
        decision = statement_decisions[statement_name]
        expected_binding = statement_bindings[statement_name]
        if (
            decision.statement_digest != expected_binding.statement_digest
            or decision.decision != "supported"
            or decision.reason_codes
            or statement.text != claim.rendered_text
            or statement.source_fact_refs != claim.ordered_source_fact_refs
        ):
            _raise("narrative_normalizer_generation_failed")
        statement_evidence.append(AdjudicatedStatementEvidence(
            statement_name=statement_name,
            statement_digest=decision.statement_digest,
            decision=decision.decision,
            reason_codes=decision.reason_codes,
            claim_id=claim.claim_id,
            claim_digest=_sha(asdict(claim)),
            inference_kind=statement.inference_kind,
            ordered_source_fact_refs=statement.source_fact_refs,
        ))
    identity = source_identity(source.source_ref, source.source_digest, source.receipt.source_contract_version)
    return CP2AdjudicationEvidence(
        policy_version=CP2_ADJUDICATION_EVIDENCE_VERSION,
        run_id=result.run_id,
        generation_model=result.generation_model,
        adjudication_model=result.adjudication_model,
        repair_model=service.configured_repair_model,
        model_call_count=result.model_call_count,
        source_identity=identity,
        candidate_id=candidate.candidate_id,
        candidate_rank=candidate.rank,
        package_digest=candidate.package_digest,
        authority_context_digest=authority.authority_context_digest,
        draft_digest=binding.draft_digest,
        candidate_adjudication_digest=_sha(asdict(adjudication)),
        candidate_adjudication=adjudication,
        validation_context=validation_context,
        editorial_context=normalization_input.generation_input.editorial_plan,
        statement_evidence=tuple(statement_evidence),
        overall_decision=adjudication.overall_decision,
        reason_codes=adjudication.reason_codes,
    )


def _assemble_public_story(claims: tuple[SupportedStoryClaim, ...]) -> tuple[str, str, str, str]:
    expected = tuple(f"claim-{name}" for name, _ in _PUBLIC_CLAIM_FIELDS)
    if tuple(item.claim_id for item in claims) != expected:
        _raise("narrative_normalizer_draft_invalid")
    by_id = {item.claim_id: item.rendered_text for item in claims}
    hook = by_id["claim-hook"]
    story = "\n\n".join(by_id[f"claim-story-{index}"] for index in range(1, 4))
    return hook, hook, story, by_id["claim-ending"]


def _human_package_snapshot(package: translator.HumanStoryPackage) -> dict[str, object]:
    payload = json.loads(translator.canonical_payload(package))
    return _exact_mapping(payload, _HUMAN_PACKAGE_KEYS, "narrative_normalizer_draft_invalid")


def _validate_human_package_snapshot(
    value: object,
    source: SourceUnit,
    claims: tuple[SupportedStoryClaim, ...],
) -> tuple[str, tuple[str, ...]]:
    payload = _exact_mapping(value, _HUMAN_PACKAGE_KEYS, "narrative_normalizer_draft_invalid")
    expected_facts = [
        {"fact_id": item.fact_id, "text": item.exact_text}
        for item in source.facts
    ]
    if (
        payload["schema"] != translator.HUMAN_STORY_SCHEMA
        or type(payload["plan_id"]) is not str
        or not payload["plan_id"]
        or payload["source_ref"] != source.source_ref
        or payload["source_facts"] != expected_facts
    ):
        _raise("narrative_normalizer_draft_invalid")
    inference_kinds: list[str] = []
    for claim, (_, package_name) in zip(claims, _PUBLIC_CLAIM_FIELDS, strict=True):
        statement = _exact_mapping(
            payload[package_name],
            _GROUNDED_STATEMENT_KEYS,
            "narrative_normalizer_draft_invalid",
        )
        if (
            statement["text"] != claim.rendered_text
            or statement["source_fact_refs"] != list(claim.ordered_source_fact_refs)
            or statement["inference_kind"] not in {"observed", "bounded_interpretation"}
            or type(statement["editorial_refs"]) is not list
            or type(statement["canon_refs"]) is not list
        ):
            _raise("narrative_normalizer_draft_invalid")
        inference_kinds.append(str(statement["inference_kind"]))
    canonical = translator.canonical_payload(payload).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest(), tuple(inference_kinds)


def _story_payload_without_digest(
    source: SourceUnit,
    candidate: generation.NarrativeCandidateResult,
    claims: tuple[SupportedStoryClaim, ...],
    factuality: FactualityReceipt,
    meaning: MeaningPreservationReceipt,
    receipt: PlainLanguageReceipt,
    adjudication_evidence: CP2AdjudicationEvidence,
) -> dict[str, object]:
    package = candidate.package
    assert package is not None
    title, hook, story, ending = _assemble_public_story(claims)
    identity = source_identity(source.source_ref, source.source_digest, source.receipt.source_contract_version)
    source_document_digest = None if source.source_documents is None else source.source_documents.bundle_digest
    source_evidence_payload = (
        None if source.verified_evidence is None else evidence.verified_bundle_to_payload(source.verified_evidence)
    )
    return {
        "schema_version": DRAFT_SCHEMA_VERSION,
        "source_ref": source.source_ref,
        "source_digest": source.source_digest,
        "source_contract_version": source.receipt.source_contract_version,
        "source_identity": identity,
        "source_facts": [asdict(item) for item in source.facts],
        "human_story_package": _human_package_snapshot(package),
        "claims": [asdict(item) for item in claims],
        "title": title,
        "hook": hook,
        "story": story,
        "ending": ending,
        "primary_character": package.primary_interpretation.character_id,
        "secondary_character": None if package.secondary_interpretation is None else package.secondary_interpretation.character_id,
        "presence_mode": package.duo_context.presence_mode,
        "factuality_receipt": asdict(factuality),
        "meaning_preservation_receipt": asdict(meaning),
        "plain_language_receipt": asdict(receipt),
        "cp2_adjudication_evidence": _adjudication_evidence_payload(adjudication_evidence),
        "evidence_mode": source.evidence_mode,
        "source_document_digest": source_document_digest,
        "source_evidence_bundle": source_evidence_payload,
        "verified_fact_bindings": [
            evidence.verified_fact_binding_to_payload(item)
            for item in source.verified_fact_bindings
        ],
        "generation_contract_version": generation.GENERATION_CONTRACT_VERSION,
        "adjudication_contract_version": generation.ADJUDICATION_SCHEMA,
        "normalization_policy_version": NORMALIZATION_POLICY_VERSION,
        "selected_candidate_id": candidate.candidate_id,
        "human_story_package_digest": candidate.package_digest,
    }


def _package_fact_refs(package: translator.HumanStoryPackage) -> frozenset[str]:
    refs: set[str] = set()
    for name in generation.STORY_FIELDS:
        refs.update(getattr(package, name).source_fact_refs)
    for item in (package.primary_interpretation, package.secondary_interpretation):
        if item is not None:
            refs.update(item.source_fact_refs)
    refs.update(package.duo_context.source_fact_refs)
    refs.update(package.visual_direction.source_fact_refs)
    for item in package.visual_direction.subjects:
        refs.update(item.source_fact_refs)
    return frozenset(refs)


def _artifact_binding_payload(
    story_payload: Mapping[str, object],
    story_markdown: str,
    manifest_core: Mapping[str, object],
) -> dict[str, object]:
    evidence_payload = story_payload.get("source_evidence_bundle")
    fact_bindings = story_payload.get("verified_fact_bindings")
    return {
        "version": "normalizer-artifact-binding-v1",
        "source_identity": story_payload["source_identity"],
        "source_document_digest": story_payload.get("source_document_digest"),
        "evidence_mode": story_payload.get("evidence_mode"),
        "evidence_bundle_digest": None if evidence_payload is None else _sha(evidence_payload),
        "verified_fact_projection_digest": _sha(fact_bindings),
        "human_story_package_digest": story_payload["human_story_package_digest"],
        "cp2_adjudication_evidence_digest": story_payload["cp2_adjudication_evidence"]["evidence_digest"],
        "ordered_public_claims_digest": _sha(story_payload["claims"]),
        "factuality_digest": _sha(story_payload["factuality_receipt"]),
        "meaning_digest": _sha(story_payload["meaning_preservation_receipt"]),
        "plain_language_digest": _sha(story_payload["plain_language_receipt"]),
        "story_json_digest": _sha(story_payload),
        "story_markdown_digest": hashlib.sha256(story_markdown.encode("utf-8")).hexdigest(),
        "draft_manifest_core_digest": _sha(manifest_core),
    }


def _review_trust_payload(review_core: Mapping[str, object], artifact_binding_digest: str) -> dict[str, object]:
    return {
        "version": "normalizer-review-binding-v1",
        "artifact_binding_digest": artifact_binding_digest,
        "review_core_digest": _sha(review_core),
    }


def assemble_draft_artifact(
    source: SourceUnit,
    result: generation.NarrativeGenerationResult,
    *,
    created_at: str,
    model_policy_identity: str,
    normalization_input: NormalizationInput,
    captured_evidence: _CapturedCP2Evidence,
    generation_service: generation.NarrativeGenerationService,
    trust_service: trust.NarrativeTrustService,
    evidence_model_call_count: int = 0,
    supersedes: Mapping[str, object] | None = None,
) -> DraftArtifact:
    if result.model_call_count not in {2, 3} or evidence_model_call_count not in {0, 2}:
        _raise("narrative_normalizer_model_budget_exceeded")
    candidate = _selected(result)
    package = candidate.package
    assert package is not None
    claims = build_supported_story_claims(source, candidate)
    adjudication_evidence = _build_cp2_adjudication_evidence(
        source,
        normalization_input,
        result,
        captured_evidence,
        claims,
        generation_service,
    )
    adjudication_evidence_digest = _adjudication_evidence_payload(adjudication_evidence)["evidence_digest"]
    assert type(adjudication_evidence_digest) is str
    human_package_digest, statement_inference_kinds = _validate_human_package_snapshot(
        _human_package_snapshot(package), source, claims
    )
    if candidate.package_digest != human_package_digest:
        _raise("narrative_normalizer_generation_failed")
    factuality = build_factuality_receipt(
        source,
        claims,
        candidate_id=candidate.candidate_id,
        package_digest=candidate.package_digest,
        statement_inference_kinds=statement_inference_kinds,
        adjudication_evidence_digest=adjudication_evidence_digest,
    )
    meaning = build_meaning_preservation_receipt(source, claims, factuality)
    title, hook, story, ending = _assemble_public_story(claims)
    receipt = build_plain_language_receipt(
        title=title,
        hook=hook,
        story=story,
        ending=ending,
        factuality_passed=factuality.passed,
        meaning_preservation_passed=meaning.passed,
        significance_mode=meaning.significance_mode,
    )
    base = _story_payload_without_digest(
        source,
        candidate,
        claims,
        factuality,
        meaning,
        receipt,
        adjudication_evidence,
    )
    expected_fact_ids = frozenset(item.fact_id for item in source.facts)
    referenced_fact_ids = _package_fact_refs(package)
    coverage_complete = referenced_fact_ids == expected_fact_ids
    package_digest = _sha(base)
    story_payload = dict(base, package_digest=package_digest)
    story_markdown = f"# {base['title']}\n\n{base['hook']}\n\n{base['story']}\n\n{base['ending']}\n"
    review_status = REVIEW_PASSED if receipt.passed and factuality.passed and meaning.passed and coverage_complete else REVIEW_REJECTED
    reason_codes = tuple(sorted({
        *(() if receipt.passed else ("narrative_normalizer_plain_language_invalid",)),
        *(() if factuality.passed else ("narrative_normalizer_factuality_invalid",)),
        *(() if meaning.passed else ("narrative_normalizer_meaning_invalid",)),
        *(() if coverage_complete else ("narrative_normalizer_fact_coverage_invalid",)),
    }))
    identity = source_identity(source.source_ref, source.source_digest, source.receipt.source_contract_version)
    draft_id = draft_identity(identity, package_digest)
    review_payload = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "draft_identity": draft_id,
        "status": review_status,
        "reason_codes": list(reason_codes),
        "fact_coverage": {
            "source_fact_count": len(source.facts),
            "referenced_fact_count": len(referenced_fact_ids),
            "referenced_fact_ids": [item.fact_id for item in source.facts if item.fact_id in referenced_fact_ids],
            "coverage_complete": coverage_complete,
        },
        "factuality_receipt": asdict(factuality),
        "meaning_preservation_receipt": asdict(meaning),
        "plain_language_receipt": asdict(receipt),
        "unsupported_claim_count": factuality.unsupported_claim_count,
        "reviewed_at": created_at,
        "reviewer_version": REVIEWER_VERSION,
        "operator_request_id": None,
        "action_digest": None,
        "supersede_binding": None,
        "trust_receipt": None,
    }
    draft_manifest = {
        "schema_version": DRAFT_MANIFEST_SCHEMA_VERSION,
        "source_ref": source.source_ref,
        "source_digest": source.source_digest,
        "source_identity": identity,
        "draft_identity": draft_id,
        "package_digest": package_digest,
        "status": "draft",
        "created_at": created_at,
        "model_policy_identity": model_policy_identity,
        "contract_versions": {
            "generation": generation.GENERATION_CONTRACT_VERSION,
            "adjudication": generation.ADJUDICATION_SCHEMA,
            "translator": translator.VALIDATION_CONTRACT_VERSION,
            "normalizer": NORMALIZATION_POLICY_VERSION,
            "source": SOURCE_CONTRACT_VERSION,
        },
        "initial_review_state": review_status,
        "idempotency_identity": _sha({
            "version": IDEMPOTENCY_VERSION,
            "source_identity": identity,
            "package_digest": package_digest,
        }),
        "generation_run_id": result.run_id,
        "supersedes": None if supersedes is None else dict(supersedes),
        "evidence_mode": source.evidence_mode,
        "source_document_digest": base["source_document_digest"],
        "evidence_bundle_digest": None if base["source_evidence_bundle"] is None else _sha(base["source_evidence_bundle"]),
        "verified_fact_projection_digest": _sha(base["verified_fact_bindings"]),
    }
    manifest_core = dict(draft_manifest)
    artifact_binding = _artifact_binding_payload(story_payload, story_markdown, manifest_core)
    artifact_binding_digest = _sha(artifact_binding)
    try:
        manifest_receipt = trust_service.sign(trust.TRUST_DOMAIN_DRAFT_REVIEW, artifact_binding)
        draft_manifest["trust_receipt"] = trust.receipt_to_payload(manifest_receipt)
        review_core = dict(review_payload)
        review_core.pop("trust_receipt", None)
        review_receipt = trust_service.sign(
            trust.TRUST_DOMAIN_DRAFT_REVIEW,
            _review_trust_payload(review_core, artifact_binding_digest),
        )
        review_payload["trust_receipt"] = trust.receipt_to_payload(review_receipt)
    except trust.TrustError:
        _raise("narrative_normalizer_trust_invalid")
    return DraftArtifact(
        source.source_ref,
        source.source_digest,
        story_markdown,
        story_payload,
        draft_manifest,
        review_payload,
        package_digest,
        review_status,
        result.model_call_count + evidence_model_call_count,
        artifact_binding_digest,
    )


def _validate_receipt(value: object) -> PlainLanguageReceipt:
    if type(value) is not dict:
        _raise("narrative_normalizer_draft_invalid")
    expected = {
        "policy_version", "unexplained_jargon_count", "acronym_count", "sentence_length_summary",
        "internal_identifier_count", "reader_questions_answered", "factuality_passed",
        "meaning_preservation_passed", "significance_mode", "passed",
    }
    if set(value) != expected:
        _raise("narrative_normalizer_draft_invalid")
    summary_value = value["sentence_length_summary"]
    if type(summary_value) is not dict or set(summary_value) != {
        "sentence_count", "minimum_words", "maximum_words", "average_words_x100", "over_limit_count",
    }:
        _raise("narrative_normalizer_draft_invalid")
    invalid = False
    receipt: PlainLanguageReceipt | None = None
    try:
        summary = SentenceLengthSummary(**summary_value)
        receipt = PlainLanguageReceipt(
            policy_version=value["policy_version"],
            unexplained_jargon_count=value["unexplained_jargon_count"],
            acronym_count=value["acronym_count"],
            sentence_length_summary=summary,
            internal_identifier_count=value["internal_identifier_count"],
            reader_questions_answered=tuple(value["reader_questions_answered"]),
            factuality_passed=value["factuality_passed"],
            meaning_preservation_passed=value["meaning_preservation_passed"],
            significance_mode=value["significance_mode"],
            passed=value["passed"],
        )
    except (TypeError, ValueError):
        invalid = True
    if invalid or receipt is None:
        _raise("narrative_normalizer_draft_invalid")
    if (
        type(receipt.passed) is not bool
        or type(receipt.factuality_passed) is not bool
        or type(receipt.meaning_preservation_passed) is not bool
        or receipt.significance_mode not in SIGNIFICANCE_MODES
    ):
        _raise("narrative_normalizer_draft_invalid")
    return receipt


def _claims_from_payload(value: object) -> tuple[SupportedStoryClaim, ...]:
    if type(value) is not list:
        _raise("narrative_normalizer_draft_invalid")
    claims: list[SupportedStoryClaim] = []
    try:
        for raw in value:
            payload = _exact_mapping(raw, _CLAIM_KEYS, "narrative_normalizer_draft_invalid")
            claims.append(SupportedStoryClaim(
                claim_id=payload["claim_id"],
                claim_kind=payload["claim_kind"],
                rendered_text=payload["rendered_text"],
                ordered_source_fact_refs=tuple(payload["ordered_source_fact_refs"]),
                semantic_anchors=tuple(payload["semantic_anchors"]),
                numbers=tuple(payload["numbers"]),
                named_entities=tuple(payload["named_entities"]),
                temporal_relation=payload["temporal_relation"],
                causal_relation=payload["causal_relation"],
                interpretation_mode=payload["interpretation_mode"],
            ))
    except (TypeError, ValueError):
        _raise("narrative_normalizer_draft_invalid")
    return tuple(claims)


def _factuality_from_payload(value: object) -> FactualityReceipt:
    payload = _exact_mapping(value, _FACTUALITY_KEYS, "narrative_normalizer_draft_invalid")
    try:
        receipt = FactualityReceipt(
            policy_version=payload["policy_version"],
            source_identity=payload["source_identity"],
            candidate_id=payload["candidate_id"],
            package_digest=payload["package_digest"],
            ordered_claim_ids=tuple(payload["ordered_claim_ids"]),
            claim_digests=tuple(payload["claim_digests"]),
            statement_inference_kinds=tuple(payload["statement_inference_kinds"]),
            unsupported_claim_ids=tuple(payload["unsupported_claim_ids"]),
            unsupported_claim_count=payload["unsupported_claim_count"],
            adjudication_binding_digest=payload["adjudication_binding_digest"],
            passed=payload["passed"],
        )
    except (TypeError, ValueError):
        _raise("narrative_normalizer_draft_invalid")
    if (
        receipt.policy_version != FACTUALITY_POLICY_VERSION
        or _HEX64.fullmatch(receipt.source_identity) is None
        or not receipt.candidate_id
        or _HEX64.fullmatch(receipt.package_digest) is None
        or any(_HEX64.fullmatch(item) is None for item in (*receipt.claim_digests, receipt.adjudication_binding_digest))
        or len(receipt.statement_inference_kinds) != len(_PUBLIC_CLAIM_FIELDS)
        or any(item not in {"observed", "bounded_interpretation"} for item in receipt.statement_inference_kinds)
        or type(receipt.unsupported_claim_count) is not int
        or type(receipt.passed) is not bool
    ):
        _raise("narrative_normalizer_draft_invalid")
    return receipt


def _typed_payload(value: object, cls: type) -> dict[str, object]:
    """Strictly select one explicitly requested frozen dataclass payload."""
    return _exact_mapping(
        value,
        frozenset(cls.__dataclass_fields__),
        "narrative_normalizer_draft_invalid",
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str for item in value):
        _raise("narrative_normalizer_draft_invalid")
    return tuple(value)


def _contract_source_fact(value: object) -> translator.SourceFact:
    return translator.SourceFact(**_typed_payload(value, translator.SourceFact))


def _grounded_statement(value: object) -> translator.GroundedStatement:
    payload = dict(_typed_payload(value, translator.GroundedStatement))
    for name in ("source_fact_refs", "editorial_refs", "canon_refs"):
        payload[name] = _string_tuple(payload[name])
    return translator.GroundedStatement(**payload)


def _character_interpretation(value: object) -> translator.CharacterInterpretation:
    payload = dict(_typed_payload(value, translator.CharacterInterpretation))
    for name in ("source_fact_refs", "canon_refs", "continuity_basis"):
        payload[name] = _string_tuple(payload[name])
    return translator.CharacterInterpretation(**payload)


def _character_state(value: object) -> translator.CharacterStateSnapshot:
    payload = dict(_typed_payload(value, translator.CharacterStateSnapshot))
    payload["recent_events"] = _string_tuple(payload["recent_events"])
    return translator.CharacterStateSnapshot(**payload)


def _canon_ref(value: object) -> translator.CanonSourceRef:
    return translator.CanonSourceRef(**_typed_payload(value, translator.CanonSourceRef))


def _character_canon(value: object) -> translator.CharacterCanonSnapshot:
    payload = dict(_typed_payload(value, translator.CharacterCanonSnapshot))
    refs = payload["canon_refs"]
    if type(refs) is not list:
        _raise("narrative_normalizer_draft_invalid")
    payload["canon_refs"] = tuple(_canon_ref(item) for item in refs)
    payload["conflict_reason_codes"] = _string_tuple(payload["conflict_reason_codes"])
    return translator.CharacterCanonSnapshot(**payload)


def _relationship_state(value: object) -> translator.RelationshipStateSnapshot:
    payload = dict(_typed_payload(value, translator.RelationshipStateSnapshot))
    for name in ("unresolved_topics", "inside_jokes", "changed_minds"):
        payload[name] = _string_tuple(payload[name])
    return translator.RelationshipStateSnapshot(**payload)


def _duo_context(value: object) -> translator.DuoNarrativeContext:
    payload = dict(_typed_payload(value, translator.DuoNarrativeContext))
    payload["source_fact_refs"] = _string_tuple(payload["source_fact_refs"])
    return translator.DuoNarrativeContext(**payload)


def _visual_subject(value: object) -> translator.VisualSubjectRef:
    payload = dict(_typed_payload(value, translator.VisualSubjectRef))
    payload["source_fact_refs"] = _string_tuple(payload["source_fact_refs"])
    payload["identity_canon_refs"] = _string_tuple(payload["identity_canon_refs"])
    return translator.VisualSubjectRef(**payload)


def _visual_direction(value: object) -> translator.VisualDirection:
    payload = dict(_typed_payload(value, translator.VisualDirection))
    for name in ("approved_motifs", "excluded_motifs", "source_fact_refs", "visual_canon_refs"):
        payload[name] = _string_tuple(payload[name])
    subjects = payload["subjects"]
    if type(subjects) is not list:
        _raise("narrative_normalizer_draft_invalid")
    payload["subjects"] = tuple(_visual_subject(item) for item in subjects)
    return translator.VisualDirection(**payload)


def _confidence(value: object) -> translator.ConfidenceAssessment:
    payload = dict(_typed_payload(value, translator.ConfidenceAssessment))
    payload["reason_codes"] = _string_tuple(payload["reason_codes"])
    return translator.ConfidenceAssessment(**payload)


def _human_story_package(value: object) -> translator.HumanStoryPackage:
    payload = dict(_typed_payload(value, translator.HumanStoryPackage))
    source_facts = payload["source_facts"]
    states = payload["character_states"]
    canons = payload["character_canons"]
    if type(source_facts) is not list or type(states) is not list or type(canons) is not list:
        _raise("narrative_normalizer_draft_invalid")
    payload["source_facts"] = tuple(_contract_source_fact(item) for item in source_facts)
    for name in generation.STORY_FIELDS:
        payload[name] = _grounded_statement(payload[name])
    payload["primary_interpretation"] = _character_interpretation(payload["primary_interpretation"])
    payload["secondary_interpretation"] = (
        None
        if payload["secondary_interpretation"] is None
        else _character_interpretation(payload["secondary_interpretation"])
    )
    payload["character_states"] = tuple(_character_state(item) for item in states)
    payload["character_canons"] = tuple(_character_canon(item) for item in canons)
    payload["relationship_state"] = (
        None if payload["relationship_state"] is None else _relationship_state(payload["relationship_state"])
    )
    payload["duo_context"] = _duo_context(payload["duo_context"])
    payload["visual_direction"] = _visual_direction(payload["visual_direction"])
    payload["confidence"] = _confidence(payload["confidence"])
    return translator.HumanStoryPackage(**payload)


def _statement_decision(value: object) -> generation.StatementDecision:
    payload = dict(_typed_payload(value, generation.StatementDecision))
    payload["reason_codes"] = _string_tuple(payload["reason_codes"])
    return generation.StatementDecision(**payload)


def _continuity_decision(value: object) -> generation.ContinuityDecision:
    payload = dict(_typed_payload(value, generation.ContinuityDecision))
    payload["reason_codes"] = _string_tuple(payload["reason_codes"])
    return generation.ContinuityDecision(**payload)


def _relationship_decision(value: object) -> generation.RelationshipDecision:
    payload = dict(_typed_payload(value, generation.RelationshipDecision))
    payload["reason_codes"] = _string_tuple(payload["reason_codes"])
    return generation.RelationshipDecision(**payload)


def _visual_decision(value: object) -> generation.VisualDecision:
    payload = dict(_typed_payload(value, generation.VisualDecision))
    payload["reason_codes"] = _string_tuple(payload["reason_codes"])
    return generation.VisualDecision(**payload)


def _editorial_decision(value: object) -> generation.EditorialAlignmentDecision:
    payload = dict(_typed_payload(value, generation.EditorialAlignmentDecision))
    payload["reason_codes"] = _string_tuple(payload["reason_codes"])
    return generation.EditorialAlignmentDecision(**payload)


def _candidate_adjudication(value: object) -> generation.CandidateAdjudication:
    payload = dict(_typed_payload(value, generation.CandidateAdjudication))
    statements = payload["statement_decisions"]
    if type(statements) is not list:
        _raise("narrative_normalizer_draft_invalid")
    payload["statement_decisions"] = tuple(_statement_decision(item) for item in statements)
    payload["primary_continuity"] = _continuity_decision(payload["primary_continuity"])
    payload["secondary_continuity"] = (
        None if payload["secondary_continuity"] is None else _continuity_decision(payload["secondary_continuity"])
    )
    payload["relationship_continuity"] = (
        None if payload["relationship_continuity"] is None else _relationship_decision(payload["relationship_continuity"])
    )
    payload["visual_grounding"] = _visual_decision(payload["visual_grounding"])
    payload["editorial_alignment"] = _editorial_decision(payload["editorial_alignment"])
    payload["reason_codes"] = _string_tuple(payload["reason_codes"])
    return generation.CandidateAdjudication(**payload)


def _editorial_plan_binding(value: object) -> translator.EditorialPlanBinding:
    return translator.EditorialPlanBinding(**_typed_payload(value, translator.EditorialPlanBinding))


def _narrative_editorial_context(value: object) -> generation.NarrativeEditorialContext:
    payload = dict(_typed_payload(value, generation.NarrativeEditorialContext))
    payload["editorial_ref_ids"] = _string_tuple(payload["editorial_ref_ids"])
    return generation.NarrativeEditorialContext(**payload)


def _character_snapshot_authority(value: object) -> translator.CharacterSnapshotAuthority:
    return translator.CharacterSnapshotAuthority(
        **_typed_payload(value, translator.CharacterSnapshotAuthority)
    )


def _relationship_snapshot_authority(value: object) -> translator.RelationshipSnapshotAuthority:
    return translator.RelationshipSnapshotAuthority(
        **_typed_payload(value, translator.RelationshipSnapshotAuthority)
    )


def _semantic_grounding_evidence(value: object) -> translator.SemanticGroundingEvidence:
    payload = dict(_typed_payload(value, translator.SemanticGroundingEvidence))
    payload["source_fact_refs"] = _string_tuple(payload["source_fact_refs"])
    return translator.SemanticGroundingEvidence(**payload)


def _character_continuity_evidence(value: object) -> translator.CharacterContinuityEvidence:
    return translator.CharacterContinuityEvidence(
        **_typed_payload(value, translator.CharacterContinuityEvidence)
    )


def _relationship_continuity_evidence(value: object) -> translator.RelationshipContinuityEvidence:
    payload = dict(_typed_payload(value, translator.RelationshipContinuityEvidence))
    payload["source_fact_refs"] = _string_tuple(payload["source_fact_refs"])
    return translator.RelationshipContinuityEvidence(**payload)


def _visual_grounding_evidence(value: object) -> translator.VisualGroundingEvidence:
    return translator.VisualGroundingEvidence(
        **_typed_payload(value, translator.VisualGroundingEvidence)
    )


def _character_evidence_policy(value: object) -> translator.CharacterEvidencePolicy:
    return translator.CharacterEvidencePolicy(**_typed_payload(value, translator.CharacterEvidencePolicy))


def _authority_policy(value: object) -> translator.EvidenceAuthorityPolicy:
    payload = dict(_typed_payload(value, translator.EvidenceAuthorityPolicy))
    character_policies = payload["character_policies"]
    if type(character_policies) is not list:
        _raise("narrative_normalizer_draft_invalid")
    payload["character_policies"] = tuple(
        _character_evidence_policy(item) for item in character_policies
    )
    return translator.EvidenceAuthorityPolicy(**payload)


def _diversity_signature(value: object) -> translator.NarrativeDiversitySignature:
    return translator.NarrativeDiversitySignature(
        **_typed_payload(value, translator.NarrativeDiversitySignature)
    )


def _diversity_context(value: object) -> translator.NarrativeDiversityContext:
    payload = dict(_typed_payload(value, translator.NarrativeDiversityContext))
    signatures = payload["recent_signatures"]
    if type(signatures) is not list:
        _raise("narrative_normalizer_draft_invalid")
    payload["recent_signatures"] = tuple(_diversity_signature(item) for item in signatures)
    return translator.NarrativeDiversityContext(**payload)


def _validation_context(value: object) -> translator.HumanStoryValidationContext:
    payload = dict(_typed_payload(value, translator.HumanStoryValidationContext))
    facts = payload["expected_source_facts"]
    character_authorities = payload["character_snapshot_authorities"]
    semantic = payload["semantic_grounding_evidence"]
    character_evidence = payload["character_continuity_evidence"]
    if any(type(item) is not list for item in (facts, character_authorities, semantic, character_evidence)):
        _raise("narrative_normalizer_draft_invalid")
    payload["plan"] = _editorial_plan_binding(payload["plan"])
    payload["expected_source_facts"] = tuple(_contract_source_fact(item) for item in facts)
    payload["character_snapshot_authorities"] = tuple(
        _character_snapshot_authority(item) for item in character_authorities
    )
    payload["relationship_snapshot_authority"] = (
        None
        if payload["relationship_snapshot_authority"] is None
        else _relationship_snapshot_authority(payload["relationship_snapshot_authority"])
    )
    payload["semantic_grounding_evidence"] = tuple(
        _semantic_grounding_evidence(item) for item in semantic
    )
    payload["character_continuity_evidence"] = tuple(
        _character_continuity_evidence(item) for item in character_evidence
    )
    payload["relationship_continuity_evidence"] = (
        None
        if payload["relationship_continuity_evidence"] is None
        else _relationship_continuity_evidence(payload["relationship_continuity_evidence"])
    )
    payload["visual_grounding_evidence"] = _visual_grounding_evidence(
        payload["visual_grounding_evidence"]
    )
    payload["authority_policy"] = _authority_policy(payload["authority_policy"])
    payload["diversity_context"] = _diversity_context(payload["diversity_context"])
    return translator.HumanStoryValidationContext(**payload)


def _adjudication_evidence_from_payload(value: object) -> tuple[CP2AdjudicationEvidence, str]:
    payload = _exact_mapping(value, _ADJUDICATION_EVIDENCE_KEYS, "narrative_normalizer_draft_invalid")
    digest = payload["evidence_digest"]
    unsigned = {key: payload[key] for key in _ADJUDICATION_EVIDENCE_KEYS if key != "evidence_digest"}
    if type(digest) is not str or _HEX64.fullmatch(digest) is None or digest != _sha(unsigned):
        _raise("narrative_normalizer_draft_invalid")
    raw_statements = payload["statement_evidence"]
    if type(raw_statements) is not list:
        _raise("narrative_normalizer_draft_invalid")
    statements: list[AdjudicatedStatementEvidence] = []
    try:
        for raw in raw_statements:
            item = _exact_mapping(raw, _ADJUDICATED_STATEMENT_KEYS, "narrative_normalizer_draft_invalid")
            statements.append(AdjudicatedStatementEvidence(
                statement_name=item["statement_name"],
                statement_digest=item["statement_digest"],
                decision=item["decision"],
                reason_codes=tuple(item["reason_codes"]),
                claim_id=item["claim_id"],
                claim_digest=item["claim_digest"],
                inference_kind=item["inference_kind"],
                ordered_source_fact_refs=tuple(item["ordered_source_fact_refs"]),
            ))
        evidence = CP2AdjudicationEvidence(
            policy_version=payload["policy_version"],
            run_id=payload["run_id"],
            generation_model=payload["generation_model"],
            adjudication_model=payload["adjudication_model"],
            repair_model=payload["repair_model"],
            model_call_count=payload["model_call_count"],
            source_identity=payload["source_identity"],
            candidate_id=payload["candidate_id"],
            candidate_rank=payload["candidate_rank"],
            package_digest=payload["package_digest"],
            authority_context_digest=payload["authority_context_digest"],
            draft_digest=payload["draft_digest"],
            candidate_adjudication_digest=payload["candidate_adjudication_digest"],
            candidate_adjudication=_candidate_adjudication(payload["candidate_adjudication"]),
            validation_context=_validation_context(payload["validation_context"]),
            editorial_context=_narrative_editorial_context(payload["editorial_context"]),
            statement_evidence=tuple(statements),
            overall_decision=payload["overall_decision"],
            reason_codes=tuple(payload["reason_codes"]),
        )
    except (TypeError, ValueError):
        _raise("narrative_normalizer_draft_invalid")
    string_fields = (
        evidence.run_id,
        evidence.generation_model,
        evidence.adjudication_model,
        evidence.candidate_id,
    )
    digest_fields = (
        evidence.source_identity,
        evidence.package_digest,
        evidence.authority_context_digest,
        evidence.draft_digest,
        evidence.candidate_adjudication_digest,
    )
    if (
        evidence.policy_version != CP2_ADJUDICATION_EVIDENCE_VERSION
        or any(type(item) is not str or not item for item in string_fields)
        or evidence.repair_model is not None and (type(evidence.repair_model) is not str or not evidence.repair_model)
        or type(evidence.model_call_count) is not int
        or evidence.model_call_count not in {2, 3}
        or type(evidence.candidate_rank) is not int
        or evidence.candidate_rank < 1
        or any(type(item) is not str or _HEX64.fullmatch(item) is None for item in digest_fields)
        or evidence.overall_decision != "supported"
        or evidence.reason_codes
        or len(evidence.statement_evidence) != len(_PUBLIC_CLAIM_FIELDS)
        or type(evidence.candidate_adjudication) is not generation.CandidateAdjudication
        or type(evidence.validation_context) is not translator.HumanStoryValidationContext
        or type(evidence.editorial_context) is not generation.NarrativeEditorialContext
    ):
        _raise("narrative_normalizer_draft_invalid")
    adjudication = evidence.candidate_adjudication
    if (
        _sha(asdict(adjudication)) != evidence.candidate_adjudication_digest
        or adjudication.candidate_id != evidence.candidate_id
        or adjudication.authority_context_digest != evidence.authority_context_digest
        or adjudication.draft_digest != evidence.draft_digest
        or adjudication.overall_decision != evidence.overall_decision
        or adjudication.reason_codes != evidence.reason_codes
        or not _adjudication_is_fully_supported(adjudication)
    ):
        _raise("narrative_normalizer_draft_invalid")
    for item in evidence.statement_evidence:
        if (
            type(item.statement_name) is not str
            or type(item.statement_digest) is not str
            or _HEX64.fullmatch(item.statement_digest) is None
            or item.decision != "supported"
            or item.reason_codes
            or type(item.claim_id) is not str
            or type(item.claim_digest) is not str
            or _HEX64.fullmatch(item.claim_digest) is None
            or item.inference_kind not in {"observed", "bounded_interpretation"}
            or type(item.ordered_source_fact_refs) is not tuple
            or any(type(ref) is not str or not ref for ref in item.ordered_source_fact_refs)
        ):
            _raise("narrative_normalizer_draft_invalid")
    if tuple(
        (item.statement_name, item.statement_digest, item.decision, item.reason_codes)
        for item in adjudication.statement_decisions
    ) != tuple(
        (item.statement_name, item.statement_digest, item.decision, item.reason_codes)
        for item in evidence.statement_evidence
    ):
        _raise("narrative_normalizer_draft_invalid")
    return evidence, digest


def _draft_from_human_package(
    package: translator.HumanStoryPackage,
    evidence: CP2AdjudicationEvidence,
) -> generation.NarrativeDraft:
    def statement(item: translator.GroundedStatement) -> generation.DraftStatement:
        return generation.DraftStatement(
            item.text,
            item.inference_kind,
            item.source_fact_refs,
            item.editorial_refs,
            item.canon_refs,
        )

    def interpretation(
        item: translator.CharacterInterpretation,
    ) -> generation.DraftCharacterInterpretation:
        return generation.DraftCharacterInterpretation(
            character_id=item.character_id,
            text=item.text,
            source_fact_refs=item.source_fact_refs,
            canon_refs=item.canon_refs,
            interpretation_mode=item.interpretation_mode,
            thematic_axis=item.thematic_axis,
            emotional_register=item.emotional_register,
            rhetorical_form=item.rhetorical_form,
            narrative_distance=item.narrative_distance,
            humor_mode=item.humor_mode,
            sarcasm_target=item.sarcasm_target,
            ending_mode=item.ending_mode,
            continuity_basis=item.continuity_basis,
        )

    visual = package.visual_direction
    draft_visual = generation.DraftVisualDirection(
        mode_hint=visual.mode_hint,
        narrative_subject=visual.narrative_subject,
        human_presence_policy=visual.human_presence_policy,
        nonhuman_presence_policy=visual.nonhuman_presence_policy,
        approved_motifs=visual.approved_motifs,
        excluded_motifs=visual.excluded_motifs,
        source_fact_refs=visual.source_fact_refs,
        visual_canon_refs=visual.visual_canon_refs,
        subjects=tuple(
            generation.DraftVisualSubject(
                item.subject_kind,
                item.character_id,
                item.source_fact_refs,
                item.identity_canon_refs,
            )
            for item in visual.subjects
        ),
    )
    secondary = package.secondary_interpretation
    return generation.NarrativeDraft(
        candidate_id=evidence.candidate_id,
        rank=evidence.candidate_rank,
        primary_character_id=package.primary_interpretation.character_id,
        secondary_character_id=None if secondary is None else secondary.character_id,
        presence_mode=package.duo_context.presence_mode,
        hook=statement(package.hook),
        human_problem=statement(package.human_problem),
        tension=statement(package.tension),
        turning_point=statement(package.turning_point),
        resolution=statement(package.resolution),
        primary_interpretation=interpretation(package.primary_interpretation),
        secondary_interpretation=None if secondary is None else interpretation(secondary),
        interaction_mode=package.duo_context.interaction_mode,
        relation_to_story=package.duo_context.relation_to_story,
        visual_direction=draft_visual,
        story_type=package.story_type,
    )


def _recomputed_draft_bindings(
    draft: generation.NarrativeDraft,
    evidence: CP2AdjudicationEvidence,
) -> generation.NarrativeDraftBindings:
    statements = tuple(
        generation.StatementBinding(
            name,
            translator._digest(generation.statement_binding_payload(draft, name)),
        )
        for name in generation.STORY_FIELDS
    )
    primary = translator._digest(
        generation.interpretation_binding_payload(draft, draft.primary_interpretation)
    )
    secondary = (
        None
        if draft.secondary_interpretation is None
        else translator._digest(
            generation.interpretation_binding_payload(draft, draft.secondary_interpretation)
        )
    )
    relationship_payload = generation.relationship_binding_payload(draft, primary, secondary)
    relationship = (
        None if relationship_payload is None else translator._digest(relationship_payload)
    )
    visual = translator._digest(generation.visual_binding_payload(draft))
    plan = evidence.editorial_context
    editorial = translator._digest({
        "binding_version": generation.DRAFT_BINDING_VERSION,
        "candidate_id": draft.candidate_id,
        "plan_id": plan.plan_id,
        "source_ref": plan.source_ref,
        "plan": asdict(plan),
        "candidate_fields": {
            "visual_mode": draft.visual_direction.mode_hint,
            "primary_ending_mode": draft.primary_interpretation.ending_mode,
            "secondary_ending_mode": (
                None
                if draft.secondary_interpretation is None
                else draft.secondary_interpretation.ending_mode
            ),
            "presence_mode": draft.presence_mode,
            "story_type": draft.story_type,
        },
    })
    draft_digest = translator._digest({
        "binding_version": generation.DRAFT_BINDING_VERSION,
        "candidate_id": draft.candidate_id,
        "authority_context_digest": evidence.authority_context_digest,
        "rank": draft.rank,
        "primary_character_id": draft.primary_character_id,
        "secondary_character_id": draft.secondary_character_id,
        "story_type": draft.story_type,
        "statement_bindings": tuple(asdict(item) for item in statements),
        "primary_interpretation_digest": primary,
        "secondary_interpretation_digest": secondary,
        "relationship_payload_digest": relationship,
        "visual_payload_digest": visual,
        "editorial_alignment_digest": editorial,
    })
    return generation.NarrativeDraftBindings(
        draft.candidate_id,
        evidence.authority_context_digest,
        draft_digest,
        statements,
        primary,
        secondary,
        relationship,
        visual,
        editorial,
    )


def _validate_adjudication_evidence_binding(
    evidence: CP2AdjudicationEvidence,
    evidence_digest: str,
    story: Mapping[str, object],
    claims: tuple[SupportedStoryClaim, ...],
) -> None:
    if (
        evidence.source_identity != story["source_identity"]
        or evidence.candidate_id != story["selected_candidate_id"]
        or evidence.package_digest != story["human_story_package_digest"]
    ):
        _raise("narrative_normalizer_draft_invalid")
    package = _exact_mapping(story["human_story_package"], _HUMAN_PACKAGE_KEYS, "narrative_normalizer_draft_invalid")
    try:
        typed_package = _human_story_package(package)
        validated_package = translator.validate_human_story_package(
            typed_package,
            evidence.validation_context,
        )
    except (TypeError, ValueError, translator.HumanStoryValidationError):
        _raise("narrative_normalizer_draft_invalid")
    if (
        validated_package.package_digest != evidence.package_digest
        or translator.canonical_payload(typed_package)
        != translator.canonical_payload(package)
    ):
        _raise("narrative_normalizer_draft_invalid")
    draft = _draft_from_human_package(typed_package, evidence)
    bindings = _recomputed_draft_bindings(draft, evidence)
    adjudication = evidence.candidate_adjudication
    plan = evidence.validation_context.plan
    editorial = evidence.editorial_context
    if (
        plan.plan_id != editorial.plan_id
        or plan.source_ref != editorial.source_ref
        or plan.production_mode != editorial.production_mode
        or plan.content_format != editorial.content_format
        or editorial.plan_id != typed_package.plan_id
        or editorial.source_ref != typed_package.source_ref
        or bindings.draft_digest != evidence.draft_digest
        or adjudication.draft_digest != bindings.draft_digest
        or adjudication.primary_continuity.character_id
        != draft.primary_character_id
        or adjudication.primary_continuity.interpretation_digest
        != bindings.primary_interpretation_digest
        or (
            bindings.secondary_interpretation_digest is None
            and adjudication.secondary_continuity is not None
        )
        or (
            bindings.secondary_interpretation_digest is not None
            and (
                adjudication.secondary_continuity is None
                or adjudication.secondary_continuity.character_id
                != draft.secondary_character_id
                or adjudication.secondary_continuity.interpretation_digest
                != bindings.secondary_interpretation_digest
            )
        )
        or (
            bindings.relationship_payload_digest is None
            and adjudication.relationship_continuity is not None
        )
        or (
            bindings.relationship_payload_digest is not None
            and (
                adjudication.relationship_continuity is None
                or adjudication.relationship_continuity.relationship_payload_digest
                != bindings.relationship_payload_digest
            )
        )
        or adjudication.visual_grounding.visual_payload_digest
        != bindings.visual_payload_digest
        or adjudication.editorial_alignment.editorial_alignment_digest
        != bindings.editorial_alignment_digest
    ):
        _raise("narrative_normalizer_draft_invalid")
    context_character_evidence = {
        item.character_id: item
        for item in evidence.validation_context.character_continuity_evidence
    }
    continuity_decisions = [adjudication.primary_continuity]
    if adjudication.secondary_continuity is not None:
        continuity_decisions.append(adjudication.secondary_continuity)
    if set(context_character_evidence) != {item.character_id for item in continuity_decisions}:
        _raise("narrative_normalizer_draft_invalid")
    if any(
        context_character_evidence[item.character_id].decision != item.decision
        for item in continuity_decisions
    ):
        _raise("narrative_normalizer_draft_invalid")
    context_relationship = evidence.validation_context.relationship_continuity_evidence
    if (
        (context_relationship is None) != (adjudication.relationship_continuity is None)
        or (
            context_relationship is not None
            and adjudication.relationship_continuity is not None
            and context_relationship.decision != adjudication.relationship_continuity.decision
        )
        or evidence.validation_context.visual_grounding_evidence.decision
        != adjudication.visual_grounding.decision
    ):
        _raise("narrative_normalizer_draft_invalid")
    expected_names = tuple(name for _, name in _PUBLIC_CLAIM_FIELDS)
    if tuple(item.statement_name for item in evidence.statement_evidence) != expected_names:
        _raise("narrative_normalizer_draft_invalid")
    for claim, item in zip(claims, evidence.statement_evidence, strict=True):
        statement = _exact_mapping(
            package[item.statement_name],
            _GROUNDED_STATEMENT_KEYS,
            "narrative_normalizer_draft_invalid",
        )
        expected_statement_digest = translator._digest({
            "binding_version": generation.DRAFT_BINDING_VERSION,
            "candidate_id": evidence.candidate_id,
            "statement_name": item.statement_name,
            "text": statement["text"],
            "inference_kind": statement["inference_kind"],
            "source_fact_refs": tuple(statement["source_fact_refs"]),
            "editorial_refs": tuple(statement["editorial_refs"]),
            "canon_refs": tuple(statement["canon_refs"]),
        })
        if (
            item.statement_digest != expected_statement_digest
            or item.claim_id != claim.claim_id
            or item.claim_digest != _sha(asdict(claim))
            or item.inference_kind != statement["inference_kind"]
            or item.ordered_source_fact_refs != tuple(statement["source_fact_refs"])
            or item.ordered_source_fact_refs != claim.ordered_source_fact_refs
        ):
            _raise("narrative_normalizer_draft_invalid")
    factuality_payload = _exact_mapping(
        story["factuality_receipt"],
        _FACTUALITY_KEYS,
        "narrative_normalizer_draft_invalid",
    )
    expected_binding = _sha({
        "source_identity": evidence.source_identity,
        "candidate_id": evidence.candidate_id,
        "package_digest": evidence.package_digest,
        "ordered_claim_ids": tuple(item.claim_id for item in claims),
        "claim_digests": tuple(_sha(asdict(item)) for item in claims),
        "statement_inference_kinds": tuple(item.inference_kind for item in evidence.statement_evidence),
        "ordered_source_fact_refs": [list(item.ordered_source_fact_refs) for item in claims],
        "adjudication_contract_version": generation.ADJUDICATION_SCHEMA,
        "cp2_adjudication_evidence_digest": evidence_digest,
    })
    if factuality_payload["adjudication_binding_digest"] != expected_binding:
        _raise("narrative_normalizer_draft_invalid")


def _meaning_from_payload(value: object) -> MeaningPreservationReceipt:
    payload = _exact_mapping(value, _MEANING_KEYS, "narrative_normalizer_draft_invalid")
    try:
        receipt = MeaningPreservationReceipt(
            policy_version=payload["policy_version"],
            required_source_anchors=tuple(payload["required_source_anchors"]),
            covered_source_anchors=tuple(payload["covered_source_anchors"]),
            omitted_anchors=tuple(payload["omitted_anchors"]),
            source_specificity_score=payload["source_specificity_score"],
            distinct_story_identity=payload["distinct_story_identity"],
            significance_mode=payload["significance_mode"],
            passed=payload["passed"],
        )
    except (TypeError, ValueError):
        _raise("narrative_normalizer_draft_invalid")
    if (
        receipt.policy_version != MEANING_POLICY_VERSION
        or receipt.significance_mode not in SIGNIFICANCE_MODES
        or type(receipt.source_specificity_score) is not int
        or not 0 <= receipt.source_specificity_score <= 100
        or _HEX64.fullmatch(receipt.distinct_story_identity) is None
        or type(receipt.passed) is not bool
    ):
        _raise("narrative_normalizer_draft_invalid")
    return receipt


def _source_from_story_payload(story: Mapping[str, object]) -> SourceUnit:
    facts = tuple(
        SourceFact(
            fact_id=str(raw["fact_id"]),
            exact_text=str(raw["exact_text"]),
            source_ref=str(raw["source_ref"]),
            order=int(raw["order"]),
        )
        for raw in story["source_facts"]
    )
    receipt = FactExtractionReceipt(
        source_contract_version=str(story["source_contract_version"]),
        source_file_count=1,
        fact_count=len(facts),
        duplicate_count=0,
        excluded_sensitive_count=0,
        exact_order_preserved=True,
    )
    mode = story.get("evidence_mode")
    if mode not in {"deterministic_fast_path", "generic"}:
        _raise("narrative_normalizer_draft_invalid")
    try:
        bindings = tuple(
            evidence.verified_fact_binding_from_payload(item)
            for item in story.get("verified_fact_bindings", [])
        )
    except evidence.EvidenceContractError:
        _raise("narrative_normalizer_draft_invalid")
    if mode == "deterministic_fast_path" and bindings:
        _raise("narrative_normalizer_draft_invalid")
    return SourceUnit(
        str(story["source_ref"]),
        str(story["source_digest"]),
        facts,
        receipt,
        None,
        None,
        bindings,
        str(mode),
    )


def _validate_supersedes(value: object, *, current_story: Mapping[str, object], current_draft_identity: str) -> None:
    if value is None:
        return
    payload = _exact_mapping(value, _SUPERSEDES_KEYS, "narrative_normalizer_draft_invalid")
    for name in ("old_source_ref", "new_source_ref"):
        if not _is_safe_source_ref(payload[name]):
            _raise("narrative_normalizer_draft_invalid")
    for name in ("old_source_digest", "old_source_identity", "old_draft_identity", "new_source_digest", "new_source_identity", "new_draft_identity"):
        if type(payload[name]) is not str or _HEX64.fullmatch(payload[name]) is None:
            _raise("narrative_normalizer_draft_invalid")
    if (
        payload["new_source_ref"] != current_story["source_ref"]
        or payload["new_source_digest"] != current_story["source_digest"]
        or payload["new_source_identity"] != current_story["source_identity"]
        or payload["new_draft_identity"] != current_draft_identity
        or payload["old_source_identity"] == payload["new_source_identity"]
        or payload["old_draft_identity"] == payload["new_draft_identity"]
    ):
        _raise("narrative_normalizer_draft_invalid")


@_privacy_boundary("narrative_normalizer_draft_invalid")
def validate_draft_directory(
    path: Path,
    *,
    expected_identity: str | None = None,
    expected_source: SourceUnit | None = None,
    validate_ready: bool = True,
    trust_service: trust.NarrativeTrustService | None = None,
    review_authority_root: Path | None = None,
    require_trust: bool = False,
) -> dict[str, object]:
    """Reread and independently reconstruct one immutable narrative draft."""
    path = Path(path)
    identity_name = path.name if expected_identity is None else expected_identity
    if type(identity_name) is not str or _HEX64.fullmatch(identity_name) is None:
        _raise("narrative_normalizer_draft_invalid")
    if path.is_symlink() or not path.is_dir():
        _raise("narrative_normalizer_draft_invalid")
    allowed = {
        "story.md", "story.json", "draft-manifest.json", "review.json",
        "approval-attestation.json", "narrative_ready.json",
    }
    names = {item.name for item in path.iterdir()}
    required = allowed - {"approval-attestation.json", "narrative_ready.json"}
    if not required.issubset(names) or not names.issubset(allowed):
        _raise("narrative_normalizer_draft_invalid")
    if any(item.is_symlink() or not item.is_file() for item in path.iterdir()):
        _raise("narrative_normalizer_draft_invalid")

    story = _json_read(path / "story.json", _STORY_KEYS, "narrative_normalizer_draft_invalid")
    manifest = _json_read(path / "draft-manifest.json", _DRAFT_MANIFEST_KEYS, "narrative_normalizer_draft_invalid")
    review = _json_read(path / "review.json", _REVIEW_KEYS, "narrative_normalizer_draft_invalid")
    markdown = (path / "story.md").read_text(encoding="utf-8")
    if any(token in markdown for token in ("source_ref", "package_digest", "reason_codes", "contract_version")):
        _raise("narrative_normalizer_draft_invalid")
    if (
        story["schema_version"] != DRAFT_SCHEMA_VERSION
        or manifest["schema_version"] != DRAFT_MANIFEST_SCHEMA_VERSION
        or review["schema_version"] != REVIEW_SCHEMA_VERSION
        or review["status"] not in REVIEW_STATES
    ):
        _raise("narrative_normalizer_draft_invalid")
    if require_trust and type(trust_service) is not trust.NarrativeTrustService:
        _raise("narrative_normalizer_trust_unavailable")
    if not _is_safe_source_ref(story["source_ref"]):
        _raise("narrative_normalizer_draft_invalid")
    if type(story["source_digest"]) is not str or _HEX64.fullmatch(story["source_digest"]) is None:
        _raise("narrative_normalizer_draft_invalid")
    if story["source_contract_version"] != SOURCE_CONTRACT_VERSION:
        _raise("narrative_normalizer_draft_invalid")
    expected_source_identity = source_identity(
        str(story["source_ref"]), str(story["source_digest"]), str(story["source_contract_version"])
    )
    if story["source_identity"] != expected_source_identity or identity_name != expected_source_identity:
        _raise("narrative_normalizer_draft_invalid")
    if story["evidence_mode"] not in {"deterministic_fast_path", "generic"}:
        _raise("narrative_normalizer_draft_invalid")
    if (
        type(story["source_document_digest"]) is not str
        or _HEX64.fullmatch(story["source_document_digest"]) is None
        or type(story["verified_fact_bindings"]) is not list
    ):
        _raise("narrative_normalizer_draft_invalid")
    if story["evidence_mode"] == "deterministic_fast_path":
        if story["source_evidence_bundle"] is not None or story["verified_fact_bindings"]:
            _raise("narrative_normalizer_draft_invalid")
    elif type(story["source_evidence_bundle"]) is not dict:
        _raise("narrative_normalizer_draft_invalid")

    source_facts = story["source_facts"]
    if type(source_facts) is not list or len(source_facts) < MIN_SOURCE_FACTS:
        _raise("narrative_normalizer_draft_invalid")
    exact_texts: set[str] = set()
    for order, raw_fact in enumerate(source_facts, start=1):
        if type(raw_fact) is not dict or frozenset(raw_fact) != _FACT_KEYS:
            _raise("narrative_normalizer_draft_invalid")
        if (
            raw_fact["fact_id"] != f"fact-{order}"
            or type(raw_fact["exact_text"]) is not str
            or not raw_fact["exact_text"].strip()
            or _is_sensitive(raw_fact["exact_text"])
            or raw_fact["source_ref"] != story["source_ref"]
            or raw_fact["order"] != order
            or raw_fact["exact_text"] in exact_texts
        ):
            _raise("narrative_normalizer_draft_invalid")
        exact_texts.add(raw_fact["exact_text"])
    persisted_source = _source_from_story_payload(story)
    if expected_source is not None and (
        expected_source.source_ref != persisted_source.source_ref
        or expected_source.source_digest != persisted_source.source_digest
        or expected_source.facts != persisted_source.facts
        or expected_source.receipt.source_contract_version != persisted_source.receipt.source_contract_version
        or expected_source.evidence_mode != persisted_source.evidence_mode
        or expected_source.verified_fact_bindings != persisted_source.verified_fact_bindings
        or (
            expected_source.source_documents is not None
            and expected_source.source_documents.bundle_digest != story["source_document_digest"]
        )
    ):
        _raise("narrative_normalizer_source_changed")

    claims = _claims_from_payload(story["claims"])
    title, hook, assembled_story, ending = _assemble_public_story(claims)
    if (
        story["title"] != title
        or story["hook"] != hook
        or story["story"] != assembled_story
        or story["ending"] != ending
    ):
        _raise("narrative_normalizer_draft_invalid")
    for field in ("title", "hook", "story", "ending", "selected_candidate_id"):
        if type(story[field]) is not str or not story[field].strip():
            _raise("narrative_normalizer_draft_invalid")
    if (
        story["primary_character"] not in generation.CHARACTER_IDS
        or (story["secondary_character"] is not None and story["secondary_character"] not in generation.CHARACTER_IDS)
        or story["secondary_character"] == story["primary_character"]
        or story["presence_mode"] not in generation.PRESENCE_MODES
        or story["generation_contract_version"] != generation.GENERATION_CONTRACT_VERSION
        or story["adjudication_contract_version"] != generation.ADJUDICATION_SCHEMA
        or story["normalization_policy_version"] != NORMALIZATION_POLICY_VERSION
        or type(story["human_story_package_digest"]) is not str
        or _HEX64.fullmatch(story["human_story_package_digest"]) is None
    ):
        _raise("narrative_normalizer_draft_invalid")

    human_package_digest, statement_inference_kinds = _validate_human_package_snapshot(
        story["human_story_package"], persisted_source, claims
    )
    if story["human_story_package_digest"] != human_package_digest:
        _raise("narrative_normalizer_draft_invalid")
    adjudication_evidence, adjudication_evidence_digest = _adjudication_evidence_from_payload(
        story["cp2_adjudication_evidence"]
    )
    _validate_adjudication_evidence_binding(
        adjudication_evidence,
        adjudication_evidence_digest,
        story,
        claims,
    )
    factuality = _factuality_from_payload(story["factuality_receipt"])
    recomputed_factuality = build_factuality_receipt(
        persisted_source,
        claims,
        candidate_id=str(story["selected_candidate_id"]),
        package_digest=str(story["human_story_package_digest"]),
        statement_inference_kinds=statement_inference_kinds,
        adjudication_evidence_digest=adjudication_evidence_digest,
    )
    meaning = _meaning_from_payload(story["meaning_preservation_receipt"])
    recomputed_meaning = build_meaning_preservation_receipt(persisted_source, claims, recomputed_factuality)
    receipt = _validate_receipt(story["plain_language_receipt"])
    recomputed_receipt = build_plain_language_receipt(
        title=title,
        hook=hook,
        story=assembled_story,
        ending=ending,
        factuality_passed=recomputed_factuality.passed,
        meaning_preservation_passed=recomputed_meaning.passed,
        significance_mode=recomputed_meaning.significance_mode,
    )
    if (
        asdict(factuality) != asdict(recomputed_factuality)
        or asdict(meaning) != asdict(recomputed_meaning)
        or asdict(receipt) != asdict(recomputed_receipt)
    ):
        _raise("narrative_normalizer_draft_invalid")

    base = {key: value for key, value in story.items() if key != "package_digest"}
    if type(story["package_digest"]) is not str or story["package_digest"] != _sha(base):
        _raise("narrative_normalizer_draft_invalid")
    current_draft_identity = draft_identity(expected_source_identity, str(story["package_digest"]))
    if (
        manifest["source_ref"] != story["source_ref"]
        or manifest["source_digest"] != story["source_digest"]
        or manifest["source_identity"] != expected_source_identity
        or manifest["package_digest"] != story["package_digest"]
        or manifest["draft_identity"] != current_draft_identity
        or review["draft_identity"] != current_draft_identity
        or manifest["generation_run_id"] != adjudication_evidence.run_id
        or manifest["model_policy_identity"] != _sha({
            "generation_model": adjudication_evidence.generation_model,
            "adjudication_model": adjudication_evidence.adjudication_model,
            "repair_model": adjudication_evidence.repair_model,
        })[:24]
    ):
        _raise("narrative_normalizer_draft_invalid")

    coverage = review["fact_coverage"]
    if type(coverage) is not dict or frozenset(coverage) != _FACT_COVERAGE_KEYS:
        _raise("narrative_normalizer_draft_invalid")
    expected_fact_ids = [item.fact_id for item in persisted_source.facts]
    referenced_ids = coverage["referenced_fact_ids"]
    actual_referenced = _ordered_unique(ref for claim in claims for ref in claim.ordered_source_fact_refs)
    if (
        type(coverage["source_fact_count"]) is not int
        or coverage["source_fact_count"] != len(expected_fact_ids)
        or type(coverage["referenced_fact_count"]) is not int
        or type(referenced_ids) is not list
        or referenced_ids != [item for item in expected_fact_ids if item in set(actual_referenced)]
        or coverage["referenced_fact_count"] != len(referenced_ids)
        or type(coverage["coverage_complete"]) is not bool
        or coverage["coverage_complete"] != (referenced_ids == expected_fact_ids)
    ):
        _raise("narrative_normalizer_draft_invalid")
    if (
        review["factuality_receipt"] != story["factuality_receipt"]
        or review["meaning_preservation_receipt"] != story["meaning_preservation_receipt"]
        or review["plain_language_receipt"] != story["plain_language_receipt"]
        or type(review["unsupported_claim_count"]) is not int
        or review["unsupported_claim_count"] != recomputed_factuality.unsupported_claim_count
    ):
        _raise("narrative_normalizer_draft_invalid")
    if (
        type(review["reason_codes"]) is not list
        or any(type(item) is not str or item not in REASON_CODES for item in review["reason_codes"])
        or review["reason_codes"] != sorted(set(review["reason_codes"]))
        or not _is_timestamp(review["reviewed_at"])
        or review["reviewer_version"] != REVIEWER_VERSION
        or (
            review["operator_request_id"] is not None
            and (
                type(review["operator_request_id"]) is not str
                or _SAFE_COMPONENT.fullmatch(review["operator_request_id"]) is None
            )
        )
        or (review["action_digest"] is not None and (type(review["action_digest"]) is not str or _HEX64.fullmatch(review["action_digest"]) is None))
    ):
        _raise("narrative_normalizer_draft_invalid")
    if (review["operator_request_id"] is None) != (review["action_digest"] is None):
        _raise("narrative_normalizer_draft_invalid")
    if review["supersede_binding"] is not None:
        binding = _exact_mapping(review["supersede_binding"], _SUPERSEDE_BINDING_KEYS, "narrative_normalizer_draft_invalid")
        for name in ("old_source_ref", "new_source_ref"):
            if not _is_safe_source_ref(binding[name]):
                _raise("narrative_normalizer_draft_invalid")
        for name in ("old_source_digest", "old_source_identity", "old_draft_identity", "new_source_digest", "new_source_identity", "new_draft_identity"):
            if type(binding[name]) is not str or _HEX64.fullmatch(binding[name]) is None:
                _raise("narrative_normalizer_draft_invalid")
        if (
            binding["operator_request_id"] != review["operator_request_id"]
            or binding["old_source_ref"] != story["source_ref"]
            or binding["old_source_digest"] != story["source_digest"]
            or binding["old_source_identity"] != story["source_identity"]
            or binding["old_draft_identity"] != current_draft_identity
            or source_identity(str(binding["new_source_ref"]), str(binding["new_source_digest"])) != binding["new_source_identity"]
        ):
            _raise("narrative_normalizer_draft_invalid")
    if (
        (review["status"] == REVIEW_REJECTED and (not review["reason_codes"] or review["supersede_binding"] is not None))
        or (review["status"] == REVIEW_SUPERSEDED and (review["reason_codes"] or review["supersede_binding"] is None))
    ):
        _raise("narrative_normalizer_draft_invalid")
    if review["operator_request_id"] is not None:
        action = {
            "version": REVIEW_ACTION_VERSION,
            "source_identity": expected_source_identity,
            "draft_identity": current_draft_identity,
            "status": review["status"],
            "reason_codes": tuple(review["reason_codes"]),
            "operator_request_id": review["operator_request_id"],
            "supersede_binding": review["supersede_binding"],
        }
        if review["action_digest"] != _sha(action):
            _raise("narrative_normalizer_draft_invalid")
    computed_initial_status = (
        REVIEW_PASSED
        if recomputed_factuality.passed and recomputed_meaning.passed and recomputed_receipt.passed and coverage["coverage_complete"]
        else REVIEW_REJECTED
    )
    if review["status"] == REVIEW_PASSED and (
        computed_initial_status != REVIEW_PASSED or review["reason_codes"] or review["operator_request_id"] is not None
    ):
        _raise("narrative_normalizer_draft_invalid")

    expected_versions = {
        "generation": generation.GENERATION_CONTRACT_VERSION,
        "adjudication": generation.ADJUDICATION_SCHEMA,
        "translator": translator.VALIDATION_CONTRACT_VERSION,
        "normalizer": NORMALIZATION_POLICY_VERSION,
        "source": SOURCE_CONTRACT_VERSION,
    }
    if (
        manifest["status"] != "draft"
        or manifest["initial_review_state"] != computed_initial_status
        or not _is_timestamp(manifest["created_at"])
        or type(manifest["model_policy_identity"]) is not str
        or re.fullmatch(r"[0-9a-f]{24}", manifest["model_policy_identity"]) is None
        or type(manifest["generation_run_id"]) is not str
        or re.fullmatch(r"[0-9a-f]{24}", manifest["generation_run_id"]) is None
        or manifest["contract_versions"] != expected_versions
        or manifest["idempotency_identity"] != _sha({
            "version": IDEMPOTENCY_VERSION,
            "source_identity": expected_source_identity,
            "package_digest": story["package_digest"],
        })
    ):
        _raise("narrative_normalizer_draft_invalid")
    _validate_supersedes(manifest["supersedes"], current_story=story, current_draft_identity=current_draft_identity)
    expected_markdown = f"# {title}\n\n{hook}\n\n{assembled_story}\n\n{ending}\n"
    if markdown != expected_markdown:
        _raise("narrative_normalizer_draft_invalid")

    manifest_core = dict(manifest)
    manifest_receipt_payload = manifest_core.pop("trust_receipt", None)
    if (
        manifest["evidence_mode"] != story["evidence_mode"]
        or manifest["source_document_digest"] != story["source_document_digest"]
        or manifest["evidence_bundle_digest"]
        != (None if story["source_evidence_bundle"] is None else _sha(story["source_evidence_bundle"]))
        or manifest["verified_fact_projection_digest"] != _sha(story["verified_fact_bindings"])
    ):
        _raise("narrative_normalizer_draft_invalid")
    artifact_binding = _artifact_binding_payload(story, markdown, manifest_core)
    artifact_binding_digest = _sha(artifact_binding)
    review_core = dict(review)
    review_receipt_payload = review_core.pop("trust_receipt", None)
    manifest_receipt = _trust_receipt(manifest_receipt_payload)
    review_receipt = _trust_receipt(review_receipt_payload)
    trust_verified = False
    if trust_service is not None:
        try:
            trust_service.require_valid(trust.TRUST_DOMAIN_DRAFT_REVIEW, artifact_binding, manifest_receipt)
            trust_service.require_valid(
                trust.TRUST_DOMAIN_DRAFT_REVIEW,
                _review_trust_payload(review_core, artifact_binding_digest),
                review_receipt,
            )
            trust_verified = True
        except trust.TrustError:
            _raise("narrative_normalizer_trust_invalid")

    ready: quarantine.NarrativeReadyManifest | None = None
    approval_attestation: review_state.ApprovalAttestation | None = None
    ready_path = path / "narrative_ready.json"
    attestation_path = path / "approval-attestation.json"
    if validate_ready and os.path.lexists(ready_path) != os.path.lexists(attestation_path):
        _raise("narrative_normalizer_draft_invalid")
    if os.path.lexists(ready_path) and validate_ready:
        if review["status"] != REVIEW_PASSED or not recomputed_factuality.passed or not recomputed_meaning.passed:
            _raise("narrative_normalizer_draft_invalid")
        ready_payload = _json_read(ready_path, _READY_KEYS, "narrative_normalizer_draft_invalid")
        try:
            ready = quarantine.NarrativeReadyManifest.from_mapping(ready_payload)
        except quarantine.QuarantineError:
            _raise("narrative_normalizer_draft_invalid")
        if (
            ready.source_ref != story["source_ref"]
            or ready.source_digest != story["source_digest"]
            or ready.narrative_package_ref != f"{expected_source_identity}/story.json"
            or ready.narrative_package_digest != _file_digest(path / "story.json")
            or ready.status != quarantine.CLASS_READY
        ):
            _raise("narrative_normalizer_draft_invalid")
        if type(trust_service) is not trust.NarrativeTrustService:
            _raise("narrative_normalizer_trust_unavailable")
        try:
            approval_attestation = review_state.approval_attestation_from_payload(
                _json_read(
                    attestation_path,
                    frozenset({
                        "schema_version", "source_identity", "source_ref", "source_digest",
                        "draft_identity", "package_digest", "narrative_ready_manifest_digest",
                        "story_markdown_digest", "draft_manifest_digest", "review_digest",
                        "completed_claim_digest", "artifact_binding_digest", "review_revision",
                        "review_event_digest", "approval_request_id", "contract_versions", "key_id",
                        "trust_receipt",
                    }),
                    "narrative_normalizer_draft_invalid",
                ),
                trust_service,
                ready_manifest_contract=quarantine.MANIFEST_SCHEMA_VERSION,
                source_contract=SOURCE_CONTRACT_VERSION,
            )
            if review_authority_root is None:
                _raise("narrative_normalizer_trust_unavailable")
            ledger = review_state.ReviewStateStore(
                review_authority_root,
                trust_service,
            ).read(expected_source_identity, expected_draft_identity=current_draft_identity)
        except review_state.ReviewStateError:
            _raise("narrative_normalizer_draft_invalid")
        claim_path = path.parent / ".normalizer-state" / "claims" / f"{expected_source_identity}.json"
        if (
            approval_attestation.source_identity != expected_source_identity
            or approval_attestation.source_ref != story["source_ref"]
            or approval_attestation.source_digest != story["source_digest"]
            or approval_attestation.draft_identity != current_draft_identity
            or approval_attestation.package_digest != ready.narrative_package_digest
            or approval_attestation.narrative_ready_manifest_digest != _file_digest(ready_path)
            or approval_attestation.story_markdown_digest != _file_digest(path / "story.md")
            or approval_attestation.draft_manifest_digest != _file_digest(path / "draft-manifest.json")
            or approval_attestation.review_digest != _file_digest(path / "review.json")
            or approval_attestation.completed_claim_digest != _file_digest(claim_path)
            or approval_attestation.artifact_binding_digest != artifact_binding_digest
            or ledger.latest.state != review_state.STATE_APPROVED
            or ledger.latest.revision != approval_attestation.review_revision
            or ledger.latest.event_digest != approval_attestation.review_event_digest
            or ledger.latest.operator_request_id != approval_attestation.approval_request_id
        ):
            _raise("narrative_normalizer_draft_invalid")
    return {
        "story": story,
        "manifest": manifest,
        "review": review,
        "markdown": markdown,
        "claims": claims,
        "source": persisted_source,
        "factuality": recomputed_factuality,
        "cp2_adjudication_evidence": adjudication_evidence,
        "cp2_adjudication_evidence_digest": adjudication_evidence_digest,
        "meaning": recomputed_meaning,
        "plain_language": recomputed_receipt,
        "artifact_binding_digest": artifact_binding_digest,
        "trust_verified": trust_verified,
        "ready": ready,
        "approval_attestation": approval_attestation,
    }


def _remove_path_strict(path: Path) -> None:
    """Remove one owned staging artifact and verify the postcondition."""
    if not os.path.lexists(path):
        return
    if path.is_symlink() or path.is_file():
        os.unlink(path)
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        _raise("narrative_normalizer_persistence_invalid")
    if os.path.lexists(path):
        _raise("narrative_normalizer_persistence_invalid")


def _cleanup_owned_path(path: Path) -> None:
    """Verify cleanup even when the primary cleanup primitive is faulty."""
    primary_failed = False
    fallback_failed = False
    cancellation: BaseException | None = None
    try:
        _remove_path_strict(path)
    except BaseException as error:
        primary_failed = True
        if not isinstance(error, Exception):
            cancellation = error
    if os.path.lexists(path):
        primary_failed = True
        try:
            if path.is_symlink() or path.is_file():
                os.unlink(path)
            elif path.is_dir():
                shutil.rmtree(path)
        except BaseException as error:
            fallback_failed = True
            if not isinstance(error, Exception) and cancellation is None:
                cancellation = error
    if os.path.lexists(path):
        _raise("narrative_normalizer_persistence_invalid")
    if cancellation is not None:
        raise cancellation from None
    if primary_failed or fallback_failed:
        _raise("narrative_normalizer_persistence_invalid")


def _write_exclusive_file(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if os.name != "nt":
        os.chmod(path, mode)


class NarrativeOutboxStore:
    """Lazy, source-identity keyed persistence for review-only drafts."""

    def __init__(
        self,
        policy: quarantine.QuarantinePathPolicy,
        *,
        trust_service: trust.NarrativeTrustService | None = None,
        review_authority: ReviewAuthorityTransport | None = None,
        permission_policy: outbox_permissions.NarrativeOutboxPermissionPolicy = (
            outbox_permissions.PRIVATE_POLICY
        ),
    ):
        if type(policy) is not quarantine.QuarantinePathPolicy:
            raise TypeError("policy")
        if type(permission_policy) is not outbox_permissions.NarrativeOutboxPermissionPolicy:
            raise TypeError("permission_policy")
        self.policy = policy
        self.root = policy.narrative_outbox_root
        self._state = self.root / ".normalizer-state"
        self._locks = self._state / "locks"
        self._claims = self._state / "claims"
        self.trust_service = trust_service
        self.review_authority = review_authority
        self.permission_policy = permission_policy

    def _require_trust(self) -> trust.NarrativeTrustService:
        service = _require_trust_service(self.trust_service)
        self.trust_service = service
        return service

    def _review_store(self) -> review_state.ReviewStateStore:
        authority_root = self.policy.narrative_review_authority_root
        if authority_root is None:
            _raise("narrative_normalizer_review_authority_unavailable")
        return review_state.ReviewStateStore(authority_root, self._require_trust())

    @property
    def broker_mode(self) -> bool:
        return self.review_authority is not None

    @staticmethod
    def _review_state_error(error: review_state.ReviewStateError) -> None:
        mapping = {
            review_state.REVIEW_STATE_CONFLICT: "narrative_normalizer_review_identity_conflict",
            review_state.REVIEW_STATE_TRANSITION_INVALID: "narrative_normalizer_review_not_passed",
            review_state.REVIEW_STATE_MISSING: "narrative_normalizer_review_identity_conflict",
            review_state.REVIEW_STATE_PERSISTENCE_INVALID: "narrative_normalizer_persistence_invalid",
        }
        _raise(mapping.get(error.reason_code, "narrative_normalizer_trust_invalid"))

    @staticmethod
    def _broker_error(error: BaseException) -> None:
        reason = getattr(error, "reason_code", "")
        if reason in {
            "review_authority_request_conflict",
            "review_authority_state_conflict",
        }:
            _raise("narrative_normalizer_review_identity_conflict")
        if reason in {
            "review_authority_transition_invalid",
            "review_authority_not_ready",
        }:
            _raise("narrative_normalizer_review_not_passed")
        _raise("narrative_normalizer_review_authority_unavailable")

    def _broker_call(
        self,
        method: str,
        request_id: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        authority = self.review_authority
        if authority is None:
            _raise("narrative_normalizer_review_authority_unavailable")
        try:
            operation = getattr(authority, method)
            result = operation(request_id, payload)
        except (KeyboardInterrupt, SystemExit, GeneratorExit):
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            self._broker_error(error)
        if type(result) is not dict:
            _raise("narrative_normalizer_review_authority_unavailable")
        return result

    @staticmethod
    def _validate_broker_event_result(
        result: object,
        *,
        source_identity_value: str,
        draft_identity_value: str,
        draft_package_digest: str,
        expected_state: str | None = None,
        allow_extra: frozenset[str] = frozenset(),
    ) -> dict[str, object]:
        base = frozenset({
            "source_identity", "draft_identity", "draft_package_digest",
            "revision", "state", "event_digest", "event", "idempotent",
            "storage_adapter_version",
        })
        if type(result) is not dict or frozenset(result) != base | allow_extra:
            _raise("narrative_normalizer_review_authority_unavailable")
        state_value = result.get("state")
        if (
            result.get("source_identity") != source_identity_value
            or result.get("draft_identity") != draft_identity_value
            or result.get("draft_package_digest") != draft_package_digest
            or type(result.get("revision")) is not int
            or int(result["revision"]) < 1
            or type(state_value) is not str
            or state_value not in {
                review_state.STATE_DRAFTED, review_state.STATE_PASSED,
                review_state.STATE_REJECTED, review_state.STATE_SUPERSEDED,
                review_state.STATE_APPROVED,
            }
            or (expected_state is not None and state_value != expected_state)
            or type(result.get("event_digest")) is not str
            or _HEX64.fullmatch(str(result["event_digest"])) is None
            or type(result.get("event")) is not dict
            or type(result.get("idempotent")) is not bool
            or result.get("storage_adapter_version") != BROKER_STORAGE_ADAPTER_VERSION
        ):
            _raise("narrative_normalizer_review_authority_unavailable")
        return result

    def _broker_latest(
        self,
        source_identity_value: str,
        draft_identity_value: str,
        draft_package_digest: str,
    ) -> dict[str, object]:
        result = self._broker_call(
            "latest_state",
            f"latest-{secrets.token_hex(16)}",
            {
                "source_identity": source_identity_value,
                "draft_identity": draft_identity_value,
            },
        )
        return self._validate_broker_event_result(
            result,
            source_identity_value=source_identity_value,
            draft_identity_value=draft_identity_value,
            draft_package_digest=draft_package_digest,
        )

    def _ensure_write_layout(self) -> None:
        if not self.permission_policy.shared:
            for path in (self.root, self._state, self._locks, self._claims):
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                if path.is_symlink() or not path.is_dir():
                    _raise("narrative_normalizer_persistence_invalid")
                if os.name != "nt":
                    os.chmod(path, 0o700)
            return
        for path, mode in (
            (self.root, outbox_permissions.SHARED_ROOT_MODE),
            (self._state, outbox_permissions.SHARED_STATE_MODE),
            (self._locks, outbox_permissions.SHARED_LOCK_DIRECTORY_MODE),
            (self._claims, outbox_permissions.SHARED_CLAIM_DIRECTORY_MODE),
        ):
            path.mkdir(parents=True, exist_ok=True, mode=mode)
            if path.is_symlink() or not path.is_dir():
                _raise("narrative_normalizer_persistence_invalid")
        outbox_permissions.finalize_shared_internal_layout(
            self.permission_policy,
            self.root,
            self._state,
            self._locks,
            self._claims,
        )

    def _verify_draft_permissions(self, path: Path) -> None:
        if self.permission_policy.shared:
            outbox_permissions.verify_shared_draft(
                self.permission_policy,
                self.root,
                path,
            )

    def _finalize_approval_file(self, path: Path) -> None:
        if self.permission_policy.shared:
            outbox_permissions.finalize_shared_approval_file(
                self.permission_policy,
                path,
            )

    def _verify_approval_file(self, path: Path) -> None:
        if self.permission_policy.shared:
            outbox_permissions.verify_shared_approval_file(
                self.permission_policy,
                path,
            )

    def _verify_approval_pair_if_present(self, draft: Path) -> None:
        if not self.permission_policy.shared:
            return
        attestation = draft / "approval-attestation.json"
        ready = draft / "narrative_ready.json"
        if os.path.lexists(attestation) != os.path.lexists(ready):
            _raise("narrative_normalizer_approval_conflict")
        if os.path.lexists(attestation):
            self._verify_approval_file(attestation)
            self._verify_approval_file(ready)

    def _identity(self, source_ref: str, source_digest: str) -> str:
        return source_identity(source_ref, source_digest, SOURCE_CONTRACT_VERSION)

    def _resolve_digest_read(self, source_digest_value: str) -> Path:
        if type(source_digest_value) is not str or _HEX64.fullmatch(source_digest_value) is None:
            _raise("narrative_normalizer_draft_invalid")
        if not self.root.exists():
            return self.root / source_digest_value
        matches: list[Path] = []
        for path in self.root.iterdir():
            if not path.is_dir() or _HEX64.fullmatch(path.name) is None:
                continue
            story_path = path / "story.json"
            if not story_path.is_file():
                continue
            try:
                payload = json.loads(story_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                _raise("narrative_normalizer_draft_invalid")
            if type(payload) is dict and payload.get("source_digest") == source_digest_value:
                matches.append(path)
        if len(matches) > 1:
            _raise("narrative_normalizer_review_identity_conflict")
        return matches[0] if matches else self.root / source_digest_value

    def draft_path(
        self,
        source_digest_value: str,
        *,
        source_ref: str | None = None,
        source_identity_value: str | None = None,
    ) -> Path:
        if source_identity_value is not None:
            if type(source_identity_value) is not str or _HEX64.fullmatch(source_identity_value) is None:
                _raise("narrative_normalizer_draft_invalid")
            return self.root / source_identity_value
        if source_ref is not None:
            return self.root / self._identity(source_ref, source_digest_value)
        return self._resolve_digest_read(source_digest_value)

    def lock_for(self, source_ref: str, source_digest_value: str, *, blocking: bool = False) -> _FileLock:
        identity = self._identity(source_ref, source_digest_value)
        return _FileLock(
            self._locks / f"{identity}.lock",
            blocking=blocking,
            mode=self.permission_policy.lock_file_mode,
            expected_gid=(
                self.permission_policy.shared_gid
                if self.permission_policy.shared
                else None
            ),
        )

    def claim_path(self, source_ref: str, source_digest_value: str) -> Path:
        return self._claims / f"{self._identity(source_ref, source_digest_value)}.json"

    def _validate_claim_payload(
        self,
        value: object,
        source_ref: str,
        source_digest_value: str,
    ) -> dict[str, object]:
        payload = _exact_mapping(value, _CLAIM_RECORD_KEYS, "narrative_normalizer_claim_invalid")
        identity = self._identity(source_ref, source_digest_value)
        if (
            payload["schema_version"] != CLAIM_SCHEMA_VERSION
            or payload["state"] not in CLAIM_STATES
            or payload["source_ref"] != source_ref
            or payload["source_digest"] != source_digest_value
            or payload["source_identity"] != identity
            or payload["source_id"] != hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:12]
            or type(payload["attempt_id"]) is not str
            or re.fullmatch(r"[0-9a-f]{32}", payload["attempt_id"]) is None
            or not _is_timestamp(payload["started_at"])
            or not _is_timestamp(payload["updated_at"])
            or _parse_time(str(payload["updated_at"])) < _parse_time(str(payload["started_at"]))
            or type(payload["reason_code"]) is not str
            or type(payload["ordered_claim_digests"]) is not list
            or any(type(item) is not str or _HEX64.fullmatch(item) is None for item in payload["ordered_claim_digests"])
            or type(payload["key_id"]) is not str
            or re.fullmatch(r"[0-9a-f]{24}", payload["key_id"]) is None
            or type(payload["claim_seal"]) is not str
            or _HEX64.fullmatch(payload["claim_seal"]) is None
        ):
            _raise("narrative_normalizer_claim_invalid")
        sealed_fields = (
            payload["generation_run_id"], payload["package_digest"], payload["draft_identity"], payload["selected_candidate_id"],
            payload["human_story_package_digest"], payload["factuality_binding_digest"],
            payload["adjudication_evidence_digest"],
        )
        if payload["state"] == CLAIM_COMPLETED:
            if (
                any(type(item) is not str or not item for item in sealed_fields)
                or re.fullmatch(r"[0-9a-f]{24}", str(payload["generation_run_id"])) is None
                or _SAFE_COMPONENT.fullmatch(str(payload["selected_candidate_id"])) is None
                or any(_HEX64.fullmatch(str(item)) is None for item in (
                    payload["package_digest"], payload["draft_identity"], payload["human_story_package_digest"],
                    payload["factuality_binding_digest"], payload["adjudication_evidence_digest"],
                ))
                or len(payload["ordered_claim_digests"]) != len(_PUBLIC_CLAIM_FIELDS)
                or type(payload["artifact_binding_digest"]) is not str
                or _HEX64.fullmatch(payload["artifact_binding_digest"]) is None
                or payload["reason_code"] != ""
            ):
                _raise("narrative_normalizer_claim_invalid")
        elif (
            any(item != "" for item in sealed_fields)
            or payload["ordered_claim_digests"]
            or payload["artifact_binding_digest"] != ""
            or (
                payload["state"] == CLAIM_PROCESSING
                and payload["reason_code"] != ""
            )
            or (
                payload["state"] == CLAIM_UNCERTAIN
                and payload["reason_code"] != "narrative_normalizer_claim_uncertain"
            )
            or (
                payload["state"] == CLAIM_FAILED
                and payload["reason_code"] not in REASON_CODES
            )
        ):
            _raise("narrative_normalizer_claim_invalid")
        if self.trust_service is not None:
            core = dict(payload)
            seal = core.pop("claim_seal")
            try:
                receipt = trust.TrustReceipt(
                    trust.TRUST_RECEIPT_SCHEMA_VERSION,
                    trust.TRUST_ALGORITHM,
                    str(payload["key_id"]),
                    trust.TRUST_DOMAIN_CLAIM,
                    hashlib.sha256(trust.canonical_payload(core)).hexdigest(),
                    str(seal),
                )
            except trust.TrustError:
                _raise("narrative_normalizer_claim_invalid")
            if not self.trust_service.verify(trust.TRUST_DOMAIN_CLAIM, core, receipt):
                _raise("narrative_normalizer_claim_invalid")
        return payload

    def _seal_claim_payload(self, payload: Mapping[str, object]) -> dict[str, object]:
        service = self._require_trust()
        core = dict(payload)
        core["key_id"] = service.key_id
        core.pop("claim_seal", None)
        try:
            receipt = service.sign(trust.TRUST_DOMAIN_CLAIM, core)
        except trust.TrustError:
            _raise("narrative_normalizer_claim_invalid")
        return dict(core, claim_seal=receipt.seal)

    def read_claim(self, source_ref: str, source_digest_value: str) -> dict[str, object] | None:
        path = self.claim_path(source_ref, source_digest_value)
        if not os.path.lexists(path):
            return None
        payload = _json_read(path, _CLAIM_RECORD_KEYS, "narrative_normalizer_claim_invalid")
        return self._validate_claim_payload(payload, source_ref, source_digest_value)

    def list_claims(self) -> tuple[dict[str, object], ...]:
        """Strictly enumerate the sealed claim ledger without creating it."""
        if not os.path.lexists(self._claims):
            return ()
        if self._claims.is_symlink() or not self._claims.is_dir():
            _raise("narrative_normalizer_claim_invalid")
        claims: list[dict[str, object]] = []
        for path in sorted(self._claims.iterdir(), key=lambda item: item.name):
            match = re.fullmatch(r"([0-9a-f]{64})\.json", path.name)
            if (
                match is None
                or path.is_symlink()
                or not path.is_file()
            ):
                _raise("narrative_normalizer_claim_invalid")
            payload = _json_read(
                path,
                _CLAIM_RECORD_KEYS,
                "narrative_normalizer_claim_invalid",
            )
            source_ref = payload.get("source_ref")
            source_digest_value = payload.get("source_digest")
            if (
                not _is_safe_source_ref(source_ref)
                or type(source_digest_value) is not str
                or _HEX64.fullmatch(source_digest_value) is None
            ):
                _raise("narrative_normalizer_claim_invalid")
            claim = self._validate_claim_payload(
                payload,
                str(source_ref),
                source_digest_value,
            )
            if claim["source_identity"] != match.group(1):
                _raise("narrative_normalizer_claim_invalid")
            claims.append(claim)
        return tuple(claims)

    def _claim_matches_draft(self, claim: Mapping[str, object], value: Mapping[str, object]) -> bool:
        story = value["story"]
        factuality = value["factuality"]
        manifest = value["manifest"]
        return bool(
            claim["state"] == CLAIM_COMPLETED
            and claim["package_digest"] == story["package_digest"]
            and claim["draft_identity"] == manifest["draft_identity"]
            and claim["generation_run_id"] == manifest["generation_run_id"]
            and claim["selected_candidate_id"] == story["selected_candidate_id"]
            and claim["human_story_package_digest"] == story["human_story_package_digest"]
            and claim["factuality_binding_digest"] == factuality.adjudication_binding_digest
            and claim["adjudication_evidence_digest"]
            == story["cp2_adjudication_evidence"]["evidence_digest"]
            and claim["ordered_claim_digests"] == list(factuality.claim_digests)
            and claim["artifact_binding_digest"] == value["artifact_binding_digest"]
            and claim["key_id"] == _trust_receipt(value["manifest"]["trust_receipt"]).key_id
        )

    def _write_claim_locked(self, payload: Mapping[str, object]) -> bool:
        payload = self._seal_claim_payload(payload)
        source_ref = payload.get("source_ref")
        source_digest_value = payload.get("source_digest")
        if not _is_safe_source_ref(source_ref) or type(source_digest_value) is not str or _HEX64.fullmatch(source_digest_value) is None:
            _raise("narrative_normalizer_claim_invalid")
        if payload.get("source_identity") != self._identity(str(source_ref), source_digest_value):
            _raise("narrative_normalizer_claim_invalid")
        candidate = self._validate_claim_payload(dict(payload), str(source_ref), source_digest_value)
        self._ensure_write_layout()
        path = self.claim_path(str(source_ref), source_digest_value)
        encoded = _canonical(candidate) + b"\n"
        existing = self.read_claim(str(source_ref), source_digest_value)
        if existing is None:
            # A completed/failed/uncertain record is a state transition, not
            # an independently assertable receipt.  Requiring the durable
            # processing predecessor prevents callers from deleting a claim
            # and fabricating a new terminal trust anchor for coherent draft
            # tampering.  Retry transitions remain governed below.
            if candidate["state"] != CLAIM_PROCESSING:
                _raise("narrative_normalizer_claim_invalid")
        else:
            old_bytes = path.read_bytes()
            if old_bytes == encoded:
                return True
            if existing["state"] == CLAIM_COMPLETED:
                _raise("narrative_normalizer_claim_invalid")
            old_attempt = str(existing["attempt_id"])
            new_attempt = str(candidate["attempt_id"])
            old_state = str(existing["state"])
            new_state = str(candidate["state"])
            allowed = (
                (old_state == CLAIM_PROCESSING and new_state in {CLAIM_PROCESSING, CLAIM_UNCERTAIN, CLAIM_FAILED, CLAIM_COMPLETED} and old_attempt == new_attempt)
                or (old_state == CLAIM_UNCERTAIN and new_state == CLAIM_PROCESSING and old_attempt == new_attempt)
                or (old_state == CLAIM_FAILED and new_state == CLAIM_PROCESSING)
            )
            if not allowed:
                _raise("narrative_normalizer_claim_invalid")
        if candidate["state"] == CLAIM_COMPLETED:
            identity = self._identity(str(source_ref), source_digest_value)
            value = validate_draft_directory(
                self.root / identity,
                expected_identity=identity,
                trust_service=self._require_trust(),
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=True,
            )
            if not self._claim_matches_draft(candidate, value):
                _raise("narrative_normalizer_claim_invalid")
        _atomic_write(path, encoded, mode=self.permission_policy.claim_file_mode)
        if self.permission_policy.shared:
            outbox_permissions.finalize_shared_claim(
                self.permission_policy,
                self.root,
                path,
            )
        final = self.read_claim(str(source_ref), source_digest_value)
        if final != candidate:
            _raise("narrative_normalizer_claim_invalid")
        return False

    @_privacy_boundary("narrative_normalizer_claim_invalid")
    def write_claim(self, payload: Mapping[str, object]) -> bool:
        source_ref = payload.get("source_ref")
        source_digest_value = payload.get("source_digest")
        if not _is_safe_source_ref(source_ref) or type(source_digest_value) is not str:
            _raise("narrative_normalizer_claim_invalid")
        with self.lock_for(str(source_ref), source_digest_value, blocking=True):
            sealed_payload = self._seal_claim_payload(payload)
            candidate = self._validate_claim_payload(
                sealed_payload,
                str(source_ref),
                source_digest_value,
            )
            if candidate["state"] == CLAIM_COMPLETED:
                # Completion seals opaque typed CP2 evidence and is owned only
                # by the service's lock-held generation transaction.  The
                # public lifecycle API may replay an already completed record
                # byte-for-byte, but it cannot mint or replace that trust
                # anchor after merely seeding a processing record.
                existing = self.read_claim(str(source_ref), source_digest_value)
                if existing is None or existing != candidate:
                    _raise("narrative_normalizer_claim_invalid")
            return self._write_claim_locked(candidate)

    @_privacy_boundary("narrative_normalizer_persistence_invalid")
    def persist(self, artifact: DraftArtifact) -> tuple[Path, bool]:
        trust_service = self._require_trust()
        source_ref = str(artifact.story_payload["source_ref"])
        identity = str(artifact.story_payload["source_identity"])
        final = self.draft_path(artifact.source_digest, source_ref=source_ref)
        if final.name != identity:
            _raise("narrative_normalizer_persistence_invalid")
        if final.exists():
            self._verify_draft_permissions(final)
            current = validate_draft_directory(
                final, expected_identity=identity, trust_service=trust_service,
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=True
            )
            if (
                current["story"] == artifact.story_payload
                and current["manifest"] == artifact.draft_manifest
                and current["review"] == artifact.review_payload
            ):
                if not self.broker_mode:
                    try:
                        self._review_store().initialize(
                            source_identity=identity,
                            draft_identity=str(artifact.draft_manifest["draft_identity"]),
                            initial_state=str(artifact.review_payload["status"]),
                            reason_codes=tuple(artifact.review_payload["reason_codes"]),
                            drafted_at=str(artifact.draft_manifest["created_at"]),
                            reviewed_at=str(artifact.review_payload["reviewed_at"]),
                        )
                    except review_state.ReviewStateError as error:
                        self._review_state_error(error)
                return final, True
            _raise("narrative_normalizer_persistence_conflict")
        self._ensure_write_layout()
        staging = self.root / f".staging-{identity}-{secrets.token_hex(8)}"
        try:
            staging.mkdir(mode=0o700)
            payloads = {
                "story.md": artifact.story_markdown.encode("utf-8"),
                "story.json": _canonical(artifact.story_payload) + b"\n",
                "draft-manifest.json": _canonical(artifact.draft_manifest) + b"\n",
                "review.json": _canonical(artifact.review_payload) + b"\n",
            }
            for name, payload in payloads.items():
                _write_exclusive_file(staging / name, payload)
            _fsync_directory(staging)
            validate_draft_directory(
                staging, expected_identity=identity, trust_service=trust_service,
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=True
            )
            try:
                staging_stat = os.stat(staging, follow_symlinks=False)
                staging_signature = (staging_stat.st_dev, staging_stat.st_ino)
                os.rename(staging, final)
            except FileExistsError:
                self._verify_draft_permissions(final)
                current = validate_draft_directory(
                    final, expected_identity=identity, trust_service=trust_service,
                    review_authority_root=self.policy.narrative_review_authority_root,
                    require_trust=True
                )
                if current["story"] == artifact.story_payload and current["manifest"] == artifact.draft_manifest:
                    _cleanup_owned_path(staging)
                    if os.path.lexists(staging):
                        _raise("narrative_normalizer_persistence_invalid")
                    if not self.broker_mode:
                        try:
                            self._review_store().initialize(
                                source_identity=identity,
                                draft_identity=str(artifact.draft_manifest["draft_identity"]),
                                initial_state=str(artifact.review_payload["status"]),
                                reason_codes=tuple(artifact.review_payload["reason_codes"]),
                                drafted_at=str(artifact.draft_manifest["created_at"]),
                                reviewed_at=str(artifact.review_payload["reviewed_at"]),
                            )
                        except review_state.ReviewStateError as error:
                            self._review_state_error(error)
                    return final, True
                _raise("narrative_normalizer_persistence_conflict")
            try:
                if self.permission_policy.shared:
                    outbox_permissions.finalize_shared_draft(
                        self.permission_policy,
                        self.root,
                        final,
                    )
            except BaseException:
                if os.path.lexists(final) and not final.is_symlink() and final.is_dir():
                    final_stat = os.stat(final, follow_symlinks=False)
                    if (final_stat.st_dev, final_stat.st_ino) == staging_signature:
                        _cleanup_owned_path(final)
                        _fsync_directory(self.root)
                raise
            _fsync_directory(self.root)
            self._verify_draft_permissions(final)
            validate_draft_directory(
                final, expected_identity=identity, trust_service=trust_service,
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=True
            )
            if not self.broker_mode:
                try:
                    self._review_store().initialize(
                        source_identity=identity,
                        draft_identity=str(artifact.draft_manifest["draft_identity"]),
                        initial_state=str(artifact.review_payload["status"]),
                        reason_codes=tuple(artifact.review_payload["reason_codes"]),
                        drafted_at=str(artifact.draft_manifest["created_at"]),
                        reviewed_at=str(artifact.review_payload["reviewed_at"]),
                    )
                except review_state.ReviewStateError as error:
                    self._review_state_error(error)
            return final, False
        finally:
            if os.path.lexists(staging):
                _cleanup_owned_path(staging)
                if os.path.lexists(staging):
                    _raise("narrative_normalizer_persistence_invalid")

    def register_draft_with_authority(
        self,
        source_ref: str,
        source_digest_value: str,
    ) -> dict[str, object]:
        """Register an exact, fully persisted draft without exposing its content."""

        if not self.broker_mode:
            _raise("narrative_normalizer_review_authority_unavailable")
        identity = self._identity(source_ref, source_digest_value)
        path = self.root / identity
        self._verify_draft_permissions(path)
        value = validate_draft_directory(
            path,
            expected_identity=identity,
            trust_service=self._require_trust(),
            review_authority_root=self.policy.narrative_review_authority_root,
            require_trust=True,
            validate_ready=False,
        )
        story = value["story"]
        manifest = value["manifest"]
        claim = self.read_claim(source_ref, source_digest_value)
        if claim is None or not self._claim_matches_draft(claim, value):
            _raise("narrative_normalizer_claim_uncertain")
        operator_request_id = f"register-{claim['attempt_id']}"
        payload = {
            "source_identity": identity,
            "source_ref": source_ref,
            "source_digest": source_digest_value,
            "draft_identity": manifest["draft_identity"],
            "draft_package_digest": story["package_digest"],
            "story_markdown_digest": _file_digest(path / "story.md"),
            "draft_manifest_digest": _file_digest(path / "draft-manifest.json"),
            "review_digest": _file_digest(path / "review.json"),
            "completed_claim_digest": _file_digest(
                self.claim_path(source_ref, source_digest_value)
            ),
            "artifact_binding_digest": value["artifact_binding_digest"],
            "contract_versions": {
                "draft": BROKER_DRAFT_CONTRACT_VERSION,
                "source": SOURCE_CONTRACT_VERSION,
            },
            "operator_request_id": operator_request_id,
            "timestamp": claim["updated_at"],
        }
        result = self._broker_call("register_draft", operator_request_id, payload)
        checked = self._validate_broker_event_result(
            result,
            source_identity_value=identity,
            draft_identity_value=str(manifest["draft_identity"]),
            draft_package_digest=str(story["package_digest"]),
            expected_state=review_state.STATE_DRAFTED,
            allow_extra=frozenset({"draft_binding_digest"}),
        )
        if (
            type(checked.get("draft_binding_digest")) is not str
            or _HEX64.fullmatch(str(checked["draft_binding_digest"])) is None
        ):
            _raise("narrative_normalizer_review_authority_unavailable")
        return checked

    def list_drafts(self) -> tuple[dict[str, object], ...]:
        if not self.root.exists():
            return ()
        if self.root.is_symlink() or not self.root.is_dir():
            _raise("narrative_normalizer_persistence_invalid")
        if self.permission_policy.shared:
            outbox_permissions.verify_shared_root(
                self.permission_policy,
                self.root,
            )
        rows = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not path.is_dir() or _HEX64.fullmatch(path.name) is None:
                continue
            self._verify_draft_permissions(path)
            self._verify_approval_pair_if_present(path)
            value = validate_draft_directory(
                path,
                expected_identity=path.name,
                validate_ready=not self.broker_mode,
                trust_service=self.trust_service,
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=self.trust_service is not None,
            )
            authoritative_state = "unverified"
            if self.trust_service is not None and not self.broker_mode:
                try:
                    authoritative_state = self._review_store().read(
                        path.name,
                        expected_draft_identity=str(value["manifest"]["draft_identity"]),
                    ).latest.state
                except review_state.ReviewStateError as error:
                    self._review_state_error(error)
            rows.append({
                "source_identity": path.name,
                "source_digest": value["story"]["source_digest"],
                "source_ref": value["story"]["source_ref"],
                "draft_identity": value["manifest"]["draft_identity"],
                "review_status": authoritative_state,
                "approved": authoritative_state == review_state.STATE_APPROVED and value["ready"] is not None,
            })
        return tuple(rows)

    def previous_supersedes(self, source: SourceUnit, new_draft_identity: str) -> dict[str, object] | None:
        candidates: list[tuple[str, dict[str, object]]] = []
        current_identity = source_identity(source.source_ref, source.source_digest)
        for row in self.list_drafts():
            if row["source_ref"] != source.source_ref or row["source_identity"] == current_identity:
                continue
            old_path = self.root / str(row["source_identity"])
            old = validate_draft_directory(
                old_path,
                expected_identity=str(row["source_identity"]),
                trust_service=self.trust_service,
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=self.trust_service is not None,
            )
            relation = {
                "old_source_ref": old["story"]["source_ref"],
                "old_source_digest": old["story"]["source_digest"],
                "old_source_identity": old["story"]["source_identity"],
                "old_draft_identity": old["manifest"]["draft_identity"],
                "new_source_ref": source.source_ref,
                "new_source_digest": source.source_digest,
                "new_source_identity": current_identity,
                "new_draft_identity": new_draft_identity,
            }
            candidates.append((str(old["manifest"]["created_at"]), relation))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def show(self, source_ref: str, source_digest_value: str) -> dict[str, object]:
        identity = self._identity(source_ref, source_digest_value)
        path = self.root / identity
        self._verify_draft_permissions(path)
        self._verify_approval_pair_if_present(path)
        value = validate_draft_directory(
            path,
            expected_identity=identity,
            validate_ready=self.trust_service is not None and not self.broker_mode,
            trust_service=self.trust_service,
            review_authority_root=self.policy.narrative_review_authority_root,
            require_trust=self.trust_service is not None,
        )
        authoritative_state = "unverified"
        if self.trust_service is not None and not self.broker_mode:
            try:
                authoritative_state = self._review_store().read(
                    identity,
                    expected_draft_identity=str(value["manifest"]["draft_identity"]),
                ).latest.state
            except review_state.ReviewStateError as error:
                self._review_state_error(error)
        return {
            "source_identity": identity,
            "source_digest": source_digest_value,
            "draft_identity": value["manifest"]["draft_identity"],
            "story": value["story"],
            "review": value["review"],
            "authoritative_review_state": authoritative_state,
            "approved": authoritative_state == review_state.STATE_APPROVED and value["ready"] is not None,
        }

    def _validate_review_transition(self, value: Mapping[str, object], current: Mapping[str, object]) -> None:
        payload = _exact_mapping(dict(value), _REVIEW_KEYS, "narrative_normalizer_review_identity_conflict")
        if (
            payload["draft_identity"] != current["manifest"]["draft_identity"]
            or payload["fact_coverage"] != current["review"]["fact_coverage"]
            or payload["factuality_receipt"] != current["story"]["factuality_receipt"]
            or payload["meaning_preservation_receipt"] != current["story"]["meaning_preservation_receipt"]
            or payload["plain_language_receipt"] != current["story"]["plain_language_receipt"]
            or payload["unsupported_claim_count"] != current["factuality"].unsupported_claim_count
            or payload["status"] not in {REVIEW_REJECTED, REVIEW_SUPERSEDED}
            or not _is_timestamp(payload["reviewed_at"])
            or payload["reviewer_version"] != REVIEWER_VERSION
            or type(payload["operator_request_id"]) is not str
            or not payload["operator_request_id"]
            or type(payload["action_digest"]) is not str
            or _HEX64.fullmatch(payload["action_digest"]) is None
        ):
            _raise("narrative_normalizer_review_identity_conflict")

    def _replace_review(self, path: Path, old_bytes: bytes, payload: Mapping[str, object], current: Mapping[str, object]) -> None:
        self._validate_review_transition(payload, current)
        encoded = _canonical(payload) + b"\n"
        staging = path.with_name(f".review-staging-{secrets.token_hex(8)}")
        backup = self._state / (
            f".review-backup-{current['story']['source_identity']}-{secrets.token_hex(8)}"
        )
        rollback = path.with_name(f".review-rollback-{secrets.token_hex(8)}")
        try:
            _write_exclusive_file(staging, encoded)
            parsed = json.loads(staging.read_bytes().decode("utf-8"))
            if parsed != payload:
                _raise("narrative_normalizer_persistence_invalid")
            self._validate_review_transition(parsed, current)
            if path.read_bytes() != old_bytes:
                _raise("narrative_normalizer_review_identity_conflict")
            os.link(path, backup)
            if backup.read_bytes() != old_bytes or path.read_bytes() != old_bytes:
                _raise("narrative_normalizer_review_identity_conflict")
            _fsync_directory(backup.parent)
            os.replace(staging, path)
            _fsync_directory(path.parent)
            if path.read_bytes() != encoded:
                _raise("narrative_normalizer_persistence_invalid")
            final = validate_draft_directory(
                path.parent,
                expected_identity=str(current["story"]["source_identity"]),
                trust_service=self._require_trust(),
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=True,
            )
            if final["review"] != payload:
                _raise("narrative_normalizer_persistence_invalid")
            os.unlink(backup)
            _fsync_directory(backup.parent)
            if os.path.lexists(backup):
                _raise("narrative_normalizer_persistence_invalid")
        except BaseException as original:
            cleanup_failure = False
            cleanup_cancellation: BaseException | None = None
            path_bytes: bytes | None = None
            try:
                if path.is_symlink() or not path.is_file():
                    cleanup_failure = True
                else:
                    path_bytes = path.read_bytes()
            except BaseException as error:
                cleanup_failure = True
                if not isinstance(error, Exception):
                    cleanup_cancellation = error
            if path_bytes != old_bytes:
                try:
                    if os.path.lexists(backup) and not backup.is_symlink() and backup.is_file() and backup.read_bytes() == old_bytes:
                        os.replace(backup, path)
                    else:
                        _write_exclusive_file(rollback, old_bytes)
                        os.replace(rollback, path)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
                try:
                    if path.is_symlink() or not path.is_file() or path.read_bytes() != old_bytes:
                        raise OSError("review-rollback-invalid")
                    _fsync_directory(path.parent)
                    _fsync_directory(backup.parent)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
            if os.path.lexists(staging):
                try:
                    _cleanup_owned_path(staging)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
                try:
                    if os.path.lexists(staging):
                        raise OSError("review-staging-cleanup-invalid")
                    _fsync_directory(staging.parent)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
            if os.path.lexists(backup):
                try:
                    _cleanup_owned_path(backup)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
                try:
                    if os.path.lexists(backup):
                        raise OSError("review-backup-cleanup-invalid")
                    _fsync_directory(backup.parent)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
            if os.path.lexists(rollback):
                try:
                    _cleanup_owned_path(rollback)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
                try:
                    if os.path.lexists(rollback):
                        raise OSError("review-rollback-cleanup-invalid")
                    _fsync_directory(rollback.parent)
                except BaseException as error:
                    cleanup_failure = True
                    if not isinstance(error, Exception) and cleanup_cancellation is None:
                        cleanup_cancellation = error
            try:
                if path.read_bytes() != old_bytes:
                    cleanup_failure = True
            except BaseException as error:
                cleanup_failure = True
                if not isinstance(error, Exception) and cleanup_cancellation is None:
                    cleanup_cancellation = error
            if os.path.lexists(staging) or os.path.lexists(backup) or os.path.lexists(rollback):
                cleanup_failure = True
            if not isinstance(original, Exception):
                raise
            if cleanup_cancellation is not None:
                raise cleanup_cancellation from None
            if cleanup_failure and isinstance(original, Exception):
                _raise("narrative_normalizer_persistence_invalid")
            raise

    def _transition_review(
        self,
        *,
        source_ref: str,
        source_digest_value: str,
        expected_draft_identity: str,
        status: str,
        reason_codes: tuple[str, ...],
        operator_request_id: str,
        reviewed_at: str,
        supersede_binding: Mapping[str, object] | None,
    ) -> ReviewUpdateResult:
        trust_service = self._require_trust()
        identity = self._identity(source_ref, source_digest_value)
        path = self.root / identity
        self._verify_draft_permissions(path)
        current = validate_draft_directory(
            path, expected_identity=identity, trust_service=trust_service,
            review_authority_root=self.policy.narrative_review_authority_root,
            require_trust=True
        )
        if current["manifest"]["draft_identity"] != expected_draft_identity:
            _raise("narrative_normalizer_review_identity_conflict")
        if current["ready"] is not None:
            _raise("narrative_normalizer_approval_conflict")
        safe_reasons = tuple(sorted(set(reason_codes)))
        if any(item not in REASON_CODES for item in safe_reasons) or (status == REVIEW_REJECTED and not safe_reasons):
            _raise("narrative_normalizer_review_identity_conflict")
        if type(operator_request_id) is not str or _SAFE_COMPONENT.fullmatch(operator_request_id) is None:
            _raise("narrative_normalizer_review_identity_conflict")
        action = {
            "version": REVIEW_ACTION_VERSION,
            "source_identity": identity,
            "draft_identity": expected_draft_identity,
            "status": status,
            "reason_codes": safe_reasons,
            "operator_request_id": operator_request_id,
            "supersede_binding": None if supersede_binding is None else dict(supersede_binding),
        }
        action_digest = _sha(action)
        if self.broker_mode:
            latest = self._broker_latest(
                identity,
                expected_draft_identity,
                str(current["story"]["package_digest"]),
            )
            result = self._broker_call(
                "append_review",
                operator_request_id,
                {
                    "source_identity": identity,
                    "draft_identity": expected_draft_identity,
                    "draft_package_digest": current["story"]["package_digest"],
                    "new_state": status,
                    "operator_request_id": operator_request_id,
                    "reason_codes": list(safe_reasons),
                    "timestamp": reviewed_at,
                    "expected_revision": latest["revision"],
                    "expected_event_digest": latest["event_digest"],
                },
            )
            checked = self._validate_broker_event_result(
                result,
                source_identity_value=identity,
                draft_identity_value=expected_draft_identity,
                draft_package_digest=str(current["story"]["package_digest"]),
                expected_state=status,
            )
            return ReviewUpdateResult(
                identity,
                expected_draft_identity,
                str(checked["state"]),
                operator_request_id,
                bool(checked["idempotent"]),
            )
        try:
            ledger, idempotent = self._review_store().transition(
                source_identity=identity,
                draft_identity=expected_draft_identity,
                new_state=status,
                operator_request_id=operator_request_id,
                reason_codes=safe_reasons,
                timestamp=reviewed_at,
                action_digest=action_digest,
            )
        except review_state.ReviewStateError as error:
            self._review_state_error(error)
        # review.json is immutable draft evidence.  The separately protected
        # ledger is the sole authority for the latest state, eliminating the
        # split mutable-owner and historical bundle replay hazards.
        return ReviewUpdateResult(
            identity,
            expected_draft_identity,
            ledger.latest.state,
            operator_request_id,
            idempotent,
        )

    @_privacy_boundary("narrative_normalizer_internal_error")
    def pass_review(
        self,
        source_ref: str,
        source_digest_value: str,
        *,
        operator_request_id: str,
        expected_draft_identity: str,
        reviewed_at: str,
    ) -> ReviewUpdateResult:
        with self.lock_for(source_ref, source_digest_value, blocking=True):
            return self._transition_review(
                source_ref=source_ref,
                source_digest_value=source_digest_value,
                expected_draft_identity=expected_draft_identity,
                status=REVIEW_PASSED,
                reason_codes=(),
                operator_request_id=operator_request_id,
                reviewed_at=reviewed_at,
                supersede_binding=None,
            )

    @_privacy_boundary("narrative_normalizer_internal_error")
    def reject(
        self,
        source_ref: str,
        source_digest_value: str,
        *,
        operator_request_id: str,
        expected_draft_identity: str,
        reason_codes: Sequence[str],
        reviewed_at: str,
    ) -> ReviewUpdateResult:
        reasons = tuple(reason_codes)
        with self.lock_for(source_ref, source_digest_value, blocking=True):
            return self._transition_review(
                source_ref=source_ref,
                source_digest_value=source_digest_value,
                expected_draft_identity=expected_draft_identity,
                status=REVIEW_REJECTED,
                reason_codes=reasons,
                operator_request_id=operator_request_id,
                reviewed_at=reviewed_at,
                supersede_binding=None,
            )

    @_privacy_boundary("narrative_normalizer_internal_error")
    def supersede(
        self,
        *,
        old_source_ref: str,
        old_source_digest: str,
        old_source_identity: str,
        old_draft_identity: str,
        new_source_ref: str,
        new_source_digest: str,
        new_source_identity: str,
        new_draft_identity: str,
        operator_request_id: str,
        reviewed_at: str,
    ) -> ReviewUpdateResult:
        trust_service = self._require_trust()
        binding = {
            "old_source_ref": old_source_ref,
            "old_source_digest": old_source_digest,
            "old_source_identity": old_source_identity,
            "old_draft_identity": old_draft_identity,
            "new_source_ref": new_source_ref,
            "new_source_digest": new_source_digest,
            "new_source_identity": new_source_identity,
            "new_draft_identity": new_draft_identity,
            "operator_request_id": operator_request_id,
        }
        if (
            self._identity(old_source_ref, old_source_digest) != old_source_identity
            or self._identity(new_source_ref, new_source_digest) != new_source_identity
        ):
            _raise("narrative_normalizer_supersede_identity_invalid")
        expected_relation = dict(binding)
        expected_relation.pop("operator_request_id")
        with self.lock_for(old_source_ref, old_source_digest, blocking=True):
            self._verify_draft_permissions(self.root / old_source_identity)
            old_value = validate_draft_directory(
                self.root / old_source_identity,
                expected_identity=old_source_identity,
                trust_service=trust_service,
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=True,
            )
            if old_value["manifest"]["draft_identity"] != old_draft_identity:
                _raise("narrative_normalizer_supersede_identity_invalid")
            action = {
                "version": REVIEW_ACTION_VERSION,
                "source_identity": old_source_identity,
                "draft_identity": old_draft_identity,
                "status": REVIEW_SUPERSEDED,
                "reason_codes": (),
                "operator_request_id": operator_request_id,
                "supersede_binding": binding,
            }
            if not self.broker_mode:
                try:
                    ledger = self._review_store().read(
                        old_source_identity,
                        expected_draft_identity=old_draft_identity,
                    )
                except review_state.ReviewStateError as error:
                    self._review_state_error(error)
                for event in ledger.events:
                    if event.operator_request_id != operator_request_id:
                        continue
                    if event.state != review_state.STATE_SUPERSEDED or event.action_digest != _sha(action):
                        _raise("narrative_normalizer_review_identity_conflict")
                    return ReviewUpdateResult(
                        old_source_identity,
                        old_draft_identity,
                        REVIEW_SUPERSEDED,
                        operator_request_id,
                        True,
                    )
            new_path = self.root / new_source_identity
            self._verify_draft_permissions(new_path)
            new_value = validate_draft_directory(
                new_path,
                expected_identity=new_source_identity,
                trust_service=trust_service,
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=True,
            )
            if (
                new_value["manifest"]["draft_identity"] != new_draft_identity
                or new_value["manifest"]["supersedes"] != expected_relation
            ):
                _raise("narrative_normalizer_supersede_identity_invalid")
            return self._transition_review(
                source_ref=old_source_ref,
                source_digest_value=old_source_digest,
                expected_draft_identity=old_draft_identity,
                status=REVIEW_SUPERSEDED,
                reason_codes=(),
                operator_request_id=operator_request_id,
                reviewed_at=reviewed_at,
                supersede_binding=binding,
            )

    def _approve_with_broker(
        self,
        source_ref: str,
        source_digest_value: str,
        *,
        expected_draft_identity: str,
        operator_request_id: str,
    ) -> ApprovalResult:
        trust_service = self._require_trust()
        identity = self._identity(source_ref, source_digest_value)
        raw_source = read_source_unit(
            self.policy,
            source_ref,
            expected_digest=source_digest_value,
            allow_insufficient=True,
        )
        source_documents = read_source_documents(
            self.policy,
            source_ref,
            expected_digest=source_digest_value,
        )
        path = self.root / identity
        self._verify_draft_permissions(path)
        value = validate_draft_directory(
            path,
            expected_identity=identity,
            validate_ready=False,
            trust_service=trust_service,
            review_authority_root=self.policy.narrative_review_authority_root,
            require_trust=True,
        )
        raw_source = _replay_source_for_story(raw_source, source_documents, value["story"])
        value = validate_draft_directory(
            path,
            expected_identity=identity,
            expected_source=raw_source,
            validate_ready=False,
            trust_service=trust_service,
            review_authority_root=self.policy.narrative_review_authority_root,
            require_trust=True,
        )
        story = value["story"]
        review = value["review"]
        manifest = value["manifest"]
        completed_claim = self.read_claim(source_ref, source_digest_value)
        if completed_claim is None or not self._claim_matches_draft(completed_claim, value):
            _raise("narrative_normalizer_approval_conflict")
        if manifest["draft_identity"] != expected_draft_identity:
            _raise("narrative_normalizer_review_identity_conflict")
        if (
            review["status"] != REVIEW_PASSED
            or value["factuality"].unsupported_claim_count != 0
            or not value["factuality"].passed
            or not value["meaning"].passed
            or not value["plain_language"].passed
        ):
            _raise("narrative_normalizer_review_not_passed")

        story_path = path / "story.json"
        narrative_package_digest = _file_digest(story_path)
        ready_payload = {
            "schema_version": quarantine.MANIFEST_SCHEMA_VERSION,
            "source_ref": source_ref,
            "source_digest": source_digest_value,
            "narrative_package_ref": f"{identity}/story.json",
            "narrative_package_digest": narrative_package_digest,
            "status": quarantine.CLASS_READY,
            "contract_versions": {
                "director": "review-only-normalizer-v1",
                "narrative": generation.GENERATION_CONTRACT_VERSION,
            },
        }
        try:
            quarantine.validate_narrative_ready_payload(self.policy, source_ref, ready_payload)
        except quarantine.QuarantineError:
            _raise("narrative_normalizer_manifest_invalid")
        ready_encoded = _canonical(ready_payload) + b"\n"
        ready_digest = hashlib.sha256(ready_encoded).hexdigest()
        draft_package_digest = str(story["package_digest"])
        latest = self._broker_latest(
            identity,
            expected_draft_identity,
            draft_package_digest,
        )
        ready_target = path / "narrative_ready.json"
        attestation_target = path / "approval-attestation.json"

        def validate_attestation(raw: object) -> review_state.DualDigestApprovalAttestation:
            if type(raw) is not dict:
                _raise("narrative_normalizer_manifest_invalid")
            try:
                parsed = review_state.dual_digest_approval_attestation_from_payload(
                    raw,
                    trust_service,
                    ready_manifest_contract=quarantine.MANIFEST_SCHEMA_VERSION,
                    source_contract=SOURCE_CONTRACT_VERSION,
                    draft_contract=BROKER_DRAFT_CONTRACT_VERSION,
                )
            except review_state.ReviewStateError:
                _raise("narrative_normalizer_manifest_invalid")
            claim_path = self.claim_path(source_ref, source_digest_value)
            if (
                parsed.source_identity != identity
                or parsed.source_ref != source_ref
                or parsed.source_digest != source_digest_value
                or parsed.draft_identity != expected_draft_identity
                or parsed.draft_package_digest != draft_package_digest
                or parsed.narrative_package_digest != narrative_package_digest
                or parsed.narrative_ready_manifest_digest != ready_digest
                or parsed.story_markdown_digest != _file_digest(path / "story.md")
                or parsed.draft_manifest_digest != _file_digest(path / "draft-manifest.json")
                or parsed.review_digest != _file_digest(path / "review.json")
                or parsed.completed_claim_digest != _file_digest(claim_path)
                or parsed.artifact_binding_digest != value["artifact_binding_digest"]
                or parsed.approval_request_id != operator_request_id
            ):
                _raise("narrative_normalizer_manifest_invalid")
            return parsed

        if latest["state"] == review_state.STATE_APPROVED:
            if (
                not os.path.lexists(attestation_target)
                or not os.path.lexists(ready_target)
                or attestation_target.is_symlink()
                or ready_target.is_symlink()
                or not attestation_target.is_file()
                or not ready_target.is_file()
                or ready_target.read_bytes() != ready_encoded
            ):
                _raise("narrative_normalizer_approval_conflict")
            self._verify_approval_file(attestation_target)
            self._verify_approval_file(ready_target)
            attestation_raw = _json_read(
                attestation_target,
                frozenset(json.loads(attestation_target.read_text(encoding="utf-8"))),
                "narrative_normalizer_manifest_invalid",
            )
            parsed = validate_attestation(attestation_raw)
            if (
                latest["revision"] != parsed.review_revision
                or latest["event_digest"] != parsed.review_event_digest
            ):
                _raise("narrative_normalizer_approval_conflict")
            return ApprovalResult(source_digest_value, _sha(ready_payload), quarantine.CLASS_READY, True)
        if latest["state"] != review_state.STATE_PASSED:
            _raise("narrative_normalizer_review_not_passed")

        prepare_payload = {
            "source_identity": identity,
            "source_ref": source_ref,
            "source_digest": source_digest_value,
            "draft_identity": expected_draft_identity,
            "draft_package_digest": draft_package_digest,
            "narrative_package_digest": narrative_package_digest,
            "narrative_ready_manifest_digest": ready_digest,
            "story_markdown_digest": _file_digest(path / "story.md"),
            "draft_manifest_digest": _file_digest(path / "draft-manifest.json"),
            "review_digest": _file_digest(path / "review.json"),
            "completed_claim_digest": _file_digest(
                self.claim_path(source_ref, source_digest_value)
            ),
            "artifact_binding_digest": value["artifact_binding_digest"],
            "ready_manifest_contract": quarantine.MANIFEST_SCHEMA_VERSION,
            "source_contract": SOURCE_CONTRACT_VERSION,
            "draft_contract_versions": {
                "draft": BROKER_DRAFT_CONTRACT_VERSION,
                "source": SOURCE_CONTRACT_VERSION,
            },
            "operator_request_id": operator_request_id,
            # A stable persisted timestamp makes a failed commit exactly
            # replayable under the same operator request identity.
            "timestamp": completed_claim["updated_at"],
            "expected_revision": latest["revision"],
            "expected_event_digest": latest["event_digest"],
        }
        prepared_result = self._broker_call(
            "prepare_approval",
            f"prepare-{operator_request_id}",
            prepare_payload,
        )
        if (
            frozenset(prepared_result) != {"prepared", "attestation_digest", "mutated"}
            or prepared_result.get("mutated") is not False
            or type(prepared_result.get("prepared")) is not dict
        ):
            _raise("narrative_normalizer_review_authority_unavailable")
        prepared = prepared_result["prepared"]
        assert type(prepared) is dict
        if (
            frozenset(prepared) != {"schema_version", "event", "attestation", "prepared_identity"}
            or prepared.get("schema_version") != BROKER_PREPARED_APPROVAL_VERSION
            or type(prepared.get("event")) is not dict
            or type(prepared.get("attestation")) is not dict
            or prepared.get("prepared_identity") != _sha({
                "event": prepared["event"],
                "attestation": prepared["attestation"],
            })
            or prepared_result.get("attestation_digest") != _sha(prepared["attestation"])
        ):
            _raise("narrative_normalizer_review_authority_unavailable")
        parsed_attestation = validate_attestation(prepared["attestation"])
        attestation_encoded = _canonical(prepared["attestation"]) + b"\n"

        if os.path.lexists(ready_target) != os.path.lexists(attestation_target):
            _raise("narrative_normalizer_approval_conflict")
        if os.path.lexists(ready_target):
            if (
                ready_target.is_symlink()
                or attestation_target.is_symlink()
                or not ready_target.is_file()
                or not attestation_target.is_file()
                or ready_target.read_bytes() != ready_encoded
                or attestation_target.read_bytes() != attestation_encoded
            ):
                _raise("narrative_normalizer_approval_conflict")
            self._verify_approval_file(attestation_target)
            self._verify_approval_file(ready_target)
        else:
            token = secrets.token_hex(8)
            attestation_staging = path / f".attestation-staging-{token}"
            ready_staging = path / f".ready-staging-{token}"
            owned_targets: list[tuple[Path, tuple[int, int]]] = []
            pair_promoted = False
            try:
                _write_exclusive_file(attestation_staging, attestation_encoded)
                if attestation_staging.read_bytes() != attestation_encoded:
                    _raise("narrative_normalizer_manifest_invalid")
                validate_attestation(json.loads(attestation_staging.read_text(encoding="utf-8")))
                _write_exclusive_file(ready_staging, ready_encoded)
                if ready_staging.read_bytes() != ready_encoded:
                    _raise("narrative_normalizer_manifest_invalid")
                quarantine.validate_narrative_ready_payload(
                    self.policy,
                    source_ref,
                    json.loads(ready_staging.read_text(encoding="utf-8")),
                )
                for staging, target, encoded in (
                    (attestation_staging, attestation_target, attestation_encoded),
                    (ready_staging, ready_target, ready_encoded),
                ):
                    signature_stat = os.stat(staging, follow_symlinks=False)
                    signature = (signature_stat.st_dev, signature_stat.st_ino)
                    os.link(staging, target)
                    owned_targets.append((target, signature))
                    _fsync_directory(path)
                    self._finalize_approval_file(target)
                    if target.is_symlink() or not target.is_file() or target.read_bytes() != encoded:
                        _raise("narrative_normalizer_manifest_invalid")
                pair_promoted = True
            except BaseException:
                if not pair_promoted:
                    for target, signature in reversed(owned_targets):
                        if os.path.lexists(target) and not target.is_symlink() and target.is_file():
                            target_stat = os.stat(target, follow_symlinks=False)
                            if (target_stat.st_dev, target_stat.st_ino) == signature:
                                _cleanup_owned_path(target)
                                _fsync_directory(path)
                raise
            finally:
                for staging in (attestation_staging, ready_staging):
                    if os.path.lexists(staging):
                        _cleanup_owned_path(staging)
                        _fsync_directory(path)

        # Recompute immediately before commit; the Broker independently reads
        # the same persisted bytes and rejects any prepare/commit race.
        if _file_digest(story_path) != narrative_package_digest:
            _raise("narrative_normalizer_approval_conflict")
        commit_payload = {
            "prepared": prepared,
            "ready_manifest": ready_payload,
            "ready_manifest_digest": ready_digest,
            "attestation_digest": str(prepared_result["attestation_digest"]),
            "draft_package_digest": draft_package_digest,
            "narrative_package_digest": narrative_package_digest,
        }
        committed = self._broker_call(
            "commit_approval",
            f"commit-{operator_request_id}",
            commit_payload,
        )
        checked = self._validate_broker_event_result(
            committed,
            source_identity_value=identity,
            draft_identity_value=expected_draft_identity,
            draft_package_digest=draft_package_digest,
            expected_state=review_state.STATE_APPROVED,
            allow_extra=frozenset({"attestation", "attestation_digest", "prepared_identity"}),
        )
        if (
            checked.get("attestation") != prepared["attestation"]
            or checked.get("attestation_digest") != prepared_result["attestation_digest"]
            or checked.get("prepared_identity") != prepared["prepared_identity"]
            or ready_target.read_bytes() != ready_encoded
            or attestation_target.read_bytes() != attestation_encoded
            or _file_digest(story_path) != narrative_package_digest
        ):
            _raise("narrative_normalizer_approval_conflict")
        final_latest = self._broker_latest(
            identity,
            expected_draft_identity,
            draft_package_digest,
        )
        if (
            final_latest["state"] != review_state.STATE_APPROVED
            or final_latest["revision"] != parsed_attestation.review_revision
            or final_latest["event_digest"] != parsed_attestation.review_event_digest
        ):
            _raise("narrative_normalizer_approval_conflict")
        return ApprovalResult(
            source_digest_value,
            _sha(ready_payload),
            quarantine.CLASS_READY,
            bool(checked["idempotent"]),
        )

    @_privacy_boundary("narrative_normalizer_internal_error")
    def approve(
        self,
        source_ref: str,
        source_digest_value: str,
        *,
        expected_draft_identity: str,
        reviewed_at: str,
        operator_request_id: str | None = None,
    ) -> ApprovalResult:
        trust_service = self._require_trust()
        identity = self._identity(source_ref, source_digest_value)
        if operator_request_id is None:
            operator_request_id = f"approval-{expected_draft_identity[:24]}"
        if type(operator_request_id) is not str or _SAFE_COMPONENT.fullmatch(operator_request_id) is None:
            _raise("narrative_normalizer_review_identity_conflict")
        with self.lock_for(source_ref, source_digest_value, blocking=True):
            if self.broker_mode:
                return self._approve_with_broker(
                    source_ref,
                    source_digest_value,
                    expected_draft_identity=expected_draft_identity,
                    operator_request_id=operator_request_id,
                )
            raw_source = read_source_unit(
                self.policy,
                source_ref,
                expected_digest=source_digest_value,
                allow_insufficient=True,
            )
            source_documents = read_source_documents(
                self.policy,
                source_ref,
                expected_digest=source_digest_value,
            )
            path = self.root / identity
            self._verify_draft_permissions(path)
            value = validate_draft_directory(
                path,
                expected_identity=identity,
                validate_ready=False,
                trust_service=trust_service,
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=True,
            )
            raw_source = _replay_source_for_story(raw_source, source_documents, value["story"])
            value = validate_draft_directory(
                path,
                expected_identity=identity,
                expected_source=raw_source,
                validate_ready=False,
                trust_service=trust_service,
                review_authority_root=self.policy.narrative_review_authority_root,
                require_trust=True,
            )
            story = value["story"]
            review = value["review"]
            completed_claim = self.read_claim(source_ref, source_digest_value)
            if completed_claim is None or not self._claim_matches_draft(completed_claim, value):
                _raise("narrative_normalizer_approval_conflict")
            if value["manifest"]["draft_identity"] != expected_draft_identity:
                _raise("narrative_normalizer_review_identity_conflict")
            if (
                review["status"] != REVIEW_PASSED
                or value["factuality"].unsupported_claim_count != 0
                or not value["factuality"].passed
                or not value["meaning"].passed
                or not value["plain_language"].passed
            ):
                _raise("narrative_normalizer_review_not_passed")
            approval_action_digest = _sha({
                "version": "normalizer-approval-action-v1",
                "source_identity": identity,
                "draft_identity": expected_draft_identity,
                "operator_request_id": operator_request_id,
                "package_digest": story["package_digest"],
                "artifact_binding_digest": value["artifact_binding_digest"],
            })
            try:
                approved_event, ledger_idempotent = self._review_store().prepare_transition(
                    source_identity=identity,
                    draft_identity=expected_draft_identity,
                    new_state=review_state.STATE_APPROVED,
                    operator_request_id=operator_request_id,
                    reason_codes=(),
                    timestamp=reviewed_at,
                    action_digest=approval_action_digest,
                )
            except review_state.ReviewStateError as error:
                self._review_state_error(error)
            actual_story_digest = _file_digest(path / "story.json")
            manifest_payload = {
                "schema_version": quarantine.MANIFEST_SCHEMA_VERSION,
                "source_ref": source_ref,
                "source_digest": source_digest_value,
                "narrative_package_ref": f"{identity}/story.json",
                "narrative_package_digest": actual_story_digest,
                "status": quarantine.CLASS_READY,
                "contract_versions": {
                    "director": "review-only-normalizer-v1",
                    "narrative": generation.GENERATION_CONTRACT_VERSION,
                },
            }
            try:
                quarantine.validate_narrative_ready_payload(self.policy, source_ref, manifest_payload)
            except quarantine.QuarantineError:
                _raise("narrative_normalizer_manifest_invalid")
            ready_target = path / "narrative_ready.json"
            ready_encoded = _canonical(manifest_payload) + b"\n"
            claim_path = self.claim_path(source_ref, source_digest_value)
            attestation = review_state.build_approval_attestation(
                trust_service,
                source_identity=identity,
                source_ref=source_ref,
                source_digest=source_digest_value,
                draft_identity=expected_draft_identity,
                package_digest=actual_story_digest,
                ready_manifest_digest=hashlib.sha256(ready_encoded).hexdigest(),
                story_markdown_digest=_file_digest(path / "story.md"),
                draft_manifest_digest=_file_digest(path / "draft-manifest.json"),
                review_digest=_file_digest(path / "review.json"),
                completed_claim_digest=_file_digest(claim_path),
                artifact_binding_digest=str(value["artifact_binding_digest"]),
                approved_event=approved_event,
                ready_manifest_contract=quarantine.MANIFEST_SCHEMA_VERSION,
                source_contract=SOURCE_CONTRACT_VERSION,
            )
            attestation_payload = attestation.to_payload()
            attestation_target = path / "approval-attestation.json"
            attestation_encoded = _canonical(attestation_payload) + b"\n"

            def existing_ready_result() -> ApprovalResult:
                self._verify_approval_file(attestation_target)
                self._verify_approval_file(ready_target)
                if (
                    ready_target.is_symlink()
                    or not ready_target.is_file()
                    or ready_target.read_bytes() != ready_encoded
                    or attestation_target.is_symlink()
                    or not attestation_target.is_file()
                    or attestation_target.read_bytes() != attestation_encoded
                ):
                    _raise("narrative_normalizer_approval_conflict")
                nonlocal ledger_idempotent
                try:
                    if not ledger_idempotent:
                        self._review_store().commit_prepared(approved_event)
                        ledger_idempotent = True
                    manifest = quarantine.validate_narrative_ready_manifest(
                        self.policy,
                        source_ref,
                        trust_service=trust_service,
                    )
                    final_claim = self.read_claim(source_ref, source_digest_value)
                    final_value = validate_draft_directory(
                        path,
                        expected_identity=identity,
                        expected_source=raw_source,
                        trust_service=trust_service,
                        review_authority_root=self.policy.narrative_review_authority_root,
                        require_trust=True,
                    )
                except (quarantine.QuarantineError, NarrativeNormalizerError):
                    _raise("narrative_normalizer_manifest_invalid")
                if final_value["ready"] is None:
                    _raise("narrative_normalizer_manifest_invalid")
                if final_claim is None or not self._claim_matches_draft(final_claim, final_value):
                    _raise("narrative_normalizer_approval_conflict")
                return ApprovalResult(source_digest_value, _sha(manifest_payload), manifest.status, True)

            if os.path.lexists(ready_target) or os.path.lexists(attestation_target):
                if not (os.path.lexists(ready_target) and os.path.lexists(attestation_target)):
                    _raise("narrative_normalizer_approval_conflict")
                return existing_ready_result()

            token = secrets.token_hex(8)
            attestation_staging = path / f".attestation-staging-{token}"
            ready_staging = path / f".ready-staging-{token}"
            owned_targets: list[tuple[Path, tuple[int, int]]] = []
            try:
                _write_exclusive_file(attestation_staging, attestation_encoded)
                staged_attestation = _json_read(
                    attestation_staging,
                    frozenset(attestation_payload),
                    "narrative_normalizer_manifest_invalid",
                )
                if staged_attestation != attestation_payload:
                    _raise("narrative_normalizer_manifest_invalid")
                try:
                    parsed_attestation = review_state.approval_attestation_from_payload(
                        staged_attestation,
                        trust_service,
                        ready_manifest_contract=quarantine.MANIFEST_SCHEMA_VERSION,
                        source_contract=SOURCE_CONTRACT_VERSION,
                    )
                except review_state.ReviewStateError:
                    _raise("narrative_normalizer_manifest_invalid")
                if parsed_attestation != attestation:
                    _raise("narrative_normalizer_manifest_invalid")

                _write_exclusive_file(ready_staging, ready_encoded)
                staged_payload = _json_read(ready_staging, _READY_KEYS, "narrative_normalizer_manifest_invalid")
                if staged_payload != manifest_payload:
                    _raise("narrative_normalizer_manifest_invalid")
                quarantine.validate_narrative_ready_payload(self.policy, source_ref, staged_payload)

                for staging, target, encoded in (
                    (attestation_staging, attestation_target, attestation_encoded),
                    (ready_staging, ready_target, ready_encoded),
                ):
                    if os.path.lexists(target):
                        if target.is_file() and not target.is_symlink() and target.read_bytes() == encoded:
                            self._verify_approval_file(target)
                            continue
                        _raise("narrative_normalizer_approval_conflict")
                    staging_stat = os.stat(staging, follow_symlinks=False)
                    signature = (staging_stat.st_dev, staging_stat.st_ino)
                    try:
                        os.link(staging, target)
                    except FileExistsError:
                        if not (
                            os.path.lexists(target)
                            and target.is_file()
                            and not target.is_symlink()
                            and target.read_bytes() == encoded
                        ):
                            _raise("narrative_normalizer_approval_conflict")
                    else:
                        owned_targets.append((target, signature))
                    _fsync_directory(path)
                    self._finalize_approval_file(target)
                    if target.is_symlink() or not target.is_file() or target.read_bytes() != encoded:
                        _raise("narrative_normalizer_manifest_invalid")

                # Remove both sibling staging names before strict directory
                # replay.  Their hard-linked targets remain owned by this call.
                for staging in (attestation_staging, ready_staging):
                    _cleanup_owned_path(staging)
                    _fsync_directory(path)
                    if os.path.lexists(staging):
                        _raise("narrative_normalizer_persistence_invalid")

                # Both files are now exact but remain consumer-ineligible until
                # the prospective approved event wins the authoritative CAS.
                try:
                    precommit_claim = self.read_claim(source_ref, source_digest_value)
                    precommit_value = validate_draft_directory(
                        path,
                        expected_identity=identity,
                        expected_source=raw_source,
                        validate_ready=False,
                        trust_service=trust_service,
                        review_authority_root=self.policy.narrative_review_authority_root,
                        require_trust=True,
                    )
                    if precommit_claim is None or not self._claim_matches_draft(
                        precommit_claim,
                        precommit_value,
                    ):
                        _raise("narrative_normalizer_approval_conflict")
                    manifest = quarantine.validate_narrative_ready_manifest(
                        self.policy,
                        source_ref,
                        trust_service=trust_service,
                        expected_review_event=approved_event,
                    )
                except quarantine.QuarantineError:
                    _raise("narrative_normalizer_manifest_invalid")
                if not ledger_idempotent:
                    self._review_store().commit_prepared(approved_event)
                    ledger_idempotent = True
                try:
                    final_ledger = self._review_store().read(
                        identity,
                        expected_draft_identity=expected_draft_identity,
                    )
                    final_manifest = quarantine.validate_narrative_ready_manifest(
                        self.policy,
                        source_ref,
                        trust_service=trust_service,
                    )
                    final_claim = self.read_claim(source_ref, source_digest_value)
                    final_value = validate_draft_directory(
                        path,
                        expected_identity=identity,
                        expected_source=raw_source,
                        trust_service=trust_service,
                        review_authority_root=self.policy.narrative_review_authority_root,
                        require_trust=True,
                    )
                except (quarantine.QuarantineError, review_state.ReviewStateError):
                    _raise("narrative_normalizer_manifest_invalid")
                if (
                    final_ledger.latest != approved_event
                    or final_claim is None
                    or not self._claim_matches_draft(final_claim, final_value)
                ):
                    _raise("narrative_normalizer_approval_conflict")
                return ApprovalResult(
                    source_digest_value,
                    _sha(manifest_payload),
                    final_manifest.status,
                    False,
                )
            except BaseException as original:
                cleanup_failure = False
                cleanup_cancellation: BaseException | None = None
                # Never remove a competitor.  Ownership is proved against the
                # pre-link staging inode, and only pre-ledger failures roll back
                # this call's pair.  Once the approved CAS commits, the pair is
                # authoritative and subsequent read failures are reported
                # without destructive rollback.
                if not ledger_idempotent:
                    for target, signature in reversed(owned_targets):
                        owns_target = False
                        if os.path.lexists(target) and not target.is_symlink() and target.is_file():
                            try:
                                target_stat = os.stat(target, follow_symlinks=False)
                                owns_target = (target_stat.st_dev, target_stat.st_ino) == signature
                            except BaseException as error:
                                cleanup_failure = True
                                if not isinstance(error, Exception) and cleanup_cancellation is None:
                                    cleanup_cancellation = error
                        if owns_target:
                            try:
                                _cleanup_owned_path(target)
                                _fsync_directory(path)
                            except BaseException as error:
                                cleanup_failure = True
                                if not isinstance(error, Exception) and cleanup_cancellation is None:
                                    cleanup_cancellation = error
                for staging in (attestation_staging, ready_staging):
                    if not os.path.lexists(staging):
                        continue
                    try:
                        _cleanup_owned_path(staging)
                        _fsync_directory(path)
                    except BaseException as error:
                        cleanup_failure = True
                        if not isinstance(error, Exception) and cleanup_cancellation is None:
                            cleanup_cancellation = error
                if not isinstance(original, Exception):
                    raise
                if cleanup_cancellation is not None:
                    raise cleanup_cancellation from None
                if cleanup_failure:
                    _raise("narrative_normalizer_persistence_invalid")
                raise


def _claim_payload(
    source: SourceUnit,
    *,
    attempt_id: str,
    state: str,
    started_at: str,
    updated_at: str,
    generation_run_id: str = "",
    package_digest: str = "",
    draft_identity: str = "",
    selected_candidate_id: str = "",
    human_story_package_digest: str = "",
    factuality_binding_digest: str = "",
    adjudication_evidence_digest: str = "",
    ordered_claim_digests: tuple[str, ...] = (),
    reason_code: str = "",
    artifact_binding_digest: str = "",
) -> dict[str, object]:
    return {
        "schema_version": CLAIM_SCHEMA_VERSION,
        "source_id": hashlib.sha256(source.source_ref.encode("utf-8")).hexdigest()[:12],
        "source_ref": source.source_ref,
        "source_digest": source.source_digest,
        "source_identity": source_identity(source.source_ref, source.source_digest, source.receipt.source_contract_version),
        "attempt_id": attempt_id,
        "state": state,
        "started_at": started_at,
        "updated_at": updated_at,
        "generation_run_id": generation_run_id,
        "package_digest": package_digest,
        "draft_identity": draft_identity,
        "selected_candidate_id": selected_candidate_id,
        "human_story_package_digest": human_story_package_digest,
        "factuality_binding_digest": factuality_binding_digest,
        "adjudication_evidence_digest": adjudication_evidence_digest,
        "ordered_claim_digests": list(ordered_claim_digests),
        "reason_code": reason_code,
        "artifact_binding_digest": artifact_binding_digest,
        "key_id": "",
        "claim_seal": "",
    }


_CP2_CAPTURE_ADAPTER_VERSION = "normalizer-cp2-final-parse-capture-v1"
_CP2_CAPTURE_GENERATION_CONTRACT = "narrative-generation-contract-v1"
_CP2_CAPTURE_CALL_PARSE_PARAMETERS = ("self", "request", "parser", "calls", "repair_used")


class _EvidenceCapturingGenerationService(generation.NarrativeGenerationService):
    """Capture only CP2's final typed parser results, never raw model exchanges."""

    def __init__(self, base: generation.NarrativeGenerationService):
        if type(base) is not generation.NarrativeGenerationService:
            raise TypeError("generation_service")
        try:
            call_parse_parameters = tuple(
                inspect.signature(generation.NarrativeGenerationService._call_parse).parameters
            )
        except (TypeError, ValueError):
            raise TypeError("generation_service") from None
        if (
            generation.GENERATION_CONTRACT_VERSION != _CP2_CAPTURE_GENERATION_CONTRACT
            or call_parse_parameters != _CP2_CAPTURE_CALL_PARSE_PARAMETERS
        ):
            raise TypeError("generation_service")
        client = getattr(base, "_client", None)
        if client is None or not callable(getattr(client, "generate_json", None)):
            raise TypeError("generation_service")
        super().__init__(
            client,
            generation_model=base.generation_model,
            adjudication_model=base.adjudication_model,
            repair_model=base.configured_repair_model,
        )
        self._evidence_capture: ContextVar[tuple[tuple[str, object], ...] | None] = ContextVar(
            f"normalizer_cp2_evidence_{id(self)}",
            default=None,
        )

    def _call_parse(
        self,
        request: generation.NarrativeModelRequest,
        parser: Callable[[Mapping[str, object] | str], object],
        calls: list[str],
        repair_used: list[bool],
    ) -> object:
        parsed = super()._call_parse(request, parser, calls, repair_used)
        capture = self._evidence_capture.get()
        if capture is not None and request.request_kind in {"generation", "adjudication"}:
            self._evidence_capture.set((*capture, (request.request_kind, parsed)))
        return parsed

    def generate_with_evidence(
        self,
        context: generation.NarrativeGenerationInput,
    ) -> tuple[generation.NarrativeGenerationResult, _CapturedCP2Evidence]:
        token = self._evidence_capture.set(())
        try:
            result = super().generate(context)
            records = self._evidence_capture.get()
        finally:
            self._evidence_capture.reset(token)
        if records is None or tuple(item[0] for item in records) != ("generation", "adjudication"):
            _raise("narrative_normalizer_generation_failed")
        drafts = records[0][1]
        adjudications = records[1][1]
        if (
            type(drafts) is not tuple
            or any(type(item) is not generation.NarrativeDraft for item in drafts)
            or type(adjudications) is not generation.NarrativeAdjudicationBatch
        ):
            _raise("narrative_normalizer_generation_failed")
        return result, _CapturedCP2Evidence(drafts, adjudications)


def _provider_source_scope(
    service: generation.NarrativeGenerationService,
    source_identity_value: str,
):
    """Bind optional provider authorization without changing CP1/CP2 contracts."""

    client = getattr(service, "_client", None)
    source_scope = getattr(client, "authorized_source_scope", None)
    if source_scope is None:
        return nullcontext()
    if not callable(source_scope):
        _raise("narrative_normalizer_generation_failed")
    return source_scope(source_identity_value)


class NarrativeNormalizerService:
    def __init__(
        self,
        *,
        policy: quarantine.QuarantinePathPolicy,
        context_provider: NarrativeContextProvider,
        generation_service: generation.NarrativeGenerationService,
        evidence_service: evidence.GenericEvidenceService | None = None,
        trust_service: trust.NarrativeTrustService | None = None,
        review_authority: ReviewAuthorityTransport | None = None,
        permission_policy: outbox_permissions.NarrativeOutboxPermissionPolicy = (
            outbox_permissions.PRIVATE_POLICY
        ),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
    ):
        if type(policy) is not quarantine.QuarantinePathPolicy:
            raise TypeError("policy")
        if type(generation_service) is not generation.NarrativeGenerationService:
            raise TypeError("generation_service")
        if type(claim_lease_seconds) is not int or claim_lease_seconds < 1:
            raise TypeError("claim_lease_seconds")
        self.policy = policy
        self.context_provider = context_provider
        self.generation_service = _EvidenceCapturingGenerationService(generation_service)
        if evidence_service is not None and type(evidence_service) is not evidence.GenericEvidenceService:
            raise TypeError("evidence_service")
        self.evidence_service = evidence_service
        self.trust_service = trust_service
        self.clock = clock
        self.claim_lease_seconds = claim_lease_seconds
        self.store = NarrativeOutboxStore(
            policy,
            trust_service=trust_service,
            review_authority=review_authority,
            permission_policy=permission_policy,
        )

    def _source_id(self, source_ref: str) -> str:
        return hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:12]

    def _outcome(
        self,
        source: SourceUnit,
        status: str,
        *,
        reasons=(),
        calls=0,
        digest=None,
        review=None,
        evidence_path: str | None = None,
    ) -> NormalizationOutcome:
        return NormalizationOutcome(
            self._source_id(source.source_ref),
            source.source_digest,
            status,
            tuple(reasons),
            calls,
            digest,
            review,
            source.evidence_mode if evidence_path is None else evidence_path,
        )

    def _record_failed_claim_locked(self, source: SourceUnit, *, attempt_id: str, started_at: str, reason_code: str) -> None:
        try:
            self.store._write_claim_locked(_claim_payload(
                source,
                attempt_id=attempt_id,
                state=CLAIM_FAILED,
                started_at=started_at,
                updated_at=_now(self.clock),
                reason_code=reason_code,
            ))
        except Exception:
            return

    @_privacy_boundary("narrative_normalizer_internal_error")
    def normalize_source(
        self,
        source_ref: str,
        expected_digest: str,
        *,
        dry_run: bool = False,
        retry_uncertain: bool = False,
        retry_failed: bool = False,
    ) -> NormalizationOutcome:
        documents = read_source_documents(
            self.policy,
            source_ref,
            expected_digest=expected_digest,
        )
        structural_coverage = evidence.classify_source_bundle(documents)
        structural_outcomes = {
            "insufficient": (
                OUTCOME_SOURCE_INSUFFICIENT,
                "narrative_normalizer_source_insufficient",
            ),
            "sensitive": (
                OUTCOME_SENSITIVE_REJECTED,
                "narrative_normalizer_source_sensitive",
            ),
            "parse_error": (
                OUTCOME_MANUAL_ATTENTION,
                "narrative_normalizer_evidence_ambiguous",
            ),
            "unsupported_binary_container": (
                OUTCOME_MANUAL_ATTENTION,
                "narrative_normalizer_evidence_ambiguous",
            ),
        }
        if structural_coverage.classification in structural_outcomes:
            status, reason = structural_outcomes[structural_coverage.classification]
            return NormalizationOutcome(
                self._source_id(documents.source_ref),
                documents.source_digest,
                OUTCOME_DRY_RUN if dry_run else status,
                () if dry_run else (reason,),
                0,
                None,
                None,
                None,
            )
        source = read_source_unit(
            self.policy,
            source_ref,
            expected_digest=expected_digest,
            allow_insufficient=True,
        )
        fast_candidate = (
            len(source.facts) >= MIN_SOURCE_FACTS
            and _source_semantically_closed(source)
        )
        coverage = evidence.classify_source_bundle(
            documents,
            deterministic_fast_path=fast_candidate,
        )
        fast_path = coverage.classification == "known_deterministic_grammar"
        source = replace(source, source_documents=documents)
        if dry_run:
            return self._outcome(
                source,
                OUTCOME_DRY_RUN,
                evidence_path=(
                    "deterministic_fast_path"
                    if fast_path
                    else "generic"
                    if coverage.generic_fallback_candidate
                    else None
                ),
            )
        if coverage.classification == "insufficient":
            return self._outcome(
                source,
                OUTCOME_SOURCE_INSUFFICIENT,
                reasons=("narrative_normalizer_source_insufficient",),
                evidence_path="generic",
            )
        if coverage.classification == "sensitive":
            return self._outcome(
                source,
                OUTCOME_SENSITIVE_REJECTED,
                reasons=("narrative_normalizer_source_sensitive",),
                evidence_path="generic",
            )
        if coverage.classification in {
            "parse_error",
            "unsupported_binary_container",
        }:
            return self._outcome(
                source,
                OUTCOME_MANUAL_ATTENTION,
                reasons=("narrative_normalizer_evidence_ambiguous",),
                evidence_path="generic",
            )
        if not fast_path and self.evidence_service is None:
            return self._outcome(
                source,
                OUTCOME_MANUAL_ATTENTION,
                reasons=("narrative_normalizer_evidence_ambiguous",),
                evidence_path="generic",
            )
        try:
            fast_normalization_input = (
                build_normalization_input(source, self.context_provider)
                if fast_path
                else None
            )
        except NarrativeNormalizerError as error:
            # Context privacy is an admission check, not an exceptional batch
            # abort.  It must happen before the trust key, source lock, claim,
            # or model boundary is touched.
            return self._outcome(
                source,
                OUTCOME_FAILED,
                reasons=(error.reason_code,),
                calls=0,
                evidence_path="deterministic_fast_path",
            )
        if (
            self.policy.narrative_review_authority_root is None
            and not self.store.broker_mode
        ):
            return self._outcome(
                source,
                OUTCOME_MANUAL_ATTENTION,
                reasons=("narrative_normalizer_review_authority_unavailable",),
                evidence_path="deterministic_fast_path" if fast_path else "generic",
            )
        try:
            trust_service = _require_trust_service(self.trust_service)
        except NarrativeNormalizerError:
            return self._outcome(
                source,
                OUTCOME_MANUAL_ATTENTION,
                reasons=("narrative_normalizer_trust_unavailable",),
                evidence_path="deterministic_fast_path" if fast_path else "generic",
            )
        self.trust_service = trust_service
        self.store.trust_service = trust_service
        self.store._ensure_write_layout()
        lock = self.store.lock_for(source.source_ref, source.source_digest, blocking=False)
        if not lock.acquire():
            return self._outcome(source, OUTCOME_PROCESSING)
        attempt_id = secrets.token_hex(16)
        started_at = _now(self.clock)
        claim_started = False
        internal_failure = False
        model_calls = 0
        evidence_model_calls = 0
        cancellation: BaseException | None = None
        try:
            identity = source_identity(source.source_ref, source.source_digest, source.receipt.source_contract_version)
            final = self.store.draft_path(source.source_digest, source_ref=source.source_ref)
            if os.path.lexists(final):
                self.store._verify_draft_permissions(final)
                value = validate_draft_directory(
                    final,
                    expected_identity=identity,
                    trust_service=trust_service,
                    review_authority_root=self.policy.narrative_review_authority_root,
                    require_trust=True,
                )
                replayed_source = _replay_source_for_story(source, documents, value["story"])
                value = validate_draft_directory(
                    final,
                    expected_identity=identity,
                    expected_source=replayed_source,
                    trust_service=trust_service,
                    review_authority_root=self.policy.narrative_review_authority_root,
                    require_trust=True,
                )
                try:
                    completed_claim = self.store.read_claim(source.source_ref, source.source_digest)
                except NarrativeNormalizerError:
                    completed_claim = None
                if completed_claim is None or not self.store._claim_matches_draft(completed_claim, value):
                    return self._outcome(
                        source,
                        OUTCOME_UNCERTAIN,
                        reasons=("narrative_normalizer_claim_uncertain",),
                        digest=value["story"]["package_digest"],
                        review=value["review"]["status"],
                    )
                try:
                    if self.store.broker_mode:
                        authoritative_review = str(self.store._broker_latest(
                            identity,
                            str(value["manifest"]["draft_identity"]),
                            str(value["story"]["package_digest"]),
                        )["state"])
                    else:
                        authoritative_review = self.store._review_store().read(
                            identity,
                            expected_draft_identity=str(value["manifest"]["draft_identity"]),
                        ).latest.state
                except (review_state.ReviewStateError, NarrativeNormalizerError):
                    return self._outcome(
                        source,
                        OUTCOME_UNCERTAIN,
                        reasons=("narrative_normalizer_review_identity_conflict",),
                        digest=value["story"]["package_digest"],
                        review=value["review"]["status"],
                    )
                if authoritative_review not in {review_state.STATE_PASSED, review_state.STATE_APPROVED}:
                    return self._outcome(
                        source,
                        OUTCOME_MANUAL_ATTENTION,
                        reasons=tuple(value["review"]["reason_codes"]),
                        digest=value["story"]["package_digest"],
                        review=(
                            authoritative_review
                            if authoritative_review in REVIEW_STATES
                            else str(value["review"]["status"])
                        ),
                    )
                return self._outcome(
                    source, OUTCOME_EXISTING, digest=value["story"]["package_digest"], review=authoritative_review
                )
            previous = self.store.read_claim(source.source_ref, source.source_digest)
            if previous is not None:
                if previous["state"] == CLAIM_COMPLETED:
                    return self._outcome(
                        source,
                        OUTCOME_UNCERTAIN,
                        reasons=("narrative_normalizer_claim_uncertain",),
                    )
                if previous["state"] == CLAIM_PROCESSING:
                    age = (self.clock().astimezone(timezone.utc) - _parse_time(previous["updated_at"])).total_seconds()
                    if age >= self.claim_lease_seconds:
                        uncertain = dict(previous, state=CLAIM_UNCERTAIN, updated_at=started_at, reason_code="narrative_normalizer_claim_uncertain")
                        self.store._write_claim_locked(uncertain)
                        if not retry_uncertain:
                            return self._outcome(source, OUTCOME_UNCERTAIN, reasons=("narrative_normalizer_claim_uncertain",))
                        attempt_id = str(previous["attempt_id"])
                        started_at = str(previous["started_at"])
                    else:
                        return self._outcome(source, OUTCOME_PROCESSING)
                elif previous["state"] == CLAIM_UNCERTAIN and not retry_uncertain:
                    return self._outcome(source, OUTCOME_UNCERTAIN, reasons=("narrative_normalizer_claim_uncertain",))
                elif previous["state"] == CLAIM_UNCERTAIN:
                    attempt_id = str(previous["attempt_id"])
                    started_at = str(previous["started_at"])
                elif previous["state"] == CLAIM_FAILED and not retry_failed:
                    reason = previous["reason_code"] or "narrative_normalizer_generation_failed"
                    prior_status = (
                        OUTCOME_SOURCE_INSUFFICIENT
                        if reason == "narrative_normalizer_source_insufficient"
                        else OUTCOME_SENSITIVE_REJECTED
                        if reason == "narrative_normalizer_source_sensitive"
                        else OUTCOME_MANUAL_ATTENTION
                        if reason in {
                            "narrative_normalizer_evidence_ambiguous",
                            "narrative_normalizer_trust_unavailable",
                        }
                        else OUTCOME_FAILED
                    )
                    return self._outcome(source, prior_status, reasons=(reason,))
            processing = _claim_payload(source, attempt_id=attempt_id, state=CLAIM_PROCESSING, started_at=started_at, updated_at=started_at)
            self.store._write_claim_locked(processing)
            claim_started = True
            if not fast_path:
                assert self.evidence_service is not None
                try:
                    with _provider_source_scope(self.generation_service, identity):
                        resolution = self.evidence_service.resolve(documents)
                except evidence.EvidenceContractError:
                    reason = "narrative_normalizer_evidence_invalid"
                    self._record_failed_claim_locked(
                        source,
                        attempt_id=attempt_id,
                        started_at=started_at,
                        reason_code=reason,
                    )
                    return self._outcome(
                        source,
                        OUTCOME_FAILED,
                        reasons=(reason,),
                        calls=model_calls,
                        evidence_path="generic",
                    )
                evidence_model_calls = resolution.model_call_count
                model_calls += evidence_model_calls
                outcome_by_resolution = {
                    "source_insufficient": OUTCOME_SOURCE_INSUFFICIENT,
                    "manual_attention": OUTCOME_MANUAL_ATTENTION,
                    "sensitive_rejected": OUTCOME_SENSITIVE_REJECTED,
                    "failed": OUTCOME_FAILED,
                }
                if resolution.status != "verified":
                    status = outcome_by_resolution.get(resolution.status, OUTCOME_FAILED)
                    reason = (
                        "narrative_normalizer_source_insufficient"
                        if status == OUTCOME_SOURCE_INSUFFICIENT
                        else "narrative_normalizer_evidence_ambiguous"
                        if status == OUTCOME_MANUAL_ATTENTION
                        else "narrative_normalizer_source_sensitive"
                        if status == OUTCOME_SENSITIVE_REJECTED
                        else "narrative_normalizer_evidence_invalid"
                    )
                    self._record_failed_claim_locked(
                        source,
                        attempt_id=attempt_id,
                        started_at=started_at,
                        reason_code=reason,
                    )
                    return self._outcome(
                        source,
                        status,
                        reasons=(reason,),
                        calls=model_calls,
                        evidence_path="generic",
                    )
                if resolution.verified_bundle is None:
                    _raise("narrative_normalizer_evidence_invalid")
                source = _source_from_verified_evidence(source, documents, resolution.verified_bundle)
            normalization_input = (
                fast_normalization_input
                if fast_normalization_input is not None
                else build_normalization_input(source, self.context_provider)
            )
            with _provider_source_scope(self.generation_service, identity):
                result, captured_evidence = self.generation_service.generate_with_evidence(
                    normalization_input.generation_input
                )
            model_calls += result.model_call_count
            if result.model_call_count not in {2, 3}:
                _raise("narrative_normalizer_model_budget_exceeded")
            model_identity = _sha({
                "generation_model": self.generation_service.generation_model,
                "adjudication_model": self.generation_service.adjudication_model,
                "repair_model": self.generation_service.configured_repair_model,
            })[:24]
            artifact = assemble_draft_artifact(
                source,
                result,
                created_at=_now(self.clock),
                model_policy_identity=model_identity,
                normalization_input=normalization_input,
                captured_evidence=captured_evidence,
                generation_service=self.generation_service,
                trust_service=trust_service,
                evidence_model_call_count=evidence_model_calls,
            )
            relation = self.store.previous_supersedes(
                source,
                str(artifact.draft_manifest["draft_identity"]),
            )
            if relation is not None:
                artifact = assemble_draft_artifact(
                    source,
                    result,
                    created_at=str(artifact.draft_manifest["created_at"]),
                    model_policy_identity=model_identity,
                    normalization_input=normalization_input,
                    captured_evidence=captured_evidence,
                    generation_service=self.generation_service,
                    trust_service=trust_service,
                    evidence_model_call_count=evidence_model_calls,
                    supersedes=relation,
                )
            _, existing = self.store.persist(artifact)
            completed = _claim_payload(
                source,
                attempt_id=attempt_id,
                state=CLAIM_COMPLETED,
                started_at=started_at,
                updated_at=_now(self.clock),
                generation_run_id=result.run_id,
                package_digest=artifact.package_digest,
                draft_identity=str(artifact.draft_manifest["draft_identity"]),
                selected_candidate_id=str(artifact.story_payload["selected_candidate_id"]),
                human_story_package_digest=str(artifact.story_payload["human_story_package_digest"]),
                factuality_binding_digest=str(
                    artifact.story_payload["factuality_receipt"]["adjudication_binding_digest"]
                ),
                adjudication_evidence_digest=str(
                    artifact.story_payload["cp2_adjudication_evidence"]["evidence_digest"]
                ),
                ordered_claim_digests=tuple(
                    artifact.story_payload["factuality_receipt"]["claim_digests"]
                ),
                artifact_binding_digest=artifact.artifact_binding_digest,
            )
            self.store._write_claim_locked(completed)
            if self.store.broker_mode:
                try:
                    self.store.register_draft_with_authority(
                        source.source_ref,
                        source.source_digest,
                    )
                except NarrativeNormalizerError as error:
                    # The immutable local draft remains available for manual
                    # review, but it is not authority-registered and can never
                    # create a ready pair through an automatic local fallback.
                    return self._outcome(
                        source,
                        OUTCOME_MANUAL_ATTENTION,
                        reasons=(error.reason_code,),
                        calls=artifact.model_call_count,
                        digest=artifact.package_digest,
                        review=artifact.review_status,
                    )
            if artifact.review_status != REVIEW_PASSED:
                return self._outcome(
                    source,
                    OUTCOME_MANUAL_ATTENTION,
                    reasons=tuple(artifact.review_payload["reason_codes"]),
                    calls=artifact.model_call_count,
                    digest=artifact.package_digest,
                    review=artifact.review_status,
                )
            return self._outcome(
                source,
                OUTCOME_EXISTING if existing else OUTCOME_CREATED,
                calls=artifact.model_call_count,
                digest=artifact.package_digest,
                review=artifact.review_status,
            )
        except NarrativeNormalizerError as error:
            if claim_started:
                self._record_failed_claim_locked(
                    source, attempt_id=attempt_id, started_at=started_at, reason_code=error.reason_code
                )
            status = (
                OUTCOME_SOURCE_INSUFFICIENT
                if error.reason_code == "narrative_normalizer_source_insufficient"
                else OUTCOME_FAILED
            )
            return self._outcome(
                source,
                status,
                reasons=(error.reason_code,),
                calls=model_calls,
                evidence_path="deterministic_fast_path" if fast_path else "generic",
            )
        except generation.NarrativeGenerationError:
            reason = "narrative_normalizer_generation_failed"
            if claim_started:
                self._record_failed_claim_locked(source, attempt_id=attempt_id, started_at=started_at, reason_code=reason)
            return self._outcome(source, OUTCOME_FAILED, reasons=(reason,), calls=model_calls)
        except Exception:
            if claim_started:
                self._record_failed_claim_locked(
                    source,
                    attempt_id=attempt_id,
                    started_at=started_at,
                    reason_code="narrative_normalizer_internal_error",
                )
            internal_failure = True
        except (KeyboardInterrupt, SystemExit, GeneratorExit) as error:
            cancellation = error
            raise
        finally:
            try:
                lock.release()
            except BaseException:
                if cancellation is not None:
                    raise cancellation from None
                raise
        if internal_failure:
            raise NarrativeNormalizerError("narrative_normalizer_internal_error") from None
        raise AssertionError("unreachable")

    def normalize_batch(
        self,
        rows: Sequence[tuple[str, str]],
        *,
        limit: int | None = None,
        max_workers: int = DEFAULT_MAX_WORKERS,
        dry_run: bool = False,
        retry_uncertain: bool = False,
        retry_failed: bool = False,
    ) -> BatchResult:
        if type(max_workers) is not int or not 1 <= max_workers <= MAX_WORKERS:
            raise TypeError("max_workers")
        ordered = tuple(sorted(rows))
        if limit is not None:
            if type(limit) is not int or limit < 1:
                raise TypeError("limit")
            ordered = ordered[:limit]
        if max_workers == 1:
            serial: list[NormalizationOutcome] = []
            for ref, digest in ordered:
                try:
                    serial.append(self.normalize_source(
                        ref,
                        digest,
                        dry_run=dry_run,
                        retry_uncertain=retry_uncertain,
                        retry_failed=retry_failed,
                    ))
                except NarrativeNormalizerError as error:
                    serial.append(NormalizationOutcome(
                        self._source_id(ref), digest, OUTCOME_FAILED, (error.reason_code,), 0, None, None
                    ))
            outcomes = tuple(serial)
        else:
            indexed: dict[int, NormalizationOutcome] = {}
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="narrative-normalizer") as pool:
                futures = {
                    pool.submit(
                        self.normalize_source, ref, digest,
                        dry_run=dry_run, retry_uncertain=retry_uncertain, retry_failed=retry_failed,
                    ): index
                    for index, (ref, digest) in enumerate(ordered)
                }
                for future in as_completed(futures):
                    index = futures[future]
                    try:
                        indexed[index] = future.result()
                    except NarrativeNormalizerError as error:
                        ref, digest = ordered[index]
                        indexed[index] = NormalizationOutcome(
                            self._source_id(ref), digest, OUTCOME_FAILED, (error.reason_code,), 0, None, None
                        )
            outcomes = tuple(indexed[index] for index in range(len(ordered)))
        return BatchResult(len(ordered), outcomes)


def load_adapter(
    spec: str,
) -> tuple[NarrativeContextProvider, generation.NarrativeGenerationService] | tuple[
    NarrativeContextProvider,
    generation.NarrativeGenerationService,
    evidence.GenericEvidenceService,
]:
    """Load an explicitly requested local dependency-injection factory."""
    if type(spec) is not str or spec.count(":") != 1 or spec != spec.strip():
        _raise("narrative_normalizer_cli_invalid")
    module_name, attribute = spec.split(":", 1)
    dotted_name = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\Z")
    simple_name = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
    if (
        dotted_name.fullmatch(module_name) is None
        or simple_name.fullmatch(attribute) is None
        or module_name.split(".", 1)[0] in {"main", "story_production"}
    ):
        _raise("narrative_normalizer_cli_invalid")
    invalid = False
    value: object = None
    try:
        factory = getattr(importlib.import_module(module_name), attribute)
        value = factory()
    except Exception:
        invalid = True
    if invalid:
        _raise("narrative_normalizer_cli_invalid")
    if type(value) is not tuple or len(value) not in {2, 3}:
        _raise("narrative_normalizer_cli_invalid")
    provider, service = value[:2]
    if type(service) is not generation.NarrativeGenerationService:
        _raise("narrative_normalizer_cli_invalid")
    if len(value) == 3 and type(value[2]) is not evidence.GenericEvidenceService:
        _raise("narrative_normalizer_cli_invalid")
    return value


def safe_error(error: BaseException) -> dict[str, object]:
    reason = error.reason_code if type(error) is NarrativeNormalizerError else "narrative_normalizer_internal_error"
    return {"ok": False, "reason_code": reason}
