"""Closed, model-free import boundary for editorial operator content packages.

The module deliberately does not import the Narrative Normalizer, provider SDKs,
Renderer, or publication code.  It validates one plain-JSON value object and
stores it in an append-only private area before Telegram presentation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "operator-content-package-v1"
RIGHTS_UNCLEAR = "UNCLEAR_DO_NOT_USE"
PACKAGE_ID_RE = re.compile(r"^ocp-[a-f0-9]{24}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,95}$")
CALLBACK_TOKEN_RE = re.compile(r"^[a-f0-9]{24}$")
CALLBACK_ACTIONS = (
    "build", "script", "skip", "publish", "remake", "cancel",
)
MAX_SOURCE_BYTES = 512 * 1024
MAX_PACKAGE_BYTES = 512 * 1024
PREVIEW_DESCRIPTION = (
    "Технический детектив о двух невидимых границах:\n"
    "обычная фраза могла ошибочно стать командой навигации,\n"
    "а AI-изображение могло пройти дальше после изменения доступа."
)
DISCLAIMER_REQUIRED_FRAGMENT = (
    "Это журнал разработки, а не независимый аудит репозитория"
)
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700


class OperatorPackageError(ValueError):
    """Fail-closed package rejection with a stable, privacy-safe reason."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class OperatorPackageConflict(OperatorPackageError):
    pass


@dataclass(frozen=True, slots=True)
class ImportedOperatorPackage:
    package_id: str
    package_digest: str
    operator_request_id: str
    operator_id: int
    package_path: Path
    created: bool


@dataclass(frozen=True, slots=True)
class CallbackBinding:
    action: str
    token: str
    package_id: str
    package_digest: str
    operator_request_id: str
    operator_id: int


_TOP_KEYS = {
    "schema_version", "package_id", "operator_request_id",
    "source_provenance", "editorial_disclaimer", "approved_fact_map",
    "prohibited_claims", "title", "story_post_adaptation", "reel",
    "voice_over", "caption", "cover_brief", "music_brief",
    "rights_status", "publication_restrictions",
}
_SOURCE_KEYS = {
    "kind", "source_date", "source_title", "source_document_sha256",
    "editorial_basis",
}
_FACT_KEYS = {"fact_id", "statement", "publication_status", "restriction"}
_REEL_KEYS = {"duration_seconds", "format", "scenes"}
_SCENE_KEYS = {
    "scene_id", "order", "start_second", "end_second", "screen_text",
    "visual", "fact_refs",
}
_COVER_KEYS = {"text", "subtitle", "composition", "alt_text"}
_MUSIC_KEYS = {"brief", "tempo_bpm", "mood", "excluded", "track"}


def _plain_dict(value: Any, keys: set[str], reason: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys or any(type(key) is not str for key in value):
        raise OperatorPackageError(reason)
    return value


def _plain_list(value: Any, reason: str) -> list[Any]:
    if type(value) is not list:
        raise OperatorPackageError(reason)
    return value


def _text(value: Any, reason: str, *, maximum: int = 20_000) -> str:
    if type(value) is not str or not value.strip() or value != value.strip() or len(value) > maximum:
        raise OperatorPackageError(reason)
    if "\x00" in value:
        raise OperatorPackageError(reason)
    return value


def canonical_package_bytes(package: Mapping[str, Any]) -> bytes:
    return (json.dumps(package, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def package_digest(package: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_package_bytes(package)).hexdigest()


def _public_text(package: Mapping[str, Any]) -> str:
    reel = package["reel"]
    cover = package["cover_brief"]
    pieces = [
        package["title"], package["story_post_adaptation"],
        package["voice_over"], package["caption"], cover["text"],
        cover["subtitle"], cover["composition"], cover["alt_text"],
    ]
    for scene in reel["scenes"]:
        pieces.extend((scene["screen_text"], scene["visual"]))
    return "\n".join(pieces).casefold()


def validate_package(value: Any) -> dict[str, Any]:
    """Validate the exact plain-JSON contract and return the same object."""
    package = _plain_dict(value, _TOP_KEYS, "operator_package_top_level_invalid")
    if type(package["schema_version"]) is not str or package["schema_version"] != SCHEMA_VERSION:
        raise OperatorPackageError("operator_package_schema_invalid")
    package_id = _text(package["package_id"], "operator_package_id_invalid", maximum=28)
    if not PACKAGE_ID_RE.fullmatch(package_id):
        raise OperatorPackageError("operator_package_id_invalid")
    request_id = _text(package["operator_request_id"], "operator_request_id_invalid", maximum=96)
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise OperatorPackageError("operator_request_id_invalid")

    source = _plain_dict(package["source_provenance"], _SOURCE_KEYS, "operator_source_provenance_invalid")
    for key in ("kind", "source_date", "source_title", "editorial_basis"):
        _text(source[key], "operator_source_provenance_invalid", maximum=1000)
    source_hash = _text(source["source_document_sha256"], "operator_source_digest_invalid", maximum=64)
    if not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise OperatorPackageError("operator_source_digest_invalid")

    disclaimer = _text(package["editorial_disclaimer"], "operator_editorial_disclaimer_missing", maximum=3000)
    if DISCLAIMER_REQUIRED_FRAGMENT.casefold() not in disclaimer.casefold():
        raise OperatorPackageError("operator_editorial_disclaimer_missing")

    facts = _plain_list(package["approved_fact_map"], "operator_fact_map_invalid")
    if len(facts) != 7:
        raise OperatorPackageError("operator_fact_map_invalid")
    fact_ids: list[str] = []
    for fact in facts:
        item = _plain_dict(fact, _FACT_KEYS, "operator_fact_invalid")
        fact_id = _text(item["fact_id"], "operator_fact_id_invalid", maximum=16)
        if not re.fullmatch(r"F[1-9][0-9]?", fact_id):
            raise OperatorPackageError("operator_fact_id_invalid")
        fact_ids.append(fact_id)
        _text(item["statement"], "operator_fact_statement_invalid", maximum=2000)
        if type(item["publication_status"]) is not str or item["publication_status"] not in {"approved", "required"}:
            raise OperatorPackageError("operator_fact_publication_status_invalid")
        _text(item["restriction"], "operator_fact_restriction_invalid", maximum=1000)
    if len(set(fact_ids)) != len(fact_ids):
        raise OperatorPackageError("operator_fact_id_duplicate")

    prohibited = _plain_list(package["prohibited_claims"], "operator_prohibited_claims_invalid")
    if not prohibited:
        raise OperatorPackageError("operator_prohibited_claims_invalid")
    prohibited_texts = [_text(item, "operator_prohibited_claim_invalid", maximum=2000) for item in prohibited]

    _text(package["title"], "operator_title_invalid", maximum=200)
    _text(package["story_post_adaptation"], "operator_story_adaptation_invalid")
    _text(package["voice_over"], "operator_voice_over_invalid")
    _text(package["caption"], "operator_caption_invalid")

    reel = _plain_dict(package["reel"], _REEL_KEYS, "operator_reel_invalid")
    if type(reel["duration_seconds"]) is not int or not 1 <= reel["duration_seconds"] <= 180:
        raise OperatorPackageError("operator_reel_duration_invalid")
    if type(reel["format"]) is not str or reel["format"] != "9:16/1080x1920/30fps":
        raise OperatorPackageError("operator_reel_format_invalid")
    scenes = _plain_list(reel["scenes"], "operator_scenes_invalid")
    if not 1 <= len(scenes) <= 24:
        raise OperatorPackageError("operator_scenes_invalid")
    scene_ids: list[str] = []
    expected_start = 0
    for index, scene in enumerate(scenes, start=1):
        item = _plain_dict(scene, _SCENE_KEYS, "operator_scene_invalid")
        scene_id = _text(item["scene_id"], "operator_scene_id_invalid", maximum=32)
        if not re.fullmatch(r"scene-[0-9]{2}", scene_id):
            raise OperatorPackageError("operator_scene_id_invalid")
        scene_ids.append(scene_id)
        if type(item["order"]) is not int or item["order"] != index:
            raise OperatorPackageError("operator_scene_order_invalid")
        if type(item["start_second"]) is not int or type(item["end_second"]) is not int:
            raise OperatorPackageError("operator_scene_timing_invalid")
        if item["start_second"] != expected_start or item["end_second"] <= item["start_second"]:
            raise OperatorPackageError("operator_scene_timing_invalid")
        expected_start = item["end_second"]
        _text(item["screen_text"], "operator_scene_screen_text_invalid", maximum=500)
        _text(item["visual"], "operator_scene_visual_invalid", maximum=2000)
        refs = _plain_list(item["fact_refs"], "operator_scene_fact_refs_invalid")
        if not refs or any(type(ref) is not str or ref not in fact_ids for ref in refs):
            raise OperatorPackageError("operator_scene_fact_refs_invalid")
        if len(set(refs)) != len(refs):
            raise OperatorPackageError("operator_scene_fact_refs_invalid")
    if len(set(scene_ids)) != len(scene_ids):
        raise OperatorPackageError("operator_scene_id_duplicate")
    if expected_start != reel["duration_seconds"]:
        raise OperatorPackageError("operator_reel_duration_mismatch")

    cover = _plain_dict(package["cover_brief"], _COVER_KEYS, "operator_cover_brief_invalid")
    for key in _COVER_KEYS:
        _text(cover[key], "operator_cover_brief_invalid", maximum=4000)
    music = _plain_dict(package["music_brief"], _MUSIC_KEYS, "operator_music_brief_invalid")
    _text(music["brief"], "operator_music_brief_invalid", maximum=12_000)
    if type(music["tempo_bpm"]) is not list or music["tempo_bpm"] != [92, 98]:
        raise OperatorPackageError("operator_music_tempo_invalid")
    _text(music["mood"], "operator_music_brief_invalid", maximum=2000)
    excluded = _plain_list(music["excluded"], "operator_music_brief_invalid")
    if not excluded or any(type(item) is not str or not item.strip() for item in excluded):
        raise OperatorPackageError("operator_music_brief_invalid")
    if music["track"] is not None:
        raise OperatorPackageError("operator_music_track_forbidden")

    if type(package["rights_status"]) is not str or package["rights_status"] != RIGHTS_UNCLEAR:
        raise OperatorPackageError("operator_rights_status_invalid")
    restrictions = _plain_list(package["publication_restrictions"], "operator_publication_restrictions_invalid")
    if any(type(item) is not str or not item.strip() for item in restrictions):
        raise OperatorPackageError("operator_publication_restrictions_invalid")
    required_restrictions = {"no_automatic_publication", "no_music_without_rights_approval", "second_admin_action_required"}
    if not required_restrictions.issubset(restrictions):
        raise OperatorPackageError("operator_publication_restrictions_invalid")

    public = _public_text(package)
    if any(claim.casefold() in public for claim in prohibited_texts):
        raise OperatorPackageError("operator_prohibited_claim_present")
    return package


def parse_json_package(data: bytes) -> dict[str, Any]:
    if type(data) is not bytes or not data or len(data) > MAX_PACKAGE_BYTES:
        raise OperatorPackageError("operator_package_bytes_invalid")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OperatorPackageError("operator_package_json_invalid") from exc
    return validate_package(value)


def _section(text: str, start: str, end: str) -> str:
    begin = text.find(start)
    finish = text.find(end, begin + len(start))
    if begin < 0 or finish < 0:
        raise OperatorPackageError("operator_editorial_source_structure_invalid")
    return text[begin + len(start):finish].strip()


def _parse_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"00:(\d{2})[–-]00:(\d{2})", value.strip())
    if not match:
        raise OperatorPackageError("operator_scene_timing_invalid")
    return int(match.group(1)), int(match.group(2))


def package_from_editorial_markdown(data: bytes, operator_request_id: str) -> dict[str, Any]:
    """Deterministically project the supplied editorial document into v1 JSON."""
    if type(data) is not bytes or not data or len(data) > MAX_SOURCE_BYTES:
        raise OperatorPackageError("operator_editorial_source_bytes_invalid")
    if type(operator_request_id) is not str or not REQUEST_ID_RE.fullmatch(operator_request_id):
        raise OperatorPackageError("operator_request_id_invalid")
    try:
        text = data.decode("utf-8").replace("\r\n", "\n")
    except UnicodeDecodeError as exc:
        raise OperatorPackageError("operator_editorial_source_utf8_invalid") from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    # A pasted Markdown heading may lose only its presentation marker.  The
    # editorial words remain exact and every substantive section is still gated.
    if first not in {"# АВТО-пакет", "АВТО-пакет"}:
        raise OperatorPackageError("operator_editorial_source_heading_invalid")
    if "«Сообщение, которое бот не отправил»" not in text:
        raise OperatorPackageError("operator_editorial_source_title_invalid")
    required_markers = (
        "1. Карта фактов", "F1", "F7", "Длительность: 47 секунд.",
        "8. Чистовой voice-over", "13. Caption для Reel",
        "14. Обложка и alt text", "9. Music brief", "11. Rights status",
        "UNCLEAR_DO_NOT_USE",
    )
    if any(marker not in text for marker in required_markers):
        raise OperatorPackageError("operator_editorial_source_structure_invalid")

    source_hash = hashlib.sha256(data).hexdigest()
    package_id = "ocp-" + hashlib.sha256((source_hash + "|" + operator_request_id).encode("utf-8")).hexdigest()[:24]
    disclaimer_match = re.search(
        r"(Материал основан.+?сформулированы как зафиксированные в отчёте\.\n"
        r"Пост, карусель и Reel.+?копии одного текста\.)",
        text,
        re.DOTALL,
    )
    if not disclaimer_match:
        raise OperatorPackageError("operator_editorial_disclaimer_missing")
    disclaimer = " ".join(disclaimer_match.group(1).split())

    facts_section = _section(text, "1. Карта фактов", "2. Что намеренно не утверждается")
    facts: list[dict[str, Any]] = []
    fact_pattern = re.compile(
        r"(?ms)^(F[1-7])\s*\n+(.+?)\n+(Да|Обязательно)\s*\n+(.+?)(?=\n+F[1-7]\s*\n|\n+Главное меню)",
    )
    for match in fact_pattern.finditer(facts_section):
        facts.append({
            "fact_id": match.group(1),
            "statement": " ".join(match.group(2).split()),
            "publication_status": "required" if match.group(3) == "Обязательно" else "approved",
            "restriction": " ".join(match.group(4).split()),
        })
    if [item["fact_id"] for item in facts] != [f"F{i}" for i in range(1, 8)]:
        raise OperatorPackageError("operator_editorial_fact_map_invalid")

    prohibited_section = _section(text, "2. Что намеренно не утверждается", "3. Выбранная подача")
    prohibited = [
        "Новая навигация уже развёрнута в production.",
        "Система абсолютно безопасна и будущих ошибок не будет.",
        "Обнаруженные дефекты затронули реальных пользователей.",
        "Изменения дали финансовый результат, конверсию, пользователей или ущерб.",
        "Публикация содержит Telegram ID, локальные пути, внутренние хеши или данные об изменении доступа.",
    ]
    if not all(anchor in prohibited_section for anchor in ("Не говорим", "Не обещаем", "полностью исключаются")):
        raise OperatorPackageError("operator_prohibited_claims_invalid")

    post = _section(text, "4. Готовый пост", "5. Карусель — 8 слайдов")
    reel_section = _section(text, "6. Reel с voice-over", "7. DaVinci Resolve — монтажная карта")
    raw_scene_lines = [line.strip() for line in reel_section.splitlines() if line.strip()]
    scenes: list[dict[str, Any]] = []
    fact_refs = {
        1: ["F4"], 2: ["F1", "F2", "F4"], 3: ["F4"], 4: ["F4"],
        5: ["F4", "F5"], 6: ["F5"], 7: ["F5"], 8: ["F6", "F7"], 9: ["F7"],
    }
    for position, line in enumerate(raw_scene_lines):
        if not re.fullmatch(r"[1-9]", line):
            continue
        number = int(line)
        if position + 3 >= len(raw_scene_lines):
            raise OperatorPackageError("operator_editorial_scene_invalid")
        timing = raw_scene_lines[position + 1]
        if not re.fullmatch(r"00:\d{2}[–-]00:\d{2}", timing):
            continue
        start_second, end_second = _parse_time(timing)
        scenes.append({
            "scene_id": f"scene-{number:02d}", "order": number,
            "start_second": start_second, "end_second": end_second,
            "screen_text": raw_scene_lines[position + 2],
            "visual": raw_scene_lines[position + 3],
            "fact_refs": fact_refs[number],
        })
    if len(scenes) != 9:
        raise OperatorPackageError("operator_editorial_scene_invalid")

    voice_over = _section(text, "8. Чистовой voice-over", "9. Music brief")
    music = _section(text, "9. Music brief", "10. Направления поиска музыки")
    rights = _section(text, "11. Rights status", "12. Stories")
    if "UNCLEAR_DO_NOT_USE" not in rights:
        raise OperatorPackageError("operator_rights_status_invalid")
    caption = _section(text, "13. Caption для Reel", "14. Обложка и alt text")
    cover = _section(text, "14. Обложка и alt text", "15. Имена файлов")
    cover_text = _section(cover, "Текст обложки", "Подзаголовок:")
    subtitle = _section(cover, "Подзаголовок:", "Композиция")
    composition = _section(cover, "Композиция", "Alt text")
    alt_text = cover[cover.find("Alt text") + len("Alt text"):].strip()
    excluded_match = re.search(r"Что исключить:\s*(.+)", music)
    excluded = [item.strip() for item in excluded_match.group(1).split(",")] if excluded_match else []

    package: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "package_id": package_id,
        "operator_request_id": operator_request_id,
        "source_provenance": {
            "kind": "editorial-development-log-adaptation",
            "source_date": "2026-08-08",
            "source_title": "future_self_bot дневной чат проекта",
            "source_document_sha256": source_hash,
            "editorial_basis": "Редакционно подготовленная адаптация дневного журнала разработки.",
        },
        "editorial_disclaimer": disclaimer,
        "approved_fact_map": facts,
        "prohibited_claims": prohibited,
        "title": "Сообщение, которое бот не отправил",
        "story_post_adaptation": post,
        "reel": {"duration_seconds": 47, "format": "9:16/1080x1920/30fps", "scenes": scenes},
        "voice_over": voice_over,
        "caption": caption,
        "cover_brief": {
            "text": cover_text, "subtitle": subtitle,
            "composition": composition, "alt_text": alt_text,
        },
        "music_brief": {
            "brief": music, "tempo_bpm": [92, 98],
            "mood": "Собранное техническое расследование с лёгким напряжением и тёплым разрешением.",
            "excluded": excluded, "track": None,
        },
        "rights_status": RIGHTS_UNCLEAR,
        "publication_restrictions": [
            "no_automatic_publication", "no_music_without_rights_approval",
            "second_admin_action_required", "no_claim_of_production_release",
        ],
    }
    return validate_package(package)


def _assert_safe_root(root: Path) -> Path:
    value = Path(root).expanduser()
    if not value.is_absolute():
        value = value.resolve()
    resolved_parent = value.parent.resolve()
    if value.exists() and value.is_symlink():
        raise OperatorPackageError("operator_store_symlink_forbidden")
    current = resolved_parent
    while current != current.parent:
        if current.is_symlink():
            raise OperatorPackageError("operator_store_symlink_forbidden")
        current = current.parent
    value.mkdir(parents=True, exist_ok=True)
    os.chmod(value, _PRIVATE_DIR_MODE)
    resolved = value.resolve()
    if not resolved.is_dir():
        raise OperatorPackageError("operator_store_root_invalid")
    return resolved


def _safe_child(root: Path, *parts: str) -> Path:
    candidate = root.joinpath(*parts)
    resolved_parent = candidate.parent.resolve()
    if root != resolved_parent and root not in resolved_parent.parents:
        raise OperatorPackageError("operator_store_path_invalid")
    for parent in (candidate.parent, *candidate.parent.parents):
        if parent == root.parent:
            break
        if parent.exists() and parent.is_symlink():
            raise OperatorPackageError("operator_store_symlink_forbidden")
    return candidate


def _atomic_create(path: Path, data: bytes) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, _PRIVATE_DIR_MODE)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, _PRIVATE_FILE_MODE)
        try:
            os.link(temporary, path)
            os.chmod(path, _PRIVATE_FILE_MODE)
            return True
        except FileExistsError:
            return False
    finally:
        temporary.unlink(missing_ok=True)


