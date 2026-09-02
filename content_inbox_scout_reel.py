"""Private append-only promotion and local Reel rendering for Scout materials."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont

import content_inbox_scout as scout


SELECTION_SCHEMA = "content-inbox-selected-material-v1"
SELECTION_REQUEST_SCHEMA = "content-inbox-selected-material-request-v1"
SELECTION_STATE_SCHEMA = "content-inbox-selected-material-state-v1"
JOB_SCHEMA = "content-inbox-scout-reel-job-v1"
JOB_STATE_SCHEMA = "content-inbox-scout-reel-job-state-v1"
RECEIPT_SCHEMA = "content-inbox-scout-reel-receipt-v1"
RENDER_PROFILE = "content-inbox-scout-local-motion-v1"
CALLBACK_PREFIX = "scoutreel"
SELECTION_ID_RE = re.compile(r"^css-[a-f0-9]{24}$")
JOB_ID_RE = re.compile(r"^csj-[a-f0-9]{24}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,95}$")
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
SELECTION_STATES = frozenset({"selected", "build_reserved", "rendering", "preview_ready", "cancelled", "failed"})
JOB_STATES = frozenset({"reserved", "rendering", "preview_ready", "failed", "cancelled"})
CALLBACK_ACTIONS = frozenset({"build", "show", "storyboard", "other", "cancel", "publish", "remake"})
TtsCall = Callable[[str], Awaitable[bytes]]


class ScoutReelError(RuntimeError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


class ScoutReelConflict(ScoutReelError):
    pass


@dataclass(frozen=True, slots=True)
class SelectedMaterial:
    selection_id: str
    admin_id: int
    run_id: str
    candidate_id: str
    ranking_artifact_digest: str
    ready_material_artifact_digest: str
    title: str
    telegram_post_digest: str
    voice_over_digest: str
    duration_seconds: int
    scene_count: int
    caption_digest: str
    cover_text_digest: str
    selection_request_id: str
    state: str
    created_timestamp: str
    selection_dir: Path


@dataclass(frozen=True, slots=True)
class ReelJob:
    job_id: str
    selection_id: str
    ready_material_digest: str
    admin_id: int
    title: str
    duration_seconds: int
    ordered_scenes: tuple[Mapping[str, Any], ...]
    voice_over_digest: str
    render_profile: str
    state: str
    output_digest: str | None
    job_dir: Path


@dataclass(frozen=True, slots=True)
class RenderedPreview:
    job: ReelJob
    output_path: Path
    receipt: Mapping[str, Any]
    tts_calls: int
    render_calls: int


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _digest_text(value: str) -> str:
    return _digest_bytes(value.encode("utf-8"))


def _timestamp(value: str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if type(value) is not str or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ScoutReelError("content_scout_reel_timestamp_invalid")
    return value


def _ensure_private_root(root: Path) -> Path:
    if not isinstance(root, Path):
        raise ScoutReelError("content_scout_reel_root_invalid")
    root = root.absolute()
    cursor = root.anchor and Path(root.anchor)
    for part in root.parts[1:] if root.anchor else root.parts:
        cursor = cursor / part if cursor else Path(part)
        if cursor.exists() and cursor.is_symlink():
            raise ScoutReelError("content_scout_reel_symlink_forbidden")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise ScoutReelError("content_scout_reel_root_invalid")
    try:
        os.chmod(root, 0o700)
    except OSError as exc:
        raise ScoutReelError("content_scout_reel_permissions_invalid") from exc
    return root


def _child(root: Path, *parts: str) -> Path:
    if any(type(part) is not str or not part or Path(part).name != part for part in parts):
        raise ScoutReelError("content_scout_reel_path_invalid")
    path = root.joinpath(*parts)
    cursor = root
    for part in parts[:-1]:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise ScoutReelError("content_scout_reel_symlink_forbidden")
    return path


def _read_bytes(path: Path, maximum: int = 2 * 1024 * 1024) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ScoutReelError("content_scout_reel_artifact_missing") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= maximum:
            raise ScoutReelError("content_scout_reel_artifact_invalid")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ScoutReelError("content_scout_reel_artifact_invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ScoutReelError("content_scout_reel_artifact_invalid")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_bytes(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoutReelError("content_scout_reel_artifact_invalid") from exc
    if type(value) is not dict:
        raise ScoutReelError("content_scout_reel_artifact_invalid")
    return value


def _prepare_private_directory(directory: Path) -> None:
    missing: list[Path] = []
    cursor = directory
    while not cursor.exists():
        missing.append(cursor)
        if cursor == cursor.parent:
            raise ScoutReelError("content_scout_reel_path_invalid")
        cursor = cursor.parent
    if cursor.is_symlink() or not cursor.is_dir():
        raise ScoutReelError("content_scout_reel_symlink_forbidden")
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        if directory.is_symlink() or not directory.is_dir():
            raise ScoutReelError("content_scout_reel_symlink_forbidden")
        os.chmod(directory, 0o700)
    if cursor.is_symlink() or not cursor.is_dir():
        raise ScoutReelError("content_scout_reel_symlink_forbidden")
    if directory.is_symlink() or not directory.is_dir():
        raise ScoutReelError("content_scout_reel_symlink_forbidden")
    os.chmod(directory, 0o700)


def _prepare_private_parent(path: Path) -> None:
    _prepare_private_directory(path.parent)


def _write_exact(path: Path, value: Mapping[str, Any], conflict: str) -> bool:
    data = _canonical(value)
    if path.exists():
        if path.is_symlink() or _read_bytes(path, max(len(data), 1)) != data:
            raise ScoutReelConflict(conflict)
        return False
    _prepare_private_parent(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True


def _write_binary_exact(path: Path, data: bytes, conflict: str) -> bool:
    if type(data) is not bytes or not data or len(data) > 32 * 1024 * 1024:
        raise ScoutReelError("content_scout_reel_binary_invalid")
    if path.exists():
        if path.is_symlink() or _read_bytes(path, max(len(data), 1)) != data:
            raise ScoutReelConflict(conflict)
        return False
    _prepare_private_parent(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    return True


def _selection_from_record(value: Mapping[str, Any], directory: Path) -> SelectedMaterial:
    fields = {field.name for field in SelectedMaterial.__dataclass_fields__.values()} - {"selection_dir"}
    if (
        type(value) is not dict
        or set(value) != fields | {"schema_version"}
        or value.get("schema_version") != SELECTION_SCHEMA
        or type(value.get("selection_id")) is not str
        or not SELECTION_ID_RE.fullmatch(value["selection_id"])
        or type(value.get("admin_id")) is not int
        or value["admin_id"] <= 0
        or type(value.get("run_id")) is not str
        or not scout.RUN_ID_RE.fullmatch(value["run_id"])
        or type(value.get("candidate_id")) is not str
        or not scout.CANDIDATE_ID_RE.fullmatch(value["candidate_id"])
        or any(type(value.get(key)) is not str or not DIGEST_RE.fullmatch(value[key]) for key in (
            "ranking_artifact_digest", "ready_material_artifact_digest", "telegram_post_digest",
            "voice_over_digest", "caption_digest", "cover_text_digest",
        ))
        or type(value.get("title")) is not str
        or not value["title"].strip()
        or len(value["title"]) > 180
        or type(value.get("duration_seconds")) is not int
        or value["duration_seconds"] != 15
        or type(value.get("scene_count")) is not int
        or value["scene_count"] != 5
        or type(value.get("selection_request_id")) is not str
        or not REQUEST_ID_RE.fullmatch(value["selection_request_id"])
        or value.get("state") != "selected"
        or type(value.get("created_timestamp")) is not str
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value["created_timestamp"])
    ):
        raise ScoutReelError("content_scout_selection_artifact_invalid")
    return SelectedMaterial(**{key: value[key] for key in fields}, selection_dir=directory)


def _validate_scenes(material: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    scenes = material.get("scenes")
    duration = material.get("reel_duration_seconds")
    if type(duration) is not int or duration != 15 or type(scenes) is not list or len(scenes) != 5:
        raise ScoutReelError("content_scout_selection_reel_contract_invalid")
    expected_start = 0
    result: list[Mapping[str, Any]] = []
    for order, scene in enumerate(scenes, start=1):
        if (
            type(scene) is not dict
            or set(scene) != {"order", "start_second", "end_second", "screen_text", "visual_brief"}
            or scene.get("order") != order
            or scene.get("start_second") != expected_start
            or type(scene.get("end_second")) is not int
            or scene["end_second"] <= expected_start
            or type(scene.get("screen_text")) is not str
            or not scene["screen_text"].strip()
            or len(scene["screen_text"]) > 180
            or type(scene.get("visual_brief")) is not str
            or not scene["visual_brief"].strip()
            or len(scene["visual_brief"]) > 500
        ):
            raise ScoutReelError("content_scout_selection_scene_invalid")
        expected_start = scene["end_second"]
        result.append(dict(scene))
    if expected_start != duration:
        raise ScoutReelError("content_scout_selection_scene_invalid")
    return tuple(result)


def promote_selection(
    state_root: Path,
    scout_root: Path,
    run_id: str,
    candidate_id: str,
    *,
    admin_id: int,
    expected_admin_id: int,
    selection_request_id: str,
    risk_detector: scout.RiskDetector,
    created_timestamp: str | None = None,
) -> tuple[SelectedMaterial, Mapping[str, Any], bool]:
    if type(admin_id) is not int or admin_id <= 0 or admin_id != expected_admin_id:
        raise ScoutReelError("content_scout_reel_admin_required")
    if type(selection_request_id) is not str or not REQUEST_ID_RE.fullmatch(selection_request_id):
        raise ScoutReelError("content_scout_selection_request_invalid")
    run, _candidate, material, ready_path = scout.load_ready_material(
        scout_root, run_id, candidate_id, risk_detector=risk_detector,
        require_current_russian=True,
    )
    scenes = _validate_scenes(material)
    ranking_path = run.run_dir / "ranking.json"
    ranking_digest = _digest_bytes(_read_bytes(ranking_path))
    ready_digest = _digest_bytes(_read_bytes(ready_path))
    identity = {
        "admin_id": admin_id,
        "run_id": run_id,
        "candidate_id": candidate_id,
        "ranking_artifact_digest": ranking_digest,
        "ready_material_artifact_digest": ready_digest,
    }
    selection_id = "css-" + _digest_bytes(_canonical(identity))[:24]
    root = _ensure_private_root(state_root)
    _prepare_private_directory(_child(root, "requests"))
    _prepare_private_directory(_child(root, "selections"))
    directory = _child(root, "selections", selection_id)
    request_key = _digest_text(selection_request_id)
    request = {
        "schema_version": SELECTION_REQUEST_SCHEMA,
        "selection_request_id": selection_request_id,
        "selection_id": selection_id,
        **identity,
    }
    _write_exact(_child(root, "requests", f"{request_key}.json"), request, "content_scout_selection_request_conflict")
    record = {
        "schema_version": SELECTION_SCHEMA,
        "selection_id": selection_id,
        "admin_id": admin_id,
        "run_id": run_id,
        "candidate_id": candidate_id,
        "ranking_artifact_digest": ranking_digest,
        "ready_material_artifact_digest": ready_digest,
        "title": material["title"],
        "telegram_post_digest": _digest_text(material["telegram_post"]),
        "voice_over_digest": _digest_text(material["reel_voice_over"]),
        "duration_seconds": material["reel_duration_seconds"],
        "scene_count": len(scenes),
        "caption_digest": _digest_text(material["caption"]),
        "cover_text_digest": _digest_text(material["cover_text"]),
        "selection_request_id": selection_request_id,
        "state": "selected",
        "created_timestamp": _timestamp(created_timestamp),
    }
    selection_path = directory / "selection.json"
    if selection_path.exists():
        existing = _read_json(selection_path)
        existing_without_timestamp = {key: value for key, value in existing.items() if key != "created_timestamp"}
        requested_without_timestamp = {key: value for key, value in record.items() if key != "created_timestamp"}
        if existing_without_timestamp != requested_without_timestamp:
            raise ScoutReelConflict("content_scout_selection_conflict")
        selected = _selection_from_record(existing, directory)
        created = False
    else:
        created = _write_exact(selection_path, record, "content_scout_selection_conflict")
        selected = _selection_from_record(record, directory)
    return selected, material, created


def locate_selected_material(
    scout_root: Path,
    *,
    expected_admin_id: int,
    risk_detector: scout.RiskDetector,
    title_fragment: str,
) -> tuple[str, str]:
    if type(expected_admin_id) is not int or expected_admin_id <= 0:
        raise ScoutReelError("content_scout_reel_admin_required")
    if type(title_fragment) is not str or not title_fragment.strip() or len(title_fragment) > 180:
        raise ScoutReelError("blocked_selection_ambiguous")
    directory = scout_root.absolute() / "preferences" / "selected"
    if not directory.is_dir() or directory.is_symlink():
        raise ScoutReelError("blocked_selection_ambiguous")
    matches: list[tuple[str, str]] = []
    for path in sorted(directory.glob("csc-*/*.json")):
        try:
            preference = json.loads(_read_bytes(path))
            run_id = preference.get("run_id")
            candidate_id = preference.get("candidate_id")
            if (
                type(preference) is not dict
                or set(preference) != {"schema_version", "run_id", "candidate_id", "admin_id", "preference"}
                or preference.get("schema_version") != scout.PREFERENCE_SCHEMA
                or preference.get("preference") != "selected"
                or preference.get("admin_id") != expected_admin_id
                or type(run_id) is not str
                or not scout.RUN_ID_RE.fullmatch(run_id)
                or type(candidate_id) is not str
                or not scout.CANDIDATE_ID_RE.fullmatch(candidate_id)
                or path.parent.name != candidate_id
                or path.stem != run_id
            ):
                continue
            _run, _candidate, material, _ready_path = scout.load_ready_material(
                scout_root, run_id, candidate_id, risk_detector=risk_detector,
                require_current_russian=True,
            )
            _validate_scenes(material)
            if title_fragment.casefold() in str(material.get("title", "")).casefold():
                matches.append((run_id, candidate_id))
        except (OSError, ValueError, scout.ScoutError, ScoutReelError):
            continue
    if len(matches) != 1:
        raise ScoutReelError("blocked_selection_ambiguous")
    return matches[0]


def load_selection(state_root: Path, selection_id: str, *, admin_id: int, expected_admin_id: int) -> SelectedMaterial:
    if admin_id != expected_admin_id or type(admin_id) is not int:
        raise ScoutReelError("content_scout_reel_admin_required")
    if type(selection_id) is not str or not SELECTION_ID_RE.fullmatch(selection_id):
        raise ScoutReelError("content_scout_selection_id_invalid")
    root = _ensure_private_root(state_root)
    directory = _child(root, "selections", selection_id)
    selected = _selection_from_record(_read_json(directory / "selection.json"), directory)
    if selected.admin_id != admin_id:
        raise ScoutReelError("content_scout_reel_admin_required")
    return selected


def load_selected_ready_material(
    scout_root: Path,
    selected: SelectedMaterial,
    *,
    risk_detector: scout.RiskDetector,
) -> tuple[scout.ScoutRunResult, Mapping[str, Any]]:
    """Re-open and verify the immutable ready artifact bound by a selection."""
    run, _candidate, material, ready_path = scout.load_ready_material(
        scout_root,
        selected.run_id,
        selected.candidate_id,
        risk_detector=risk_detector,
        require_current_russian=True,
    )
    ranking_path = run.run_dir / "ranking.json"
    bindings = {
        "ranking_artifact_digest": _digest_bytes(_read_bytes(ranking_path)),
        "ready_material_artifact_digest": _digest_bytes(_read_bytes(ready_path)),
        "telegram_post_digest": _digest_text(material["telegram_post"]),
        "voice_over_digest": _digest_text(material["reel_voice_over"]),
        "caption_digest": _digest_text(material["caption"]),
        "cover_text_digest": _digest_text(material["cover_text"]),
    }
    for field, actual in bindings.items():
        if getattr(selected, field) != actual:
            raise ScoutReelError("content_scout_selection_binding_invalid")
    scenes = _validate_scenes(material)
    if (
        material["title"] != selected.title
        or material["reel_duration_seconds"] != selected.duration_seconds
        or len(scenes) != selected.scene_count
    ):
        raise ScoutReelError("content_scout_selection_binding_invalid")
    return run, material


def _state_event(
    directory: Path,
    schema: str,
    identity_key: str,
    identity: str,
    state: str,
    reason: str | None = None,
    *,
    output_digest: str | None = None,
) -> bool:
    if (
        (schema == SELECTION_STATE_SCHEMA and state not in SELECTION_STATES)
        or (schema == JOB_STATE_SCHEMA and state not in JOB_STATES)
        or schema not in {SELECTION_STATE_SCHEMA, JOB_STATE_SCHEMA}
        or type(reason) not in {str, type(None)}
    ):
        raise ScoutReelError("content_scout_reel_state_invalid")
    record: dict[str, Any] = {"schema_version": schema, identity_key: identity, "state": state}
    if reason is not None:
        record["reason"] = reason
    if output_digest is not None:
        if not DIGEST_RE.fullmatch(output_digest):
            raise ScoutReelError("content_scout_reel_output_invalid")
        record["output_digest"] = output_digest
    return _write_exact(directory / "states" / f"{state}.json", record, "content_scout_reel_state_conflict")


def cancel_selection(state_root: Path, selection_id: str, *, admin_id: int, expected_admin_id: int) -> SelectedMaterial:
    selected = load_selection(state_root, selection_id, admin_id=admin_id, expected_admin_id=expected_admin_id)
    _state_event(selected.selection_dir, SELECTION_STATE_SCHEMA, "selection_id", selection_id, "cancelled")
    return selected


def _selection_is_terminal(selected: SelectedMaterial) -> bool:
    states = selected.selection_dir / "states"
    return (states / "cancelled.json").exists() or (states / "failed.json").exists()


def selection_card_text(selected: SelectedMaterial) -> str:
    return (
        f"✅ Материал выбран и готов к сборке\n\n{selected.title}\n\n"
        f"Хронометраж: {selected.duration_seconds} секунд\n"
        f"Сцен: {selected.scene_count}\nЯзык: русский\nСтатус: приватный черновик\n\n"
        "Текст, озвучка и сцен-план уже сохранены.\n"
        "Повторная генерация материала не требуется."
    )


def callback_data(action: str, selection_id: str) -> str:
    if action not in CALLBACK_ACTIONS or type(selection_id) is not str or not SELECTION_ID_RE.fullmatch(selection_id):
        raise ScoutReelError("content_scout_reel_callback_invalid")
    value = f"{CALLBACK_PREFIX}:{action}:{selection_id[4:]}"
    if len(value.encode("utf-8")) > 64:
        raise ScoutReelError("content_scout_reel_callback_invalid")
    return value


def parse_callback(value: Any) -> tuple[str, str]:
    if type(value) is not str:
        raise ScoutReelError("content_scout_reel_callback_invalid")
    match = re.fullmatch(r"scoutreel:(build|show|storyboard|other|cancel|publish|remake):([a-f0-9]{24})", value)
    if match is None:
        raise ScoutReelError("content_scout_reel_callback_invalid")
    return match.group(1), "css-" + match.group(2)


def _job_from_record(value: Mapping[str, Any], directory: Path) -> ReelJob:
    expected = {
        "schema_version", "job_id", "selection_id", "ready_material_digest", "admin_id",
        "title", "duration_seconds", "ordered_scenes", "voice_over_digest", "render_profile",
        "state", "output_digest",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_version") != JOB_SCHEMA
        or type(value.get("job_id")) is not str
        or not JOB_ID_RE.fullmatch(value["job_id"])
        or type(value.get("selection_id")) is not str
        or not SELECTION_ID_RE.fullmatch(value["selection_id"])
        or any(type(value.get(key)) is not str or not DIGEST_RE.fullmatch(value[key]) for key in ("ready_material_digest", "voice_over_digest"))
        or type(value.get("admin_id")) is not int
        or type(value.get("title")) is not str
        or value.get("duration_seconds") != 15
        or type(value.get("ordered_scenes")) is not list
        or len(value["ordered_scenes"]) != 5
        or value.get("render_profile") != RENDER_PROFILE
        or value.get("state") != "reserved"
        or value.get("output_digest") is not None
    ):
        raise ScoutReelError("content_scout_reel_job_invalid")
    _validate_scenes({
        "reel_duration_seconds": value["duration_seconds"],
        "scenes": value["ordered_scenes"],
    })
    return ReelJob(
        job_id=value["job_id"], selection_id=value["selection_id"],
        ready_material_digest=value["ready_material_digest"], admin_id=value["admin_id"],
        title=value["title"], duration_seconds=value["duration_seconds"],
        ordered_scenes=tuple(dict(item) for item in value["ordered_scenes"]),
        voice_over_digest=value["voice_over_digest"], render_profile=value["render_profile"],
        state=value["state"], output_digest=value["output_digest"], job_dir=directory,
    )


def reserve_job(state_root: Path, selected: SelectedMaterial, material: Mapping[str, Any]) -> tuple[ReelJob, bool]:
    if _selection_is_terminal(selected):
        raise ScoutReelError("content_scout_selection_terminal")
    scenes = _validate_scenes(material)
    if _digest_text(material["reel_voice_over"]) != selected.voice_over_digest:
        raise ScoutReelError("content_scout_reel_voice_binding_invalid")
    job_id = "csj-" + _digest_text(f"{selected.selection_id}|{selected.ready_material_artifact_digest}|{RENDER_PROFILE}")[:24]
    root = _ensure_private_root(state_root)
    _prepare_private_directory(_child(root, "jobs"))
    directory = _child(root, "jobs", job_id)
    record = {
        "schema_version": JOB_SCHEMA,
        "job_id": job_id,
        "selection_id": selected.selection_id,
        "ready_material_digest": selected.ready_material_artifact_digest,
        "admin_id": selected.admin_id,
        "title": selected.title,
        "duration_seconds": selected.duration_seconds,
        "ordered_scenes": [dict(item) for item in scenes],
        "voice_over_digest": selected.voice_over_digest,
        "render_profile": RENDER_PROFILE,
        "state": "reserved",
        "output_digest": None,
    }
    created = _write_exact(directory / "job.json", record, "content_scout_reel_job_conflict")
    job = _job_from_record(record if created else _read_json(directory / "job.json"), directory)
    _state_event(selected.selection_dir, SELECTION_STATE_SCHEMA, "selection_id", selected.selection_id, "build_reserved")
    return job, created


def existing_local_storyboard(state_root: Path, selected: SelectedMaterial) -> Path | None:
    """Return an already-rendered immutable technical storyboard; never create one."""
    job_id = "csj-" + _digest_text(
        f"{selected.selection_id}|{selected.ready_material_artifact_digest}|{RENDER_PROFILE}"
    )[:24]
    root = state_root.absolute()
    job_path = root / "jobs" / job_id / "job.json"
    output = root / "jobs" / job_id / "preview.mp4"
    receipt = root / "jobs" / job_id / "receipt.json"
    if not (job_path.is_file() and output.is_file() and receipt.is_file()):
        return None
    job = _job_from_record(_read_json(job_path), job_path.parent)
    payload = _read_json(receipt)
    if (
        job.selection_id != selected.selection_id
        or payload.get("render_profile") != RENDER_PROFILE
        or payload.get("output_sha256") != _digest_bytes(_read_bytes(output, 64 * 1024 * 1024))
    ):
        raise ScoutReelError("content_scout_reel_receipt_invalid")
    return output


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    )
    for path in candidates:
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines[:8]


def _draw_scene(path: Path, scene: Mapping[str, Any], title: str) -> None:
    image = Image.new("RGB", (1080, 1920), "#181c1b")
    draw = ImageDraw.Draw(image)
    accent = "#d4a642" if int(scene["order"]) <= 3 else "#6eaa83"
    draw.rounded_rectangle((70, 90, 1010, 1830), radius=48, fill="#252b29", outline=accent, width=6)
    draw.text((110, 135), f"СЦЕНА {scene['order']} ИЗ 5", font=_font(38), fill=accent)
    y = 300
    title_font = _font(62)
    for line in _wrap(draw, str(scene["screen_text"]), title_font, 820):
        draw.text((130, y), line, font=title_font, fill="white")
        y += 82
    if int(scene["order"]) == 2:
        draw.rounded_rectangle((150, 980, 930, 1280), radius=32, fill="#303836")
        draw.text((220, 1090), "SQLite", font=_font(72), fill="#e8ecea")
    elif int(scene["order"]) == 3:
        draw.line((180, 1110, 900, 1110), fill=accent, width=28)
        draw.ellipse((500, 1050, 610, 1160), fill="#181c1b", outline="#d4a642", width=10)
    elif int(scene["order"]) == 4:
        draw.line((180, 1100, 900, 1100), fill="#6eaa83", width=28)
        draw.polygon(((850, 1035), (950, 1100), (850, 1165)), fill="#6eaa83")
    elif int(scene["order"]) == 5:
        for index in range(4):
            top = 900 + index * 135
            draw.rounded_rectangle((170, top, 900, top + 90), radius=22, fill="#303836")
            draw.text((210, top + 18), "✓", font=_font(44), fill="#6eaa83")
    else:
        draw.rounded_rectangle((160, 900, 920, 1250), radius=34, fill="#303836")
        draw.rounded_rectangle((220, 965, 760, 1055), radius=20, fill="#424b48")
        draw.rounded_rectangle((360, 1110, 860, 1200), radius=20, fill="#394d42")
    draw.text((110, 1745), title[:60], font=_font(30), fill="#a9b2ae")
    image.save(path, format="PNG")
    os.chmod(path, 0o600)


def _run_command(args: Sequence[str], timeout: int = 180) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(args), shell=False, check=True, capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ScoutReelError("content_scout_reel_render_failed") from exc


def _probe(path: Path) -> dict[str, Any]:
    completed = _run_command((
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,width,height,pix_fmt,avg_frame_rate",
        "-of", "json", str(path),
    ))
    try:
        value = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScoutReelError("content_scout_reel_probe_invalid") from exc
    if type(value) is not dict:
        raise ScoutReelError("content_scout_reel_probe_invalid")
    return value


def _audio_duration(path: Path) -> float:
    value = _probe(path)
    try:
        duration = float(value["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoutReelError("content_scout_reel_tts_duration_invalid") from exc
    if duration <= 0 or duration > 30.0:
        raise ScoutReelError("content_scout_reel_tts_duration_invalid")
    return duration


def _validate_output(path: Path, duration_seconds: int) -> dict[str, Any]:
    value = _probe(path)
    streams = value.get("streams")
    if type(streams) is not list:
        raise ScoutReelError("content_scout_reel_output_invalid")
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    try:
        duration = float(value["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ScoutReelError("content_scout_reel_output_invalid") from exc
    if (
        type(video) is not dict
        or video.get("codec_name") != "h264"
        or video.get("width") != 1080
        or video.get("height") != 1920
        or video.get("pix_fmt") != "yuv420p"
        or video.get("avg_frame_rate") not in {"30/1", "60/2"}
        or type(audio) is not dict
        or audio.get("codec_name") != "aac"
        or not duration_seconds - 0.2 <= duration <= duration_seconds + 0.2
    ):
        raise ScoutReelError("content_scout_reel_output_invalid")
    return {"duration": round(duration, 3), "video": video, "audio": audio}


def _validate_receipt(
    value: Mapping[str, Any],
    job: ReelJob,
    selected: SelectedMaterial,
    output_digest: str,
    technical: Mapping[str, Any],
) -> None:
    expected = {
        "schema_version", "job_id", "selection_id", "run_id", "candidate_id",
        "ready_material_digest", "output_sha256", "duration_seconds", "resolution",
        "fps", "video_codec", "pixel_format", "audio_codec", "audio_present",
        "music_present", "scene_count",
    }
    if (
        type(value) is not dict
        or set(value) != expected
        or value.get("schema_version") != RECEIPT_SCHEMA
        or value.get("job_id") != job.job_id
        or value.get("selection_id") != selected.selection_id
        or value.get("run_id") != selected.run_id
        or value.get("candidate_id") != selected.candidate_id
        or value.get("ready_material_digest") != selected.ready_material_artifact_digest
        or value.get("output_sha256") != output_digest
        or type(value.get("duration_seconds")) not in {int, float}
        or abs(float(value["duration_seconds"]) - float(technical["duration"])) > 0.001
        or value.get("resolution") != "1080x1920"
        or value.get("fps") != 30
        or value.get("video_codec") != "h264"
        or value.get("pixel_format") != "yuv420p"
        or value.get("audio_codec") != "aac"
        or value.get("audio_present") is not True
        or value.get("music_present") is not False
        or value.get("scene_count") != 5
    ):
        raise ScoutReelError("content_scout_reel_receipt_invalid")


async def render_job(
    state_root: Path,
    selected: SelectedMaterial,
    material: Mapping[str, Any],
    *,
    tts_call: TtsCall,
) -> RenderedPreview:
    job, _created = reserve_job(state_root, selected, material)
    receipt_path = job.job_dir / "receipt.json"
    output_path = job.job_dir / "preview.mp4"
    if receipt_path.is_file() and output_path.is_file():
        receipt = _read_json(receipt_path)
        output_digest = _digest_bytes(_read_bytes(output_path, 100 * 1024 * 1024))
        technical = _validate_output(output_path, selected.duration_seconds)
        _validate_receipt(receipt, job, selected, output_digest, technical)
        return RenderedPreview(job, output_path, receipt, 0, 0)
    if (job.job_dir / "states" / "failed.json").exists():
        raise ScoutReelError("content_scout_reel_job_failed")
    lock_path = job.job_dir / "render.lock"
    try:
        lock_descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ScoutReelError("content_scout_reel_render_in_progress") from exc
    with os.fdopen(lock_descriptor, "wb") as handle:
        handle.write(job.job_id.encode("ascii"))
        handle.flush()
        os.fsync(handle.fileno())
    audio_path = job.job_dir / "voice.opus"
    tts_calls = 0
    staging = job.job_dir / "preview.staging.mp4"
    try:
        _state_event(job.job_dir, JOB_STATE_SCHEMA, "job_id", job.job_id, "rendering")
        _state_event(selected.selection_dir, SELECTION_STATE_SCHEMA, "selection_id", selected.selection_id, "rendering")
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise ScoutReelError("content_scout_reel_tools_unavailable")
        if not audio_path.exists():
            audio = await tts_call(material["reel_voice_over"])
            tts_calls = 1
            _write_binary_exact(audio_path, audio, "content_scout_reel_audio_conflict")
        if _digest_text(material["reel_voice_over"]) != selected.voice_over_digest:
            raise ScoutReelError("content_scout_reel_voice_binding_invalid")
        audio_duration = _audio_duration(audio_path)
        frames_dir = job.job_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        scene_paths: list[Path] = []
        for scene in job.ordered_scenes:
            path = frames_dir / f"scene-{scene['order']:02d}.png"
            if not path.exists():
                _draw_scene(path, scene, selected.title)
            scene_paths.append(path)
        manifest = ""
        for scene, path in zip(job.ordered_scenes, scene_paths, strict=True):
            manifest += f"file '{path.as_posix()}'\nduration {scene['end_second'] - scene['start_second']}\n"
        manifest += f"file '{scene_paths[-1].as_posix()}'\n"
        manifest_path = job.job_dir / "scenes.concat"
        _write_binary_exact(manifest_path, manifest.encode("utf-8"), "content_scout_reel_manifest_conflict")
        audio_filter = "apad"
        if audio_duration > selected.duration_seconds:
            ratio = audio_duration / selected.duration_seconds
            if not 1.0 < ratio <= 2.0:
                raise ScoutReelError("content_scout_reel_tts_duration_invalid")
            audio_filter = f"atempo={ratio:.8f},apad"
        _run_command((
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "concat", "-safe", "0", "-i", str(manifest_path),
            "-i", str(audio_path), "-vf", "fps=30,format=yuv420p",
            "-af", audio_filter, "-c:v", "libx264", "-preset", "medium",
            "-profile:v", "high", "-level", "4.1", "-c:a", "aac", "-b:a", "160k",
            "-t", str(selected.duration_seconds), "-movflags", "+faststart", str(staging),
        ), timeout=300)
        os.chmod(staging, 0o600)
        technical = _validate_output(staging, selected.duration_seconds)
        if output_path.exists():
            if _read_bytes(output_path, 100 * 1024 * 1024) != _read_bytes(staging, 100 * 1024 * 1024):
                raise ScoutReelConflict("content_scout_reel_output_conflict")
            staging.unlink()
        else:
            os.replace(staging, output_path)
            os.chmod(output_path, 0o600)
        output_digest = _digest_bytes(_read_bytes(output_path, 100 * 1024 * 1024))
        receipt = {
            "schema_version": RECEIPT_SCHEMA,
            "job_id": job.job_id,
            "selection_id": selected.selection_id,
            "run_id": selected.run_id,
            "candidate_id": selected.candidate_id,
            "ready_material_digest": selected.ready_material_artifact_digest,
            "output_sha256": output_digest,
            "duration_seconds": technical["duration"],
            "resolution": "1080x1920",
            "fps": 30,
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "audio_codec": "aac",
            "audio_present": True,
            "music_present": False,
            "scene_count": 5,
        }
        _validate_receipt(receipt, job, selected, output_digest, technical)
        _write_exact(receipt_path, receipt, "content_scout_reel_receipt_conflict")
        _state_event(
            job.job_dir,
            JOB_STATE_SCHEMA,
            "job_id",
            job.job_id,
            "preview_ready",
            output_digest=output_digest,
        )
        _state_event(selected.selection_dir, SELECTION_STATE_SCHEMA, "selection_id", selected.selection_id, "preview_ready")
        return RenderedPreview(job, output_path, receipt, tts_calls, 1)
    except ScoutReelError as exc:
        if staging.exists() and not staging.is_symlink():
            staging.unlink(missing_ok=True)
        _state_event(job.job_dir, JOB_STATE_SCHEMA, "job_id", job.job_id, "failed", exc.reason_code)
        _state_event(selected.selection_dir, SELECTION_STATE_SCHEMA, "selection_id", selected.selection_id, "failed", exc.reason_code)
        raise
    except Exception as exc:
        reason = "content_scout_reel_tts_failed" if tts_calls else "content_scout_reel_render_failed"
        if staging.exists() and not staging.is_symlink():
            staging.unlink(missing_ok=True)
        _state_event(job.job_dir, JOB_STATE_SCHEMA, "job_id", job.job_id, "failed", reason)
        _state_event(selected.selection_dir, SELECTION_STATE_SCHEMA, "selection_id", selected.selection_id, "failed", reason)
        raise ScoutReelError(reason) from exc
    finally:
        if lock_path.exists() and not lock_path.is_symlink():
            lock_path.unlink(missing_ok=True)
