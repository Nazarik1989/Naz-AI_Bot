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
VISUAL_CONCEPT_RU = {
    "constraint recovery through physical rerouting": "Ограничение — ручное восстановление системы",
    "idea becoming a physical prototype": "Идея становится физическим прототипом",
    "separate parts becoming one connected system": "Разрозненные части становятся связанной системой",
    "laboratory prototype entering the physical world": "Прототип выходит из лаборатории в физический мир",
}
SCENE_ROLE_RU = {
    "hook": "ЗАЦЕПКА",
    "problem": "ПРОБЛЕМА",
    "hypothesis": "ГИПОТЕЗА",
    "test": "ПРОВЕРКА",
    "result": "РЕЗУЛЬТАТ",
    "solution": "РЕШЕНИЕ",
    "conclusion": "ВЫВОД",
}
SCENE_FALLBACK_RU = {
    "hook": "Показываем исходную ситуацию и сразу задаём вопрос.",
    "problem": "Показываем конкретное препятствие.",
    "hypothesis": "Показываем идею, которую предстоит проверить.",
    "test": "Проверяем идею реальным действием.",
    "result": "Показываем наблюдаемый результат проверки.",
    "solution": "Закрепляем рабочее решение.",
    "conclusion": "Оставляем ясный финальный вывод.",
}
SHOT_SIZE_RU = {
    "wide": "общий",
    "medium": "средний",
    "close": "крупный",
    "macro": "макро",
}
CAMERA_MOTION_RU = {
    "slow push": "медленное приближение",
    "controlled pan": "плавная панорама",
    "handheld follow": "ручная камера следует за действием",
    "locked with real subject motion": "статичная камера, движение внутри кадра",
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


def _reference_keyframe_retry_candidates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Find legacy Turbo reference failures eligible for one explicit retry."""
    candidates: list[dict[str, Any]] = []
    for job in payload.get("scene_jobs", []):
        if not isinstance(job, dict):
            continue
        intent = job.get("keyframe_submit_intent")
        if not isinstance(intent, Mapping):
            continue
        if (
            bool(job.get("requires_naz_reference"))
            and job.get("state") == "terminal_failed"
            and job.get("keyframe_state") == "terminal_failed"
            and job.get("keyframe_failure_code") == "provider_terminal_failure"
            and int(job.get("keyframe_attempts", 0)) == 1
            and intent.get("model") == "gen4_image_turbo"
            and not job.get("keyframe_retry_approved_at")
        ):
            candidates.append(job)
    return candidates


def _frontal_reference_retry_candidates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Find explicitly approved legacy retries that have not used the frontal reference."""
    candidates: list[dict[str, Any]] = []
    for job in payload.get("scene_jobs", []):
        if not isinstance(job, dict) or not bool(job.get("requires_naz_reference")):
            continue
        if not job.get("keyframe_retry_approved_at") or job.get(
            "keyframe_frontal_retry_approved_at"
        ):
            continue
        if job.get("keyframe_retry_model") != "gen4_image":
            continue
        attempts = int(job.get("keyframe_attempts", 0))
        intent = job.get("keyframe_submit_intent")
        queued_legacy_retry = (
            job.get("state") == "queued"
            and job.get("keyframe_state") == "queued"
            and attempts == 1
            and not job.get("keyframe_external_job_id")
            and isinstance(intent, Mapping)
            and intent.get("model") == "gen4_image_turbo"
            and job.get("keyframe_retry_phase") in {None, "legacy_model"}
        )
        failed_standard_retry = (
            job.get("state") == "terminal_failed"
            and job.get("keyframe_state") == "terminal_failed"
            and job.get("keyframe_failure_code") == "provider_terminal_failure"
            and attempts == 2
            and isinstance(intent, Mapping)
            and intent.get("model") == "gen4_image"
            and intent.get("approval_scope") == "reference_model_retry"
        )
        if queued_legacy_retry or failed_standard_retry:
            candidates.append(job)
    return candidates


def confirm_generation(root: Path, plan_id: str) -> str:
    """Context-aware confirmation with no provider/API call.

    The first press approves the pack.  A later press approves only a secondary
    video retry or the bounded legacy reference-keyframe retry already waiting;
    it never pre-approves future failures.
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
            confirmed_at = _now()
            if pending:
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
                keyframe_retries = _reference_keyframe_retry_candidates(payload)
                if not keyframe_retries:
                    return "already_approved"
                if len(keyframe_retries) > 4:
                    raise story_production.StoryPlanError(
                        "reference keyframe retry limit exceeded"
                    )
                for job in keyframe_retries:
                    external_id = str(job.get("keyframe_external_job_id") or "")
                    history = job.setdefault("keyframe_provider_job_history", [])
                    if external_id and external_id not in history:
                        history.append(external_id)
                    job.update({
                        "state": "queued",
                        "failure_code": None,
                        "keyframe_state": "queued",
                        "keyframe_external_job_id": None,
                        "keyframe_submitted_at": None,
                        "keyframe_provider_status": None,
                        "keyframe_failure_code": None,
                        "keyframe_retry_model": "gen4_image",
                        "keyframe_retry_phase": "legacy_model",
                        "keyframe_retry_reason_code": "provider_terminal_failure",
                        "keyframe_retry_approved_at": confirmed_at,
                    })
                payload["pack_status"] = "queued"
                result = "reference_keyframes_retry_approved"
        else:
            raise story_production.StoryPlanError("story pack cannot be approved in current state")
        payload["updated_at"] = _now()
        story_production.atomic_json(path, payload)
        return result


def approve_frontal_reference_retry(root: Path, plan_id: str) -> str:
    """Retarget only already-approved failed/queued reference retries.

    This operator-only control is deliberately not wired to a Telegram button:
    it requires separate explicit cost approval after reference-quality review.
    It records approval but never calls a provider.
    """
    path = manifest_path(root, plan_id)
    with _manifest_lock(path):
        payload = story_production.read_manifest(path)
        if not story_production.manifest_has_current_production_contract(payload):
            raise story_production.StoryPlanError("story_manifest_contract_stale")
        if str(payload.get("approval", {}).get("status")) != "approved":
            raise story_production.StoryPlanError("story pack is not approved")
        candidates = _frontal_reference_retry_candidates(payload)
        if not candidates:
            return "already_approved"
        if len(candidates) > 4:
            raise story_production.StoryPlanError(
                "frontal reference retry limit exceeded"
            )
        confirmed_at = _now()
        for job in candidates:
            external_id = str(job.get("keyframe_external_job_id") or "")
            history = job.setdefault("keyframe_provider_job_history", [])
            if external_id and external_id not in history:
                history.append(external_id)
            job.update({
                "state": "queued",
                "failure_code": None,
                "keyframe_state": "queued",
                "keyframe_external_job_id": None,
                "keyframe_submitted_at": None,
                "keyframe_provider_status": None,
                "keyframe_failure_code": None,
                "keyframe_retry_model": "gen4_image",
                "keyframe_retry_phase": "reference_quality",
                "keyframe_retry_reference_role": "frontal_identity",
                "keyframe_retry_reason_code": "provider_terminal_failure",
                "keyframe_frontal_retry_approved_at": confirmed_at,
            })
        payload["pack_status"] = "queued"
        payload["updated_at"] = confirmed_at
        story_production.atomic_json(path, payload)
        return "frontal_reference_keyframes_retry_approved"


def create_next_variant(
    root: Path,
    plan_id: str,
    *,
    director_treatment: story_production.DirectorTreatment | None = None,
) -> Path:
    """Persist a separately directed variant before any media-provider task exists."""
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
            plan,
            tuple(str(item) for item in facts),
            variant_index=next_index,
            director_treatment=director_treatment,
        )
        new_dir = story_production.persist_story_queue(pack, root)
        now = _now()
        payload["approval"].update({"status": "superseded", "superseded_at": now})
        payload["pack_status"] = "superseded"
        payload["superseded_by_plan_id"] = pack.plan_id
        payload["updated_at"] = now
        story_production.atomic_json(path, payload)
        return new_dir


def _legacy_concept_ru(scenes: list[Any]) -> str:
    text = " ".join(
        " ".join(
            str(scene.get(field, ""))
            for field in ("setting", "concrete_action", "end_state")
        )
        for scene in scenes
        if isinstance(scene, Mapping)
    ).casefold()
    if any(marker in text for marker in ("live service", "http request", "public page")):
        return "Naz проверяет живой сервис из лаборатории и со стороны пользователя"
    return ""


def _legacy_scene_summary_ru(scene: Mapping[str, Any]) -> str:
    """Explain known pre-localization director actions without exposing raw prompts."""
    text = " ".join(
        str(scene.get(field, ""))
        for field in ("setting", "concrete_action", "start_state", "end_state")
    ).casefold()
    if "laptop" in text and any(marker in text for marker in ("closes", "leaves", "walks away")):
        return "Naz закрывает ноутбук и выходит из кадра после завершённой проверки."
    if "refused connection" in text and "response" in text:
        return "На экране отказ соединения сменяется обычным ответом сервиса."
    if "http request" in text or ("terminal" in text and "request" in text):
        return "Naz отправляет простой запрос из терминала и ждёт фактический ответ сервиса."
    if "laptop" in text and any(marker in text for marker in ("public page", "outside", "courtyard")):
        return "Naz открывает ноутбук снаружи лаборатории и проверяет публичную страницу как пользователь."
    if "cable" in text and "response" in text:
        return "Подключение стабилизируется рядом с кабелем, и Naz убирает руку от клавиатуры."
    if "keyboard" in text and any(marker in text for marker in ("rests flat", "fingers move away")):
        return "Рядом с клавиатурой показываем деталь, которая остановила работу."
    if "laptop" in text and any(marker in text for marker in ("trackpad", "taps", "pauses")):
        return "Naz касается ноутбука и замирает перед началом проверки."
    return ""


def _story_progress(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return truthful manifest-backed progress without estimating elapsed time."""
    scene_jobs = (
        payload.get("scene_jobs", [])
        if isinstance(payload.get("scene_jobs"), list)
        else []
    )
    reel_jobs = (
        payload.get("reel_jobs", [])
        if isinstance(payload.get("reel_jobs"), list)
        else []
    )
    video_started_states = {
        "submitting", "submitted", "in_progress", "downloaded", "composed", "completed",
    }
    keyframes_ready = sum(
        str(job.get("keyframe_state", "")) == "ready"
        or str(job.get("state", "")) in video_started_states
        for job in scene_jobs
        if isinstance(job, Mapping)
    )
    videos_ready = sum(
        str(job.get("state", "")) == "completed"
        for job in scene_jobs
        if isinstance(job, Mapping)
    )
    reels_ready = sum(
        str(job.get("state", "")) == "completed"
        for job in reel_jobs
        if isinstance(job, Mapping)
    )
    completed_units = keyframes_ready + videos_ready + reels_ready
    total_units = len(scene_jobs) * 2 + len(reel_jobs)
    percent = int(completed_units * 100 / total_units) if total_units else 0
    filled = min(10, int(completed_units * 10 / total_units)) if total_units else 0
    return {
        "scene_jobs": scene_jobs,
        "reel_jobs": reel_jobs,
        "keyframes_ready": keyframes_ready,
        "videos_ready": videos_ready,
        "reels_ready": reels_ready,
        "completed_units": completed_units,
        "total_units": total_units,
        "percent": percent,
        "bar": "█" * filled + "░" * (10 - filled),
    }


def _current_story_stage(payload: Mapping[str, Any], progress: Mapping[str, Any]) -> str:
    approval = payload.get("approval", {})
    if isinstance(approval, Mapping) and approval.get("status") == "awaiting_approval":
        return "ожидается подтверждение генерации"

    pack_status = str(payload.get("pack_status", "unknown"))
    terminal_pack_labels = {
        "completed": "всё готово",
        "blocked_music": "Stories готовы, подбор музыки заблокирован",
        "partially_blocked": "одна или несколько сцен требуют внимания",
        "awaiting_secondary_approval": "нужно подтверждение повтора проблемной сцены",
        "superseded": "этот вариант заменён новым",
    }
    if pack_status in terminal_pack_labels:
        return terminal_pack_labels[pack_status]

    scene_jobs = progress.get("scene_jobs", [])
    scene_total = len(scene_jobs)
    for index, job in enumerate(scene_jobs, 1):
        if not isinstance(job, Mapping):
            continue
        scene_state = str(job.get("state", "planned"))
        if scene_state == "completed":
            continue
        keyframe_state = str(job.get("keyframe_state", "planned"))
        if keyframe_state in {"terminal_failed", "submit_ambiguous"}:
            return f"сцена {index}/{scene_total} — проблема с ключевым кадром"
        if keyframe_state in {"planned", "queued", "retryable_failed"}:
            return f"сцена {index}/{scene_total} — ключевой кадр ждёт очереди"
        if keyframe_state in {"submitting", "submitted", "in_progress"}:
            return f"сцена {index}/{scene_total} — создаётся ключевой кадр"
        if scene_state in {"planned", "queued"}:
            return f"сцена {index}/{scene_total} — видео ждёт очереди"
        if scene_state in {"submitting", "submitted", "in_progress"}:
            return f"сцена {index}/{scene_total} — создаётся видео"
        if scene_state in {"downloaded", "composed"}:
            return f"сцена {index}/{scene_total} — локальная сборка и проверка"
        if scene_state == "retryable_failed":
            return f"сцена {index}/{scene_total} — ожидается повтор после временной ошибки"
        if scene_state in {"terminal_failed", "blocked_reference", "submit_ambiguous"}:
            return f"сцена {index}/{scene_total} — требуется внимание"
        return f"сцена {index}/{scene_total} — {SCENE_STATUS_RU.get(scene_state, scene_state)}"

    reel_jobs = progress.get("reel_jobs", [])
    if reel_jobs and any(
        isinstance(job, Mapping) and str(job.get("state", "")) != "completed"
        for job in reel_jobs
    ):
        return "собираются итоговые Reels"
    return PACK_STATUS_RU.get(pack_status, pack_status)


def safe_progress_summary(payload: Mapping[str, Any]) -> str:
    """Compact progress card backed only by safe manifest state fields."""
    progress = _story_progress(payload)
    scene_total = len(progress["scene_jobs"])
    reel_total = len(progress["reel_jobs"])
    pack_status = str(payload.get("pack_status", "unknown"))
    return (
        "🎬 Reels Maker · прогресс\n\n"
        f"План: {str(payload.get('plan_id', ''))[:24]}\n"
        f"Статус: {PACK_STATUS_RU.get(pack_status, pack_status)}\n"
        f"[{progress['bar']}] {progress['percent']}% этапов\n\n"
        f"Ключевые кадры: {progress['keyframes_ready']}/{scene_total}\n"
        f"Видео сцен: {progress['videos_ready']}/{scene_total}\n"
        f"Готовые Reels: {progress['reels_ready']}/{reel_total}\n\n"
        f"Сейчас: {_current_story_stage(payload, progress)}."
    )


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
        5 for scene in directed if isinstance(scene, Mapping)
    )
    video_credits = int(duration) * 5
    concept = str(payload.get("visual_concept", "")).strip()
    admin_concept = " ".join(str(payload.get("admin_concept_ru", "")).split())
    concept_label = admin_concept if re.search(r"[А-Яа-яЁё]", admin_concept) else ""
    if not concept_label:
        concept_label = VISUAL_CONCEPT_RU.get(concept, "")
    thesis = " ".join(str(payload.get("central_thesis", "")).split())
    if not concept_label and re.search(r"[А-Яа-яЁё]", thesis):
        concept_label = thesis[:120]
    if not concept_label:
        for scene in directed:
            if not isinstance(scene, Mapping):
                continue
            overlay = " ".join(str(scene.get("story_overlay", "")).split())
            prefix, separator, remainder = overlay.partition(":")
            if separator and prefix.casefold() in SCENE_ROLE_RU:
                overlay = remainder.strip()
            if re.search(r"[А-Яа-яЁё]", overlay):
                concept_label = overlay[:120]
                break
    if not concept_label:
        concept_label = _legacy_concept_ru(directed)
    if not concept_label:
        concept_label = "Рабочий эпизод превращается в проверяемый результат"
    statuses = "\n".join(
        f"• {SCENE_STATUS_RU.get(state, state)}: {count}"
        for state, count in sorted(counts.items())
    ) or "Сцены ещё не созданы"
    treatment = []
    for index, scene in enumerate(directed, 1):
        if isinstance(scene, Mapping):
            role = str(scene.get("role", "scene")).casefold()
            overlay = " ".join(str(scene.get("admin_summary_ru", "")).split())
            if not re.search(r"[А-Яа-яЁё]", overlay):
                overlay = _legacy_scene_summary_ru(scene)
            if not re.search(r"[А-Яа-яЁё]", overlay):
                overlay = " ".join(str(scene.get("story_overlay", "")).split())
            prefix, separator, remainder = overlay.partition(":")
            if separator and prefix.casefold() in SCENE_ROLE_RU:
                overlay = remainder.strip()
            if not re.search(r"[А-Яа-яЁё]", overlay):
                overlay = SCENE_FALLBACK_RU.get(
                    role, "Показываем один понятный этап рабочего эпизода."
                )
            subject = "Naz" if bool(scene.get("requires_naz_reference")) else "объект или механизм"
            shot_size = SHOT_SIZE_RU.get(str(scene.get("shot_size", "")).casefold(), "не задан")
            camera = CAMERA_MOTION_RU.get(
                str(scene.get("camera_motion", "")).casefold(), "спокойное движение камеры"
            )
            treatment.append(
                f"{index}. {SCENE_ROLE_RU.get(role, 'СЦЕНА')}\n"
                f"   Смысл: {overlay[:112]}\n"
                f"   В кадре: {subject} · План: {shot_size} · Камера: {camera}"
            )
    pack_status = str(payload.get("pack_status", "unknown"))
    return (
        "🎬 Reels Maker\n\n"
        f"План: {str(payload.get('plan_id', ''))[:24]}\n"
        f"Вариант: {int(payload.get('variant_index', 0)) + 1}\n"
        f"Рубрика: {str(payload.get('rubric', ''))[:120]}\n"
        f"Сюжетная линия: {concept_label}\n"
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
