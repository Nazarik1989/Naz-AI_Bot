"""Safe admin controls for Story-first approval, variants, status and delivery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from editorial_orchestrator import EditorialPlan
from runway_reference_health import (
    HEALTH_POLICY_VERSION,
    PROMPT_POLICY_VERSION,
    REFERENCE_PROFILE_VERSION,
    ReferenceHealthError,
    ReferenceHealthRegistry,
    ReferenceRoute,
    reference_set_digest,
    sha256_file,
)
import story_production
from story_pack_lock import (
    StoryPackLock,
    StoryPackLockError,
    ensure_private_group_access,
)


_PLAN_ID_RE = re.compile(r"^[a-f0-9]{24}$")
CORRECTED_SCENE_PROPOSAL_SCHEMA = "naz-runway-corrected-scene-proposal-v1"
CORRECTED_SCENE_REVISION_SCHEMA = "naz-runway-corrected-scene-revision-v1"
CORRECTED_SCENE_APPROVAL_SCHEMA = "naz-runway-corrected-scene-approval-v1"
CORRECTED_SCENE_RUNTIME_SCHEMA = "naz-runway-corrected-scene-runtime-v1"
CORRECTED_SCENE_CANCEL_SCHEMA = "naz-runway-corrected-scene-cancel-v1"
CORRECTED_SCENE_IDS = ("02_problem", "05_conclusion")
CORRECTED_COMPLETED_SCENE_IDS = ("01_hook", "03_test", "04_result")
_REVISION_CALLBACK_ACTIONS = frozenset({"approve", "technical", "status", "cancel"})
_CORRECTED_SCENE_INHERITED_RETRY_FIELDS = (
    "keyframe_retry_approved_at",
    "keyframe_retry_model",
    "keyframe_retry_phase",
    "keyframe_retry_reason_code",
    "keyframe_retry_reference_role",
    "keyframe_frontal_retry_approved_at",
    "keyframe_current_frontal_retry_approved_at",
    "keyframe_concise_retry_approved_at",
    "keyframe_automatic_fallbacks",
    "keyframe_fallback_state",
    "keyframe_retry_not_before",
    "keyframe_failure_category",
    "keyframe_provider_failure_code",
    "keyframe_retry_prompt_mode",
)
PACK_STATUS_RU = {
    "awaiting_approval": "ожидает подтверждения",
    "queued": "поставлен в очередь",
    "in_progress": "создаётся",
    "composing_reels": "собираются Reels",
    "blocked_music": "Stories готовы, нужна лицензированная музыка",
    "blocked_voice": "Stories готовы, озвучка требует внимания",
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


def _current_frontal_reference_retry_candidates(
    payload: Mapping[str, Any], *, pack_dir: Path | None = None
) -> list[dict[str, Any]]:
    """Find first-attempt current ``gen4_image`` reference failures.

    This is deliberately separate from the legacy Turbo migration predicates.
    A downloaded keyframe, a started video, or any previous approval makes the
    scene ineligible.  When ``pack_dir`` is supplied, an unrecorded keyframe
    file also closes the recovery path.
    """
    candidates: list[dict[str, Any]] = []
    for job in payload.get("scene_jobs", []):
        if not isinstance(job, dict):
            continue
        intent = job.get("keyframe_submit_intent")
        route = job.get("model_route")
        if (
            bool(job.get("requires_naz_reference"))
            and job.get("reference_role") == "three_quarter_identity"
            and job.get("state") == "terminal_failed"
            and job.get("failure_code") == "provider_terminal_failure"
            and job.get("keyframe_state") == "terminal_failed"
            and job.get("keyframe_failure_code") == "provider_terminal_failure"
            and job.get("keyframe_provider_status") == "terminal_failed"
            and int(job.get("keyframe_attempts", 0)) == 1
            and isinstance(intent, Mapping)
            and intent.get("model") == "gen4_image"
            and intent.get("state") == "accepted"
            and intent.get("approval_scope") is None
            and bool(job.get("keyframe_external_job_id"))
            and intent.get("external_job_id") == job.get("keyframe_external_job_id")
            and not job.get("keyframe_checksum")
            and int(job.get("attempts", 0)) == 0
            and not job.get("external_job_id")
            and not job.get("clean_checksum")
            and not job.get("story_checksum")
            and isinstance(route, Mapping)
            and route.get("selected_model") == "gen4.5"
            and not job.get("keyframe_current_frontal_retry_approved_at")
            and not job.get("keyframe_retry_approved_at")
            and not job.get("keyframe_frontal_retry_approved_at")
        ):
            if pack_dir is not None:
                relative = str(job.get("keyframe_path") or "")
                candidate = (pack_dir / relative).resolve()
                if pack_dir.resolve() not in candidate.parents or candidate.exists():
                    continue
            candidates.append(job)
    return candidates


def _current_recovery_manifest_supported(payload: Mapping[str, Any]) -> bool:
    return story_production.manifest_has_current_production_contract(
        payload
    ) or story_production.manifest_has_previous_production_contract(payload)


def _migration_binding(
    registry: ReferenceHealthRegistry,
    *,
    plan_id: str,
    manifest: Path,
) -> dict[str, Any] | None:
    binding = registry.migration(plan_id)
    payload = story_production.read_manifest(manifest)
    if (
        isinstance(binding, dict)
        and binding.get("policy_version") == HEALTH_POLICY_VERSION
        and binding.get("manifest_digest") == sha256_file(manifest)
        and binding.get("immutable_plan_fingerprint")
        == payload.get("immutable_plan_fingerprint")
    ):
        return binding
    return None


def import_current_plan_reference_health(
    root: Path,
    plan_id: str,
    *,
    health_root: Path,
    references: Mapping[str, Path],
    audited_tasks: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Import exact current-plan evidence without mutating it or calling Runway."""
    path = manifest_path(root, plan_id)
    before = path.read_bytes()
    payload = story_production.read_manifest(path)
    if not _current_recovery_manifest_supported(payload):
        raise story_production.StoryPlanError("story_manifest_contract_stale")
    completed_scene_ids = ("01_hook", "03_test", "04_result")
    retry_scene_ids = ("02_problem", "05_conclusion")
    jobs_by_id = {
        str(job.get("scene_id")): job
        for job in payload.get("scene_jobs", [])
        if isinstance(job, Mapping)
    }
    if set(jobs_by_id) != {
        "01_hook", "02_problem", "03_test", "04_result", "05_conclusion"
    }:
        raise story_production.StoryPlanError("current reference evidence mismatch")
    for scene_id in completed_scene_ids:
        job = jobs_by_id[scene_id]
        if (
            job.get("state") != "completed"
            or job.get("keyframe_state") != "ready"
            or int(job.get("attempts", 0)) != 1
        ):
            raise story_production.StoryPlanError("current reference evidence mismatch")
        for path_field, digest_field in (
            ("keyframe_path", "keyframe_checksum"),
            ("clean_path", "clean_checksum"),
            ("story_path", "story_checksum"),
        ):
            relative = str(job.get(path_field, ""))
            artifact = (path.parent / relative).resolve()
            if (
                path.parent.resolve() not in artifact.parents
                or sha256_file(artifact, maximum_bytes=1024 * 1024 * 1024)
                != job.get(digest_field)
            ):
                raise story_production.StoryPlanError(
                    "current completed scene evidence invalid"
                )
    for scene_id in retry_scene_ids:
        job = jobs_by_id[scene_id]
        if (
            job.get("state") != "terminal_failed"
            or job.get("keyframe_state") != "terminal_failed"
            or int(job.get("keyframe_attempts", 0)) != 2
            or int(job.get("attempts", 0)) != 0
            or job.get("keyframe_checksum")
            or job.get("clean_checksum")
            or job.get("story_checksum")
        ):
            raise story_production.StoryPlanError("current reference evidence mismatch")
    required = ("frontal_identity", "three_quarter_identity")
    if any(role not in references for role in required):
        raise story_production.StoryPlanError("current reference evidence unavailable")
    digests = {role: sha256_file(Path(value)) for role, value in references.items()}
    frontal_rows = (("frontal_identity", digests["frontal_identity"]),)
    three_quarter_rows = (("three_quarter_identity", digests["three_quarter_identity"]),)
    frontal_route = ReferenceRoute(
        "runway", "gen4_image", REFERENCE_PROFILE_VERSION, PROMPT_POLICY_VERSION,
        "frontal_identity", digests["frontal_identity"],
        reference_set_digest(frontal_rows),
    )
    three_quarter_route = ReferenceRoute(
        "runway", "gen4_image", REFERENCE_PROFILE_VERSION, PROMPT_POLICY_VERSION,
        "three_quarter_identity", digests["three_quarter_identity"],
        reference_set_digest(three_quarter_rows),
    )
    manifest_digest = sha256_file(path)
    registry = ReferenceHealthRegistry(health_root)
    registry.import_route_evidence(
        frontal_route,
        successful_count=3,
        terminal_count=2,
        consecutive_terminal_count=2,
        last_failure_category="bad_output",
        health_state="degraded",
        evidence_id=hashlib.sha256(
            f"{manifest_digest}|frontal|3|2|bad_output".encode("ascii")
        ).hexdigest(),
    )
    registry.import_route_evidence(
        three_quarter_route,
        successful_count=0,
        terminal_count=3,
        consecutive_terminal_count=3,
        last_failure_category="unknown_terminal",
        health_state="quarantined",
        evidence_id=hashlib.sha256(
            f"{manifest_digest}|three-quarter|3".encode("ascii")
        ).hexdigest(),
    )
    registry.bind_migration(
        plan_id=plan_id,
        manifest_digest=manifest_digest,
        immutable_plan_fingerprint=str(payload.get("immutable_plan_fingerprint", "")),
        completed_scene_ids=completed_scene_ids,
        retry_scene_ids=retry_scene_ids,
        task_audit=audited_tasks,
    )
    if path.read_bytes() != before:
        raise story_production.StoryPlanError("current plan changed during health import")
    return {
        "plan_id": plan_id,
        "policy_version": HEALTH_POLICY_VERSION,
        "frontal_route_state": registry.health_state(frontal_route),
        "three_quarter_route_state": registry.health_state(three_quarter_route),
        "completed_scene_ids": completed_scene_ids,
        "retry_scene_ids": retry_scene_ids,
        "provider_calls": 0,
    }


def current_runway_failure_decision(
    root: Path, plan_id: str, *, health_root: Path
) -> dict[str, Any]:
    """Return the evidence-bound current-plan decision without provider access."""
    path = manifest_path(root, plan_id)
    payload = story_production.read_manifest(path)
    if not _current_recovery_manifest_supported(payload):
        raise story_production.StoryPlanError("story_manifest_contract_stale")
    binding = _migration_binding(
        ReferenceHealthRegistry(health_root), plan_id=plan_id, manifest=path
    )
    if (
        binding is None
        or binding.get("completed_scene_ids") != ["01_hook", "03_test", "04_result"]
        or binding.get("retry_scene_ids") != ["02_problem", "05_conclusion"]
        or type(binding.get("task_audit")) is not dict
    ):
        raise story_production.StoryPlanError("current failure evidence unavailable")
    audit = binding["task_audit"]
    if (
        audit.get("01_hook", {}).get("status") != "SUCCEEDED"
        or audit.get("02_problem", {}).get("failure_category") != "bad_output"
        or audit.get("05_conclusion", {}).get("failure_category") != "bad_output"
    ):
        raise story_production.StoryPlanError("current failure evidence unavailable")
    return {
        "plan_id": plan_id,
        "completed_scene_ids": ("01_hook", "03_test", "04_result"),
        "blocked_scene_ids": ("02_problem", "05_conclusion"),
        "scene_total": 5,
        "control_status": "SUCCEEDED",
        "scene_failures": {
            scene_id: {
                "safe_provider_failure_code": audit[scene_id][
                    "safe_provider_failure_code"
                ],
                "failure_category": audit[scene_id]["failure_category"],
                "automatic_retry": False,
                "same_input_retry": False,
                "recommended_action": "corrected_input_scene_revision",
            }
            for scene_id in ("02_problem", "05_conclusion")
        },
        "frontal_route_state": "degraded",
        "three_quarter_primary_route_state": "quarantined",
        "corrected_input_proposal_available": True,
        "maximum_new_keyframes": 2,
        "maximum_new_videos": 2,
        "additional_credits": 130,
        "provider_calls": 0,
    }


def current_runway_failure_decision_card(
    root: Path, plan_id: str, *, health_root: Path
) -> str:
    decision = current_runway_failure_decision(
        root, plan_id, health_root=health_root
    )
    scene_2 = decision["scene_failures"]["02_problem"]
    scene_5 = decision["scene_failures"]["05_conclusion"]
    return (
        "⚠️ Runway: точная причина двух сцен определена\n\n"
        f"План:\n{plan_id}\n\n"
        "Готово:\n3/5 сцен\n\n"
        "Сохранены:\n1, 3 и 4\n\n"
        f"Сцена 2:\n{scene_2['safe_provider_failure_code']} → bad_output\n"
        "Нужна новая неизменяемая ревизия сцены с упрощённым visual prompt, "
        "без читаемого текста/UI; object-only — если смысл это допускает.\n\n"
        f"Сцена 5:\n{scene_5['safe_provider_failure_code']} → bad_output\n"
        "Нужна новая неизменяемая ревизия сцены с упрощённым visual prompt, "
        "без читаемого текста/UI; object-only — если смысл это допускает.\n\n"
        "Сцены 1, 3 и 4 останутся без изменений. Подготовка плана замены "
        "не запускает Runway; стоимость потребует отдельного подтверждения.\n\n"
        "Новых генераций не выполнялось."
    )