def _read_private_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_PACKAGE_BYTES:
        raise OperatorPackageError("operator_store_artifact_invalid")
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise OperatorPackageError("operator_store_artifact_invalid")
    return value


def import_package(
    root: Path,
    package: dict[str, Any],
    *,
    operator_id: int,
    expected_operator_id: int,
) -> ImportedOperatorPackage:
    if type(operator_id) is not int or type(expected_operator_id) is not int or operator_id <= 0 or operator_id != expected_operator_id:
        raise OperatorPackageError("operator_package_admin_required")
    validate_package(package)
    base = _assert_safe_root(root)
    digest = package_digest(package)
    package_id = package["package_id"]
    request_id = package["operator_request_id"]
    package_path = _safe_child(base, "packages", package_id, "package.json")
    request_key = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    request_path = _safe_child(base, "requests", f"{request_key}.json")
    request_record = {
        "schema_version": "operator-content-package-request-v1",
        "operator_request_id": request_id,
        "operator_id": operator_id,
        "package_id": package_id,
        "package_digest": digest,
    }
    request_bytes = (json.dumps(request_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    if request_path.exists():
        if request_path.read_bytes() != request_bytes:
            raise OperatorPackageConflict("operator_request_conflict")
        if not package_path.is_file() or package_path.read_bytes() != canonical_package_bytes(package):
            raise OperatorPackageConflict("operator_request_package_mismatch")
        imported = ImportedOperatorPackage(package_id, digest, request_id, operator_id, package_path, False)
        _ensure_callback_bindings(base, imported)
        return imported
    created = _atomic_create(package_path, canonical_package_bytes(package))
    if not created and package_path.read_bytes() != canonical_package_bytes(package):
        raise OperatorPackageConflict("operator_package_id_conflict")
    if not _atomic_create(request_path, request_bytes) and request_path.read_bytes() != request_bytes:
        raise OperatorPackageConflict("operator_request_conflict")
    imported = ImportedOperatorPackage(package_id, digest, request_id, operator_id, package_path, created)
    _ensure_callback_bindings(base, imported)
    return imported


def _callback_token(imported: ImportedOperatorPackage, action: str) -> str:
    return hashlib.sha256(
        f"{action}|{imported.operator_id}|{imported.package_id}|{imported.package_digest}|{imported.operator_request_id}".encode("utf-8")
    ).hexdigest()[:24]


def _ensure_callback_bindings(root: Path, imported: ImportedOperatorPackage) -> None:
    for action in CALLBACK_ACTIONS:
        token = _callback_token(imported, action)
        record = {
            "schema_version": "operator-content-package-callback-v1",
            "action": action, "token": token,
            "package_id": imported.package_id,
            "package_digest": imported.package_digest,
            "operator_request_id": imported.operator_request_id,
            "operator_id": imported.operator_id,
        }
        data = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        path = _safe_child(root, "callbacks", f"{token}.json")
        if not _atomic_create(path, data) and path.read_bytes() != data:
            raise OperatorPackageConflict("operator_callback_conflict")


def callback_data(imported: ImportedOperatorPackage, action: str) -> str:
    if action not in CALLBACK_ACTIONS:
        raise OperatorPackageError("operator_callback_action_invalid")
    return f"ocp:{action}:{_callback_token(imported, action)}"


def resolve_callback(root: Path, callback: str, *, operator_id: int, expected_operator_id: int) -> CallbackBinding:
    if type(callback) is not str:
        raise OperatorPackageError("operator_callback_invalid")
    match = re.fullmatch(r"ocp:(build|script|skip|publish|remake|cancel):([a-f0-9]{24})", callback)
    if not match:
        raise OperatorPackageError("operator_callback_invalid")
    if type(operator_id) is not int or operator_id <= 0 or operator_id != expected_operator_id:
        raise OperatorPackageError("operator_package_admin_required")
    action, token = match.groups()
    base = _assert_safe_root(root)
    record = _read_private_json(_safe_child(base, "callbacks", f"{token}.json"))
    expected_keys = {"schema_version", "action", "token", "package_id", "package_digest", "operator_request_id", "operator_id"}
    if set(record) != expected_keys or record.get("schema_version") != "operator-content-package-callback-v1":
        raise OperatorPackageError("operator_callback_binding_invalid")
    if record.get("action") != action or record.get("token") != token or record.get("operator_id") != operator_id:
        raise OperatorPackageError("operator_callback_binding_invalid")
    package_path = _safe_child(base, "packages", str(record.get("package_id")), "package.json")
    package = parse_json_package(package_path.read_bytes())
    if package_digest(package) != record.get("package_digest") or package["operator_request_id"] != record.get("operator_request_id"):
        raise OperatorPackageError("operator_callback_binding_invalid")
    return CallbackBinding(action, token, package["package_id"], record["package_digest"], package["operator_request_id"], operator_id)


def load_bound_package(root: Path, binding: CallbackBinding) -> dict[str, Any]:
    base = _assert_safe_root(root)
    path = _safe_child(base, "packages", binding.package_id, "package.json")
    package = parse_json_package(path.read_bytes())
    if package_digest(package) != binding.package_digest or package["operator_request_id"] != binding.operator_request_id:
        raise OperatorPackageError("operator_callback_binding_invalid")
    return package


def preview_text(package: Mapping[str, Any]) -> str:
    return (
        f"🎬 Операторский контент-пакет\n\n"
        f"Заголовок: {package['title']}\n"
        f"Длительность: {package['reel']['duration_seconds']} секунд\n"
        f"Сцен: {len(package['reel']['scenes'])}\n\n"
        f"{PREVIEW_DESCRIPTION}\n\n"
        f"Редакционная граница:\n{package['editorial_disclaimer']}\n\n"
        f"Права: {package['rights_status']}\n"
        "Музыка не выбрана и не встраивается: права пока не подтверждены."
    )


def script_text(package: Mapping[str, Any]) -> str:
    scene_lines = [
        f"{scene['order']}. {scene['start_second']:02d}–{scene['end_second']:02d} сек · {scene['screen_text']}\n{scene['visual']}"
        for scene in package["reel"]["scenes"]
    ]
    cover = package["cover_brief"]
    return (
        f"🎙 {package['title']}\n\nСЦЕН-ПЛАН\n" + "\n\n".join(scene_lines)
        + f"\n\nVOICE-OVER\n{package['voice_over']}"
        + f"\n\nCAPTION\n{package['caption']}"
        + f"\n\nОБЛОЖКА\n{cover['text']}\n{cover['subtitle']}"
    )


def media_pipeline_compatible(package: Mapping[str, Any]) -> bool:
    """Truthful bridge gate for the current story worker contract.

    The existing worker accepts 4–7 scenes and a 12–20 second Reel.  The gate
    prevents the 9-scene/47-second editorial package from being distorted or
    silently sent to providers.
    """
    return (
        4 <= len(package["reel"]["scenes"]) <= 7
        and 12 <= package["reel"]["duration_seconds"] <= 20
        and package["rights_status"] == RIGHTS_UNCLEAR
        and package["music_brief"]["track"] is None
    )


def assert_no_music(package: Mapping[str, Any]) -> None:
    if package["rights_status"] != RIGHTS_UNCLEAR or package["music_brief"]["track"] is not None:
        raise OperatorPackageError("operator_music_rights_gate_failed")


def private_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)
