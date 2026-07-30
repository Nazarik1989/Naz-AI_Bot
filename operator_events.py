"""Privacy-safe shadow import and plan binding for OperatorEvent sidecars.

Phase 2A deliberately keeps this module outside the editorial and media
contracts.  It validates deterministic ``operator-event-set.v1`` sidecars and
stores one private, immutable binding record per grounded story member.  No
event content is added to prompts or production manifests here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4


OPERATOR_EVENT_CONTRACT = "operator-event-set.v1"
OPERATOR_EVENT_BINDING_CONTRACT = "operator-event-binding.v2"
OPERATOR_EVENT_BINDING_SCOPE = "agent_content_date_member"
CHARACTER_REELS_MODES = frozenset({"off", "shadow", "manual"})

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TOPIC_ID_RE = re.compile(r"[a-f0-9]{12,64}\Z")
_SOURCE_HASH_RE = re.compile(r"[a-f0-9]{64}\Z")
_EVENT_ID_RE = re.compile(r"oev-[a-f0-9]{24}\Z")
_SESSION_REF_RE = re.compile(r"session-[a-f0-9]{24}\Z")
_MESSAGE_REF_RE = re.compile(r"message-[a-f0-9]{24}\Z")
_PLAN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,79}\Z")
_REASON_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_]{2,79}\Z")
_EVENT_TYPE_RE = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")
_TOPIC_LINE_RE = re.compile(
    r"^\s*(?:Тема-ID|Topic-ID|topic_id)\s*:\s*(?:t-)?(?P<value>[a-f0-9]{12,64})\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_SOURCE_HASH_LINE_RE = re.compile(
    r"^\s*(?:Источник-хеш|Source[- ]hash|source_hash)\s*:\s*"
    r"(?:sha256:)?(?P<value>[a-f0-9]{64})\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_RAW_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?:\b[A-Za-z]:[\\/]|(?<![A-Za-z0-9_])/(?:home|root|opt|etc|srv|Users|tmp|"
    r"var/(?:tmp|lib|log|www))/)",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?:\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9_]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,})\b|"
    r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b|"
    r"\b(?:api[_ -]?key|access[_ -]?token|secret|password|passwd)"
    r"\s*[:=]\s*['\"]?[^'\"\s]{6,})",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IP_ADDRESS_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PRIVATE_URL_RE = re.compile(
    r"\bhttps?://(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|"
    r"192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[0-1])\.\d+\.\d+|"
    r"[^\s/]+\.internal)[^\s)]*",
    re.IGNORECASE,
)
_SSH_TARGET_RE = re.compile(
    r"\b(?:ssh\s+)?[A-Za-z0-9._-]+@(?:[A-Za-z0-9.-]+|(?:\d{1,3}\.){3}\d{1,3})\b",
    re.IGNORECASE,
)
_SENSITIVE_KEYWORD_RE = re.compile(
    r"\b(?:client|customer|nda|internal prompt|production secret|private project|"
    r"коммерческая тайна|клиент|пароль|секрет|токен)\b",
    re.IGNORECASE,
)

_ENVELOPE_KEYS = {
    "contract_version", "project", "date", "topic_id", "source_hash", "events",
}
_EVENT_KEYS = {
    "event_id", "event_type", "occurred_at", "source_session_refs",
    "source_message_refs", "event_facts", "operator_commentary",
    "publication_copy_ref", "privacy_status", "content_status", "reason_codes",
}
_FACT_KEYS = {
    "event_summary", "initial_state", "trigger", "initial_assumption",
    "actual_cause", "change", "evidence", "technical_result",
}
_COMMENTARY_KEYS = {"human_consequence", "lesson", "open_questions"}
_SCALAR_FACT_KEYS = tuple(sorted(_FACT_KEYS - {"evidence"}))


class OperatorEventValidationError(ValueError):
    """A fail-closed validation error carrying only safe reason codes."""

    def __init__(self, *reason_codes: str) -> None:
        self.reason_codes = _dedupe_codes(reason_codes or ("operator_event_invalid",))
        super().__init__(",".join(self.reason_codes))


@dataclass(frozen=True)
class ValidatedOperatorEventSet:
    project: str
    date: str
    topic_id: str
    source_hash: str
    event: Mapping[str, Any]
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class OperatorEventBindingResult:
    mode: str
    plan_id: str
    status: str
    event_id: str | None
    content_status: str | None
    privacy_status: str | None
    reason_codes: tuple[str, ...]
    record_path: Path | None
    created: bool
    write_status: str = "already"

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "plan_id": self.plan_id,
            "status": self.status,
            "event_id": self.event_id,
            "content_status": self.content_status,
            "privacy_status": self.privacy_status,
            "reason_codes": list(self.reason_codes),
            "record_path": str(self.record_path) if self.record_path else None,
            "created": self.created,
            "write_status": self.write_status,
        }


@dataclass(frozen=True)
class StorySourceEntry:
    topic_id: str
    source_hash: str
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class StorySourceCatalog:
    entries: tuple[StorySourceEntry, ...]
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class OperatorEventBindingBatchResult:
    mode: str
    plan_id: str
    outcomes: tuple[OperatorEventBindingResult, ...]
    reason_codes: tuple[str, ...] = ()

    @property
    def discovered_count(self) -> int:
        return len(self.outcomes)

    @property
    def bound_count(self) -> int:
        return sum(
            item.status == "bound" and item.write_status in {"created", "updated"}
            for item in self.outcomes
        )

    @property
    def already_bound_count(self) -> int:
        return sum(
            item.status == "bound" and item.write_status == "already"
            for item in self.outcomes
        )

    @property
    def rejected_count(self) -> int:
        return sum(item.status == "rejected" for item in self.outcomes)

    @property
    def total_bound_count(self) -> int:
        return sum(item.status == "bound" for item in self.outcomes)

    @property
    def created_count(self) -> int:
        return sum(item.created for item in self.outcomes)

    @property
    def reason_codes_by_event(self) -> dict[str, tuple[str, ...]]:
        return {
            item.event_id: item.reason_codes
            for item in self.outcomes
            if item.event_id is not None
        }

    # Compatibility properties deliberately expose a value only when the batch
    # really contains one member.  They never choose or collapse a multi-topic
    # batch.
    @property
    def status(self) -> str:
        if self.mode == "off":
            return "disabled"
        if len(self.outcomes) == 1:
            return self.outcomes[0].status
        if not self.outcomes:
            return "rejected"
        if self.total_bound_count and self.rejected_count:
            return "partial"
        return "bound" if self.total_bound_count else "rejected"

    @property
    def event_id(self) -> str | None:
        return self.outcomes[0].event_id if len(self.outcomes) == 1 else None

    @property
    def content_status(self) -> str | None:
        return self.outcomes[0].content_status if len(self.outcomes) == 1 else None

    @property
    def privacy_status(self) -> str | None:
        return self.outcomes[0].privacy_status if len(self.outcomes) == 1 else None

    @property
    def record_path(self) -> Path | None:
        return self.outcomes[0].record_path if len(self.outcomes) == 1 else None

    @property
    def created(self) -> bool:
        return self.outcomes[0].created if len(self.outcomes) == 1 else False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "plan_id": self.plan_id,
            "status": self.status,
            "discovered_count": self.discovered_count,
            "bound_count": self.bound_count,
            "already_bound_count": self.already_bound_count,
            "rejected_count": self.rejected_count,
            "created_count": self.created_count,
            "reason_codes": list(self.reason_codes),
            "reason_codes_by_event": {
                event_id: list(codes)
                for event_id, codes in self.reason_codes_by_event.items()
            },
            "outcomes": [item.to_dict() for item in self.outcomes],
        }


def normalize_character_reels_mode(value: object) -> str:
    mode = str(value or "off").strip().casefold()
    return mode if mode in CHARACTER_REELS_MODES else "off"


def expected_event_id(project: str, date_text: str, topic_id: str, source_hash: str) -> str:
    seed = "\0".join((OPERATOR_EVENT_CONTRACT, project, date_text, topic_id, source_hash))
    return "oev-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]


def read_story_source_catalog(story_dirs: Iterable[Path]) -> StorySourceCatalog:
    """Read privacy-safe topic/hash pairs without collapsing date members."""

    pair_counts: dict[tuple[str, str], int] = {}
    topic_hashes: dict[str, set[str]] = {}
    batch_codes: list[str] = []
    paths: list[Path] = []
    for raw_dir in story_dirs:
        day_dir = Path(raw_dir)
        if not day_dir.exists() or not day_dir.is_dir() or day_dir.is_symlink():
            continue
        paths.extend(
            path for path in day_dir.rglob("*.md")
            if path.is_file() and not path.is_symlink()
        )
    for path in sorted(paths, key=lambda item: item.as_posix().casefold())[:512]:
        try:
            if path.stat().st_size > 512_000:
                batch_codes.append("operator_event_story_metadata_invalid")
                continue
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError):
            batch_codes.append("operator_event_story_metadata_unreadable")
            continue
        topic_match = _TOPIC_LINE_RE.search(text)
        hash_match = _SOURCE_HASH_LINE_RE.search(text)
        if not topic_match or not hash_match:
            batch_codes.append("operator_event_story_metadata_missing")
            continue
        topic_id = topic_match.group("value").casefold()
        source_hash = hash_match.group("value").casefold()
        pair = (topic_id, source_hash)
        pair_counts[pair] = pair_counts.get(pair, 0) + 1
        topic_hashes.setdefault(topic_id, set()).add(source_hash)

    entries: list[StorySourceEntry] = []
    for topic_id, source_hash in sorted(pair_counts):
        codes: list[str] = []
        if pair_counts[(topic_id, source_hash)] > 1:
            codes.append("operator_event_story_metadata_duplicate")
        if len(topic_hashes[topic_id]) > 1:
            codes.append("operator_event_story_metadata_ambiguous")
        entries.append(
            StorySourceEntry(
                topic_id=topic_id,
                source_hash=source_hash,
                reason_codes=_dedupe_codes_allow_empty(codes),
            )
        )
    if not entries:
        batch_codes.append("operator_event_story_metadata_missing")
    return StorySourceCatalog(
        entries=tuple(entries),
        reason_codes=_dedupe_codes_allow_empty(batch_codes),
    )


def read_story_source_index(story_dirs: Iterable[Path]) -> dict[str, frozenset[str]]:
    """Compatibility view of the source catalog for read-only callers."""

    index: dict[str, set[str]] = {}
    for entry in read_story_source_catalog(story_dirs).entries:
        index.setdefault(entry.topic_id, set()).add(entry.source_hash)
    return {key: frozenset(value) for key, value in index.items()}


def validate_operator_event_set(
    payload: object,
    *,
    expected_project: str | None = None,
    expected_date: str | None = None,
) -> ValidatedOperatorEventSet:
    if not isinstance(payload, dict) or set(payload) != _ENVELOPE_KEYS:
        raise OperatorEventValidationError("operator_event_envelope_schema_invalid")
    if payload.get("contract_version") != OPERATOR_EVENT_CONTRACT:
        raise OperatorEventValidationError("operator_event_contract_version_invalid")

    project = _validate_component(payload.get("project"), "operator_event_project_invalid")
    date_text = _validate_date(payload.get("date"))
    topic_id = _validate_pattern(
        payload.get("topic_id"), _TOPIC_ID_RE, "operator_event_topic_id_invalid"
    )
    source_hash = _validate_pattern(
        payload.get("source_hash"), _SOURCE_HASH_RE, "operator_event_source_hash_invalid"
    )
    if expected_project is not None and project != expected_project:
        raise OperatorEventValidationError("operator_event_project_mismatch")
    if expected_date is not None and date_text != expected_date:
        raise OperatorEventValidationError("operator_event_date_mismatch")

    events = payload.get("events")
    if not isinstance(events, list) or len(events) != 1:
        raise OperatorEventValidationError("operator_event_selection_ambiguous")
    event = _validate_event(events[0], project, date_text, topic_id, source_hash)
    normalized = json.loads(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )
    return ValidatedOperatorEventSet(
        project=project,
        date=date_text,
        topic_id=topic_id,
        source_hash=source_hash,
        event=normalized["events"][0],
        payload=normalized,
    )


def bind_plan_to_operator_events(
    *,
    mode: str,
    source_root: Path,
    private_root: Path,
    markdown_root: Path,
    project: str,
    date_text: str,
    plan_id: str,
    story_dirs: Sequence[Path],
) -> OperatorEventBindingBatchResult:
    """Validate and persist every grounded date member independently."""

    normalized_mode = normalize_character_reels_mode(mode)
    if normalized_mode == "off":
        return OperatorEventBindingBatchResult(
            mode="off",
            plan_id=str(plan_id),
            outcomes=(),
            reason_codes=("operator_events_off",),
        )

    _validate_separate_roots(
        source_root=Path(source_root),
        private_root=Path(private_root),
        markdown_root=Path(markdown_root),
    )
    safe_plan_id = _validate_pattern(plan_id, _PLAN_ID_RE, "operator_event_plan_id_invalid")
    safe_project = _validate_component(project, "operator_event_project_invalid")
    safe_date = _validate_date(date_text)
    _validate_private_plan_root(Path(private_root), safe_plan_id)
    catalog = read_story_source_catalog(story_dirs)
    batch_codes = list(catalog.reason_codes)
    source_day = Path(source_root) / safe_project / safe_date
    expected_names = {f"t-{entry.topic_id}.json" for entry in catalog.entries}
    batch_codes.extend(_unmatched_sidecar_codes(source_day, expected_names))
    legacy_target = Path(private_root) / f"{safe_plan_id}.json"
    if legacy_target.exists() or legacy_target.is_symlink():
        batch_codes.append("legacy_single_binding_present")

    discovered = tuple(
        (
            entry,
            expected_event_id(
                safe_project, safe_date, entry.topic_id, entry.source_hash
            ),
        )
        for entry in catalog.entries
    )
    event_id_counts: dict[str, int] = {}
    for _, event_id in discovered:
        event_id_counts[event_id] = event_id_counts.get(event_id, 0) + 1

    outcomes: list[OperatorEventBindingResult] = []
    for entry, event_id in discovered:
        if event_id_counts[event_id] > 1:
            collision_codes = ["operator_event_duplicate_binding_conflict"]
            if normalized_mode == "manual":
                collision_codes.append("character_manual_phase_not_implemented")
            outcomes.append(
                OperatorEventBindingResult(
                    mode=normalized_mode,
                    plan_id=safe_plan_id,
                    status="rejected",
                    event_id=event_id,
                    content_status=None,
                    privacy_status=None,
                    reason_codes=_dedupe_codes(collision_codes),
                    record_path=None,
                    created=False,
                    write_status="conflict",
                )
            )
            continue
        selected: ValidatedOperatorEventSet | None = None
        select_codes = entry.reason_codes
        if not select_codes:
            selected, select_codes = _evaluate_sidecar(
                source_root=Path(source_root),
                project=safe_project,
                date_text=safe_date,
                topic_id=entry.topic_id,
                source_hash=entry.source_hash,
            )

        event: Mapping[str, Any] | None = selected.event if selected else None
        content_status = str(event["content_status"]) if event else None
        privacy_status = str(event["privacy_status"]) if event else None
        event_codes = list(select_codes)
        if event:
            event_codes.extend(str(item) for item in event.get("reason_codes", ()))
            event_codes.append("operator_event_bound")
            if content_status == "needs_review":
                event_codes.append("operator_event_content_needs_review")
            if privacy_status == "needs_review":
                event_codes.append("operator_event_privacy_needs_review")
        if normalized_mode == "manual":
            event_codes.append("character_manual_phase_not_implemented")
        reason_codes = _dedupe_codes(event_codes)
        record: dict[str, Any] = {
            "contract_version": OPERATOR_EVENT_BINDING_CONTRACT,
            "binding_scope": OPERATOR_EVENT_BINDING_SCOPE,
            "mode": normalized_mode,
            "plan_id": safe_plan_id,
            "project": safe_project,
            "date": safe_date,
            "binding_status": "bound" if selected else "rejected",
            "event_id": event_id,
            "topic_id": entry.topic_id,
            "source_hash": entry.source_hash,
            "content_status": content_status,
            "privacy_status": privacy_status,
            "reason_codes": list(reason_codes),
            "operator_event": dict(event) if event else None,
        }
        target = _private_record_target(
            Path(private_root), safe_plan_id, event_id
        )
        stored_record, created, conflict, write_status = _store_private_record(
            target, record
        )
        if conflict:
            outcomes.append(
                OperatorEventBindingResult(
                    mode=normalized_mode,
                    plan_id=safe_plan_id,
                    status="rejected",
                    event_id=event_id,
                    content_status=None,
                    privacy_status=None,
                    reason_codes=("operator_event_plan_binding_conflict",),
                    record_path=target,
                    created=False,
                    write_status="conflict",
                )
            )
            continue
        outcomes.append(
            _binding_result_from_record(
                stored_record,
                mode=normalized_mode,
                plan_id=safe_plan_id,
                target=target,
                created=created,
                write_status=write_status,
            )
        )

    if normalized_mode == "manual" and not outcomes:
        batch_codes.append("character_manual_phase_not_implemented")
    return OperatorEventBindingBatchResult(
        mode=normalized_mode,
        plan_id=safe_plan_id,
        outcomes=tuple(outcomes),
        reason_codes=_dedupe_codes_allow_empty(batch_codes),
    )


def bind_plan_to_operator_event(**kwargs: Any) -> OperatorEventBindingBatchResult:
    """Compatibility alias returning the complete batch, never one chosen event."""

    return bind_plan_to_operator_events(**kwargs)


def _evaluate_sidecar(
    *,
    source_root: Path,
    project: str,
    date_text: str,
    topic_id: str,
    source_hash: str,
) -> tuple[ValidatedOperatorEventSet | None, tuple[str, ...]]:
    day_dir = source_root / project / date_text
    if not day_dir.exists() or not day_dir.is_dir() or day_dir.is_symlink():
        return None, ("operator_event_sidecar_missing",)
    path = day_dir / f"t-{topic_id}.json"
    if not path.exists():
        return None, ("operator_event_sidecar_missing",)
    try:
        payload = _read_sidecar(path)
        validated = validate_operator_event_set(
            payload, expected_project=project, expected_date=date_text
        )
    except OperatorEventValidationError as exc:
        return None, exc.reason_codes
    if validated.topic_id != topic_id:
        return None, ("operator_event_topic_id_mismatch",)
    if validated.source_hash != source_hash:
        return None, ("operator_event_source_hash_mismatch",)
    if "ambiguous_event_boundary" in validated.event.get("reason_codes", ()):
        return None, ("ambiguous_event_boundary",)
    return validated, ()


def _unmatched_sidecar_codes(
    day_dir: Path, expected_names: set[str]
) -> tuple[str, ...]:
    if not day_dir.exists() or not day_dir.is_dir() or day_dir.is_symlink():
        return ()
    try:
        paths = tuple(day_dir.glob("*.json"))
    except OSError:
        return ("operator_event_sidecar_directory_unreadable",)
    if any(path.name not in expected_names for path in paths):
        return ("operator_event_unmatched_sidecar",)
    return ()


def _private_record_target(private_root: Path, plan_id: str, event_id: str) -> Path:
    _validate_pattern(plan_id, _PLAN_ID_RE, "operator_event_plan_id_invalid")
    _validate_pattern(event_id, _EVENT_ID_RE, "operator_event_id_invalid")
    _validate_private_plan_root(private_root, plan_id)
    plan_dir = private_root / plan_id
    return plan_dir / f"{event_id}.json"


def _validate_private_plan_root(private_root: Path, plan_id: str) -> None:
    if private_root.is_symlink() or (
        private_root.exists() and not private_root.is_dir()
    ):
        raise OperatorEventValidationError("operator_event_private_root_invalid")
    plan_dir = private_root / plan_id
    if plan_dir.is_symlink() or (plan_dir.exists() and not plan_dir.is_dir()):
        raise OperatorEventValidationError("operator_event_private_root_invalid")
    try:
        root_resolved = private_root.resolve(strict=False)
        plan_dir.resolve(strict=False).relative_to(root_resolved)
    except (OSError, ValueError) as exc:
        raise OperatorEventValidationError("operator_event_private_root_invalid") from exc


def _binding_result_from_record(
    record: Mapping[str, Any],
    *,
    mode: str,
    plan_id: str,
    target: Path,
    created: bool,
    write_status: str,
) -> OperatorEventBindingResult:
    return OperatorEventBindingResult(
        mode=str(record.get("mode", mode)),
        plan_id=plan_id,
        status=str(record.get("binding_status", "rejected")),
        event_id=(
            str(record.get("event_id"))
            if isinstance(record.get("event_id"), str)
            else None
        ),
        content_status=(
            str(record.get("content_status"))
            if isinstance(record.get("content_status"), str)
            else None
        ),
        privacy_status=(
            str(record.get("privacy_status"))
            if isinstance(record.get("privacy_status"), str)
            else None
        ),
        reason_codes=_dedupe_codes(record.get("reason_codes", ())),
        record_path=target,
        created=created,
        write_status=write_status,
    )


def _read_sidecar(path: Path) -> object:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 512_000:
            raise OperatorEventValidationError("operator_event_sidecar_file_invalid")
        return json.loads(path.read_text(encoding="utf-8", errors="strict"))
    except OperatorEventValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorEventValidationError("operator_event_sidecar_parse_failed") from exc


def _validate_event(
    raw: object,
    project: str,
    date_text: str,
    topic_id: str,
    source_hash: str,
) -> Mapping[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _EVENT_KEYS:
        raise OperatorEventValidationError("operator_event_schema_invalid")
    event_id = _validate_pattern(raw.get("event_id"), _EVENT_ID_RE, "operator_event_id_invalid")
    if event_id != expected_event_id(project, date_text, topic_id, source_hash):
        raise OperatorEventValidationError("operator_event_id_binding_invalid")
    _validate_pattern(raw.get("event_type"), _EVENT_TYPE_RE, "operator_event_type_invalid")
    _validate_occurred_at(raw.get("occurred_at"))
    session_refs = _validate_ref_list(
        raw.get("source_session_refs"), _SESSION_REF_RE,
        "operator_event_session_refs_invalid", required=True,
    )
    message_refs = _validate_ref_list(
        raw.get("source_message_refs"), _MESSAGE_REF_RE,
        "operator_event_message_refs_invalid", required=True,
    )
    del session_refs

    facts = raw.get("event_facts")
    if not isinstance(facts, dict) or set(facts) != _FACT_KEYS:
        raise OperatorEventValidationError("operator_event_facts_schema_invalid")
    for fact_name in _SCALAR_FACT_KEYS:
        _validate_fact(facts.get(fact_name), message_refs, fact_name)
    evidence = facts.get("evidence")
    if not isinstance(evidence, list) or len(evidence) > 8:
        raise OperatorEventValidationError("operator_event_evidence_schema_invalid")
    for item in evidence:
        value, refs = _validate_fact(item, message_refs, "evidence", require_value=True)
        if value is None or not refs:
            raise OperatorEventValidationError("operator_event_evidence_grounding_invalid")

    commentary = raw.get("operator_commentary")
    if not isinstance(commentary, dict) or set(commentary) != _COMMENTARY_KEYS:
        raise OperatorEventValidationError("operator_event_commentary_schema_invalid")
    for key in ("human_consequence", "lesson"):
        value = commentary.get(key)
        if value is not None:
            _validate_safe_text(value, f"operator_event_{key}_invalid", limit=800)
    questions = commentary.get("open_questions")
    if not isinstance(questions, list) or len(questions) > 8:
        raise OperatorEventValidationError("operator_event_open_questions_invalid")
    for question in questions:
        _validate_safe_text(question, "operator_event_open_questions_invalid", limit=500)

    if raw.get("publication_copy_ref") is not None:
        raise OperatorEventValidationError("operator_event_publication_copy_forbidden")
    privacy_status = raw.get("privacy_status")
    if privacy_status not in {"clear", "masked", "needs_review"}:
        raise OperatorEventValidationError("operator_event_privacy_status_invalid")
    content_status = raw.get("content_status")
    if content_status not in {"ready", "needs_review"}:
        raise OperatorEventValidationError("operator_event_content_status_invalid")
    reason_codes = _validate_reason_codes(raw.get("reason_codes"))
    if content_status == "needs_review" and not reason_codes:
        raise OperatorEventValidationError("operator_event_review_reason_missing")
    if privacy_status != "clear" and content_status != "needs_review":
        raise OperatorEventValidationError("operator_event_privacy_review_status_invalid")

    proof_requirements = (
        (facts["actual_cause"].get("value") is None, "actual_cause_unconfirmed"),
        (not evidence, "evidence_unconfirmed"),
        (facts["technical_result"].get("value") is None, "technical_result_unconfirmed"),
    )
    missing_proof_codes = tuple(
        reason for missing, reason in proof_requirements if missing
    )
    if missing_proof_codes:
        if content_status != "needs_review":
            raise OperatorEventValidationError("operator_event_proof_status_invalid")
        if any(reason not in reason_codes for reason in missing_proof_codes):
            raise OperatorEventValidationError("operator_event_proof_reason_missing")

    summary_value = facts["event_summary"].get("value")
    summary_refs = facts["event_summary"].get("source_message_refs")
    if summary_value is None:
        if content_status != "needs_review" or summary_refs:
            raise OperatorEventValidationError("operator_event_summary_missing")
        if not {"event_summary_unconfirmed", "event_summary_privacy_blocked"}.intersection(
            reason_codes
        ):
            raise OperatorEventValidationError("operator_event_summary_reason_missing")
    elif not summary_refs:
        raise OperatorEventValidationError("operator_event_summary_grounding_invalid")
    return raw


def _validate_fact(
    raw: object,
    event_message_refs: frozenset[str],
    fact_name: str,
    *,
    require_value: bool = False,
) -> tuple[str | None, frozenset[str]]:
    if not isinstance(raw, dict) or set(raw) != {"value", "source_message_refs"}:
        raise OperatorEventValidationError(f"operator_event_{fact_name}_schema_invalid")
    value = raw.get("value")
    if value is not None:
        value = _validate_safe_text(
            value, f"operator_event_{fact_name}_value_invalid", limit=1000
        )
    elif require_value:
        raise OperatorEventValidationError(f"operator_event_{fact_name}_value_missing")
    refs = _validate_ref_list(
        raw.get("source_message_refs"), _MESSAGE_REF_RE,
        f"operator_event_{fact_name}_refs_invalid", required=value is not None,
    )
    if not refs.issubset(event_message_refs):
        raise OperatorEventValidationError(f"operator_event_{fact_name}_refs_unknown")
    if value is None and refs:
        raise OperatorEventValidationError(f"operator_event_{fact_name}_null_with_refs")
    return value, refs


def _validate_ref_list(
    raw: object,
    pattern: re.Pattern[str],
    reason_code: str,
    *,
    required: bool,
) -> frozenset[str]:
    if not isinstance(raw, list) or len(raw) > 128 or (required and not raw):
        raise OperatorEventValidationError(reason_code)
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not pattern.fullmatch(item):
            raise OperatorEventValidationError(reason_code)
        values.append(item)
    if len(values) != len(set(values)):
        raise OperatorEventValidationError(reason_code)
    return frozenset(values)


def _validate_reason_codes(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list) or len(raw) > 32:
        raise OperatorEventValidationError("operator_event_reason_codes_invalid")
    values = []
    for item in raw:
        if not isinstance(item, str) or not _REASON_CODE_RE.fullmatch(item):
            raise OperatorEventValidationError("operator_event_reason_codes_invalid")
        values.append(item)
    if len(values) != len(set(values)):
        raise OperatorEventValidationError("operator_event_reason_codes_invalid")
    return tuple(values)


def _validate_occurred_at(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 64:
        raise OperatorEventValidationError("operator_event_occurred_at_invalid")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OperatorEventValidationError("operator_event_occurred_at_invalid") from exc


def _validate_date(value: object) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise OperatorEventValidationError("operator_event_date_invalid")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise OperatorEventValidationError("operator_event_date_invalid") from exc
    return value


def _validate_component(value: object, reason_code: str) -> str:
    if not isinstance(value, str):
        raise OperatorEventValidationError(reason_code)
    clean = value.strip()
    if (
        not clean or clean in {".", ".."} or len(clean) > 120
        or "/" in clean or "\\" in clean
        or any(ord(char) < 32 for char in clean)
    ):
        raise OperatorEventValidationError(reason_code)
    return clean


def _validate_pattern(value: object, pattern: re.Pattern[str], reason_code: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise OperatorEventValidationError(reason_code)
    return value


def _validate_safe_text(value: object, reason_code: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise OperatorEventValidationError(reason_code)
    clean = " ".join(value.split())
    if (
        not clean or len(clean) > limit or "\x00" in clean
        or "\n" in value or "\r" in value
        or _RAW_UUID_RE.search(clean) or _ABSOLUTE_PATH_RE.search(clean)
        or _SECRET_RE.search(clean) or _EMAIL_RE.search(clean)
        or _IP_ADDRESS_RE.search(clean) or _PRIVATE_URL_RE.search(clean)
        or _SSH_TARGET_RE.search(clean) or _SENSITIVE_KEYWORD_RE.search(clean)
    ):
        raise OperatorEventValidationError(reason_code)
    return clean


def _dedupe_codes(values: Iterable[object]) -> tuple[str, ...]:
    return _dedupe_codes_allow_empty(values) or ("operator_event_invalid",)


def _dedupe_codes_allow_empty(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for item in values:
        code = str(item).strip().casefold()
        if _REASON_CODE_RE.fullmatch(code) and code not in result:
            result.append(code)
    return tuple(result)


def _store_private_record(
    target: Path,
    record: Mapping[str, Any],
) -> tuple[Mapping[str, Any], bool, bool, str]:
    """Atomically keep one event record while allowing rejected-to-bound recovery."""

    payload = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if parent.is_symlink():
        raise OperatorEventValidationError("operator_event_private_root_invalid")
    for private_dir in (parent.parent, parent):
        try:
            private_dir.chmod(0o700)
        except OSError:
            pass

    lock = parent / f".{target.name}.lock"
    with _advisory_record_lock(lock):
        existing = _read_private_record(target)
        if existing is not None:
            existing_payload = _canonical_record(existing)
            if existing_payload == payload:
                return existing, False, False, "already"
            transition = _binding_transition(existing, record)
            if transition == "retain":
                # Keep immutable accepted bytes, but report the current failed
                # validation instead of disguising it as an already-bound run.
                return record, False, False, "retained"
            if transition == "conflict":
                return record, False, True, "conflict"
        _atomic_replace_record(target, payload)
        return record, existing is None, False, "created" if existing is None else "updated"


@contextmanager
def _advisory_record_lock(path: Path):
    """Use an OS-released lock so a crashed process cannot poison the plan."""

    try:
        handle = path.open("a+b")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError) as exc:
        try:
            handle.close()
        except (NameError, OSError):
            pass
        raise OperatorEventValidationError("operator_event_binding_lock_busy") from exc
    try:
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _read_private_record(target: Path) -> Mapping[str, Any] | None:
    if target.is_symlink():
        raise OperatorEventValidationError("operator_event_binding_record_invalid")
    if not target.exists():
        return None
    try:
        if not target.is_file() or target.stat().st_size > 512_000:
            raise OperatorEventValidationError("operator_event_binding_record_invalid")
        payload = json.loads(target.read_text(encoding="utf-8", errors="strict"))
    except OperatorEventValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorEventValidationError("operator_event_binding_record_invalid") from exc
    if not isinstance(payload, dict):
        raise OperatorEventValidationError("operator_event_binding_record_invalid")
    return payload


def _binding_transition(existing: Mapping[str, Any], desired: Mapping[str, Any]) -> str:
    identity_keys = (
        "contract_version",
        "binding_scope",
        "plan_id",
        "project",
        "date",
        "event_id",
    )
    if any(existing.get(key) != desired.get(key) for key in identity_keys):
        return "conflict"
    existing_status = existing.get("binding_status")
    desired_status = desired.get("binding_status")
    if existing_status == "rejected":
        return "replace"
    if existing_status != "bound":
        return "conflict"
    if desired_status == "rejected":
        return "retain"
    binding_keys = ("event_id", "topic_id", "source_hash", "operator_event")
    if any(existing.get(key) != desired.get(key) for key in binding_keys):
        return "conflict"
    return "replace"


def _canonical_record(record: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _atomic_replace_record(target: Path, payload: bytes) -> None:
    temp = target.parent / f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        with temp.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temp.chmod(0o600)
        except OSError:
            pass
        os.replace(temp, target)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _validate_separate_roots(
    *, source_root: Path, private_root: Path, markdown_root: Path,
) -> None:
    try:
        source = source_root.resolve(strict=False)
        private = private_root.resolve(strict=False)
        markdown = markdown_root.resolve(strict=False)
    except OSError as exc:
        raise OperatorEventValidationError("operator_event_root_invalid") from exc
    pairs = (
        (source, markdown),
        (private, markdown),
        (source, private),
    )
    if any(_paths_overlap(left, right) for left, right in pairs):
        raise OperatorEventValidationError("operator_event_root_overlap")


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents
