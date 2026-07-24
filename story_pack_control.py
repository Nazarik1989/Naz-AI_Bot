"""Safe admin controls for Story-first approval, variants, status and delivery."""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from editorial_orchestrator import EditorialPlan
import story_production


_PLAN_ID_RE = re.compile(r"^[a-f0-9]{24}$")
PACK_STATUS_RU = {
    "awaiting_approval": "ожидает подтверждения",
    "queued": "поставлен в очередь",
    "in_progress": "создаётся",
    "composing_reels": "собираются Reels",
    "blocked_music": "Stories готовы, нужна лицензированная музыка",
    "partially_blocked": "часть сцен заблокирована",
    "completed": "готов",
    "superseded": "заменён другим вариантом",
}
SCENE_STATUS_RU = {
    "planned": "запланировано", "queued": "в очереди", "submitted": "отправлено",
    "in_progress": "создаётся", "downloaded": "скачано", "composed": "собрано",
    "completed": "готово", "retryable_failed": "временная ошибка",
    "terminal_failed": "ошибка", "blocked_reference": "нужен референс Naz",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_pack_dir(root: Path, plan_id: str) -> Path:
    if not _PLAN_ID_RE.fullmatch(plan_id):
        raise story_production.StoryPlanError("invalid story plan_id")
    base = Path(root).expanduser().resolve()
    pack_dir = (base / plan_id).resolve()
    if base not in pack_dir.parents:
        raise story_production.StoryPlanError("unsafe story pack path")
    return pack_dir


def manifest_path(root: Path, plan_id: str) -> Path:
    return _safe_pack_dir(root, plan_id) / "story_manifest.json"


def list_manifests(root: Path) -> list[Path]:
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        return []
    manifests = [
        path for path in base.glob("*/story_manifest.json")
        if _PLAN_ID_RE.fullmatch(path.parent.name)
    ]
    return sorted(manifests, key=lambda path: path.stat().st_mtime, reverse=True)


def latest_manifest(root: Path, *, approval_status: str | None = None) -> Path | None:
    for path in list_manifests(root):
        try:
            payload = story_production.read_manifest(path)
        except (OSError, ValueError):
            continue
        if payload.get("schema") != story_production.STORY_SCHEMA:
            continue
        if approval_status and str(payload.get("approval", {}).get("status")) != approval_status:
            continue
        if payload.get("pack_status") == "superseded":
            continue
        return path
    return None


@contextmanager
def _manifest_lock(path: Path) -> Iterator[None]:
    lock = path.parent / ".control.lock"
    descriptor: int | None = None
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise story_production.StoryPlanError("story pack control is busy") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        lock.unlink(missing_ok=True)


def approve_pack(root: Path, plan_id: str) -> str:
    """Approve once without invoking a provider or spending credits."""
    path = manifest_path(root, plan_id)
    with _manifest_lock(path):
        payload = story_production.read_manifest(path)
        approval = payload.get("approval")
        if not isinstance(approval, dict):
            raise story_production.StoryPlanError("story approval contract missing")
        status = str(approval.get("status", ""))
        if status == "approved":
            return "already_approved"
        if status != "awaiting_approval" or payload.get("pack_status") != "awaiting_approval":
            raise story_production.StoryPlanError("story pack cannot be approved in current state")
        if any(job.get("external_job_id") for job in payload.get("scene_jobs", [])):
            raise story_production.StoryPlanError("story pack already has provider jobs")
        approval.update({"status": "approved", "approved_at": _now()})
        payload["pack_status"] = "queued"
        payload["renderer"] = {"status": "queued", "name": "ffmpeg"}
        for job in payload.get("scene_jobs", []):
            if job.get("state") == "planned":
                job["state"] = "queued"
        for output in payload.get("expected_outputs", {}).get("stories", []):
            if output.get("status") == "planned":
                output["status"] = "queued"
        payload["updated_at"] = _now()
        story_production.atomic_json(path, payload)
        return "approved"


def create_next_variant(root: Path, plan_id: str) -> Path:
    """Create a different free plan before any provider task exists."""
    path = manifest_path(root, plan_id)
    with _manifest_lock(path):
        payload = story_production.read_manifest(path)
        if str(payload.get("approval", {}).get("status")) != "awaiting_approval":
            raise story_production.StoryPlanError("another variant is allowed before approval only")
        if any(job.get("external_job_id") for job in payload.get("scene_jobs", [])):
            raise story_production.StoryPlanError("another variant is unavailable after provider submit")
        editorial = payload.get("editorial_plan")
        facts = payload.get("safe_facts")
        if not isinstance(editorial, Mapping) or not isinstance(facts, list):
            raise story_production.StoryPlanError("story variant source contract missing")
        plan = EditorialPlan.from_dict(editorial)
        next_index = int(payload.get("variant_index", 0)) + 1
        pack = story_production.plan_story_pack(
            plan, tuple(str(item) for item in facts), variant_index=next_index,
        )
        new_dir = story_production.persist_story_queue(pack, root)
        now = _now()
        payload["approval"].update({"status": "superseded", "superseded_at": now})
        payload["pack_status"] = "superseded"
        payload["superseded_by_plan_id"] = pack.plan_id
        payload["updated_at"] = now
        story_production.atomic_json(path, payload)
        return new_dir


def safe_summary(payload: Mapping[str, Any]) -> str:
    scenes = payload.get("scene_jobs", []) if isinstance(payload.get("scene_jobs"), list) else []
    counts: dict[str, int] = {}
    for job in scenes:
        state = str(job.get("state", "unknown"))
        counts[state] = counts.get(state, 0) + 1
    status_lines = [
        f"• {SCENE_STATUS_RU.get(state, state)}: {count}"
        for state, count in sorted(counts.items())
    ]
    duration = sum(float(job.get("planned_duration_seconds", 0)) for job in scenes)
    references = sum(bool(job.get("requires_naz_reference")) for job in scenes)
    pack_status = str(payload.get("pack_status", "unknown"))
    return (
        "🎬 Reels Maker\n\n"
        f"План: {str(payload.get('plan_id', ''))[:24]}\n"
        f"Вариант: {int(payload.get('variant_index', 0)) + 1}\n"
        f"Рубрика: {str(payload.get('rubric', ''))[:120]}\n"
        f"Статус: {PACK_STATUS_RU.get(pack_status, pack_status)}\n"
        f"Сцен: {len(scenes)}, запланировано секунд: {duration:g}\n"
        f"Сцен с Naz: {references}\n\n"
        + ("\n".join(status_lines) if status_lines else "Сцены ещё не созданы")
    )


def delivery_files(manifest: Path) -> list[Path]:
    payload = story_production.read_manifest(manifest)
    if payload.get("pack_status") != "completed":
        return []
    root = manifest.parent.resolve()
    relative_paths = [
        str(job.get("story_path", ""))
        for job in payload.get("scene_jobs", [])
        if job.get("state") == "completed"
    ] + [
        str(job.get("path", ""))
        for job in payload.get("reel_jobs", [])
        if job.get("state") == "completed"
    ]
    files: list[Path] = []
    for relative in relative_paths:
        candidate = (root / relative).resolve()
        if root not in candidate.parents or candidate.suffix.casefold() != ".mp4":
            raise story_production.StoryPlanError("unsafe story delivery path")
        if not candidate.is_file():
            raise story_production.StoryPlanError("story delivery file missing")
        files.append(candidate)
    return files


def ready_deliveries(root: Path) -> list[Path]:
    result = []
    for path in reversed(list_manifests(root)):
        try:
            payload = story_production.read_manifest(path)
        except (OSError, ValueError):
            continue
        if (
            payload.get("pack_status") == "completed"
            and str(payload.get("delivery", {}).get("status")) in {"ready", "delivering"}
        ):
            result.append(path)
    return result


def mark_delivery_file_sent(manifest: Path, filename: str) -> None:
    def mutate(payload: dict[str, Any]) -> None:
        delivery = payload.setdefault("delivery", {})
        sent = delivery.setdefault("sent_files", [])
        if filename not in sent:
            sent.append(filename)
        delivery["status"] = "delivering"

    story_production.update_manifest(manifest, mutate)


def mark_delivery_complete(manifest: Path) -> None:
    def mutate(payload: dict[str, Any]) -> None:
        delivery = payload.setdefault("delivery", {})
        delivery.update({"status": "completed", "completed_at": _now()})

    story_production.update_manifest(manifest, mutate)
