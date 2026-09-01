"""Closed, manual Content Inbox Scout with local discovery and private state.

The module owns no provider and performs no work at import time.  Callers inject
the existing code-owned risk detector/redactor and the two bounded model calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence


RUN_SCHEMA = "content-inbox-scout-run-v1"
RANKING_SCHEMA = "content-inbox-scout-ranking-v1"
READY_SCHEMA = "content-inbox-ready-material-v1"
PREFERENCE_SCHEMA = "content-inbox-scout-preference-v1"
RANKING_REQUEST_SCHEMA = "content-inbox-scout-ranking-request-v1"
PREPARE_REQUEST_SCHEMA = "content-inbox-scout-prepare-request-v1"
SCOUT_CALLBACK_PREFIX = "scout"
MAX_FILE_BYTES = 256 * 1024
MAX_AGGREGATE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERED = 500
MAX_SHORTLIST = 12
MIN_CANDIDATE_CHARS = 250
MAX_CANDIDATE_CHARS = 2500
MAX_REQUESTED = 5
RUN_ID_RE = re.compile(r"^csr-[a-f0-9]{24}$")
CANDIDATE_ID_RE = re.compile(r"^csc-[a-f0-9]{24}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,95}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,95}$")
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
ALLOWED_FORMATS = frozenset({"short_reel", "post", "carousel", "long_reel"})
ALLOWED_RISKS = frozenset({"none", "low", "requires_manual_check"})
ALLOWED_REASON_CODES = frozenset({
    "source_grounded", "clear_conflict", "clear_change", "clear_result",
    "clear_consequence", "simple_visuals", "fresh_topic", "low_privacy_risk",
    "technical_jargon", "duplicate_recent_topic", "requires_private_screenshot",
    "unverifiable_user_impact", "duration_outside_short_form", "manual_check",
})
CALLBACK_ACTIONS = frozenset({"prepare", "details", "hide", "select", "other", "skip"})
SKIP_NAMES = frozenset({".git", ".pytest_cache", "__pycache__", ".cache", "node_modules"})

RiskDetector = Callable[[str], Sequence[str]]
Redactor = Callable[[str], str]
ModelCall = Callable[[Sequence[Mapping[str, str]], Mapping[str, Any]], Awaitable[str]]


class ScoutError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ScoutConflict(ScoutError):
    pass


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    project: str
    date_label: str
    source_file_digest: str
    heading_identity: str
    character_start: int
    character_end: int
    candidate_digest: str
    safe_text: str
    local_features: Mapping[str, int | float | bool]
    local_score: float


@dataclass(frozen=True, slots=True)
class InboxSnapshot:
    project: str
    snapshot_digest: str
    discovered_count: int
    deduplicated_count: int
    candidates: tuple[Candidate, ...]
    shortlist: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate_id: str
    rank: int
    story_strength_score: int
    reel_ease_score: int
    clarity_score: int
    novelty_score: int
    confidence_score: int
    human_title: str
    one_sentence_pitch: str
    why_it_works: str
    recommended_format: str
    recommended_duration_seconds: int
    recommended_scene_count: int
    editorial_risk: str
    reason_codes: tuple[str, ...]
    final_score: float


@dataclass(frozen=True, slots=True)
class ScoutRunResult:
    run_id: str
    snapshot: InboxSnapshot
    ranked: tuple[RankedCandidate, ...]
    model_calls: int
    created: bool
    run_dir: Path


@dataclass(frozen=True, slots=True)
class ReadyMaterialResult:
    run_id: str
    candidate_id: str
    material: Mapping[str, Any]
    model_calls: int
    created: bool


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _normalize(value: str) -> str:
    return " ".join(value.split()).casefold()


def _plain_text(value: Any, reason: str, *, minimum: int = 1, maximum: int = 1200) -> str:
    if type(value) is not str:
        raise ScoutError(reason)
    text = value.strip()
    if not minimum <= len(text) <= maximum or "[REDACTED]" in text:
        raise ScoutError(reason)
    if re.search(r"(?:[A-Za-z]:\\|/(?:home|opt|var|etc|root)/|\b[a-f0-9]{40,64}\b|(?:TOKEN|API_KEY|PASSWORD)\s*=)", text, re.I):
        raise ScoutError(reason)
    return text


def _private_dir(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise ScoutError("scout_state_root_invalid")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ScoutError("scout_state_root_invalid")
    os.chmod(path, 0o700)


def assert_private_state_location(path: Path, protected_roots: Sequence[Path]) -> None:
    state = Path(os.path.abspath(os.fspath(Path(path).expanduser()))).resolve(strict=False)
    cursor = Path(state.anchor)
    for part in state.parts[1:]:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ScoutError("scout_state_root_invalid")
    for protected in protected_roots:
        boundary = Path(protected).expanduser().resolve(strict=False)
        if state == boundary or state in boundary.parents or boundary in state.parents:
            raise ScoutError("scout_state_root_overlap")


def _safe_root(path: Path) -> Path:
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raw = raw.resolve()
    if raw.exists() and raw.is_symlink():
        raise ScoutError("scout_state_root_invalid")
    _private_dir(raw)
    return raw.resolve()


def _safe_child(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts)
    parent = candidate.parent.resolve()
    if parent != root and root not in parent.parents:
        raise ScoutError("scout_state_path_invalid")
    current = candidate.parent
    while True:
        if current.exists() and current.is_symlink():
            raise ScoutError("scout_state_path_invalid")
        if current == root:
            break
        if current == current.parent:
            raise ScoutError("scout_state_path_invalid")
        current = current.parent
    return candidate


def _atomic_create(path: Path, data: bytes) -> bool:
    _private_dir(path.parent)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
            os.chmod(path, 0o600)
            return True
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)


def _read_bytes(path: Path, maximum: int = 4 * 1024 * 1024) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ScoutError("scout_state_artifact_invalid")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ScoutError("scout_state_artifact_invalid") from exc


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_bytes(path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ScoutError("scout_state_artifact_invalid") from exc
    if type(value) is not dict:
        raise ScoutError("scout_state_artifact_invalid")
    return value


def _write_exact(path: Path, value: Mapping[str, Any], conflict: str) -> bool:
    data = _canonical(value)
    created = _atomic_create(path, data)
    if not created and _read_bytes(path) != data:
        raise ScoutConflict(conflict)
    return created


def _assert_source_root(root: Path, project: str) -> tuple[Path, Path]:
    if type(project) is not str or not PROJECT_RE.fullmatch(project):
        raise ScoutError("scout_project_invalid")
    source_root = Path(root).expanduser()
    if not source_root.is_absolute():
        source_root = source_root.resolve()
    if source_root.is_symlink() or not source_root.is_dir():
        raise ScoutError("scout_inbox_root_invalid")
    source_root = source_root.resolve()
    project_root = source_root / project
    if project_root.is_symlink() or not project_root.is_dir() or project_root.resolve().parent != source_root:
        raise ScoutError("scout_project_root_invalid")
    return source_root, project_root.resolve()


def _markdown_files(root: Path, project: str) -> list[tuple[str, Path]]:
    _, project_root = _assert_source_root(root, project)
    result: list[tuple[str, Path]] = []
    for date_dir in sorted(project_root.iterdir(), key=lambda item: item.name.casefold()):
        if date_dir.name in SKIP_NAMES:
            continue
        if date_dir.is_symlink():
            raise ScoutError("scout_source_symlink_forbidden")
        if not date_dir.is_dir() or not DATE_RE.fullmatch(date_dir.name):
            continue
        for current_raw, dir_names, file_names in os.walk(date_dir, topdown=True, followlinks=False):
            current = Path(current_raw)
            kept: list[str] = []
            for name in sorted(dir_names, key=str.casefold):
                child = current / name
                if name in SKIP_NAMES:
                    continue
                if child.is_symlink():
                    raise ScoutError("scout_source_symlink_forbidden")
                if not child.is_dir():
                    raise ScoutError("scout_source_node_invalid")
                kept.append(name)
            dir_names[:] = kept
            for name in sorted(file_names, key=str.casefold):
                path = current / name
                if path.is_symlink():
                    raise ScoutError("scout_source_symlink_forbidden")
                if not path.is_file():
                    raise ScoutError("scout_source_node_invalid")
                if path.suffix.casefold() not in {".md", ".markdown"}:
                    continue
                resolved = path.resolve()
                if project_root not in resolved.parents:
                    raise ScoutError("scout_source_path_escape")
                result.append((date_dir.name, resolved))
    return result


def _raw_sections(text: str) -> list[tuple[str, int, int]]:
    headings = list(re.finditer(r"(?m)^#{1,3}[ \t]+([^\r\n]+?)[ \t]*\r?$", text))
    if headings:
        sections: list[tuple[str, int, int]] = []
        for index, match in enumerate(headings):
            start = match.start()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            sections.append((match.group(1).strip(), start, end))
        return sections
    paragraphs: list[tuple[str, int, int]] = []
    cursor = 0
    for separator in re.finditer(r"(?:\r?\n[ \t]*){2,}", text):
        if text[cursor:separator.start()].strip():
            paragraphs.append(("paragraphs", cursor, separator.start()))
        cursor = separator.end()
    if text[cursor:].strip():
        paragraphs.append(("paragraphs", cursor, len(text)))
    return paragraphs


def _bounded_sections(text: str) -> list[tuple[str, int, int]]:
    source = _raw_sections(text)
    grouped: list[tuple[str, int, int]] = []
    index = 0
    while index < len(source):
        heading, start, end = source[index]
        while end - start < MIN_CANDIDATE_CHARS and index + 1 < len(source):
            index += 1
            end = source[index][2]
        cursor = start
        while end - cursor > MAX_CANDIDATE_CHARS:
            window = text[cursor:cursor + MAX_CANDIDATE_CHARS]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("! "), window.rfind("? "))
            if cut < MIN_CANDIDATE_CHARS:
                cut = MAX_CANDIDATE_CHARS
            else:
                cut += 1
            grouped.append((heading, cursor, cursor + cut))
            cursor += cut
        if end - cursor >= MIN_CANDIDATE_CHARS:
            grouped.append((heading, cursor, end))
        index += 1
    return grouped


def _technical_noise(text: str) -> bool:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return True
    hash_lines = sum(bool(re.search(r"\b[a-f0-9]{32,64}\b", line, re.I)) for line in lines)
    test_lines = sum(bool(re.search(r"\b(?:passed|failed|skipped|test_|pytest|unittest)\b", line, re.I)) for line in lines)
    config_lines = sum(bool(re.match(r"^[A-Z][A-Z0-9_]{2,}=", line)) for line in lines)
    stack_lines = sum(bool(re.search(r"(?:Traceback|File \".*\", line \d+|Error:)", line)) for line in lines)
    dependency_lines = sum(bool(re.match(r"^[A-Za-z0-9_.-]+(?:==|>=|<=|~=)\S+$", line)) for line in lines)
    table_lines = sum(line.count("|") >= 3 for line in lines)
    return max(hash_lines, test_lines, config_lines, stack_lines, dependency_lines, table_lines) / len(lines) >= 0.55


def _features(text: str, date_label: str, risks: Sequence[str], recent: Sequence[str]) -> dict[str, int | float | bool]:
    normalized = _normalize(text)
    words = re.findall(r"[A-Za-zА-Яа-яЁё0-9-]+", text)
    jargon = re.findall(r"\b(?:api|sdk|json|schema|digest|hash|commit|pytest|runtime|worker|provider|pipeline|validator|contract|deploy)\b", text, re.I)
    markers = {
        "conflict": bool(re.search(r"\b(?:ошиб|не работ|конфликт|слом|мешал|проблем|блокир|вместо)\w*", normalized)),
        "change": bool(re.search(r"\b(?:измен|замен|исправ|убрал|добавил|переш[её]л|стало)\w*", normalized)),
        "result": bool(re.search(r"\b(?:результат|получил|заработал|готов|прош[её]л|удалось|итог)\w*", normalized)),
        "before_after": bool(re.search(r"\b(?:до|раньше|было)\b", normalized) and re.search(r"\b(?:после|теперь|стало)\b", normalized)),
        "consequence": bool(re.search(r"\b(?:пользовател|человек|оператор|читател|проще|понятн|быстр|не отправ|не увид)\w*", normalized)),
    }
    visual = len(re.findall(r"\b(?:экран|окно|кнопк|карточк|сообщени|телефон|бот|меню|стрелк|свет|объект|рук|движ)\w*", normalized))
    repetitions = [SequenceMatcher(None, normalized[:1200], _normalize(item)[:1200]).ratio() for item in recent if item.strip()]
    repetition = max(repetitions, default=0.0)
    locations = 1 if visual <= 3 else 2 if visual <= 8 else 3
    return {
        "date_recency": int(date_label.replace("-", "")) if DATE_RE.fullmatch(date_label) else 0,
        "text_length": len(text),
        "sentence_count": len(re.findall(r"[.!?](?:\s|$)", text)),
        "concrete_entity_number_count": len(re.findall(r"\b(?:\d+(?:[.,]\d+)?|[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё-]{2,})\b", text)),
        "conflict_marker": markers["conflict"],
        "change_marker": markers["change"],
        "result_marker": markers["result"],
        "before_after": markers["before_after"],
        "consequence_marker": markers["consequence"],
        "visual_action_count": visual,
        "jargon_density": round(len(jargon) / max(1, len(words)), 4),
        "recent_repetition": round(repetition, 4),
        "privacy_risk_count": len(risks),
        "estimated_scene_complexity": locations,
    }


def _local_score(features: Mapping[str, int | float | bool]) -> float:
    score = 20.0
    score += min(12.0, float(features["sentence_count"]) * 1.5)
    score += min(10.0, float(features["concrete_entity_number_count"]))
    for key, value in (("conflict_marker", 12), ("change_marker", 8), ("result_marker", 12), ("before_after", 10), ("consequence_marker", 12)):
        if features[key]:
            score += value
    score += min(8.0, float(features["visual_action_count"]))
    score -= float(features["privacy_risk_count"]) * 12
    score -= float(features["jargon_density"]) * 35
    score -= float(features["recent_repetition"]) * 18
    score -= max(0.0, float(features["estimated_scene_complexity"]) - 2) * 8
    return round(max(0.0, min(100.0, score)), 3)


def discover_candidates(
    inbox_root: Path,
    project: str,
    *,
    risk_detector: RiskDetector,
    redactor: Redactor,
    recent_summaries: Sequence[str] = (),
) -> InboxSnapshot:
    """Read the exact project-first Markdown archive and perform zero model calls."""
    files = _markdown_files(inbox_root, project)
    total_bytes = 0
    discovered = 0
    candidates: list[Candidate] = []
    seen_comparison: list[str] = []
    source_inventory: list[dict[str, Any]] = []
    for date_label, path in files:
        raw = _read_bytes(path, MAX_FILE_BYTES)
        total_bytes += len(raw)
        if total_bytes > MAX_AGGREGATE_BYTES:
            raise ScoutError("scout_source_aggregate_too_large")
        try:
            text = raw.decode("utf-8")
        except UnicodeError as exc:
            raise ScoutError("scout_source_encoding_invalid") from exc
        file_digest = _digest_bytes(raw)
        source_inventory.append({"date_label": date_label, "source_file_digest": file_digest, "size": len(raw)})
        for ordinal, (heading, start, end) in enumerate(_bounded_sections(text), start=1):
            if discovered >= MAX_DISCOVERED:
                break
            original = text[start:end].strip()
            if len(original) < MIN_CANDIDATE_CHARS:
                continue
            discovered += 1
            risks = tuple(str(item) for item in risk_detector(original))
            safe = redactor(original).strip()
            if len(safe) < MIN_CANDIDATE_CHARS or risk_detector(safe) or _technical_noise(safe):
                continue
            comparison = _normalize(safe)
            if len(comparison) < MIN_CANDIDATE_CHARS:
                continue
            if any(comparison == prior or SequenceMatcher(None, comparison, prior).ratio() >= 0.92 for prior in seen_comparison):
                continue
            seen_comparison.append(comparison)
            safe = safe[:MAX_CANDIDATE_CHARS]
            candidate_digest = _digest_bytes(original.encode("utf-8"))
            heading_identity = _digest_bytes(f"{ordinal}|{_normalize(heading)}".encode("utf-8"))
            identity = f"{project}|{date_label}|{file_digest}|{heading_identity}|{start}|{end}|{candidate_digest}"
            candidate_id = "csc-" + _digest_bytes(identity.encode("utf-8"))[:24]
            features = _features(safe, date_label, risks, recent_summaries)
            candidates.append(Candidate(
                candidate_id, project, date_label, file_digest, heading_identity,
                start, end, candidate_digest, safe, features, _local_score(features),
            ))
        if discovered >= MAX_DISCOVERED:
            break
    candidates.sort(key=lambda item: item.candidate_id)
    shortlist = tuple(sorted(candidates, key=lambda item: (-item.local_score, item.candidate_id))[:MAX_SHORTLIST])
    snapshot_payload = {
        "project": project,
        "source_inventory": sorted(source_inventory, key=lambda item: (item["date_label"], item["source_file_digest"])),
        "candidate_ids": [item.candidate_id for item in candidates],
        "candidate_digests": [item.candidate_digest for item in candidates],
    }
    snapshot_digest = _digest_bytes(_canonical(snapshot_payload))
    return InboxSnapshot(project, snapshot_digest, discovered, len(candidates), tuple(candidates), shortlist)


def ranking_response_format(
    run_id: str,
    snapshot_digest: str,
    candidate_ids: Sequence[str],
) -> dict[str, Any]:
    if type(run_id) is not str or not RUN_ID_RE.fullmatch(run_id):
        raise ScoutError("scout_provider_schema_identity_invalid")
    if type(snapshot_digest) is not str or not DIGEST_RE.fullmatch(snapshot_digest):
        raise ScoutError("scout_provider_schema_identity_invalid")
    if type(candidate_ids) not in {list, tuple}:
        raise ScoutError("scout_provider_schema_identity_invalid")
    exact_candidate_ids = tuple(candidate_ids)
    if (
        not exact_candidate_ids
        or len(exact_candidate_ids) > MAX_SHORTLIST
        or len(set(exact_candidate_ids)) != len(exact_candidate_ids)
        or any(type(item) is not str or not CANDIDATE_ID_RE.fullmatch(item) for item in exact_candidate_ids)
    ):
        raise ScoutError("scout_provider_schema_identity_invalid")
    item_properties: dict[str, Any] = {
        "candidate_id": {"type": "string", "enum": list(exact_candidate_ids)},
        "rank": {"type": "integer"},
    }
    for key in ("story_strength_score", "reel_ease_score", "clarity_score", "novelty_score", "confidence_score"):
        item_properties[key] = {"type": "integer"}
    for key in ("human_title", "one_sentence_pitch", "why_it_works"):
        item_properties[key] = {"type": "string"}
    item_properties.update({
        "recommended_format": {"type": "string", "enum": sorted(ALLOWED_FORMATS)},
        "recommended_duration_seconds": {"type": "integer"},
        "recommended_scene_count": {"type": "integer"},
        "editorial_risk": {"type": "string", "enum": sorted(ALLOWED_RISKS)},
        "reason_codes": {"type": "array", "items": {"type": "string", "enum": sorted(ALLOWED_REASON_CODES)}},
    })
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "content_inbox_scout_ranking_v1", "strict": True,
            "schema": {
                "type": "object", "additionalProperties": False,
                "required": ["schema_version", "scout_run_id", "source_snapshot_digest", "ranked_candidates"],
                "properties": {
                    "schema_version": {"type": "string", "const": RANKING_SCHEMA},
                    "scout_run_id": {"type": "string", "const": run_id},
                    "source_snapshot_digest": {"type": "string", "const": snapshot_digest},
                    "ranked_candidates": {
                        "type": "array",
                        "items": {"type": "object", "additionalProperties": False, "required": list(item_properties), "properties": item_properties},
                    },
                },
            },
        },
    }


def ready_material_response_format(run_id: str, candidate_id: str) -> dict[str, Any]:
    if (
        type(run_id) is not str
        or not RUN_ID_RE.fullmatch(run_id)
        or type(candidate_id) is not str
        or not CANDIDATE_ID_RE.fullmatch(candidate_id)
    ):
        raise ScoutError("scout_provider_schema_identity_invalid")
    scene = {
        "order": {"type": "integer"},
        "start_second": {"type": "integer"},
        "end_second": {"type": "integer"},
        "screen_text": {"type": "string"},
        "visual_brief": {"type": "string"},
    }
    properties = {
        "schema_version": {"type": "string", "const": READY_SCHEMA},
        "scout_run_id": {"type": "string", "const": run_id},
        "candidate_id": {"type": "string", "const": candidate_id},
        "title": {"type": "string"},
        "hook": {"type": "string"},
        "telegram_post": {"type": "string"},
        "reel_voice_over": {"type": "string"},
        "reel_duration_seconds": {"type": "integer"},
        "scenes": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": list(scene), "properties": scene}},
        "caption": {"type": "string"},
        "cover_text": {"type": "string"},
        "safety_note": {"type": "string"},
        "source_limitations": {"type": "string"},
    }
    return {"type": "json_schema", "json_schema": {"name": "content_inbox_ready_material_v1", "strict": True, "schema": {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}}}


def validate_provider_response_format(response_format: Mapping[str, Any]) -> None:
    """Validate the closed provider schema locally, without constructing a client."""
    if (
        type(response_format) is not dict
        or set(response_format) != {"type", "json_schema"}
        or type(response_format.get("type")) is not str
        or response_format["type"] != "json_schema"
    ):
        raise ScoutError("scout_provider_schema_invalid")
    envelope = response_format.get("json_schema")
    if (
        type(envelope) is not dict
        or set(envelope) != {"name", "strict", "schema"}
        or type(envelope.get("name")) is not str
        or not envelope["name"]
        or envelope.get("strict") is not True
    ):
        raise ScoutError("scout_provider_schema_invalid")
    root = envelope.get("schema")
    if type(root) is not dict or root.get("type") != "object":
        raise ScoutError("scout_provider_schema_invalid")

    allowed_types = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
    allowed_keywords = frozenset({"type", "properties", "required", "additionalProperties", "items", "enum", "const", "description"})

    def value_matches_type(value: Any, declared_type: str) -> bool:
        if declared_type == "string":
            return type(value) is str
        if declared_type == "integer":
            return type(value) is int
        if declared_type == "number":
            return type(value) in {int, float}
        if declared_type == "boolean":
            return type(value) is bool
        if declared_type == "null":
            return value is None
        if declared_type == "object":
            return type(value) is dict
        if declared_type == "array":
            return type(value) is list
        return False

    def validate_node(node: Any) -> None:
        if type(node) is not dict:
            raise ScoutError("scout_provider_schema_invalid")
        if not set(node).issubset(allowed_keywords):
            raise ScoutError("scout_provider_schema_invalid")
        node_type = node.get("type")
        if type(node_type) is not str or node_type not in allowed_types:
            raise ScoutError("scout_provider_schema_invalid")
        type_keywords = {"type", "enum", "const", "description"}
        if node_type == "object":
            type_keywords.update({"properties", "required", "additionalProperties"})
        elif node_type == "array":
            type_keywords.add("items")
        if not set(node).issubset(type_keywords):
            raise ScoutError("scout_provider_schema_invalid")
        if "description" in node and type(node["description"]) is not str:
            raise ScoutError("scout_provider_schema_invalid")
        if "const" in node and not value_matches_type(node["const"], node_type):
            raise ScoutError("scout_provider_schema_invalid")
        if "enum" in node:
            values = node["enum"]
            if (
                type(values) is not list
                or not values
                or node_type in {"object", "array"}
                or any(not value_matches_type(value, node_type) for value in values)
                or len({(type(value).__name__, value) for value in values}) != len(values)
            ):
                raise ScoutError("scout_provider_schema_invalid")
        if node_type == "object":
            properties = node.get("properties")
            required = node.get("required")
            if (
                type(properties) is not dict
                or type(required) is not list
                or node.get("additionalProperties") is not False
                or any(type(key) is not str for key in properties)
                or any(type(key) is not str for key in required)
                or len(set(required)) != len(required)
                or set(required) != set(properties)
            ):
                raise ScoutError("scout_provider_schema_invalid")
            for property_node in properties.values():
                validate_node(property_node)
        elif node_type == "array":
            items = node.get("items")
            if type(items) is not dict or type(items.get("type")) is not str:
                raise ScoutError("scout_provider_schema_invalid")
            validate_node(items)

    validate_node(root)


def _candidate_record(candidate: Candidate) -> dict[str, Any]:
    value = asdict(candidate)
    value["local_features"] = dict(candidate.local_features)
    return value


def _candidate_from_record(value: Mapping[str, Any]) -> Candidate:
    expected = {"candidate_id", "project", "date_label", "source_file_digest", "heading_identity", "character_start", "character_end", "candidate_digest", "safe_text", "local_features", "local_score"}
    if type(value) is not dict or set(value) != expected:
        raise ScoutError("scout_candidate_artifact_invalid")
    if (
        type(value.get("candidate_id")) is not str
        or not CANDIDATE_ID_RE.fullmatch(value["candidate_id"])
        or type(value.get("project")) is not str
        or not PROJECT_RE.fullmatch(value["project"])
        or type(value.get("date_label")) is not str
        or not DATE_RE.fullmatch(value["date_label"])
        or any(type(value.get(key)) is not str or not DIGEST_RE.fullmatch(value[key]) for key in ("source_file_digest", "heading_identity", "candidate_digest"))
        or type(value.get("character_start")) is not int
        or type(value.get("character_end")) is not int
        or not 0 <= value["character_start"] < value["character_end"]
        or type(value.get("safe_text")) is not str
        or not MIN_CANDIDATE_CHARS <= len(value["safe_text"]) <= MAX_CANDIDATE_CHARS
        or type(value.get("local_features")) is not dict
        or type(value.get("local_score")) not in {int, float}
        or isinstance(value.get("local_score"), bool)
        or not 0 <= value["local_score"] <= 100
    ):
        raise ScoutError("scout_candidate_artifact_invalid")
    return Candidate(**value)


def _ranking_prompt(run_id: str, snapshot: InboxSnapshot, recent: Sequence[str]) -> str:
    payload = {
        "schema_version": RANKING_SCHEMA,
        "scout_run_id": run_id,
        "source_snapshot_digest": snapshot.snapshot_digest,
        "candidates": [
            {"candidate_id": item.candidate_id, "date_label": item.date_label, "safe_text": item.safe_text, "local_features": dict(item.local_features)}
            for item in snapshot.shortlist
        ],
        "recent_topic_style_summaries": [" ".join(item.split())[:500] for item in recent[:12]],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ranking_penalty(row: Mapping[str, Any], candidate: Candidate) -> float:
    features = candidate.local_features
    penalty = float(features["privacy_risk_count"]) * 15
    if float(features["jargon_density"]) > 0.18:
        penalty += 7
    if int(features["estimated_scene_complexity"]) > 2:
        penalty += 10
    if float(features["recent_repetition"]) >= 0.55:
        penalty += 10
    codes = set(row["reason_codes"])
    if "unverifiable_user_impact" in codes:
        penalty += 15
    if "requires_private_screenshot" in codes:
        penalty += 18
    if row["recommended_duration_seconds"] > 20 or "duration_outside_short_form" in codes:
        penalty += 12
    return penalty


def parse_ranking(raw: str, run_id: str, snapshot: InboxSnapshot, risk_detector: RiskDetector) -> tuple[RankedCandidate, ...]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ScoutError("scout_ranking_json_invalid") from exc
    expected_top = {"schema_version", "scout_run_id", "source_snapshot_digest", "ranked_candidates"}
    if type(value) is not dict or set(value) != expected_top or value.get("schema_version") != RANKING_SCHEMA or value.get("scout_run_id") != run_id or value.get("source_snapshot_digest") != snapshot.snapshot_digest or type(value.get("ranked_candidates")) is not list:
        raise ScoutError("scout_ranking_contract_invalid")
    by_id = {item.candidate_id: item for item in snapshot.shortlist}
    expected_item = {"candidate_id", "rank", "story_strength_score", "reel_ease_score", "clarity_score", "novelty_score", "confidence_score", "human_title", "one_sentence_pitch", "why_it_works", "recommended_format", "recommended_duration_seconds", "recommended_scene_count", "editorial_risk", "reason_codes"}
    result: list[RankedCandidate] = []
    seen_ids: set[str] = set()
    seen_ranks: set[int] = set()
    for row in value["ranked_candidates"]:
        if type(row) is not dict or set(row) != expected_item:
            raise ScoutError("scout_ranking_contract_invalid")
        candidate_id = row.get("candidate_id")
        if type(candidate_id) is not str or candidate_id not in by_id or candidate_id in seen_ids:
            raise ScoutError("scout_ranking_candidate_invalid")
        rank = row.get("rank")
        score_keys = ("story_strength_score", "reel_ease_score", "clarity_score", "novelty_score", "confidence_score")
        if type(rank) is not int or rank < 1 or rank > len(by_id) or rank in seen_ranks or any(type(row.get(key)) is not int or not 0 <= row[key] <= 100 for key in score_keys):
            raise ScoutError("scout_ranking_score_invalid")
        title = _plain_text(row.get("human_title"), "scout_ranking_text_invalid", maximum=180)
        pitch = _plain_text(row.get("one_sentence_pitch"), "scout_ranking_text_invalid", maximum=500)
        why = _plain_text(row.get("why_it_works"), "scout_ranking_text_invalid", maximum=1000)
        if risk_detector("\n".join((title, pitch, why))):
            raise ScoutError("scout_ranking_text_unsafe")
        fmt = row.get("recommended_format")
        duration = row.get("recommended_duration_seconds")
        scenes = row.get("recommended_scene_count")
        if fmt not in ALLOWED_FORMATS or type(duration) is not int or type(scenes) is not int:
            raise ScoutError("scout_ranking_recommendation_invalid")
        if fmt == "short_reel" and not (12 <= duration <= 20 and 4 <= scenes <= 7):
            raise ScoutError("scout_short_reel_bounds_invalid")
        if row.get("editorial_risk") not in ALLOWED_RISKS or type(row.get("reason_codes")) is not list or len(set(row["reason_codes"])) != len(row["reason_codes"]) or any(type(item) is not str or item not in ALLOWED_REASON_CODES for item in row["reason_codes"]):
            raise ScoutError("scout_ranking_risk_invalid")
        weighted = row["story_strength_score"] * .35 + row["reel_ease_score"] * .35 + row["clarity_score"] * .15 + row["novelty_score"] * .10 + row["confidence_score"] * .05
        final = round(max(0.0, weighted - _ranking_penalty(row, by_id[candidate_id])), 3)
        result.append(RankedCandidate(candidate_id, rank, *(row[key] for key in score_keys), title, pitch, why, fmt, duration, scenes, row["editorial_risk"], tuple(row["reason_codes"]), final))
        seen_ids.add(candidate_id)
        seen_ranks.add(rank)
    if seen_ids != set(by_id):
        raise ScoutError("scout_ranking_candidate_set_invalid")
    return tuple(sorted(result, key=lambda item: (-item.final_score, -item.reel_ease_score, -item.story_strength_score, item.candidate_id)))


def _run_record(snapshot: InboxSnapshot, run_id: str, request_id: str, admin_id: int, refresh: bool) -> dict[str, Any]:
    return {
        "schema_version": RUN_SCHEMA, "run_id": run_id, "operator_request_id": request_id,
        "admin_id": admin_id, "project": snapshot.project, "source_snapshot_digest": snapshot.snapshot_digest,
        "refresh": refresh, "discovered_count": snapshot.discovered_count,
        "deduplicated_count": snapshot.deduplicated_count,
        "candidates": [_candidate_record(item) for item in snapshot.candidates],
        "shortlist_ids": [item.candidate_id for item in snapshot.shortlist],
    }


def _run_from_record(record: Mapping[str, Any], run_dir: Path, ranked: tuple[RankedCandidate, ...], model_calls: int, created: bool) -> ScoutRunResult:
    expected = {
        "schema_version", "run_id", "operator_request_id", "admin_id", "project",
        "source_snapshot_digest", "refresh", "discovered_count",
        "deduplicated_count", "candidates", "shortlist_ids",
    }
    if (
        type(record) is not dict
        or set(record) != expected
        or record.get("schema_version") != RUN_SCHEMA
        or type(record.get("run_id")) is not str
        or not RUN_ID_RE.fullmatch(record["run_id"])
        or type(record.get("operator_request_id")) is not str
        or not REQUEST_ID_RE.fullmatch(record["operator_request_id"])
        or type(record.get("admin_id")) is not int
        or record["admin_id"] <= 0
        or type(record.get("project")) is not str
        or not PROJECT_RE.fullmatch(record["project"])
        or type(record.get("source_snapshot_digest")) is not str
        or not DIGEST_RE.fullmatch(record["source_snapshot_digest"])
        or type(record.get("refresh")) is not bool
        or type(record.get("discovered_count")) is not int
        or type(record.get("deduplicated_count")) is not int
        or type(record.get("candidates")) is not list
        or type(record.get("shortlist_ids")) is not list
    ):
        raise ScoutError("scout_run_artifact_invalid")
    candidates = tuple(_candidate_from_record(item) for item in record["candidates"])
    by_id = {item.candidate_id: item for item in candidates}
    try:
        shortlist = tuple(by_id[item] for item in record["shortlist_ids"])
    except (KeyError, TypeError) as exc:
        raise ScoutError("scout_run_artifact_invalid") from exc
    if len(by_id) != len(candidates) or not 0 <= record["deduplicated_count"] == len(candidates) <= record["discovered_count"] <= MAX_DISCOVERED:
        raise ScoutError("scout_run_artifact_invalid")
    if len(shortlist) > MAX_SHORTLIST or len(set(record["shortlist_ids"])) != len(shortlist):
        raise ScoutError("scout_run_artifact_invalid")
    snapshot = InboxSnapshot(record["project"], record["source_snapshot_digest"], record["discovered_count"], record["deduplicated_count"], candidates, shortlist)
    return ScoutRunResult(record["run_id"], snapshot, ranked, model_calls, created, run_dir)


def _ranking_record(run_id: str, snapshot_digest: str, ranked: Sequence[RankedCandidate]) -> dict[str, Any]:
    rows = []
    for item in ranked:
        row = asdict(item)
        row["reason_codes"] = list(item.reason_codes)
        rows.append(row)
    return {"schema_version": RANKING_SCHEMA, "scout_run_id": run_id, "source_snapshot_digest": snapshot_digest, "ranked_candidates": rows}


def _ranked_from_stored(value: Mapping[str, Any]) -> tuple[RankedCandidate, ...]:
    top_keys = {"schema_version", "scout_run_id", "source_snapshot_digest", "ranked_candidates"}
    if (
        type(value) is not dict
        or set(value) != top_keys
        or value.get("schema_version") != RANKING_SCHEMA
        or type(value.get("scout_run_id")) is not str
        or not RUN_ID_RE.fullmatch(value["scout_run_id"])
        or type(value.get("source_snapshot_digest")) is not str
        or not DIGEST_RE.fullmatch(value["source_snapshot_digest"])
        or type(value.get("ranked_candidates")) is not list
    ):
        raise ScoutError("scout_ranking_artifact_invalid")
    item_keys = {field.name for field in RankedCandidate.__dataclass_fields__.values()}
    score_keys = {"story_strength_score", "reel_ease_score", "clarity_score", "novelty_score", "confidence_score"}
    for item in value["ranked_candidates"]:
        if (
            type(item) is not dict
            or set(item) != item_keys
            or type(item.get("candidate_id")) is not str
            or not CANDIDATE_ID_RE.fullmatch(item["candidate_id"])
            or type(item.get("rank")) is not int
            or item["rank"] < 1
            or any(type(item.get(key)) is not int or not 0 <= item[key] <= 100 for key in score_keys)
            or type(item.get("reason_codes")) is not list
            or any(type(code) is not str or code not in ALLOWED_REASON_CODES for code in item["reason_codes"])
            or item.get("recommended_format") not in ALLOWED_FORMATS
            or item.get("editorial_risk") not in ALLOWED_RISKS
            or type(item.get("recommended_duration_seconds")) is not int
            or type(item.get("recommended_scene_count")) is not int
            or type(item.get("final_score")) not in {int, float}
            or isinstance(item.get("final_score"), bool)
            or not 0 <= item["final_score"] <= 100
        ):
            raise ScoutError("scout_ranking_artifact_invalid")
    try:
        return tuple(RankedCandidate(**{**item, "reason_codes": tuple(item["reason_codes"])}) for item in value["ranked_candidates"])
    except (KeyError, TypeError) as exc:
        raise ScoutError("scout_ranking_artifact_invalid") from exc


async def rank_snapshot(
    state_root: Path,
    snapshot: InboxSnapshot,
    *,
    admin_id: int,
    expected_admin_id: int,
    operator_request_id: str,
    refresh: bool,
    recent_summaries: Sequence[str],
    risk_detector: RiskDetector,
    model_call: ModelCall,
) -> ScoutRunResult:
    if type(admin_id) is not int or admin_id <= 0 or admin_id != expected_admin_id:
        raise ScoutError("scout_admin_required")
    if type(operator_request_id) is not str or not REQUEST_ID_RE.fullmatch(operator_request_id):
        raise ScoutError("scout_request_id_invalid")
    if not snapshot.shortlist:
        raise ScoutError("scout_no_candidates")
    root = _safe_root(state_root)
    identity = f"{admin_id}|{snapshot.snapshot_digest}|{operator_request_id if refresh else 'default'}|{int(refresh)}"
    run_id = "csr-" + _digest_bytes(identity.encode("utf-8"))[:24]
    index_path = _safe_child(root, "snapshots", f"{snapshot.snapshot_digest}.json")
    if not refresh and index_path.exists():
        existing = _read_json(index_path)
        existing_run = existing.get("run_id")
        expected_index_keys = {"schema_version", "source_snapshot_digest", "admin_id", "run_id"}
        if (
            set(existing) != expected_index_keys
            or existing.get("schema_version") != "content-inbox-scout-snapshot-index-v1"
            or existing.get("source_snapshot_digest") != snapshot.snapshot_digest
            or existing.get("admin_id") != admin_id
            or type(existing_run) is not str
            or not RUN_ID_RE.fullmatch(existing_run)
        ):
            raise ScoutConflict("scout_snapshot_index_conflict")
        run_id = existing_run
    request_key = _digest_bytes(operator_request_id.encode("utf-8"))
    request_path = _safe_child(root, "requests", f"{request_key}.json")
    request_record = {"schema_version": "content-inbox-scout-operator-request-v1", "operator_request_id": operator_request_id, "admin_id": admin_id, "run_id": run_id, "source_snapshot_digest": snapshot.snapshot_digest, "refresh": refresh}
    if request_path.exists() and _read_bytes(request_path) != _canonical(request_record):
        raise ScoutConflict("scout_request_conflict")
    run_dir = _safe_child(root, "runs", run_id)
    run_path = run_dir / "run.json"
    if run_path.exists():
        record = _read_json(run_path)
        created = False
    else:
        record = _run_record(snapshot, run_id, operator_request_id, admin_id, refresh)
        created = _write_exact(run_path, record, "scout_run_conflict")
    if not refresh and not index_path.exists():
        index = {"schema_version": "content-inbox-scout-snapshot-index-v1", "source_snapshot_digest": snapshot.snapshot_digest, "admin_id": admin_id, "run_id": run_id}
        _write_exact(index_path, index, "scout_snapshot_index_conflict")
    _write_exact(request_path, request_record, "scout_request_conflict")
    ranking_path = run_dir / "ranking.json"
    if ranking_path.is_file():
        stored = _read_json(ranking_path)
        if stored.get("scout_run_id") != run_id or stored.get("source_snapshot_digest") != snapshot.snapshot_digest:
            raise ScoutError("scout_ranking_artifact_invalid")
        return _run_from_record(record, run_dir, _ranked_from_stored(stored), 0, False)
    response_format = ranking_response_format(
        run_id,
        snapshot.snapshot_digest,
        tuple(item.candidate_id for item in snapshot.shortlist),
    )
    validate_provider_response_format(response_format)
    marker_path = run_dir / "ranking-requested.json"
    marker = {"schema_version": RANKING_REQUEST_SCHEMA, "run_id": run_id, "source_snapshot_digest": snapshot.snapshot_digest, "model_call_budget": 1}
    if marker_path.exists():
        raise ScoutError("scout_ranking_interrupted")
    _write_exact(marker_path, marker, "scout_ranking_request_conflict")
    prompt = _ranking_prompt(run_id, snapshot, recent_summaries)
    raw = await model_call([
        {"role": "system", "content": "Rank only supplied privacy-safe candidates. Return the closed JSON schema exactly; invent no facts or metrics."},
        {"role": "user", "content": prompt},
    ], response_format)
    ranked = parse_ranking(raw, run_id, snapshot, risk_detector)
    _write_exact(ranking_path, _ranking_record(run_id, snapshot.snapshot_digest, ranked), "scout_ranking_conflict")
    return _run_from_record(record, run_dir, ranked, 1, created)


def load_run(state_root: Path, run_id: str) -> ScoutRunResult:
    if type(run_id) is not str or not RUN_ID_RE.fullmatch(run_id):
        raise ScoutError("scout_run_id_invalid")
    root = _safe_root(state_root)
    run_dir = _safe_child(root, "runs", run_id)
    record = _read_json(run_dir / "run.json")
    if record.get("run_id") != run_id:
        raise ScoutError("scout_run_artifact_invalid")
    stored = _read_json(run_dir / "ranking.json")
    if stored.get("scout_run_id") != run_id or stored.get("source_snapshot_digest") != record.get("source_snapshot_digest"):
        raise ScoutError("scout_ranking_artifact_invalid")
    ranking = _ranked_from_stored(stored)
    return _run_from_record(record, run_dir, ranking, 0, False)


def candidate_for_run(run: ScoutRunResult, candidate_id: str) -> Candidate:
    matches = [item for item in run.snapshot.candidates if item.candidate_id == candidate_id]
    if len(matches) != 1:
        raise ScoutError("scout_candidate_not_owned")
    return matches[0]


def ranked_for_run(run: ScoutRunResult, candidate_id: str) -> RankedCandidate:
    matches = [item for item in run.ranked if item.candidate_id == candidate_id]
    if len(matches) != 1:
        raise ScoutError("scout_candidate_not_ranked")
    return matches[0]


def callback_data(action: str, run_id: str, candidate_id: str) -> str:
    if action not in CALLBACK_ACTIONS or not RUN_ID_RE.fullmatch(run_id) or not CANDIDATE_ID_RE.fullmatch(candidate_id):
        raise ScoutError("scout_callback_invalid")
    value = f"{SCOUT_CALLBACK_PREFIX}:{action}:{run_id[4:]}:{candidate_id[4:]}"
    if len(value.encode("utf-8")) > 64:
        raise ScoutError("scout_callback_invalid")
    return value


def parse_callback(value: str) -> tuple[str, str, str]:
    if type(value) is not str:
        raise ScoutError("scout_callback_invalid")
    match = re.fullmatch(r"scout:(prepare|details|hide|select|other|skip):([a-f0-9]{24}):([a-f0-9]{24})", value)
    if not match:
        raise ScoutError("scout_callback_invalid")
    action, run_tail, candidate_tail = match.groups()
    return action, f"csr-{run_tail}", f"csc-{candidate_tail}"


def hidden_candidate_ids(state_root: Path, admin_id: int) -> frozenset[str]:
    root = _safe_root(state_root)
    directory = _safe_child(root, "preferences", "hidden")
    if not directory.is_dir():
        return frozenset()
    result: set[str] = set()
    for path in sorted(directory.glob("csc-*/*.json")):
        value = _read_json(path)
        if value.get("schema_version") == PREFERENCE_SCHEMA and value.get("admin_id") == admin_id and value.get("preference") == "hidden" and CANDIDATE_ID_RE.fullmatch(str(value.get("candidate_id", ""))):
            result.add(str(value["candidate_id"]))
    return frozenset(result)


def store_preference(state_root: Path, run: ScoutRunResult, candidate_id: str, admin_id: int, preference: str) -> bool:
    if preference not in {"hidden", "skipped", "selected"}:
        raise ScoutError("scout_preference_invalid")
    record = _read_json(run.run_dir / "run.json")
    if record.get("admin_id") != admin_id:
        raise ScoutError("scout_admin_required")
    candidate_for_run(run, candidate_id)
    root = _safe_root(state_root)
    path = _safe_child(root, "preferences", preference, candidate_id, f"{run.run_id}.json")
    value = {"schema_version": PREFERENCE_SCHEMA, "run_id": run.run_id, "candidate_id": candidate_id, "admin_id": admin_id, "preference": preference}
    return _write_exact(path, value, "scout_preference_conflict")


def _parse_ready(raw: str, run: ScoutRunResult, candidate: Candidate, risk_detector: RiskDetector) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ScoutError("scout_ready_json_invalid") from exc
    expected = {"schema_version", "scout_run_id", "candidate_id", "title", "hook", "telegram_post", "reel_voice_over", "reel_duration_seconds", "scenes", "caption", "cover_text", "safety_note", "source_limitations"}
    if type(value) is not dict or set(value) != expected or value.get("schema_version") != READY_SCHEMA or value.get("scout_run_id") != run.run_id or value.get("candidate_id") != candidate.candidate_id:
        raise ScoutError("scout_ready_contract_invalid")
    for key, bounds in {"title": (1, 180), "hook": (1, 300), "telegram_post": (600, 1100), "reel_voice_over": (80, 1000), "caption": (1, 1000), "cover_text": (1, 160), "safety_note": (1, 600), "source_limitations": (1, 600)}.items():
        value[key] = _plain_text(value.get(key), "scout_ready_text_invalid", minimum=bounds[0], maximum=bounds[1])
    public = "\n".join(str(value[key]) for key in ("title", "hook", "telegram_post", "reel_voice_over", "caption", "cover_text", "safety_note", "source_limitations"))
    if risk_detector(public):
        raise ScoutError("scout_ready_text_unsafe")
    duration = value.get("reel_duration_seconds")
    scenes = value.get("scenes")
    if type(duration) is not int or not 12 <= duration <= 20 or type(scenes) is not list or not 4 <= len(scenes) <= 7:
        raise ScoutError("scout_ready_reel_invalid")
    previous = 0
    expected_scene_keys = {"order", "start_second", "end_second", "screen_text", "visual_brief"}
    for order, scene in enumerate(scenes, start=1):
        if type(scene) is not dict or set(scene) != expected_scene_keys or scene.get("order") != order or type(scene.get("start_second")) is not int or type(scene.get("end_second")) is not int or scene["start_second"] != previous or scene["end_second"] <= previous:
            raise ScoutError("scout_ready_scene_timing_invalid")
        scene["screen_text"] = _plain_text(scene.get("screen_text"), "scout_ready_scene_invalid", maximum=180)
        scene["visual_brief"] = _plain_text(scene.get("visual_brief"), "scout_ready_scene_invalid", maximum=500)
        if risk_detector(scene["screen_text"] + "\n" + scene["visual_brief"]) or re.search(r"(?:реальн\w* (?:лог|скриншот)|private screenshot|music|музык)", scene["visual_brief"], re.I):
            raise ScoutError("scout_ready_scene_unsafe")
        previous = scene["end_second"]
    if previous != duration:
        raise ScoutError("scout_ready_scene_timing_invalid")
    return value


async def prepare_candidate(
    state_root: Path,
    run_id: str,
    candidate_id: str,
    *,
    admin_id: int,
    expected_admin_id: int,
    risk_detector: RiskDetector,
    model_call: ModelCall,
) -> ReadyMaterialResult:
    if type(admin_id) is not int or admin_id <= 0 or admin_id != expected_admin_id:
        raise ScoutError("scout_admin_required")
    run = load_run(state_root, run_id)
    record = _read_json(run.run_dir / "run.json")
    if record.get("admin_id") != admin_id:
        raise ScoutError("scout_admin_required")
    candidate = candidate_for_run(run, candidate_id)
    ranking = ranked_for_run(run, candidate_id)
    ready_path = run.run_dir / "prepared" / candidate_id / "ready-material.json"
    if ready_path.is_file():
        stored = _read_json(ready_path)
        material = _parse_ready(json.dumps(stored, ensure_ascii=False), run, candidate, risk_detector)
        return ReadyMaterialResult(run_id, candidate_id, material, 0, False)
    response_format = ready_material_response_format(run_id, candidate_id)
    validate_provider_response_format(response_format)
    marker_path = run.run_dir / "prepared" / candidate_id / "prepare-requested.json"
    marker = {"schema_version": PREPARE_REQUEST_SCHEMA, "run_id": run_id, "candidate_id": candidate_id, "admin_id": admin_id, "model_call_budget": 1}
    if marker_path.exists():
        raise ScoutError("scout_prepare_interrupted")
    _write_exact(marker_path, marker, "scout_prepare_request_conflict")
    payload = {
        "schema_version": READY_SCHEMA, "scout_run_id": run_id,
        "candidate_id": candidate_id, "safe_candidate_text": candidate.safe_text,
        "stored_ranking_context": {
            "human_title": ranking.human_title, "one_sentence_pitch": ranking.one_sentence_pitch,
            "why_it_works": ranking.why_it_works, "recommended_format": ranking.recommended_format,
            "recommended_duration_seconds": ranking.recommended_duration_seconds,
            "recommended_scene_count": ranking.recommended_scene_count,
            "editorial_risk": ranking.editorial_risk, "reason_codes": list(ranking.reason_codes),
        },
    }
    raw = await model_call([
        {"role": "system", "content": "Prepare one source-grounded private material. Use only supplied candidate facts. Return the closed JSON schema exactly. No publication or media calls."},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))},
    ], response_format)
    material = _parse_ready(raw, run, candidate, risk_detector)
    _write_exact(ready_path, material, "scout_ready_material_conflict")
    return ReadyMaterialResult(run_id, candidate_id, material, 1, True)


def safe_cards(run: ScoutRunResult, count: int, hidden: Iterable[str] = ()) -> tuple[RankedCandidate, ...]:
    if type(count) is not int or not 1 <= count <= MAX_REQUESTED:
        raise ScoutError("scout_count_invalid")
    hidden_set = set(hidden)
    return tuple(item for item in run.ranked if item.candidate_id not in hidden_set)[:count]


def format_label(value: str) -> str:
    return {"short_reel": "короткий Reel", "post": "пост", "carousel": "карусель", "long_reel": "длинный Reel"}[value]


def card_text(item: RankedCandidate, position: int) -> str:
    risk = {"none": "нет", "low": "низкий", "requires_manual_check": "нужна ручная проверка"}[item.editorial_risk]
    return (
        f"🔥 Вариант {position} — {item.human_title}\n\n"
        f"Сила истории: {item.story_strength_score}/100\n"
        f"Простота Reel: {item.reel_ease_score}/100\n"
        f"Ясность: {item.clarity_score}/100\n"
        f"Новизна: {item.novelty_score}/100\n\n"
        f"Формат: {format_label(item.recommended_format)}\n"
        f"Оценка: {item.recommended_duration_seconds} секунд · {item.recommended_scene_count} сцен\n\n"
        f"Суть:\n{item.one_sentence_pitch}\n\n"
        f"Почему сработает:\n{item.why_it_works}\n\n"
        f"Риск:\n{risk}"
    )


def details_text(item: RankedCandidate) -> str:
    return (
        f"Подробнее · {item.human_title}\n\n"
        f"Итоговая code-owned оценка: {item.final_score:.1f}/100\n"
        f"Уверенность: {item.confidence_score}/100\n"
        f"Формат: {format_label(item.recommended_format)}\n"
        f"Хронометраж: {item.recommended_duration_seconds} секунд · {item.recommended_scene_count} сцен\n\n"
        f"Суть: {item.one_sentence_pitch}\n\nПочему: {item.why_it_works}\n\n"
        f"Коды решения: {', '.join(item.reason_codes) or 'нет'}"
    )


def ready_material_text(material: Mapping[str, Any]) -> str:
    scenes = "\n\n".join(
        f"{item['order']}. {item['start_second']:02d}–{item['end_second']:02d} сек · {item['screen_text']}\n{item['visual_brief']}"
        for item in material["scenes"]
    )
    return (
        f"✅ Материал готов\n\n{material['title']}\n\nХук:\n{material['hook']}\n\n"
        f"Telegram-пост:\n{material['telegram_post']}\n\nVoice-over:\n{material['reel_voice_over']}\n\n"
        f"Сцен-план ({material['reel_duration_seconds']} секунд):\n{scenes}\n\n"
        f"Caption:\n{material['caption']}\n\nОбложка:\n{material['cover_text']}\n\n"
        f"Safety note:\n{material['safety_note']}\n\nОграничения источника:\n{material['source_limitations']}"
    )