def propose_current_runway_scene_revisions(
    root: Path,
    plan_id: str,
    *,
    health_root: Path,
    admin_id: int,
    expected_admin_id: int,
) -> dict[str, Any]:
    """Create one immutable, provider-free proposal for the two blocked scenes."""
    if type(admin_id) is not int or admin_id != expected_admin_id:
        raise story_production.StoryPlanError("story recovery admin required")
    decision = current_runway_failure_decision(root, plan_id, health_root=health_root)
    canonical = json.dumps(
        {
            "schema": "naz-runway-corrected-scene-proposal-v1",
            "plan_id": plan_id,
            "scene_ids": list(decision["blocked_scene_ids"]),
            "failure_category": "bad_output",
            "revision_rules": [
                "simplify_visual_prompt",
                "remove_readable_text_and_ui",
                "prefer_object_only_when_semantically_valid",
                "preserve_completed_scenes",
            ],
            "maximum_new_keyframes": 2,
            "maximum_new_videos": 2,
            "additional_credits": 130,
            "generation_authorized": False,
            "separate_cost_approval_required": True,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    proposal_id = hashlib.sha256(canonical).hexdigest()[:24]
    proposal_path = _safe_pack_dir(root, plan_id) / "recovery-proposals" / f"{proposal_id}.json"
    proposal_path.parent.mkdir(parents=True, exist_ok=True)
    if proposal_path.exists():
        if proposal_path.read_bytes() != canonical:
            raise story_production.StoryPlanError("story recovery proposal conflict")
    else:
        try:
            with proposal_path.open("xb") as target:
                target.write(canonical)
        except FileExistsError:
            if proposal_path.read_bytes() != canonical:
                raise story_production.StoryPlanError("story recovery proposal conflict")
    return {
        "proposal_id": proposal_id,
        "scene_ids": decision["blocked_scene_ids"],
        "additional_credits": 130,
        "separate_cost_approval_required": True,
        "provider_calls": 0,
    }


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8") + b"\n"


def _digest_value(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _read_closed_json(
    path: Path, *, schema: str, canonical: bool = False
) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise story_production.StoryPlanError("corrected scene revision state invalid") from exc
    if type(payload) is not dict or payload.get("schema") != schema:
        raise story_production.StoryPlanError("corrected scene revision state invalid")
    if canonical and raw != _canonical_bytes(payload):
        raise story_production.StoryPlanError("corrected scene revision state invalid")
    return payload


def _write_exact_private(path: Path, payload: Mapping[str, Any]) -> bool:
    raw = _canonical_bytes(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_group_access(path.parent, directory=True)
    if path.exists():
        if path.read_bytes() != raw:
            raise story_production.StoryPlanError("corrected scene revision conflict")
        return False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
    except FileExistsError:
        if path.read_bytes() != raw:
            raise story_production.StoryPlanError("corrected scene revision conflict")
        return False
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        ensure_private_group_access(path, directory=False)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return True


def write_corrected_scene_runtime(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace only a closed corrected-scene runtime document."""
    if (
        type(payload) is not dict
        or payload.get("schema") != CORRECTED_SCENE_RUNTIME_SCHEMA
        or path.name != "revision-runtime.json"
    ):
        raise story_production.StoryPlanError("corrected scene runtime invalid")
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".revision-runtime-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        ensure_private_group_access(temporary, directory=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_corrected_scene_runtime(path: Path) -> dict[str, Any]:
    return _read_closed_json(path, schema=CORRECTED_SCENE_RUNTIME_SCHEMA)


def _safe_revision_dir(root: Path, revision_plan_id: str) -> Path:
    return _safe_pack_dir(root, revision_plan_id)


def _exact_current_proposal(pack_dir: Path, plan_id: str) -> tuple[Path, dict[str, Any], str]:
    proposal_root = pack_dir / "recovery-proposals"
    matches: list[tuple[Path, dict[str, Any], str]] = []
    for path in sorted(proposal_root.glob("*.json")) if proposal_root.is_dir() else []:
        if path.is_symlink() or not path.is_file() or not _PLAN_ID_RE.fullmatch(path.stem):
            continue
        try:
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            type(value) is dict
            and value.get("schema") == CORRECTED_SCENE_PROPOSAL_SCHEMA
            and value.get("plan_id") == plan_id
            and value.get("scene_ids") == list(CORRECTED_SCENE_IDS)
            and value.get("failure_category") == "bad_output"
            and value.get("maximum_new_keyframes") == 2
            and value.get("maximum_new_videos") == 2
            and value.get("generation_authorized") is False
            and value.get("separate_cost_approval_required") is True
        ):
            digest = hashlib.sha256(raw).hexdigest()
            matches.append((path, value, digest))
    if len(matches) != 1:
        raise story_production.StoryPlanError("story recovery proposal ambiguous")
    if matches[0][0].stem != matches[0][2][:24]:
        raise story_production.StoryPlanError("story recovery proposal ambiguous")
    return matches[0]


def _artifact_path(pack_dir: Path, relative: object) -> Path:
    if type(relative) is not str or not relative or Path(relative).is_absolute():
        raise story_production.StoryPlanError("corrected scene asset invalid")
    candidate = (pack_dir / relative).resolve()
    if pack_dir.resolve() not in candidate.parents:
        raise story_production.StoryPlanError("corrected scene asset invalid")
    return candidate


def _completed_asset_inventory(
    manifest: Mapping[str, Any], pack_dir: Path
) -> tuple[dict[str, Any], ...]:
    jobs = {
        str(job.get("scene_id")): job
        for job in manifest.get("scene_jobs", [])
        if isinstance(job, Mapping)
    }
    rows: list[dict[str, Any]] = []
    for scene_id in CORRECTED_COMPLETED_SCENE_IDS:
        job = jobs.get(scene_id)
        if not isinstance(job, Mapping) or job.get("state") != "completed":
            raise story_production.StoryPlanError("completed scene reuse invalid")
        assets: dict[str, str] = {}
        for role, path_field, checksum_field in (
            ("keyframe", "keyframe_path", "keyframe_checksum"),
            ("clean", "clean_path", "clean_checksum"),
            ("story", "story_path", "story_checksum"),
        ):
            expected = job.get(checksum_field)
            path = _artifact_path(pack_dir, job.get(path_field))
            if (
                type(expected) is not str
                or not re.fullmatch(r"[a-f0-9]{64}", expected)
                or path.is_symlink()
                or not path.is_file()
                or sha256_file(path, maximum_bytes=1024 * 1024 * 1024) != expected
            ):
                raise story_production.StoryPlanError("completed scene reuse invalid")
            assets[role] = expected
        rows.append({"scene_id": scene_id, "asset_checksums": assets})
    return tuple(rows)


def _failed_input_inventory(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    jobs = {
        str(job.get("scene_id")): job
        for job in manifest.get("scene_jobs", [])
        if isinstance(job, Mapping)
    }
    rows: list[dict[str, Any]] = []
    for scene_id in CORRECTED_SCENE_IDS:
        job = jobs.get(scene_id)
        if (
            not isinstance(job, Mapping)
            or job.get("state") != "terminal_failed"
            or job.get("keyframe_state") != "terminal_failed"
            or int(job.get("keyframe_attempts", 0)) != 2
            or int(job.get("attempts", 0)) != 0
            or job.get("keyframe_checksum")
            or job.get("clean_checksum")
            or job.get("story_checksum")
        ):
            raise story_production.StoryPlanError("failed scene history invalid")
        task_ids = [
            str(value)
            for value in [
                *job.get("keyframe_provider_job_history", []),
                job.get("keyframe_external_job_id"),
            ]
            if type(value) is str and value
        ]
        intents = [
            value
            for value in [
                *job.get("keyframe_submit_intent_history", []),
                job.get("keyframe_submit_intent"),
            ]
            if isinstance(value, Mapping)
        ]
        rows.append({
            "scene_id": scene_id,
            "failed_job_digest": _digest_value(job),
            "failed_task_identity_digests": [
                hashlib.sha256(value.encode("utf-8")).hexdigest()
                for value in task_ids
            ],
            "failed_input_identity_digests": [_digest_value(value) for value in intents],
            "keyframe_attempts": 2,
            "video_attempts": 0,
            "failure_category": "bad_output",
        })
    return tuple(rows)


def _corrected_scene_inputs(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    scenes = {
        str(scene.get("scene_id")): scene
        for scene in manifest.get("scenes", [])
        if isinstance(scene, Mapping)
    }
    jobs = {
        str(job.get("scene_id")): job
        for job in manifest.get("scene_jobs", [])
        if isinstance(job, Mapping)
    }
    definitions = {
        "02_problem": {
            "semantic_meaning": "Conversation history exists, but it does not reach answer generation.",
            "visual_direction": "Object-only memory archive and a visibly disconnected handoff.",
            "keyframe_prompt": (
                "Vertical cinematic macro view of a dark physical memory archive holding intact "
                "luminous modules. One clean signal leaves the archive, reaches a visible empty "
                "gap before a separate generation chamber, and stops. Pure physical mechanism, "
                "single clear action, no people, no written symbols, no interface graphics, no logos."
            ),
            "provider_prompt": (
                "A single luminous signal travels from the intact memory archive toward the separate "
                "generation chamber and stops at the visible gap. Slow controlled push, stable geometry."
            ),
        },
        "05_conclusion": {
            "semantic_meaning": (
                "Context transfer is restored while separate user contexts remain isolated, and the "
                "final response uses the prior context."
            ),
            "visual_direction": "Object-only isolated signal lanes with one restored memory handoff.",
            "keyframe_prompt": (
                "Vertical cinematic view of two isolated luminous signal lanes that remain physically "
                "separate. A single bridge reconnects a memory path to a generation path and one stable "
                "pulse reaches the output without crossing between the lanes. Pure physical mechanism, "
                "single clear action, no people, no written symbols, no interface graphics, no logos."
            ),
            "provider_prompt": (
                "One stable pulse crosses the restored bridge from memory to generation while two user "
                "signal lanes remain fully separated. Locked camera with restrained physical motion."
            ),
        },
    }
    result: list[dict[str, Any]] = []
    for scene_id in CORRECTED_SCENE_IDS:
        scene = scenes.get(scene_id)
        job = jobs.get(scene_id)
        if not isinstance(scene, Mapping) or not isinstance(job, Mapping):
            raise story_production.StoryPlanError("corrected scene source invalid")
        duration = job.get("planned_duration_seconds")
        if type(duration) not in {int, float} or float(duration) not in {5.0, 10.0}:
            raise story_production.StoryPlanError("corrected scene duration invalid")
        base = {
            "scene_id": scene_id,
            **definitions[scene_id],
            "story_overlay": str(scene.get("story_overlay", "")),
            "text_safe_zone": str(scene.get("text_safe_zone", "center")),
            "requires_naz_reference": False,
            "identity_reference": None,
            "keyframe_model": "gen4_image",
            "video_model": "gen4_turbo",
            "video_duration_seconds": int(duration),
            "visual_exclusions": [
                "readable_text", "user_interface", "chat_screenshot", "source_code",
                "database_rows", "labels", "logos", "human_identity",
            ],
        }
        input_digest = _digest_value(base)
        result.append({
            **base,
            "revision_id": hashlib.sha256(
                f"{manifest.get('plan_id')}|{scene_id}|{input_digest}".encode("utf-8")
            ).hexdigest()[:24],
            "keyframe_input_digest": input_digest,
        })
    return tuple(result)


def _revision_plan_identity_payload(
    *, proposal_id: str, proposal_digest: str, parent_plan_id: str,
    parent_manifest_digest: str, immutable_plan_fingerprint: str,
    completed_assets: tuple[dict[str, Any], ...],
    failed_inputs: tuple[dict[str, Any], ...],
    corrected_scenes: tuple[dict[str, Any], ...], admin_id: int,
) -> dict[str, Any]:
    keyframe_credits = story_production.RUNWAY_KEYFRAME_CREDITS * len(corrected_scenes)
    video_credits = sum(
        int(scene["video_duration_seconds"])
        * story_production.RUNWAY_VIDEO_CREDITS_PER_SECOND[str(scene["video_model"])]
        for scene in corrected_scenes
    )
    return {
        "schema": CORRECTED_SCENE_REVISION_SCHEMA,
        "proposal_id": proposal_id,
        "proposal_digest": proposal_digest,
        "parent_plan_id": parent_plan_id,
        "parent_manifest_digest": parent_manifest_digest,
        "parent_immutable_plan_fingerprint": immutable_plan_fingerprint,
        "completed_scenes": list(completed_assets),
        "failed_scenes": list(failed_inputs),
        "corrected_scenes": list(corrected_scenes),
        "revised_scene_set_digest": _digest_value(list(CORRECTED_SCENE_IDS)),
        "provider_workload": {
            "maximum_new_keyframes": len(corrected_scenes),
            "maximum_new_videos": len(corrected_scenes),
            "old_input_retries": 0,
            "completed_scene_regenerations": 0,
        },
        "credit_estimate": {
            "keyframe_credits": keyframe_credits,
            "video_credits": video_credits,
            "ceiling": keyframe_credits + video_credits,
        },
        "approval_status": "awaiting_cost_approval",
        "generation_authorized": False,
        "admin_id": admin_id,
    }


def _copy_verified_asset(source: Path, destination: Path, expected_digest: str) -> None:
    if (
        source.is_symlink()
        or not source.is_file()
        or sha256_file(source, maximum_bytes=1024 * 1024 * 1024) != expected_digest
    ):
        raise story_production.StoryPlanError("completed scene reuse invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    ensure_private_group_access(destination.parent, directory=True)
    if destination.exists():
        if destination.is_symlink() or sha256_file(
            destination, maximum_bytes=1024 * 1024 * 1024
        ) != expected_digest:
            raise story_production.StoryPlanError("corrected scene asset conflict")
        return
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o660)
    try:
        with source.open("rb") as incoming, os.fdopen(descriptor, "wb") as outgoing:
            while True:
                chunk = incoming.read(1024 * 1024)
                if not chunk:
                    break
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        ensure_private_group_access(destination, directory=False)
        if sha256_file(destination, maximum_bytes=1024 * 1024 * 1024) != expected_digest:
            raise story_production.StoryPlanError("corrected scene asset conflict")
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def _copy_revision_reuse_assets(
    *, parent_manifest: Mapping[str, Any], parent_dir: Path, child_dir: Path
) -> None:
    jobs = {
        str(job.get("scene_id")): job
        for job in parent_manifest.get("scene_jobs", [])
        if isinstance(job, Mapping)
    }
    for scene_id in CORRECTED_COMPLETED_SCENE_IDS:
        job = jobs[scene_id]
        for path_field, checksum_field in (
            ("keyframe_path", "keyframe_checksum"),
            ("clean_path", "clean_checksum"),
            ("story_path", "story_checksum"),
        ):
            source = _artifact_path(parent_dir, job[path_field])
            destination = _artifact_path(child_dir, job[path_field])
            _copy_verified_asset(source, destination, str(job[checksum_field]))
    voice = parent_manifest.get("voice_over_plan")
    if isinstance(voice, Mapping) and voice.get("status") == "ready":
        digest = voice.get("audio_digest")
        if type(digest) is not str or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise story_production.StoryPlanError("corrected scene voice invalid")
        _copy_verified_asset(
            _artifact_path(parent_dir, voice.get("path")),
            _artifact_path(child_dir, voice.get("path")),
            digest,
        )


def create_corrected_scene_revision_plan(
    root: Path, plan_id: str, *, admin_id: int, expected_admin_id: int
) -> dict[str, Any]:
    """Create or reuse one immutable provider-free corrected-input child plan."""
    if (
        type(admin_id) is not int
        or type(expected_admin_id) is not int
        or not expected_admin_id
        or admin_id != expected_admin_id
    ):
        raise story_production.StoryPlanError("corrected scene revision admin required")
    parent_path = manifest_path(root, plan_id)
    with _manifest_lock(parent_path):
        parent_raw = parent_path.read_bytes()
        parent = story_production.read_manifest(parent_path)
        if not _current_recovery_manifest_supported(parent):
            raise story_production.StoryPlanError("story_manifest_contract_stale")
        proposal_path, _proposal, proposal_digest = _exact_current_proposal(
            parent_path.parent, plan_id
        )
        completed = _completed_asset_inventory(parent, parent_path.parent)
        failed = _failed_input_inventory(parent)
        corrected = _corrected_scene_inputs(parent)
        identity = _revision_plan_identity_payload(
            proposal_id=proposal_path.stem,
            proposal_digest=proposal_digest,
            parent_plan_id=plan_id,
            parent_manifest_digest=hashlib.sha256(parent_raw).hexdigest(),
            immutable_plan_fingerprint=str(parent.get("immutable_plan_fingerprint", "")),
            completed_assets=completed,
            failed_inputs=failed,
            corrected_scenes=corrected,
            admin_id=admin_id,
        )
        revision_plan_id = hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:24]
        child_dir = _safe_revision_dir(root, revision_plan_id)
        plan_path = child_dir / "revision-plan.json"
        if plan_path.exists():
            stored = _read_closed_json(
                plan_path, schema=CORRECTED_SCENE_REVISION_SCHEMA, canonical=True
            )
            if {key: value for key, value in stored.items() if key != "created_timestamp"} != {
                **identity, "revision_plan_id": revision_plan_id
            }:
                raise story_production.StoryPlanError("corrected scene revision conflict")
            plan = stored
        else:
            plan = {
                **identity,
                "revision_plan_id": revision_plan_id,
                "created_timestamp": _now(),
            }
            _write_exact_private(plan_path, plan)
        _copy_revision_reuse_assets(
            parent_manifest=parent,
            parent_dir=parent_path.parent,
            child_dir=child_dir,
        )
        if parent_path.read_bytes() != parent_raw:
            raise story_production.StoryPlanError("parent plan changed during revision create")
    return corrected_scene_revision_summary(root, revision_plan_id)


def read_corrected_scene_revision_plan(root: Path, revision_plan_id: str) -> dict[str, Any]:
    plan_path = _safe_revision_dir(root, revision_plan_id) / "revision-plan.json"
    plan = _read_closed_json(
        plan_path, schema=CORRECTED_SCENE_REVISION_SCHEMA, canonical=True
    )
    expected_keys = {
        "schema", "proposal_id", "proposal_digest", "parent_plan_id",
        "parent_manifest_digest", "parent_immutable_plan_fingerprint",
        "completed_scenes", "failed_scenes", "corrected_scenes",
        "revised_scene_set_digest", "provider_workload", "credit_estimate",
        "approval_status", "generation_authorized", "admin_id",
        "revision_plan_id", "created_timestamp",
    }
    identity = {
        key: value
        for key, value in plan.items()
        if key not in {"revision_plan_id", "created_timestamp"}
    }
    corrected_rows = plan.get("corrected_scenes", [])
    if (
        set(plan) != expected_keys
        or plan.get("revision_plan_id") != revision_plan_id
        or plan.get("parent_plan_id") == revision_plan_id
        or plan.get("approval_status") != "awaiting_cost_approval"
        or plan.get("generation_authorized") is not False
        or plan.get("provider_workload") != {
            "maximum_new_keyframes": 2,
            "maximum_new_videos": 2,
            "old_input_retries": 0,
            "completed_scene_regenerations": 0,
        }
        or [row.get("scene_id") for row in plan.get("corrected_scenes", [])]
        != list(CORRECTED_SCENE_IDS)
        or hashlib.sha256(_canonical_bytes(identity)).hexdigest()[:24]
        != revision_plan_id
        or any(
            type(row) is not dict
            or row.get("keyframe_input_digest")
            != _digest_value({
                key: value
                for key, value in row.items()
                if key not in {"revision_id", "keyframe_input_digest"}
            })
            or row.get("revision_id")
            != hashlib.sha256(
                f"{plan.get('parent_plan_id')}|{row.get('scene_id')}|"
                f"{row.get('keyframe_input_digest')}".encode("utf-8")
            ).hexdigest()[:24]
            for row in corrected_rows
        )
    ):
        raise story_production.StoryPlanError("corrected scene revision state invalid")
    return plan


def corrected_scene_revision_callback_token(
    plan: Mapping[str, Any], *, action: str, admin_id: int
) -> str:
    if action not in _REVISION_CALLBACK_ACTIONS or type(admin_id) is not int:
        raise story_production.StoryPlanError("corrected scene callback invalid")
    binding = {
        "action": action,
        "admin_id": admin_id,
        "revision_plan_id": plan.get("revision_plan_id"),
        "proposal_id": plan.get("proposal_id"),
        "proposal_digest": plan.get("proposal_digest"),
        "parent_plan_id": plan.get("parent_plan_id"),
        "parent_manifest_digest": plan.get("parent_manifest_digest"),
        "revised_scene_set_digest": plan.get("revised_scene_set_digest"),
        "approved_credit_ceiling": plan.get("credit_estimate", {}).get("ceiling"),
    }
    return _digest_value(binding)[:16]


def corrected_scene_revision_summary(root: Path, revision_plan_id: str) -> dict[str, Any]:
    plan = read_corrected_scene_revision_plan(root, revision_plan_id)
    approval_path = _safe_revision_dir(root, revision_plan_id) / "approval.json"
    cancelled_path = _safe_revision_dir(root, revision_plan_id) / "cancelled.json"
    status = (
        "cancelled" if cancelled_path.exists()
        else "approved" if approval_path.exists()
        else "awaiting_cost_approval"
    )
    return {
        "revision_plan_id": revision_plan_id,
        "proposal_id": str(plan["proposal_id"]),
        "proposal_digest": str(plan["proposal_digest"]),
        "parent_plan_id": str(plan["parent_plan_id"]),
        "corrected_scene_ids": tuple(CORRECTED_SCENE_IDS),
        "completed_scene_ids": tuple(CORRECTED_COMPLETED_SCENE_IDS),
        "completed_checksum_count": sum(
            len(row["asset_checksums"]) for row in plan["completed_scenes"]
        ),
        "models": {
            str(row["scene_id"]): {
                "keyframe": str(row["keyframe_model"]),
                "video": str(row["video_model"]),
            }
            for row in plan["corrected_scenes"]
        },
        "credit_ceiling": int(plan["credit_estimate"]["ceiling"]),
        "maximum_new_keyframes": 2,
        "maximum_new_videos": 2,
        "status": status,
        "provider_calls": 0,
        "tts_calls": 0,
        "publication_calls": 0,
    }


def corrected_scene_revision_card(root: Path, revision_plan_id: str) -> str:
    summary = corrected_scene_revision_summary(root, revision_plan_id)
    return (
        "🎬 План замены двух сцен готов\n\n"
        f"Исходный план:\n{summary['parent_plan_id']}\n\n"
        "Сохранены без изменений:\nсцены 1, 3 и 4\n\n"
        "Новые исправленные версии:\nсцены 2 и 5\n\n"
        "Сцена 2:\nобъектная визуализация сохранённой памяти и разорванной передачи контекста\n\n"
        "Сцена 5:\nобъектная визуализация восстановленной передачи и разделённых пользовательских линий\n\n"
        "Будут созданы:\n- 2 новых ключевых кадра;\n- до 2 новых видеосцен;\n"
        "- 0 повторов старого input;\n- 0 перегенераций готовых сцен.\n\n"
        "Маршруты:\n"
        "- Сцена 2: gen4_image → gen4_turbo;\n"
        "- Сцена 5: gen4_image → gen4_turbo.\n\n"
        f"Максимальная дополнительная стоимость:\n{summary['credit_ceiling']} Runway credits\n\n"
        "Платные вызовы начнутся только после отдельного подтверждения."
    )


def corrected_scene_revision_technical_card(root: Path, revision_plan_id: str) -> str:
    summary = corrected_scene_revision_summary(root, revision_plan_id)
    return (
        f"Технический план {revision_plan_id}\n"
        f"Статус: {summary['status']}\n"
        "Сохранённые assets: 9/9\n"
        "Новые immutable revisions: 02_problem, 05_conclusion\n"
        "Обе сцены object-only, identity references: none\n"
        "Лимит: 2 keyframes + 2 videos, без старых input и без hidden retry\n"
        f"Credit ceiling: {summary['credit_ceiling']}"
    )


def _progress_bucket(value: object, *, keyframe: bool) -> str:
    state = str(value or "")
    if keyframe:
        if state in {"planned", "queued"}:
            return "queued"
        if state in {"submitting", "submitted"}:
            return "submitted"
        if state in {"in_progress", "downloaded"}:
            return "in_progress"
        if state == "ready":
            return "ready"
        return "failed"
    if state in {"planned", "queued"}:
        return "queued"
    if state in {"submitting", "submitted"}:
        return "submitted"
    if state in {"in_progress", "downloaded", "composed"}:
        return "in_progress"
    if state == "completed":
        return "completed"
    return "failed"


def _safe_revision_failure(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if re.fullmatch(r"[a-z0-9_]{1,80}", text) else "provider_failure"


def corrected_scene_revision_progress(
    root: Path, revision_plan_id: str
) -> dict[str, Any]:
    """Read one approved child runtime and return a detached safe progress view."""
    plan, runtime, _runtime_path = validate_corrected_scene_revision_for_worker(
        root, revision_plan_id
    )
    corrected_jobs = [
        job for job in runtime["scene_jobs"]
        if str(job.get("scene_id")) in CORRECTED_SCENE_IDS
    ]
    keyframes = {name: 0 for name in ("queued", "submitted", "in_progress", "ready", "failed")}
    videos = {name: 0 for name in ("queued", "submitted", "in_progress", "completed", "failed")}
    failures: dict[str, dict[str, str] | None] = {}
    active_provider_tasks = 0
    for job in corrected_jobs:
        keyframe_bucket = _progress_bucket(job.get("keyframe_state"), keyframe=True)
        video_bucket = _progress_bucket(job.get("state"), keyframe=False)
        keyframes[keyframe_bucket] += 1
        videos[video_bucket] += 1
        scene_id = str(job["scene_id"])
        if keyframe_bucket == "failed":
            failures[scene_id] = {
                "stage": "keyframe",
                "category": _safe_revision_failure(job.get("keyframe_failure_code"))
                or "provider_failure",
            }
        elif video_bucket == "failed":
            failures[scene_id] = {
                "stage": "video",
                "category": _safe_revision_failure(job.get("failure_code"))
                or "provider_failure",
            }
        else:
            failures[scene_id] = None
        if job.get("keyframe_external_job_id") and keyframe_bucket in {
            "submitted", "in_progress"
        }:
            active_provider_tasks += 1
        if job.get("external_job_id") and video_bucket in {"submitted", "in_progress"}:
            active_provider_tasks += 1

    reel_jobs = runtime.get("reel_jobs", [])
    reel_states = [str(row.get("state", "")) for row in reel_jobs]
    if reel_states and all(state == "completed" for state in reel_states):
        final_reel_state = "ready"
    elif any(state in {"terminal_failed", "blocked_music", "blocked_voice"} for state in reel_states):
        final_reel_state = "failed"
    elif any(state not in {"planned", "queued"} for state in reel_states):
        final_reel_state = "assembling"
    else:
        final_reel_state = "not_started"

    if failures and any(value is not None for value in failures.values()):
        failed_scene = next(scene for scene, value in failures.items() if value is not None)
        detail = failures[failed_scene]
        current_stage = f"scene_{failed_scene}_{detail['stage']}_failed"
    elif final_reel_state == "ready":
        current_stage = "private_delivery"
    elif videos["completed"] == len(CORRECTED_SCENE_IDS):
        current_stage = "final_composition"
    elif videos["submitted"] or videos["in_progress"]:
        current_stage = "corrected_video_generation"
    elif keyframes["ready"] == len(CORRECTED_SCENE_IDS):
        current_stage = "corrected_video_queue"
    elif keyframes["submitted"] or keyframes["in_progress"]:
        current_stage = "corrected_keyframe_generation"
    else:
        current_stage = "corrected_keyframe_queue"

    completed_units = keyframes["ready"] + videos["completed"] + int(
        final_reel_state == "ready"
    )
    total_units = len(CORRECTED_SCENE_IDS) * 2 + 1
    return {
        "revision_plan_id": revision_plan_id,
        "parent_plan_id": str(plan["parent_plan_id"]),
        "approval_state": str(runtime["approval"]["status"]),
        "pack_status": str(runtime.get("pack_status", "")),
        "completed_reused_scenes": tuple(CORRECTED_COMPLETED_SCENE_IDS),
        "corrected_scene_count": len(CORRECTED_SCENE_IDS),
        "corrected_keyframes": keyframes,
        "corrected_videos": videos,
        "final_reel_state": final_reel_state,
        "delivery_state": str(runtime.get("delivery", {}).get("status", "not_ready")),
        "current_stage": current_stage,
        "failures": failures,
        "active_provider_task_count": active_provider_tasks,
        "completed_technical_units": completed_units,
        "total_technical_units": total_units,
        "progress_percent": completed_units * 100 // total_units,
        "keyframe_submission_count": sum(int(job.get("keyframe_attempts", 0)) for job in corrected_jobs),
        "video_submission_count": sum(int(job.get("attempts", 0)) for job in corrected_jobs),
    }


_REVISION_STAGE_RU = {
    "corrected_keyframe_queue": "исправленные ключевые кадры стоят в очереди",
    "corrected_keyframe_generation": "создаются исправленные ключевые кадры",
    "corrected_video_queue": "исправленные видеосцены стоят в очереди",
    "corrected_video_generation": "создаются исправленные видеосцены",
    "final_composition": "собирается итоговый Reel",
    "private_delivery": "итоговый Reel готов к приватной доставке",
}


def corrected_scene_revision_progress_card(root: Path, revision_plan_id: str) -> str:
    progress = corrected_scene_revision_progress(root, revision_plan_id)
    percent = int(progress["progress_percent"])
    filled = min(10, percent // 10)
    stage = str(progress["current_stage"])
    if stage.startswith("scene_") and stage.endswith("_failed"):
        phase_name = "keyframe" if stage.endswith("_keyframe_failed") else "video"
        scene = stage.removeprefix("scene_").removesuffix(f"_{phase_name}_failed")
        phase = "ключевой кадр" if phase_name == "keyframe" else "видеосцена"
        failure = progress["failures"].get(scene) or {}
        now = f"сцена {scene}: {phase}, {failure.get('category', 'provider_failure')}"
    else:
        now = _REVISION_STAGE_RU.get(stage, "состояние проверено")
    reel_ru = {
        "not_started": "не начат", "assembling": "собирается",
        "ready": "готов", "failed": "ошибка",
    }[str(progress["final_reel_state"])]
    return (
        "🎬 Замена сцен · прогресс\n\n"
        "Сохранённые сцены:\n1, 3 и 4 — готовы\n\n"
        "Исправленные сцены:\n2 и 5\n\n"
        f"Ключевые кадры:\n{progress['corrected_keyframes']['ready']}/2\n\n"
        f"Видеосцены:\n{progress['corrected_videos']['completed']}/2\n\n"
        f"Итоговый Reel:\n{reel_ru}\n\n"
        f"Прогресс:\n[{'█' * filled}{'░' * (10 - filled)}] {percent}%\n\n"
        f"Сейчас:\n{now}"
    )


def _reset_corrected_scene_job(job: dict[str, Any]) -> None:
    for field in _CORRECTED_SCENE_INHERITED_RETRY_FIELDS:
        job.pop(field, None)
    job.pop("visual_identity_qa", None)


def resume_corrected_scene_revision_runtime(
    root: Path, revision_plan_id: str
) -> bool:
    """Repair only the zero-call inherited-retry failure in an approved child."""
    _plan, runtime, runtime_path = validate_corrected_scene_revision_for_worker(
        root, revision_plan_id
    )
    child_dir = runtime_path.parent
    resumable: list[dict[str, Any]] = []
    for job in runtime["scene_jobs"]:
        if str(job.get("scene_id")) not in CORRECTED_SCENE_IDS:
            continue
        exact_failure = (
            job.get("state") == "terminal_failed"
            and job.get("keyframe_state") == "terminal_failed"
            and job.get("failure_code") == "keyframe_retry_contract_invalid"
            and job.get("keyframe_failure_code") == "keyframe_retry_contract_invalid"
            and int(job.get("attempts", 0)) == 0
            and int(job.get("keyframe_attempts", 0)) == 0
            and not job.get("external_job_id")
            and not job.get("keyframe_external_job_id")
            and not job.get("submit_intent")
            and not job.get("keyframe_submit_intent")
            and not job.get("submit_intent_history")
            and not job.get("keyframe_submit_intent_history")
            and not job.get("provider_job_history")
            and not job.get("keyframe_provider_job_history")
            and not _artifact_path(child_dir, job.get("keyframe_path")).exists()
            and not _artifact_path(child_dir, job.get("clean_path")).exists()
            and not _artifact_path(child_dir, job.get("story_path")).exists()
            and any(field in job for field in _CORRECTED_SCENE_INHERITED_RETRY_FIELDS)
        )
        if exact_failure:
            resumable.append(job)
    if not resumable:
        return False
    for job in resumable:
        _reset_corrected_scene_job(job)
        job.update({
            "state": "queued", "failure_code": None,
            "keyframe_state": "queued", "keyframe_failure_code": None,
            "provider_status": None, "keyframe_provider_status": None,
        })
    runtime["pack_status"] = "queued"
    write_corrected_scene_runtime(runtime_path, runtime)
    validate_corrected_scene_revision_for_worker(root, revision_plan_id)
    return True


def _revision_plan_matches_current_parent(
    root: Path, plan: Mapping[str, Any]
) -> tuple[Path, dict[str, Any]]:
    parent_id = str(plan.get("parent_plan_id", ""))
    parent_path = manifest_path(root, parent_id)
    parent_raw = parent_path.read_bytes()
    if hashlib.sha256(parent_raw).hexdigest() != plan.get("parent_manifest_digest"):
        raise story_production.StoryPlanError("corrected scene parent stale")
    parent = story_production.read_manifest(parent_path)
    if parent.get("immutable_plan_fingerprint") != plan.get(
        "parent_immutable_plan_fingerprint"
    ):
        raise story_production.StoryPlanError("corrected scene parent stale")
    proposal_path, _proposal, proposal_digest = _exact_current_proposal(
        parent_path.parent, parent_id
    )
    if (
        proposal_path.stem != plan.get("proposal_id")
        or proposal_digest != plan.get("proposal_digest")
        or list(_completed_asset_inventory(parent, parent_path.parent))
        != plan.get("completed_scenes")
        or list(_failed_input_inventory(parent)) != plan.get("failed_scenes")
    ):
        raise story_production.StoryPlanError("corrected scene binding stale")
    child_dir = _safe_revision_dir(root, str(plan["revision_plan_id"]))
    child_jobs = {
        str(job.get("scene_id")): job
        for job in parent.get("scene_jobs", [])
        if isinstance(job, Mapping)
    }
    for row in plan["completed_scenes"]:
        job = child_jobs[str(row["scene_id"])]
        for role, path_field in (
            ("keyframe", "keyframe_path"),
            ("clean", "clean_path"),
            ("story", "story_path"),
        ):
            child_asset = _artifact_path(child_dir, job[path_field])
            if (
                child_asset.is_symlink()
                or not child_asset.is_file()
                or sha256_file(child_asset, maximum_bytes=1024 * 1024 * 1024)
                != row["asset_checksums"][role]
            ):
                raise story_production.StoryPlanError("corrected scene reused asset stale")
    return parent_path, parent


def _revision_runtime_payload(
    plan: Mapping[str, Any], parent: Mapping[str, Any], *, approved_at: str
) -> dict[str, Any]:
    runtime = deepcopy(dict(parent))
    runtime.update({
        "schema": CORRECTED_SCENE_RUNTIME_SCHEMA,
        "plan_id": plan["revision_plan_id"],
        "parent_plan_id": plan["parent_plan_id"],
        "revision_plan_digest": hashlib.sha256(_canonical_bytes(plan)).hexdigest(),
        "approval": {"status": "approved", "approved_at": approved_at},
        "pack_status": "queued",
        "provider": {"name": None, "model": None},
        "renderer": {"status": "queued", "name": "ffmpeg"},
        "composer": {"status": "planned", "name": "ffmpeg"},
        "delivery": {"status": "not_ready", "sent_files": [], "completed_at": None},
        "updated_at": approved_at,
    })
    corrected = {str(row["scene_id"]): row for row in plan["corrected_scenes"]}
    for scene in runtime["scenes"]:
        scene_id = str(scene.get("scene_id"))
        if scene_id not in corrected:
            continue
        row = corrected[scene_id]
        scene.update({
            "requires_naz_reference": False,
            "reference_role": "none",
            "identity_anchor_role": "none",
            "desired_view_role": "none",
            "auxiliary_reference_roles": [],
            "setting": row["visual_direction"],
            "concrete_action": row["provider_prompt"],
            "end_state": row["semantic_meaning"],
            "keyframe_prompt": row["keyframe_prompt"],
            "provider_prompt": row["provider_prompt"],
            "story_overlay": row["story_overlay"],
            "text_safe_zone": row["text_safe_zone"],
            "corrected_revision_id": row["revision_id"],
            "corrected_keyframe_input_digest": row["keyframe_input_digest"],
        })
    for job in runtime["scene_jobs"]:
        scene_id = str(job.get("scene_id"))
        if scene_id not in corrected:
            continue
        row = corrected[scene_id]
        _reset_corrected_scene_job(job)
        job.update({
            "state": "queued", "external_job_id": None, "attempts": 0,
            "submitted_at": None, "provider_status": None, "actual_duration_seconds": None,
            "media_probe": None, "clean_checksum": None, "story_checksum": None,
            "technical_qa": {"status": "not_run"}, "failure_code": None,
            "requires_naz_reference": False, "reference_role": "none",
            "identity_anchor_role": "none", "desired_view_role": "none",
            "auxiliary_reference_roles": [], "keyframe_state": "queued",
            "keyframe_external_job_id": None, "keyframe_attempts": 0,
            "keyframe_submitted_at": None, "keyframe_provider_status": None,
            "keyframe_checksum": None, "keyframe_failure_code": None,
            "keyframe_submit_intent": None, "keyframe_submit_intent_history": [],
            "keyframe_provider_job_history": [], "submit_intent": None,
            "submit_intent_history": [], "provider_job_history": [],
            "corrected_revision_id": row["revision_id"],
            "corrected_keyframe_input_digest": row["keyframe_input_digest"],
            "model_route": {
                "tier": "primary", "primary_model": "gen4_turbo",
                "secondary_model": "gen4.5", "primary_failure_code": None,
                "secondary_requested_at": None, "secondary_approved_at": None,
                "scene_strategy": story_production.HYBRID_MODEL_ROUTE,
                "selected_model": "gen4_turbo",
            },
        })
    for reel in runtime.get("reel_jobs", []):
        reel.update({
            "state": "planned", "media_probe": None, "checksum": None,
            "failure_code": None,
        })
    statuses = {
        str(job["scene_id"]): (
            "completed" if job["scene_id"] in CORRECTED_COMPLETED_SCENE_IDS else "queued"
        )
        for job in runtime["scene_jobs"]
    }
    for output in runtime.get("expected_outputs", {}).get("stories", []):
        scene_id = Path(str(output.get("clean", ""))).stem.removesuffix("_clean")
        output["status"] = statuses.get(scene_id, "queued")
    return runtime


def approve_corrected_scene_revision_plan(
    root: Path, revision_plan_id: str, *, callback_token: str,
    admin_id: int, expected_admin_id: int,
) -> str:
    """Provider-free cost approval for one exact immutable child plan."""
    plan = read_corrected_scene_revision_plan(root, revision_plan_id)
    expected_token = corrected_scene_revision_callback_token(
        plan, action="approve", admin_id=admin_id
    )
    if (
        type(admin_id) is not int
        or type(expected_admin_id) is not int
        or admin_id != expected_admin_id
        or type(callback_token) is not str
        or callback_token != expected_token
    ):
        raise story_production.StoryPlanError("corrected scene approval binding invalid")
    parent_path = manifest_path(root, str(plan["parent_plan_id"]))
    with _manifest_lock(parent_path):
        plan = read_corrected_scene_revision_plan(root, revision_plan_id)
        if callback_token != corrected_scene_revision_callback_token(
            plan, action="approve", admin_id=admin_id
        ):
            raise story_production.StoryPlanError("corrected scene approval binding invalid")
        child_dir = _safe_revision_dir(root, revision_plan_id)
        approval_path = child_dir / "approval.json"
        runtime_path = child_dir / "revision-runtime.json"
        cancelled_path = child_dir / "cancelled.json"
        if cancelled_path.exists():
            raise story_production.StoryPlanError("corrected scene revision cancelled")
        parent_before = parent_path.read_bytes()
        _path, parent = _revision_plan_matches_current_parent(root, plan)
        if approval_path.exists():
            approval = _read_closed_json(
                approval_path,
                schema=CORRECTED_SCENE_APPROVAL_SCHEMA,
                canonical=True,
            )
            runtime = read_corrected_scene_runtime(runtime_path)
            if (
                approval.get("revision_plan_id") != revision_plan_id
                or approval.get("approved_credit_ceiling")
                != plan["credit_estimate"]["ceiling"]
                or runtime.get("revision_plan_digest")
                != hashlib.sha256(_canonical_bytes(plan)).hexdigest()
            ):
                raise story_production.StoryPlanError("corrected scene approval conflict")
            return "already_approved"
        if runtime_path.exists():
            raise story_production.StoryPlanError("corrected scene approval conflict")
        for job in parent.get("scene_jobs", []):
            if (
                isinstance(job, Mapping)
                and job.get("scene_id") in CORRECTED_SCENE_IDS
                and job.get("state") != "terminal_failed"
            ):
                raise story_production.StoryPlanError("corrected scene provider job active")
        approved_at = _now()
        runtime = _revision_runtime_payload(plan, parent, approved_at=approved_at)
        write_corrected_scene_runtime(runtime_path, runtime)
        approval = {
            "schema": CORRECTED_SCENE_APPROVAL_SCHEMA,
            "revision_plan_id": revision_plan_id,
            "proposal_id": plan["proposal_id"],
            "proposal_digest": plan["proposal_digest"],
            "parent_plan_id": plan["parent_plan_id"],
            "parent_manifest_digest": plan["parent_manifest_digest"],
            "revised_scene_set_digest": plan["revised_scene_set_digest"],
            "approved_credit_ceiling": plan["credit_estimate"]["ceiling"],
            "approved_by_admin_id": admin_id,
            "approved_at": approved_at,
            "generation_authorized": True,
        }
        _write_exact_private(approval_path, approval)
        if parent_path.read_bytes() != parent_before:
            raise story_production.StoryPlanError("parent plan changed during revision approval")
    return "approved"


def cancel_corrected_scene_revision_plan(
    root: Path, revision_plan_id: str, *, callback_token: str,
    admin_id: int, expected_admin_id: int,
) -> str:
    plan = read_corrected_scene_revision_plan(root, revision_plan_id)
    if (
        type(admin_id) is not int
        or type(expected_admin_id) is not int
        or admin_id != expected_admin_id
        or callback_token != corrected_scene_revision_callback_token(
            plan, action="cancel", admin_id=admin_id
        )
    ):
        raise story_production.StoryPlanError("corrected scene cancel binding invalid")
    child_dir = _safe_revision_dir(root, revision_plan_id)
    if (child_dir / "approval.json").exists() or (child_dir / "revision-runtime.json").exists():
        raise story_production.StoryPlanError("approved corrected scene revision cannot be cancelled")
    created = _write_exact_private(child_dir / "cancelled.json", {
        "schema": CORRECTED_SCENE_CANCEL_SCHEMA,
        "revision_plan_id": revision_plan_id,
        "parent_plan_id": plan["parent_plan_id"],
        "cancelled_by_admin_id": admin_id,
        "cancelled_at": _now(),
    })
    return "cancelled" if created else "already_cancelled"


def validate_corrected_scene_revision_for_worker(
    root: Path, revision_plan_id: str
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    plan = read_corrected_scene_revision_plan(root, revision_plan_id)
    child_dir = _safe_revision_dir(root, revision_plan_id)
    approval = _read_closed_json(
        child_dir / "approval.json",
        schema=CORRECTED_SCENE_APPROVAL_SCHEMA,
        canonical=True,
    )
    runtime_path = child_dir / "revision-runtime.json"
    runtime = read_corrected_scene_runtime(runtime_path)
    _parent_path, parent = _revision_plan_matches_current_parent(root, plan)
    if (
        approval.get("revision_plan_id") != revision_plan_id
        or approval.get("proposal_digest") != plan.get("proposal_digest")
        or approval.get("approved_credit_ceiling")
        != plan.get("credit_estimate", {}).get("ceiling")
        or approval.get("generation_authorized") is not True
        or runtime.get("plan_id") != revision_plan_id
        or runtime.get("parent_plan_id") != plan.get("parent_plan_id")
        or runtime.get("revision_plan_digest")
        != hashlib.sha256(_canonical_bytes(plan)).hexdigest()
        or [
            job.get("scene_id") for job in runtime.get("scene_jobs", [])
            if job.get("corrected_revision_id")
        ] != list(CORRECTED_SCENE_IDS)
    ):
        raise story_production.StoryPlanError("corrected scene runtime binding invalid")
    corrected = {str(row["scene_id"]): row for row in plan["corrected_scenes"]}
    runtime_scenes = {
        str(row.get("scene_id")): row
        for row in runtime.get("scenes", [])
        if isinstance(row, Mapping)
    }
    runtime_jobs = {
        str(row.get("scene_id")): row
        for row in runtime.get("scene_jobs", [])
        if isinstance(row, Mapping)
    }
    parent_jobs = {
        str(row.get("scene_id")): row
        for row in parent.get("scene_jobs", [])
        if isinstance(row, Mapping)
    }
    if (
        set(runtime_scenes) != set(parent_jobs)
        or set(runtime_jobs) != set(parent_jobs)
        or runtime.get("reel_edits") != parent.get("reel_edits")
        or runtime.get("caption_plan") != parent.get("caption_plan")
    ):
        raise story_production.StoryPlanError("corrected scene runtime binding invalid")
    for scene_id, row in corrected.items():
        scene = runtime_scenes.get(scene_id, {})
        job = runtime_jobs.get(scene_id, {})
        if (
            scene.get("corrected_revision_id") != row["revision_id"]
            or scene.get("corrected_keyframe_input_digest")
            != row["keyframe_input_digest"]
            or scene.get("keyframe_prompt") != row["keyframe_prompt"]
            or scene.get("provider_prompt") != row["provider_prompt"]
            or scene.get("story_overlay") != row["story_overlay"]
            or scene.get("requires_naz_reference") is not False
            or job.get("corrected_revision_id") != row["revision_id"]
            or job.get("corrected_keyframe_input_digest")
            != row["keyframe_input_digest"]
            or job.get("requires_naz_reference") is not False
            or job.get("model_route", {}).get("selected_model") != row["video_model"]
            or int(job.get("keyframe_attempts", 0)) > 1
            or int(job.get("attempts", 0)) > 1
        ):
            raise story_production.StoryPlanError("corrected scene runtime binding invalid")
    completed_by_id = {
        str(row["scene_id"]): row for row in plan["completed_scenes"]
    }
    for scene_id, binding in completed_by_id.items():
        job = runtime_jobs.get(scene_id, {})
        parent_job = parent_jobs.get(scene_id, {})
        if (
            job.get("state") != "completed"
            or job.get("keyframe_checksum") != binding["asset_checksums"]["keyframe"]
            or job.get("clean_checksum") != binding["asset_checksums"]["clean"]
            or job.get("story_checksum") != binding["asset_checksums"]["story"]
            or job.get("external_job_id") != parent_job.get("external_job_id")
            or job.get("keyframe_external_job_id")
            != parent_job.get("keyframe_external_job_id")
            or job.get("attempts") != parent_job.get("attempts")
            or job.get("keyframe_attempts") != parent_job.get("keyframe_attempts")
        ):
            raise story_production.StoryPlanError("corrected scene reused asset stale")
    return plan, runtime, runtime_path


def queued_corrected_scene_revision_ids(root: Path) -> list[str]:
    base = Path(root).expanduser().resolve()
    result: list[str] = []
    if not base.is_dir():
        return result
    for path in sorted(base.glob("*/revision-runtime.json"), key=lambda item: item.stat().st_mtime):
        revision_plan_id = path.parent.name
        if not _PLAN_ID_RE.fullmatch(revision_plan_id):
            continue
        try:
            _plan, runtime, _runtime_path = validate_corrected_scene_revision_for_worker(
                base, revision_plan_id
            )
        except (OSError, ValueError, story_production.StoryPlanError):
            continue
        if runtime.get("pack_status") not in {"completed", "awaiting_secondary_approval"}:
            result.append(revision_plan_id)
    return result


def current_frontal_reference_retry_plan(
    root: Path,
    plan_id: str,
    *,
    health_root: Path | None = None,
) -> dict[str, Any]:
    """Return a detached, read-only recovery summary for an approval card."""
    path = manifest_path(root, plan_id)
    payload = story_production.read_manifest(path)
    if not _current_recovery_manifest_supported(payload):
        raise story_production.StoryPlanError("story_manifest_contract_stale")
    if str(payload.get("approval", {}).get("status")) != "approved":
        raise story_production.StoryPlanError("story pack is not approved")
    candidates = _current_frontal_reference_retry_candidates(
        payload, pack_dir=path.parent
    )
    if not candidates or len(candidates) > 4:
        raise story_production.StoryPlanError(
            "current frontal reference retry unavailable"
        )
    completed = tuple(
        str(job.get("scene_id"))
        for job in payload.get("scene_jobs", [])
        if isinstance(job, Mapping) and job.get("state") == "completed"
    )
    scene_ids = tuple(str(job.get("scene_id")) for job in candidates)
    video_credits = sum(
        int(job.get("planned_duration_seconds", 0))
        * story_production.RUNWAY_VIDEO_CREDITS_PER_SECOND["gen4.5"]
        for job in candidates
    )
    result = {
        "plan_id": plan_id,
        "completed_scene_ids": completed,
        "retry_scene_ids": scene_ids,
        "scene_total": len(payload.get("scene_jobs", [])),
        "keyframe_jobs": len(candidates),
        "video_jobs": len(candidates),
        "reference_role": "frontal_identity",
        "keyframe_model": "gen4_image",
        "video_model": "gen4.5",
        "additional_credits": 5 * len(candidates) + video_credits,
    }
    if health_root is not None:
        registry = ReferenceHealthRegistry(health_root)
        binding = _migration_binding(
            registry, plan_id=plan_id, manifest=path
        )
        if binding is None or binding.get("retry_scene_ids") != list(scene_ids):
            raise story_production.StoryPlanError(
                "current reference health policy unavailable"
            )
        result["health_policy_version"] = HEALTH_POLICY_VERSION
    return result


def current_frontal_reference_retry_card(
    root: Path,
    plan_id: str,
    *,
    health_root: Path | None = None,
) -> str:
    """Build the closed admin recovery card without mutating the pack."""
    plan = current_frontal_reference_retry_plan(
        root, plan_id, health_root=health_root
    )
    scene_numbers = ", ".join(
        scene_id.split("_", 1)[0].lstrip("0") or "0"
        for scene_id in plan["retry_scene_ids"]
    )
    return (
        "⚠️ Причина повторения устранена системно\n\n"
        "frontal_identity теперь главный identity anchor.\n"
        "three_quarter route помещён в карантин; будущие queued-сцены не пойдут по этому маршруту.\n\n"
        f"Готовые сцены: {len(plan['completed_scene_ids'])}/{plan['scene_total']} — сохраняются без изменений.\n"
        f"Повторить сцены: {scene_numbers}.\n\n"
        f"Максимум новых keyframes: {plan['keyframe_jobs']} ({plan['keyframe_model']}).\n"
        f"Максимум новых video jobs: {plan['video_jobs']} ({plan['video_model']}).\n"
        f"Референс: {plan['reference_role']}.\n"
        f"Дополнительная оценка: около {plan['additional_credits']} Runway credits.\n\n"
        "Текущие сцены можно один раз восстановить с фронтальным референсом.\n"
        "Платные вызовы начнутся только после отдельного подтверждения ниже."
    )


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


def _concise_identity_retry_candidates(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Find frontal retries eligible for one concise-prompt recovery."""
    candidates: list[dict[str, Any]] = []
    for job in payload.get("scene_jobs", []):
        if not isinstance(job, dict) or not bool(job.get("requires_naz_reference")):
            continue
        if (
            not job.get("keyframe_frontal_retry_approved_at")
            or job.get("keyframe_concise_retry_approved_at")
            or job.get("keyframe_retry_reference_role") != "frontal_identity"
        ):
            continue
        attempts = int(job.get("keyframe_attempts", 0))
        intent = job.get("keyframe_submit_intent")
        queued_frontal_retry = (
            job.get("state") == "queued"
            and job.get("keyframe_state") == "queued"
            and attempts == 1
            and not job.get("keyframe_external_job_id")
            and job.get("keyframe_retry_phase") == "reference_quality"
        )
        failed_frontal_retry = (
            job.get("state") == "terminal_failed"
            and job.get("keyframe_state") == "terminal_failed"
            and job.get("keyframe_failure_code") == "provider_terminal_failure"
            and attempts == 3
            and isinstance(intent, Mapping)
            and intent.get("model") == "gen4_image"
            and intent.get("approval_scope") == "frontal_reference_retry"
        )
        if queued_frontal_retry or failed_frontal_retry:
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


def approve_current_frontal_reference_retry(
    root: Path,
    plan_id: str,
    *,
    admin_id: int,
    expected_admin_id: int,
    health_root: Path | None = None,
) -> str:
    """Approve only current ``gen4_image`` failures for a frontal retry.

    This boundary is provider-free.  It archives the completed submit records
    and queues only the exact eligible scenes; the one-shot worker owns all
    later provider transport.
    """
    if (
        type(admin_id) is not int
        or type(expected_admin_id) is not int
        or not expected_admin_id
        or admin_id != expected_admin_id
    ):
        raise story_production.StoryPlanError(
            "current frontal reference retry admin required"
        )
    path = manifest_path(root, plan_id)
    with _manifest_lock(path):
        payload = story_production.read_manifest(path)
        if not _current_recovery_manifest_supported(payload):
            raise story_production.StoryPlanError("story_manifest_contract_stale")
        if health_root is not None and _migration_binding(
            ReferenceHealthRegistry(health_root), plan_id=plan_id, manifest=path
        ) is None:
            raise story_production.StoryPlanError(
                "current reference health policy unavailable"
            )
        if str(payload.get("approval", {}).get("status")) != "approved":
            raise story_production.StoryPlanError("story pack is not approved")
        candidates = _current_frontal_reference_retry_candidates(
            payload, pack_dir=path.parent
        )
        if not candidates:
            if any(
                isinstance(job, Mapping)
                and job.get("keyframe_current_frontal_retry_approved_at")
                for job in payload.get("scene_jobs", [])
            ):
                return "already_approved"
            raise story_production.StoryPlanError(
                "current frontal reference retry unavailable"
            )
        if len(candidates) > 4:
            raise story_production.StoryPlanError(
                "current frontal reference retry limit exceeded"
            )
        confirmed_at = _now()
        for job in candidates:
            external_id = str(job.get("keyframe_external_job_id") or "")
            provider_history = job.setdefault("keyframe_provider_job_history", [])
            if external_id not in provider_history:
                provider_history.append(external_id)
            intent = job.get("keyframe_submit_intent")
            intent_history = job.setdefault("keyframe_submit_intent_history", [])
            archived_intent = dict(intent) if isinstance(intent, Mapping) else None
            if archived_intent is not None and archived_intent not in intent_history:
                intent_history.append(archived_intent)
            job.update({
                "state": "queued",
                "failure_code": None,
                "provider_status": None,
                "keyframe_state": "queued",
                "keyframe_external_job_id": None,
                "keyframe_submitted_at": None,
                "keyframe_provider_status": None,
                "keyframe_failure_code": None,
                "keyframe_submit_intent": None,
                "keyframe_retry_model": "gen4_image",
                "keyframe_retry_phase": "reference_quality",
                "keyframe_retry_reference_role": "frontal_identity",
                "keyframe_retry_reason_code": "provider_terminal_failure",
                "keyframe_retry_approved_at": confirmed_at,
                "keyframe_frontal_retry_approved_at": confirmed_at,
                "keyframe_current_frontal_retry_approved_at": confirmed_at,
            })
        payload["pack_status"] = "queued"
        payload["updated_at"] = confirmed_at
        story_production.atomic_json(path, payload)
        return "current_frontal_reference_keyframes_retry_approved"


def approve_concise_identity_retry(root: Path, plan_id: str) -> str:
    """Approve one concise-prompt recovery after frontal BAD_OUTPUT evidence."""
    path = manifest_path(root, plan_id)
    with _manifest_lock(path):
        payload = story_production.read_manifest(path)
        if not story_production.manifest_has_current_production_contract(payload):
            raise story_production.StoryPlanError("story_manifest_contract_stale")
        if str(payload.get("approval", {}).get("status")) != "approved":
            raise story_production.StoryPlanError("story pack is not approved")
        candidates = _concise_identity_retry_candidates(payload)
        if not candidates:
            return "already_approved"
        if len(candidates) > 4:
            raise story_production.StoryPlanError(
                "concise identity retry limit exceeded"
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
                "keyframe_retry_phase": "concise_identity",
                "keyframe_retry_prompt_mode": "concise_structured",
                "keyframe_retry_reason_code": "provider_terminal_failure",
                "keyframe_concise_retry_approved_at": confirmed_at,
            })
        payload["pack_status"] = "queued"
        payload["updated_at"] = confirmed_at
        story_production.atomic_json(path, payload)
        return "concise_identity_keyframes_retry_approved"


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
        if not story_production.manifest_has_current_production_contract(payload):
            raise story_production.StoryPlanError("story_manifest_contract_stale")
        if str(payload.get("approval", {}).get("status")) != "awaiting_approval":
            raise story_production.StoryPlanError("another variant is allowed before approval only")
        if any(job.get("external_job_id") for job in payload.get("scene_jobs", [])):
            raise story_production.StoryPlanError("another variant is unavailable after provider submit")
        editorial = payload.get("editorial_plan")
        facts = payload.get("safe_facts")
        if not isinstance(editorial, Mapping) or not isinstance(facts, list):
            raise story_production.StoryPlanError("story variant source contract missing")
        if director_treatment is None:
            raise story_production.StoryPlanError("director_treatment_required")
        if director_treatment.version != story_production.DIRECTOR_VERSION:
            raise story_production.StoryPlanError("story_director_contract_stale")
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
        "blocked_voice": "Stories готовы, озвучка требует внимания",
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
    primary_attempts = sum(
        min(int(job.get("keyframe_attempts", 0)), 1)
        for job in progress["scene_jobs"]
        if isinstance(job, Mapping)
    )
    automatic_recoveries = sum(
        int(job.get("keyframe_automatic_fallbacks", 0))
        for job in progress["scene_jobs"]
        if isinstance(job, Mapping)
    )
    quarantined_routes = {
        role
        for job in progress["scene_jobs"]
        if isinstance(job, Mapping)
        for intent in (job.get("keyframe_submit_intent"),)
        if isinstance(intent, Mapping)
        for role in intent.get("quarantined_reference_roles", [])
        if isinstance(role, str)
    }
    terminal_scenes = sum(
        str(job.get("state")) in {"terminal_failed", "blocked_reference", "submit_ambiguous"}
        for job in progress["scene_jobs"]
        if isinstance(job, Mapping)
    )
    return (
        "🎬 Reels Maker · прогресс\n\n"
        f"План: {str(payload.get('plan_id', ''))[:24]}\n"
        f"Статус: {PACK_STATUS_RU.get(pack_status, pack_status)}\n"
        f"[{progress['bar']}] {progress['percent']}% этапов\n\n"
        f"Ключевые кадры: {progress['keyframes_ready']}/{scene_total}\n"
        f"Видео сцен: {progress['videos_ready']}/{scene_total}\n"
        f"Готовые Reels: {progress['reels_ready']}/{reel_total}\n\n"
        f"Основные попытки: {primary_attempts}\n"
        f"Автоматические восстановления: {automatic_recoveries}\n"
        f"Маршруты в карантине: {len(quarantined_routes)}\n"
        f"Готовые сцены: {progress['videos_ready']}\n"
        f"Терминальные сцены: {terminal_scenes}\n\n"
        f"Сейчас: {_current_story_stage(payload, progress)}."
    )


def _safe_admin_semantic_text(payload: Mapping[str, Any], value: Any) -> str:
    """Return display text only when it cannot quote a stored source fragment."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    facts = payload.get("safe_facts")
    safe_facts = (
        tuple(item for item in facts if isinstance(item, str))
        if isinstance(facts, list)
        else ()
    )
    if story_production._raw_source_fragment_in_text(text, safe_facts):
        return ""
    return text


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
    model_seconds = {"gen4_turbo": 0, "gen4.5": 0}
    for job in jobs:
        route = job.get("model_route", {}) if isinstance(job, Mapping) else {}
        model = str(route.get("selected_model") or "gen4_turbo")
        if model not in model_seconds:
            model = "gen4_turbo"
        model_seconds[model] += int(job.get("planned_duration_seconds", 0))
    video_credits = sum(
        seconds * story_production.RUNWAY_VIDEO_CREDITS_PER_SECOND[model]
        for model, seconds in model_seconds.items()
    )
    model_mix = (
        f"Gen-4.5 {model_seconds['gen4.5']}s + Turbo {model_seconds['gen4_turbo']}s"
    )
    fallback_keyframes = sum(
        bool(scene.get("requires_naz_reference"))
        for scene in directed
        if isinstance(scene, Mapping)
    )
    primary_credits = keyframe_credits + video_credits
    maximum_credits = primary_credits + 5 * fallback_keyframes
    concept = _safe_admin_semantic_text(payload, payload.get("visual_concept"))
    admin_concept = _safe_admin_semantic_text(payload, payload.get("admin_concept_ru"))
    concept_label = admin_concept if re.search(r"[А-Яа-яЁё]", admin_concept) else ""
    if not concept_label:
        concept_label = VISUAL_CONCEPT_RU.get(concept, "")
    thesis = _safe_admin_semantic_text(payload, payload.get("central_thesis"))
    if not concept_label and re.search(r"[А-Яа-яЁё]", thesis):
        concept_label = thesis[:120]
    if not concept_label:
        for scene in directed:
            if not isinstance(scene, Mapping):
                continue
            overlay = _safe_admin_semantic_text(payload, scene.get("story_overlay"))
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
    semantic_contract = (
        str(payload.get("director_version", ""))
        == story_production.DIRECTOR_VERSION
    )
    core_thesis = _safe_admin_semantic_text(
        payload, payload.get("central_thesis")
    )[:180]
    hook = _safe_admin_semantic_text(payload, payload.get("hook"))[:160]
    payoff = _safe_admin_semantic_text(payload, payload.get("payoff"))[:160]
    semantic_overview = ""
    if semantic_contract and core_thesis:
        semantic_overview += f"Тезис: {core_thesis}\n"
    if semantic_contract and hook and payoff:
        semantic_overview += f"Зацепка → развязка: {hook} → {payoff}\n"
    statuses = "\n".join(
        f"• {SCENE_STATUS_RU.get(state, state)}: {count}"
        for state, count in sorted(counts.items())
    ) or "Сцены ещё не созданы"
    treatment = []
    for index, scene in enumerate(directed, 1):
        if isinstance(scene, Mapping):
            role = str(scene.get("role", "scene")).casefold()
            semantic_goal = _safe_admin_semantic_text(
                payload, scene.get("semantic_goal")
            )
            relation = _safe_admin_semantic_text(
                payload, scene.get("relation_to_previous")
            )
            if semantic_contract and semantic_goal:
                overlay = semantic_goal
            else:
                overlay = _safe_admin_semantic_text(
                    payload, scene.get("admin_summary_ru")
                )
                if not re.search(r"[А-Яа-яЁё]", overlay):
                    overlay = _legacy_scene_summary_ru(scene)
                if not re.search(r"[А-Яа-яЁё]", overlay):
                    overlay = _safe_admin_semantic_text(
                        payload, scene.get("story_overlay")
                    )
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
            transition_line = (
                f"   Переход: {relation[:112]}\n"
                if semantic_contract and index > 1 and relation
                else ""
            )
            treatment.append(
                f"{index}. {SCENE_ROLE_RU.get(role, 'СЦЕНА')}\n"
                f"   Смысл: {overlay[:112]}\n"
                f"{transition_line}"
                f"   В кадре: {subject} · План: {shot_size} · Камера: {camera}"
            )
    pack_status = str(payload.get("pack_status", "unknown"))
    return (
        "🎬 Reels Maker\n\n"
        f"План: {str(payload.get('plan_id', ''))[:24]}\n"
        f"Вариант: {int(payload.get('variant_index', 0)) + 1}\n"
        f"Рубрика: {str(payload.get('rubric', ''))[:120]}\n"
        f"Сюжетная линия: {concept_label}\n"
        f"{semantic_overview}"
        f"Статус: {PACK_STATUS_RU.get(pack_status, pack_status)}\n"
        f"Сцен: {len(jobs)}, запланировано секунд: {duration:g}\n"
        f"Сцен с Naz: {references}\n\n{statuses}\n\n"
        "Режиссёрский план:\n" + ("\n".join(treatment) or "—")
        + f"\n\nОценка Runway: keyframes {keyframe_credits} + video {video_credits} "
        f"= {primary_credits} кредитов.\n"
        f"Основной лимит: {len(directed)} keyframes + {len(jobs)} video jobs.\n"
        f"Максимальный лимит: +{fallback_keyframes} fallback keyframes, "
        f"+0 fallback video jobs, до {maximum_credits} кредитов.\n"
        "Политика: не более одного автоматического fallback на identity-сцену; "
        "только gen4_image с frontal_identity, без video-model fallback.\n"
        f"Модели: {model_mix}.\n"
        "Аватар используется только для внешности; фон, поза и постановка создаются заново."
    )


def delivery_files(manifest: Path) -> list[Path]:
    payload = (
        read_corrected_scene_runtime(manifest)
        if manifest.name == "revision-runtime.json"
        else story_production.read_manifest(manifest)
    )
    if payload.get("pack_status") != "completed":
        return []
    root = manifest.parent.resolve()
    reel_paths = [
        str(job.get("path", ""))
        for job in payload.get("reel_jobs", [])
        if job.get("state") == "completed"
    ]
    relative_paths = (
        reel_paths
        if isinstance(payload.get("scout_runway_bridge"), Mapping)
        else [
            str(job.get("story_path", ""))
            for job in payload.get("scene_jobs", [])
            if job.get("state") == "completed"
        ] + reel_paths
    )
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
    paths = list_manifests(root) + sorted(
        Path(root).expanduser().resolve().glob("*/revision-runtime.json"),
        key=lambda item: item.stat().st_mtime,
    )
    for path in reversed(paths):
        try:
            payload = (
                read_corrected_scene_runtime(path)
                if path.name == "revision-runtime.json"
                else story_production.read_manifest(path)
            )
        except (OSError, ValueError):
            continue
        if (
            payload.get("pack_status") == "completed"
            and str(payload.get("delivery", {}).get("status")) in {"ready", "delivering"}
        ):
            result.append(path)
    return result


def mark_delivery_file_sent(manifest: Path, filename: str) -> None:
    if manifest.name == "revision-runtime.json":
        payload = read_corrected_scene_runtime(manifest)
        delivery = payload.setdefault("delivery", {})
        sent = delivery.setdefault("sent_files", [])
        if filename not in sent:
            sent.append(filename)
        delivery["status"] = "delivering"
        write_corrected_scene_runtime(manifest, payload)
        return

    def mutate(payload: dict[str, Any]) -> None:
        delivery = payload.setdefault("delivery", {})
        sent = delivery.setdefault("sent_files", [])
        if filename not in sent:
            sent.append(filename)
        delivery["status"] = "delivering"

    story_production.update_manifest(manifest, mutate)


def mark_delivery_complete(manifest: Path) -> None:
    if manifest.name == "revision-runtime.json":
        payload = read_corrected_scene_runtime(manifest)
        payload.setdefault("delivery", {}).update({
            "status": "completed", "completed_at": _now()
        })
        write_corrected_scene_runtime(manifest, payload)
        return

    def mutate(payload: dict[str, Any]) -> None:
        delivery = payload.setdefault("delivery", {})
        delivery.update({"status": "completed", "completed_at": _now()})

    story_production.update_manifest(manifest, mutate)
