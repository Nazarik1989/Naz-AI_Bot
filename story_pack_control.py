"""Safe admin controls for Story-first approval, variants, status and delivery."""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from editorial_orchestrator import EditorialPlan
import story_production
from story_pack_lock import StoryPackLock, StoryPackLockError


_PLAN_ID_RE = re.compile(r"^[a-f0-9]{24}$")
PACK_STATUS_RU = {
    "awaiting_approval": "ожидает подтверждения",
    "queued": "поставлен в очередь",
    "in_progress": "создаётся",
    "composing_reels": "собираются Reels",
    "blocked_music": "Stories готовы, нужна лицензированная музыка",
    "partially_blocked": "часть сцен заблокирована",
    "awaiting_secondary_approval": "нужно подтверждение повтора в Gen-4.5",
    "completed": "готов",
    "superseded": "заменён другим вариантом",
}
SCENE_STATUS_RU = {
    "planned": "запланировано", "queued": "в очереди", "submitted": "отправлено",
    "in_progress": "создаётся", "downloaded": "скачано", "composed": "собрано",
    "completed": "готово", "retryable_failed": "временная ошибка",
    "terminal_failed": "ошибка", "blocked_reference": "нужен референс Naz",
    "awaiting_secondary_approval": "ожидает подтверждения Gen-4.5",
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
    try:
        with StoryPackLock(path.parent):
            yield
    except StoryPackLockError as exc:
        raise story_production.StoryPlanError("story pack control is busy") from exc


def approve_pack(root: Path, plan_id: str) -> str:
    """Approve once without invoking a provider or spending credits."""
    path = manifest_path(root, plan_id)
    with _manifest_lock(path):
        payload = story_production.read_manifest(path)
        if not story_production.manifest_has_current_production_contract(payload):
            raise story_production.StoryPlanError("story_manifest_contract_stale")
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
            if job.get("keyframe_state") == "planned":
                job["keyframe_state"] = "queued"
        for output in payload.get("expected_outputs", {}).get("stories", []):
            if output.get("status") == "planned":
                output["status"] = "queued"
        payload["updated_at"] = _now()
        story_production.atomic_json(path, payload)
        return "approved"


def confirm_generation(root: Path, plan_id: str) -> str:
    """Context-aware confirmation with no provider/API call.

    The first press approves the pack.  A later press approves only secondary
    retries that are already waiting; it never pre-approves future failures.
    """
    path = manifest_path(root, plan_id)
    with _manifest_lock(path):
        payload = story_production.read_manifest(path)
        if not story_production.manifest_has_current_production_contract(payload):
            raise story_production.StoryPlanError("story_manifest_contract_stale")
        approval = payload.get("approval")
        if not isinstance(approval, dict):
            raise story_production.StoryPlanError("story approval contract missing")
        status = str(approval.get("status", ""))
        if status == "awaiting_approval":
            if payload.get("pack_status") != "awaiting_approval":
                raise story_production.StoryPlanError("story pack cannot be approved in current state")
            if any(job.get("external_job_id") for job in payload.get("scene_jobs", [])):
                raise story_production.StoryPlanError("story pack already has provider jobs")
            approval.update({"status": "approved", "approved_at": _now()})
            payload["pack_status"] = "queued"
            payload["renderer"] = {"status": "queued", "name": "ffmpeg"}
            for job in payload.get("scene_jobs", []):
                if job.get("state") == "planned":
                    job["state"] = "queued"
                if job.get("keyframe_state") == "planned":
                    job["keyframe_state"] = "queued"
            for output in payload.get("expected_outputs", {}).get("stories", []):
                if output.get("status") == "planned":
                    output["status"] = "queued"
            result = "approved"
        elif status == "approved":
            pending = [
                job for job in payload.get("scene_jobs", [])
                if job.get("state") == "awaiting_secondary_approval"
            ]
            if not pending:
                return "already_approved"
            confirmed_at = _now()
            for job in pending:
                route = job.get("model_route")
                if not isinstance(route, dict) or not route.get("secondary_requested_at"):
                    raise story_production.StoryPlanError("secondary model route missing")
                route.update({"tier": "secondary", "secondary_approved_at": confirmed_at})
                job.update({
                    "state": "queued", "external_job_id": None,
                    "provider_status": None, "failure_code": None,
                })
            payload["pack_status"] = "queued"
            result = "secondary_approved"
        else:
            raise story_production.StoryPlanError("story pack cannot be approved in current state")
        payload["updated_at"] = _now()
        story_production.atomic_json(path, payload)
        return result


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
    jobs = payload.get("scene_jobs", []) if isinstance(payload.get("scene_jobs"), list) else []
    directed = payload.get("scenes", []) if isinstance(payload.get("scenes"), list) else []
    counts: dict[str, int] = {}
    for job in jobs:
        state = str(job.get("state", "unknown"))
        counts[state] = counts.get(state, 0) + 1
    duration = sum(float(job.get("planned_duration_seconds", 0)) for job in jobs)
    references = sum(bool(job.get("requires_naz_reference")) for job in jobs)
    keyframe_credits = sum(
        2 if bool(scene.get("requires_naz_reference")) else 5
        for scene in directed if isinstance(scene, Mapping)
    )
    video_credits = int(duration) * 5
    statuses = "\n".join(
        f"• {SCENE_STATUS_RU.get(state, state)}: {count}"
        for state, count in sorted(counts.items())
    ) or "Сцены ещё не созданы"
    treatment = []
    for index, scene in enumerate(directed, 1):
        if isinstance(scene, Mapping):
            treatment.append(
                f"{index}. {str(scene.get('role', 'scene')).upper()} · "
                f"{str(scene.get('setting', ''))[:105]}\n"
                f"   Действие: {str(scene.get('concrete_action', ''))[:115]} · "
                f"Камера: {str(scene.get('camera_motion', ''))[:40]}"
            )
    pack_status = str(payload.get("pack_status", "unknown"))
    return (
        "🎬 Reels Maker\n\n"
        f"План: {str(payload.get('plan_id', ''))[:24]}\n"
        f"Вариант: {int(payload.get('variant_index', 0)) + 1}\n"
        f"Рубрика: {str(payload.get('rubric', ''))[:120]}\n"
        f"Статус: {PACK_STATUS_RU.get(pack_status, pack_status)}\n"
        f"Сцен: {len(jobs)}, запланировано секунд: {duration:g}\n"
        f"Сцен с Naz: {references}\n\n{statuses}\n\n"
        "Режиссёрский план:\n" + ("\n".join(treatment) or "—")
        + f"\n\nОценка Runway: keyframes {keyframe_credits} + video {video_credits} "
        f"= {keyframe_credits + video_credits} кредитов.\n"
        "Аватар используется только для внешности; фон, поза и постановка создаются заново."
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
