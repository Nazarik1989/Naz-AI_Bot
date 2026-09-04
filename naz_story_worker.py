"""Resumable CLI worker for private Naz Story-first media packs.

The worker has no publication code.  Production rendering is disabled unless
both the feature flag and an explicit provider are configured.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import story_production
import story_pack_control
from runway_reference_health import (
    FAILURE_CATEGORIES,
    HEALTH_POLICY_VERSION,
    PROMPT_POLICY_VERSION,
    REFERENCE_PROFILE_VERSION,
    ReferenceHealthError,
    ReferenceHealthRegistry,
    ReferenceRoute,
    reference_set_digest,
    sha256_file,
)
from story_audio_evidence import eligible_segment_starts
from story_media_composer import MediaComposer, MediaError, checksum, load_music_library
from story_pack_lock import AdvisoryFileLock, StoryPackLock, StoryPackLockError, ensure_private_group_access
from story_video_provider import (
    KeyframeRequest,
    ProviderError,
    SceneRequest,
    VideoProvider,
    append_prompt_guidance,
    provider_from_environment,
)


LOGGER = logging.getLogger("naz.story.worker")
PROJECT_ROOT = Path(__file__).resolve().parent
SAFE_FAILURE_CODES = {
    "approved_reference_invalid", "approved_reference_too_large", "cyrillic_font_missing",
    "daily_job_limit_reached", "daily_seconds_limit_reached", "licensed_music_invalid",
    "licensed_music_metadata_invalid", "licensed_music_segment_invalid",
    "media_codec_invalid", "media_duration_invalid", "music_rotation_state_invalid",
    "media_frame_rate_invalid", "media_missing_or_empty", "media_motion_not_detected",
    "media_pixel_format_invalid", "media_probe_invalid", "media_resolution_invalid",
    "media_tool_failed", "media_tool_unavailable_or_timed_out", "overlay_text_unsafe",
    "provider_download_failed", "provider_download_not_mp4", "provider_job_id_missing",
    "provider_download_not_image", "keyframe_identity_tag_missing",
    "keyframe_reference_count_invalid",
    "keyframe_artifact_invalid", "keyframe_prompt_invalid", "keyframe_prompt_too_long",
    "keyframe_retry_contract_invalid",
    "daily_keyframe_limit_reached",
    "provider_cancel_uncertain",
    "provider_endpoint_not_found", "provider_input_invalid", "provider_output_url_missing",
    "provider_payment_required", "provider_permission_denied", "provider_request_rejected",
    "provider_response_invalid",
    "provider_prompt_unsafe",
    "provider_status_unknown", "provider_temporarily_unavailable", "provider_terminal_failure",
    "provider_timeout", "provider_transport_error", "reel_crop_missing",
    "reel_cuts_not_on_beat_grid", "reel_fragment_duration_invalid", "reel_fragment_out_of_source",
    "unsafe_media_path", "video_api_key_invalid", "video_api_key_missing",
    "video_provider_disabled", "video_provider_unknown", "video_prompt_invalid",
    "video_prompt_too_long",
    "provider_reference_required", "video_auto_fallback_forbidden",
    "video_duration_unsupported", "video_model_priority_invalid",
    "video_model_route_mismatch", "video_model_unsupported", "video_prompt_image_required",
    "story_manifest_contract_stale", "provider_submit_outcome_ambiguous",
    *FAILURE_CATEGORIES,
    "reference_health_state_invalid", "reference_health_write_failed",
}
CANONICAL_PRIMARY_MODEL = "gen4_turbo"
CANONICAL_SECONDARY_MODEL = "gen4.5"
CANONICAL_MODEL_PRIORITY = (CANONICAL_PRIMARY_MODEL, CANONICAL_SECONDARY_MODEL)
REFERENCE_KEYFRAME_RETRY_DAILY_LIMIT = 4
FRONTAL_REFERENCE_RETRY_DAILY_LIMIT = 4
CONCISE_IDENTITY_RETRY_DAILY_LIMIT = 4
SECONDARY_ESCALATION_CODES = frozenset({
    "video_prompt_image_required",
    "provider_terminal_failure",
})
DEFINITIVE_SUBMIT_FAILURE_CODES = frozenset({
    "approved_reference_invalid",
    "approved_reference_too_large",
    "provider_endpoint_not_found",
    "provider_input_invalid",
    "provider_payment_required",
    "provider_permission_denied",
    "keyframe_identity_tag_missing",
    "keyframe_reference_count_invalid",
    "keyframe_prompt_invalid",
    "keyframe_prompt_too_long",
    "provider_prompt_unsafe",
    "provider_request_rejected",
    "video_duration_unsupported",
    "video_model_unsupported",
    "video_api_key_invalid",
    "video_prompt_invalid",
    "video_prompt_image_required",
    "video_prompt_too_long",
})


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _int(values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(values.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name.lower()}_invalid") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name.lower()}_out_of_range")
    return value


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    pack_root: Path
    render_enabled: bool
    provider_name: str
    model: str
    reference_path: Path | None
    music_library_path: Path | None
    ffmpeg: str
    ffprobe: str
    font_path: Path | None
    max_scene_jobs: int
    concurrency: int
    poll_timeout_seconds: int
    max_retries: int
    daily_job_limit: int
    daily_seconds_limit: int
    media_timeout_seconds: int
    primary_model: str = CANONICAL_PRIMARY_MODEL
    secondary_model: str = CANONICAL_SECONDARY_MODEL
    model_priority: tuple[str, ...] = CANONICAL_MODEL_PRIORITY
    auto_fallback: bool = False
    music_rotation_state_path: Path | None = None
    daily_keyframe_limit: int = 7
    reference_health_root: Path | None = None


@dataclass(frozen=True, slots=True)
class ReferenceSelection:
    path: Path
    role: str
    body_guidance: str = ""


def load_config(env: Mapping[str, str] | None = None) -> WorkerConfig:
    values = os.environ if env is None else env
    reference = (
        values.get("NAZ_VIDEO_REFERENCE_DIR", "").strip()
        or values.get("NAZ_VIDEO_REFERENCE_PATH", "").strip()
    )
    music = values.get("NAZ_STORY_MUSIC_LIBRARY", "").strip()
    music_rotation = values.get(
        "NAZ_STORY_MUSIC_ROTATION_STATE",
        "/var/lib/naz-ai-bot/story_music_rotation.json",
    ).strip()
    font = values.get("NAZ_STORY_FONT_PATH", "").strip()
    primary_model = (
        values.get("NAZ_VIDEO_PRIMARY_MODEL", "").strip()
        or values.get("NAZ_VIDEO_MODEL", "").strip()
        or CANONICAL_PRIMARY_MODEL
    )
    secondary_model = (
        values.get("NAZ_VIDEO_SECONDARY_MODEL", CANONICAL_SECONDARY_MODEL).strip()
        or CANONICAL_SECONDARY_MODEL
    )
    priority = tuple(
        item.strip() for item in values.get(
            "NAZ_VIDEO_MODEL_PRIORITY", f"{primary_model},{secondary_model}"
        ).split(",") if item.strip()
    )
    return WorkerConfig(
        pack_root=Path(values.get("NAZ_STORY_PACK_ROOT", "/var/lib/naz-ai-bot/story-packs")).expanduser().resolve(),
        render_enabled=_bool(values.get("NAZ_STORY_RENDER_ENABLED"), False),
        provider_name=values.get("NAZ_VIDEO_PROVIDER", "disabled").strip().casefold(),
        model=primary_model,
        reference_path=Path(reference).expanduser().resolve() if reference else None,
        music_library_path=Path(music).expanduser().resolve() if music else None,
        ffmpeg=values.get("NAZ_FFMPEG_BIN", "ffmpeg").strip(),
        ffprobe=values.get("NAZ_FFPROBE_BIN", "ffprobe").strip(),
        font_path=Path(font).expanduser().resolve() if font else None,
        max_scene_jobs=_int(values, "NAZ_STORY_MAX_SCENE_JOBS", 7, 1, 7),
        concurrency=_int(values, "NAZ_STORY_CONCURRENCY", 1, 1, 1),
        poll_timeout_seconds=_int(values, "NAZ_VIDEO_POLL_TIMEOUT_SECONDS", 900, 30, 3600),
        max_retries=_int(values, "NAZ_VIDEO_MAX_RETRIES", 2, 0, 3),
        daily_job_limit=_int(values, "NAZ_VIDEO_DAILY_JOB_LIMIT", 7, 1, 49),
        daily_seconds_limit=_int(values, "NAZ_VIDEO_DAILY_SECONDS_LIMIT", 56, 4, 392),
        media_timeout_seconds=_int(values, "NAZ_MEDIA_TIMEOUT_SECONDS", 180, 10, 900),
        primary_model=primary_model, secondary_model=secondary_model,
        model_priority=priority,
        auto_fallback=_bool(values.get("NAZ_VIDEO_AUTO_FALLBACK"), False),
        music_rotation_state_path=(
            Path(music_rotation).expanduser().resolve() if music_rotation else None
        ),
        daily_keyframe_limit=_int(values, "NAZ_KEYFRAME_DAILY_JOB_LIMIT", 7, 1, 7),
        reference_health_root=Path(
            values.get(
                "NAZ_RUNWAY_REFERENCE_HEALTH_ROOT",
                "/var/lib/naz-ai-bot/runway-reference-health",
            )
        ).expanduser().resolve(),
    )


def check_config(config: WorkerConfig, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Static checks only.  It never constructs a provider or calls an API."""
    values = os.environ if env is None else env
    issues: list[str] = []
    if config.render_enabled:
        if config.provider_name == "disabled":
            issues.append("video_provider_disabled")
        elif config.provider_name != "runway":
            issues.append("video_provider_unknown")
        if not values.get("NAZ_VIDEO_API_KEY", "").strip():
            issues.append("video_api_key_missing")
        if config.auto_fallback:
            issues.append("video_auto_fallback_forbidden")
        if (
            config.primary_model != CANONICAL_PRIMARY_MODEL
            or config.secondary_model != CANONICAL_SECONDARY_MODEL
            or config.model_priority != CANONICAL_MODEL_PRIORITY
        ):
            issues.append("video_model_priority_invalid")
    if shutil.which(config.ffmpeg) is None:
        issues.append("ffmpeg_unavailable")
    if shutil.which(config.ffprobe) is None:
        issues.append("ffprobe_unavailable")
    if config.font_path is None or not config.font_path.is_file():
        issues.append("cyrillic_font_missing")
    references = _reference_catalog(config.reference_path)
    reference_status = (
        "available"
        if references.get("frontal_identity") and references.get("three_quarter_identity")
        else "partial"
        if references
        else "unavailable"
    )
    music_status = (
        "available"
        if config.music_library_path
        and load_music_library(config.music_library_path, pack_root=config.pack_root)
        else "unavailable"
    )
    if config.reference_path and (config.reference_path == PROJECT_ROOT or PROJECT_ROOT in config.reference_path.parents):
        issues.append("approved_reference_inside_repository")
    if config.reference_health_root and (
        config.reference_health_root == PROJECT_ROOT
        or PROJECT_ROOT in config.reference_health_root.parents
    ):
        issues.append("reference_health_inside_repository")
    return {
        "ok": not issues, "render_enabled": config.render_enabled,
        "provider": config.provider_name, "model": config.model,
        "primary_model": config.primary_model, "secondary_model": config.secondary_model,
        "automatic_fallback": config.auto_fallback,
        "reference": reference_status, "music_library": music_status,
        "issues": sorted(set(issues)), "live_api_called": False,
    }


class PackLock(StoryPackLock):
    """Backward-compatible name for the shared crash-safe pack lock."""


def _failure_code(exc: BaseException) -> str:
    code = str(getattr(exc, "code", "") or str(exc) or "worker_failure")
    return code if code in SAFE_FAILURE_CODES else "worker_failure"


def _utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _worker_payloads(root: Path) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for manifest in root.glob("*/story_manifest.json"):
        try:
            payloads.append(story_production.read_manifest(manifest))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    for runtime in root.glob("*/revision-runtime.json"):
        try:
            payloads.append(story_pack_control.read_corrected_scene_runtime(runtime))
        except (OSError, ValueError, json.JSONDecodeError, story_production.StoryPlanError):
            continue
    return payloads


def _budget_usage(root: Path) -> tuple[int, int]:
    now = datetime.now(timezone.utc).date()
    jobs = seconds = 0
    for payload in _worker_payloads(root):
        if payload.get("schema") not in {
            story_production.STORY_SCHEMA,
            story_pack_control.CORRECTED_SCENE_RUNTIME_SCHEMA,
        }:
            continue
        for job in payload.get("scene_jobs", []):
            history = job.get("submit_intent_history")
            intents = list(history) if isinstance(history, list) else []
            if isinstance(job.get("submit_intent"), dict):
                intents.append(job["submit_intent"])
            reserved = 0
            for intent in intents:
                if not isinstance(intent, dict) or intent.get("state") not in {
                    "accepted", "ambiguous", "submitting",
                }:
                    continue
                created = _utc(intent.get("created_at"))
                if created and created.date() == now:
                    reserved += 1
            if not intents and job.get("external_job_id"):
                submitted = _utc(job.get("submitted_at"))
                reserved = int(bool(submitted and submitted.date() == now))
            jobs += reserved
            seconds += int(job.get("planned_duration_seconds", 0)) * reserved
    return jobs, seconds


def _keyframe_budget_usage(root: Path) -> int:
    now = datetime.now(timezone.utc).date()
    jobs = 0
    for payload in _worker_payloads(root):
        for job in payload.get("scene_jobs", []):
            history = job.get("keyframe_submit_intent_history")
            intents = list(history) if isinstance(history, list) else []
            if isinstance(job.get("keyframe_submit_intent"), dict):
                intents.append(job["keyframe_submit_intent"])
            for intent in intents:
                if not isinstance(intent, dict) or intent.get("state") not in {
                    "accepted", "ambiguous", "submitting",
                }:
                    continue
                created = _utc(intent.get("created_at"))
                jobs += int(bool(created and created.date() == now))
    return jobs


def _keyframe_retry_budget_usage(root: Path, approval_scope: str) -> int:
    now = datetime.now(timezone.utc).date()
    jobs = 0
    for payload in _worker_payloads(root):
        for job in payload.get("scene_jobs", []):
            history = job.get("keyframe_submit_intent_history")
            intents = list(history) if isinstance(history, list) else []
            if isinstance(job.get("keyframe_submit_intent"), dict):
                intents.append(job["keyframe_submit_intent"])
            for intent in intents:
                if not isinstance(intent, dict):
                    continue
                if intent.get("approval_scope") != approval_scope:
                    continue
                if intent.get("state") not in {"accepted", "ambiguous", "submitting"}:
                    continue
                created = _utc(intent.get("created_at"))
                jobs += int(bool(created and created.date() == now))
    return jobs


def _write(manifest: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if payload.get("schema") == story_pack_control.CORRECTED_SCENE_RUNTIME_SCHEMA:
        story_pack_control.write_corrected_scene_runtime(manifest, payload)
    else:
        story_production.atomic_json(manifest, payload)


def _scene_plan(payload: Mapping[str, Any], scene_id: str) -> Mapping[str, Any]:
    return next(row for row in payload.get("scenes", []) if row.get("scene_id") == scene_id)


def _bounded_direction(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")


def _concise_identity_keyframe_prompt(scene: Mapping[str, Any]) -> str:
    """Build a short scene prompt for a separately approved BAD_OUTPUT retry."""
    setting = _bounded_direction(scene.get("setting"), 80)
    action = _bounded_direction(scene.get("concrete_action"), 100)
    end_state = _bounded_direction(scene.get("end_state"), 80)
    shot = _bounded_direction(scene.get("shot_size"), 20)
    if not setting or not action or not end_state or not shot:
        raise ProviderError("keyframe_retry_contract_invalid")
    return story_production.validate_provider_prompt(
        f"@Naz is the only person and keeps the reference face and build. "
        f"Scene: {setting}. Action: {action}. Final state: {end_state}. "
        f"{shot} vertical frame. Replace reference clothing, background, pose and light "
        "with a matte-black Naz AI Lab wardrobe and physical lab. Deep black, electric "
        "blue, cold silver; photoreal anatomy. No text, logos, HUD or extra people."
    )


def _set_pack_status(payload: dict[str, Any]) -> None:
    scenes = [str(row.get("state")) for row in payload.get("scene_jobs", [])]
    keyframes = [str(row.get("keyframe_state")) for row in payload.get("scene_jobs", [])]
    reels = [str(row.get("state")) for row in payload.get("reel_jobs", [])]
    if "awaiting_secondary_approval" in scenes:
        payload["pack_status"] = "awaiting_secondary_approval"
    elif scenes and all(state == "completed" for state in scenes):
        if reels and all(state == "completed" for state in reels):
            payload["pack_status"] = "completed"
            delivery = payload.setdefault(
                "delivery", {"status": "not_ready", "sent_files": [], "completed_at": None}
            )
            if delivery.get("status") == "not_ready":
                delivery["status"] = "ready"
        elif "blocked_music" in reels:
            payload["pack_status"] = "blocked_music"
        elif "blocked_voice" in reels:
            payload["pack_status"] = "blocked_voice"
        else:
            payload["pack_status"] = "composing_reels"
    elif any(
        state in {"terminal_failed", "blocked_reference", "submit_ambiguous"}
        for state in scenes
    ):
        payload["pack_status"] = "partially_blocked"
    elif any(state in {"submitting", "submitted", "in_progress", "ready"} for state in keyframes):
        payload["pack_status"] = "in_progress"
    elif any(
        state in {"submitting", "submitted", "in_progress", "downloaded", "composed"}
        for state in scenes
    ):
        payload["pack_status"] = "in_progress"
    else:
        payload["pack_status"] = "queued"


def _approved_reference_path(path: Path | None) -> Path | None:
    """Compatibility helper returning the canonical frontal reference."""
    catalog = _reference_catalog(path)
    selected = catalog.get("frontal_identity") or catalog.get("three_quarter_identity")
    return selected.path if selected else None


def _safe_reference_file(root: Path, filename: str) -> Path | None:
    raw = str(filename).strip()
    if not raw or Path(raw).name != raw:
        return None
    candidate = (root / raw).resolve()
    if candidate.parent != root or candidate == PROJECT_ROOT or PROJECT_ROOT in candidate.parents:
        return None
    if not candidate.is_file() or candidate.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
        return None
    return candidate


def _reference_catalog(path: Path | None) -> dict[str, ReferenceSelection]:
    if path is None:
        return {}
    candidate = path.expanduser().resolve()
    if candidate == PROJECT_ROOT or PROJECT_ROOT in candidate.parents:
        return {}
    if candidate.is_file() and candidate.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}:
        selection = ReferenceSelection(candidate, "frontal_identity")
        return {"frontal_identity": selection, "three_quarter_identity": selection}
    if not candidate.is_dir():
        return {}
    profile_path = candidate / "naz-reference-profile.json"
    rows: Mapping[str, Any] = {}
    body_guidance = ""
    if profile_path.is_file():
        try:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            if not isinstance(profile, dict):
                return {}
            schema = profile.get("schema")
            if (
                schema not in {"naz-reference-profile.v1", "naz-reference-profile.v2"}
                or profile.get("persona") != "naz"
            ):
                return {}
            rows = profile.get("reference_files", {})
            body = profile.get("body_profile", {})
            if not isinstance(rows, dict) or not isinstance(body, dict):
                return {}
            required_names = (
                ("primary", "secondary")
                if schema == "naz-reference-profile.v1"
                else ("frontal_identity", "three_quarter_identity")
            )
            if any(not isinstance(rows.get(name), str) for name in required_names):
                return {}
            if (
                schema == "naz-reference-profile.v2"
                and rows.get("full_body_identity") is not None
                and not isinstance(rows.get("full_body_identity"), str)
            ):
                return {}
            if body and (
                type(body.get("height_cm")) is not int
                or type(body.get("weight_kg")) is not int
                or not isinstance(body.get("build"), str)
                or not isinstance(body.get("visual_guidance"), str)
            ):
                return {}
            height = int(body.get("height_cm", 0))
            weight = int(body.get("weight_kg", 0))
            build = " ".join(body.get("build", "").split())[:80]
            guidance = " ".join(body.get("visual_guidance", "").split())[:300]
            if body and not (
                140 <= height <= 220
                and 45 <= weight <= 160
                and build
                and guidance
            ):
                return {}
            if body:
                body_guidance = (
                    f"Adult man, {height} cm, {weight} kg, {build}. {guidance}"
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {}
    else:
        schema = "naz-reference-profile.v1"
        rows = {"primary": "naz-primary.jpg", "secondary": "naz-secondary.jpg"}
    if schema == "naz-reference-profile.v2":
        primary_name = str(rows.get("frontal_identity", ""))
        secondary_name = str(rows.get("three_quarter_identity", ""))
        full_body_name = str(rows.get("full_body_identity", ""))
    else:
        primary_name = str(rows.get("primary", ""))
        secondary_name = str(rows.get("secondary", ""))
        full_body_name = ""
    primary = _safe_reference_file(candidate, primary_name)
    secondary = _safe_reference_file(candidate, secondary_name)
    full_body = _safe_reference_file(candidate, full_body_name)
    result: dict[str, ReferenceSelection] = {}
    if primary:
        result["frontal_identity"] = ReferenceSelection(primary, "frontal_identity")
    if secondary:
        result["three_quarter_identity"] = ReferenceSelection(
            secondary, "three_quarter_identity", body_guidance
        )
    if full_body:
        result["full_body_identity"] = ReferenceSelection(
            full_body, "full_body_identity", body_guidance
        )
    return result


def _identity_reference_set(
    catalog: Mapping[str, ReferenceSelection],
    preferred_role: str = "frontal_identity",
    *,
    registry: ReferenceHealthRegistry | None = None,
) -> tuple[ReferenceSelection, ...]:
    """Return the canonical frontal-first identity plate.

    ``preferred_role`` is retained only for callers reading legacy manifests;
    camera direction never changes the canonical anchor.  Quarantined
    auxiliaries are removed before a provider intent is persisted.
    """
    del preferred_role
    roles = ("frontal_identity", "three_quarter_identity", "full_body_identity")
    selected: list[ReferenceSelection] = []
    seen: set[Path] = set()
    for role in roles:
        reference = catalog.get(role)
        if reference is None or reference.path in seen:
            continue
        if role != "frontal_identity" and registry is not None:
            reference_digest = sha256_file(reference.path)
            proposed = tuple(selected) + (reference,)
            proposed_rows = tuple(
                (item.role, sha256_file(item.path)) for item in proposed
            )
            route = ReferenceRoute(
                provider_name="runway",
                keyframe_model="gen4_image",
                reference_profile_version=REFERENCE_PROFILE_VERSION,
                prompt_policy_version=PROMPT_POLICY_VERSION,
                reference_role=role,
                reference_digest=reference_digest,
                reference_set_digest=reference_set_digest(proposed_rows),
            )
            if registry.role_is_quarantined(route):
                continue
        selected.append(reference)
        seen.add(reference.path)
        if len(selected) == 3:
            break
    return tuple(selected)


def _legacy_identity_reference_set(
    catalog: Mapping[str, ReferenceSelection], preferred_role: str
) -> tuple[ReferenceSelection, ...]:
    selected: list[ReferenceSelection] = []
    seen: set[Path] = set()
    for role in (
        preferred_role,
        "frontal_identity",
        "three_quarter_identity",
        "full_body_identity",
    ):
        reference = catalog.get(role)
        if reference is None or reference.path in seen:
            continue
        selected.append(reference)
        seen.add(reference.path)
        if len(selected) == 3:
            break
    return tuple(selected)


def _reference_route(
    references: tuple[ReferenceSelection, ...], *, observed_role: str
) -> ReferenceRoute:
    rows = tuple((item.role, sha256_file(item.path)) for item in references)
    digest_by_role = dict(rows)
    observed_digest = digest_by_role.get(observed_role)
    if observed_digest is None:
        raise ReferenceHealthError("reference_health_route_invalid")
    return ReferenceRoute(
        provider_name="runway",
        keyframe_model="gen4_image",
        reference_profile_version=REFERENCE_PROFILE_VERSION,
        prompt_policy_version=PROMPT_POLICY_VERSION,
        reference_role=observed_role,
        reference_digest=observed_digest,
        reference_set_digest=reference_set_digest(rows),
    )


def _registry(config: WorkerConfig) -> ReferenceHealthRegistry | None:
    if config.reference_health_root is None:
        return None
    if (
        config.reference_health_root == PROJECT_ROOT
        or PROJECT_ROOT in config.reference_health_root.parents
    ):
        raise ReferenceHealthError("reference_health_root_invalid")
    return ReferenceHealthRegistry(config.reference_health_root)


def _migrated_legacy_contract(
    payload: Mapping[str, Any], *, health_root: Path | None
) -> bool:
    if (
        payload.get("schema") != story_production.PREVIOUS_STORY_SCHEMA
        or payload.get("director_version") != story_production.DIRECTOR_VERSION
        or not story_production.manifest_has_previous_production_contract(payload)
        or health_root is None
    ):
        return False
    try:
        binding = ReferenceHealthRegistry(health_root).migration(
            str(payload.get("plan_id", ""))
        )
    except ReferenceHealthError:
        return False
    return bool(
        isinstance(binding, dict)
        and binding.get("policy_version") == HEALTH_POLICY_VERSION
        and binding.get("immutable_plan_fingerprint")
        == payload.get("immutable_plan_fingerprint")
    )


def _paid_manifest_contract(payload: Mapping[str, Any], config: WorkerConfig) -> bool:
    return story_production.manifest_has_current_production_contract(
        payload
    ) or _migrated_legacy_contract(
        payload, health_root=config.reference_health_root
    )


def _identity_reference_guidance(references: tuple[ReferenceSelection, ...]) -> str:
    tags = ("@Naz", "@NazView2", "@NazView3")[: len(references)]
    if len(tags) <= 1:
        return ""
    return f"{' '.join(tags)} are the same man; preserve face and build, not clothes or background."


def _reference_for(
    scene: Mapping[str, Any], config: WorkerConfig, provider: VideoProvider,
) -> ReferenceSelection | None:
    if not bool(scene.get("requires_naz_reference")):
        return None
    role = str(scene.get("reference_role") or "frontal_identity")
    reference = _reference_catalog(config.reference_path).get(role)
    if not provider.supports_reference or reference is None:
        raise ProviderError("approved_reference_invalid")
    return reference


def _model_route(job: dict[str, Any], config: WorkerConfig) -> dict[str, Any]:
    route = job.setdefault("model_route", {})
    if not isinstance(route, dict):
        raise RuntimeError("video_model_route_mismatch")
    primary = str(route.get("primary_model") or config.primary_model)
    secondary = str(route.get("secondary_model") or config.secondary_model)
    if primary != CANONICAL_PRIMARY_MODEL or secondary != CANONICAL_SECONDARY_MODEL:
        raise RuntimeError("video_model_route_mismatch")
    if str(route.get("tier") or "primary") not in {"primary", "secondary"}:
        raise RuntimeError("video_model_route_mismatch")
    route.setdefault("tier", "primary")
    route["primary_model"] = primary
    route["secondary_model"] = secondary
    route.setdefault("primary_failure_code", None)
    route.setdefault("secondary_requested_at", None)
    route.setdefault("secondary_approved_at", None)
    strategy = route.get("scene_strategy")
    if strategy is not None:
        expected_model = (
            CANONICAL_SECONDARY_MODEL
            if bool(job.get("requires_naz_reference"))
            else CANONICAL_PRIMARY_MODEL
        )
        if (
            strategy != story_production.HYBRID_MODEL_ROUTE
            or route.get("selected_model") != expected_model
            or route.get("tier") != "primary"
            or route.get("primary_failure_code") is not None
            or route.get("secondary_requested_at") is not None
            or route.get("secondary_approved_at") is not None
        ):
            raise RuntimeError("video_model_route_mismatch")
    return route


def _selected_model(job: dict[str, Any], config: WorkerConfig) -> str | None:
    if str(job.get("state")) == "awaiting_secondary_approval":
        return None
    route = _model_route(job, config)
    if route.get("scene_strategy") == story_production.HYBRID_MODEL_ROUTE:
        return str(route["selected_model"])
    if str(route.get("tier")) == "secondary":
        if (
            not route.get("secondary_requested_at")
            or not route.get("secondary_approved_at")
            or route.get("primary_failure_code") not in SECONDARY_ESCALATION_CODES
        ):
            raise RuntimeError("video_model_route_mismatch")
        return str(route["secondary_model"])
    return str(route["primary_model"])


def _request_secondary(
    job: dict[str, Any], payload: dict[str, Any], code: str,
    config: WorkerConfig, provider: VideoProvider,
) -> bool:
    """Persist a manual escalation request; never construct/call secondary."""
    route = _model_route(job, config)
    if route.get("scene_strategy") == story_production.HYBRID_MODEL_ROUTE:
        # A new hybrid plan already has an explicitly approved model per scene.
        # Never reinterpret an object-scene failure as permission to change it.
        return False
    if (
        code not in SECONDARY_ESCALATION_CODES
        or config.auto_fallback
        or provider.name != "runway"
        or provider.model != str(route.get("primary_model"))
        or str(route.get("secondary_model")) == provider.model
        or route.get("secondary_approved_at")
    ):
        return False
    external_id = str(job.get("external_job_id") or "")
    if external_id:
        history = job.setdefault("provider_job_history", [])
        if external_id not in history:
            history.append(external_id)
    now = datetime.now(timezone.utc).isoformat()
    route.update({
        "tier": "primary", "primary_failure_code": code,
        "secondary_requested_at": now, "secondary_approved_at": None,
    })
    job.update({
        "state": "awaiting_secondary_approval", "external_job_id": None,
        "provider_status": "primary_failed", "failure_code": code,
    })
    payload["pack_status"] = "awaiting_secondary_approval"
    return True


def _request_secondary_for_text_only_scenes(
    payload: dict[str, Any], config: WorkerConfig, provider: VideoProvider,
) -> None:
    """Batch the known Turbo incompatibilities into one admin confirmation."""
    scenes = {
        str(scene.get("scene_id")): scene for scene in payload.get("scenes", [])
    }
    for candidate in payload.get("scene_jobs", []):
        scene = scenes.get(str(candidate.get("scene_id")), {})
        if (
            not bool(scene.get("requires_naz_reference"))
            and str(candidate.get("state")) in {"planned", "queued", "retryable_failed"}
            and not candidate.get("external_job_id")
        ):
            _request_secondary(
                candidate, payload, "video_prompt_image_required", config, provider
            )


def _mark_failure(job: dict[str, Any], exc: BaseException, config: WorkerConfig) -> None:
    code = _failure_code(exc)
    retryable = bool(getattr(exc, "retryable", isinstance(exc, MediaError)))
    job["failure_code"] = code
    if job.get("corrected_revision_id"):
        # The cost approval authorizes one new video task for this new input,
        # never another paid submit hidden behind the worker's legacy retry.
        job["state"] = "terminal_failed"
        return
    if retryable and int(job.get("attempts", 0)) <= config.max_retries:
        job["state"] = "retryable_failed"
        job["external_job_id"] = None
    else:
        job["state"] = "terminal_failed"


def _mark_submit_ambiguous(
    job: dict[str, Any], payload: dict[str, Any], manifest: Path,
) -> None:
    intent = job.get("submit_intent")
    if isinstance(intent, dict):
        intent.update({
            "state": "ambiguous",
            "failure_code": "provider_submit_outcome_ambiguous",
        })
    job.update({
        "state": "submit_ambiguous",
        "provider_status": "submit_outcome_ambiguous",
        "failure_code": "provider_submit_outcome_ambiguous",
        "external_job_id": None,
    })
    _set_pack_status(payload)
    _write(manifest, payload)


def _keyframe_destination(manifest: Path, job: Mapping[str, Any]) -> Path:
    root = manifest.parent.resolve()
    destination = (root / str(job.get("keyframe_path", ""))).resolve()
    if root not in destination.parents or destination.suffix.casefold() != ".jpg":
        raise ProviderError("unsafe_media_path")
    return destination


def _mark_keyframe_ambiguous(
    job: dict[str, Any], payload: dict[str, Any], manifest: Path,
) -> None:
    intent = job.get("keyframe_submit_intent")
    if isinstance(intent, dict):
        intent.update({"state": "ambiguous", "failure_code": "provider_submit_outcome_ambiguous"})
    job.update({
        "keyframe_state": "submit_ambiguous",
        "keyframe_provider_status": "submit_outcome_ambiguous",
        "keyframe_failure_code": "provider_submit_outcome_ambiguous",
        "state": "submit_ambiguous", "failure_code": "provider_submit_outcome_ambiguous",
    })
    _set_pack_status(payload)
    _write(manifest, payload)


def _automatic_fallback_contract(payload: Mapping[str, Any]) -> bool:
    policy = payload.get("reference_recovery_policy")
    return (
        payload.get("schema") == story_production.STORY_SCHEMA
        and isinstance(policy, Mapping)
        and policy.get("version") == HEALTH_POLICY_VERSION
        and policy.get("maximum_keyframe_fallbacks_per_identity_scene") == 1
        and policy.get("maximum_total_automatic_keyframe_attempts") == 2
        and policy.get("fallback_keyframe_model") == "gen4_image"
        and policy.get("fallback_reference_roles") == ["frontal_identity"]
        and policy.get("delayed_retry_failure_categories") == [
            "provider_preprocessing_internal",
            "provider_dependency_unavailable",
        ]
        and policy.get("corrected_input_failure_categories") == ["bad_output"]
        and policy.get("same_input_retry_allowed") is False
        and policy.get("automatic_video_fallback") is False
        and str(payload.get("approval", {}).get("status")) == "approved"
    )


def _queue_automatic_fallback(
    job: dict[str, Any], *, reason: str, retry_phase: str
) -> bool:
    if (
        job.get("state") != "terminal_failed"
        or job.get("keyframe_state") != "terminal_failed"
        or int(job.get("keyframe_attempts", 0)) != 1
        or int(job.get("keyframe_automatic_fallbacks", 0)) != 0
        or int(job.get("attempts", 0)) != 0
        or job.get("keyframe_checksum")
        or job.get("clean_checksum")
        or job.get("story_checksum")
    ):
        return False
    external_id = str(job.get("keyframe_external_job_id") or "")
    if external_id:
        history = job.setdefault("keyframe_provider_job_history", [])
        if external_id not in history:
            history.append(external_id)
    previous = job.get("keyframe_submit_intent")
    if isinstance(previous, dict):
        history = job.setdefault("keyframe_submit_intent_history", [])
        if previous not in history:
            history.append(dict(previous))
    job.update({
        "state": "queued",
        "failure_code": None,
        "keyframe_state": "queued",
        "keyframe_external_job_id": None,
        "keyframe_submitted_at": None,
        "keyframe_provider_status": None,
        "keyframe_failure_code": None,
        "keyframe_submit_intent": None,
        "keyframe_retry_model": "gen4_image",
        "keyframe_retry_phase": retry_phase,
        "keyframe_retry_reference_role": (
            "frontal_identity" if bool(job.get("requires_naz_reference")) else "none"
        ),
        "keyframe_retry_reason_code": reason,
        "keyframe_retry_approved_at": str(job.get("keyframe_submitted_at") or "preapproved"),
        "keyframe_automatic_fallbacks": 1,
        "keyframe_fallback_state": "running",
        "keyframe_retry_not_before": (
            (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            if retry_phase == "automatic_delayed"
            else None
        ),
    })
    return True


def _route_from_intent(intent: object) -> ReferenceRoute | None:
    if not isinstance(intent, Mapping):
        return None
    try:
        return ReferenceRoute(
            provider_name=str(intent.get("reference_provider", "")),
            keyframe_model=str(intent.get("model", "")),
            reference_profile_version=str(intent.get("reference_profile_version", "")),
            prompt_policy_version=str(intent.get("prompt_policy_version", "")),
            reference_role=str(intent.get("observed_reference_role", "")),
            reference_digest=str(intent.get("observed_reference_digest", "")),
            reference_set_digest=str(intent.get("reference_set_digest", "")),
        )
    except ReferenceHealthError:
        return None


def _process_keyframe(
    *, payload: dict[str, Any], job: dict[str, Any], scene: Mapping[str, Any],
    manifest: Path, config: WorkerConfig, provider: VideoProvider,
) -> Path | None:
    state = str(job.get("keyframe_state", "planned"))
    destination = _keyframe_destination(manifest, job)
    if state == "ready":
        if not destination.is_file() or checksum(destination) != job.get("keyframe_checksum"):
            job.update({
                "keyframe_state": "terminal_failed",
                "keyframe_failure_code": "keyframe_artifact_invalid",
                "state": "terminal_failed", "failure_code": "keyframe_artifact_invalid",
            })
            _set_pack_status(payload)
            _write(manifest, payload)
            return None
        return destination
    if state in {"terminal_failed", "submit_ambiguous"}:
        return None
    if state == "submitting":
        _mark_keyframe_ambiguous(job, payload, manifest)
        return None
    if state in {"planned", "queued"}:
        reference_retry = bool(job.get("keyframe_retry_approved_at"))
        retry_phase = str(job.get("keyframe_retry_phase") or "legacy_model")
        attempts_before_submit = int(job.get("keyframe_attempts", 0))
        legacy_retry = reference_retry and retry_phase == "legacy_model"
        frontal_retry = reference_retry and retry_phase == "reference_quality"
        concise_retry = reference_retry and retry_phase == "concise_identity"
        automatic_frontal_retry = reference_retry and retry_phase == "automatic_frontal"
        automatic_delayed_retry = reference_retry and retry_phase == "automatic_delayed"
        automatic_retry = automatic_frontal_retry or automatic_delayed_retry
        valid_legacy_retry = legacy_retry and attempts_before_submit == 1
        valid_frontal_retry = (
            frontal_retry
            and attempts_before_submit in {1, 2}
            and job.get("keyframe_retry_reference_role") == "frontal_identity"
            and bool(job.get("keyframe_frontal_retry_approved_at"))
        )
        valid_concise_retry = (
            concise_retry
            and attempts_before_submit in {1, 3}
            and job.get("keyframe_retry_reference_role") == "frontal_identity"
            and bool(job.get("keyframe_concise_retry_approved_at"))
        )
        valid_automatic_retry = (
            automatic_retry
            and attempts_before_submit == 1
            and job.get("keyframe_retry_reference_role")
            == ("frontal_identity" if scene.get("requires_naz_reference") else "none")
            and int(job.get("keyframe_automatic_fallbacks", 0)) == 1
            and job.get("keyframe_fallback_state") == "running"
            and _automatic_fallback_contract(payload)
        )
        if reference_retry and not (
            job.get("keyframe_retry_model") == "gen4_image"
            and (
                valid_legacy_retry
                or valid_frontal_retry
                or valid_concise_retry
                or valid_automatic_retry
            )
        ):
            job.update({
                "state": "terminal_failed",
                "failure_code": "keyframe_retry_contract_invalid",
                "keyframe_state": "terminal_failed",
                "keyframe_failure_code": "keyframe_retry_contract_invalid",
            })
            _set_pack_status(payload)
            _write(manifest, payload)
            return None
        if automatic_retry:
            retry_scope = (
                "automatic_delayed_fallback"
                if automatic_delayed_retry
                else "automatic_frontal_fallback"
            )
            retry_limit = config.daily_keyframe_limit
        elif concise_retry:
            retry_scope = "concise_identity_retry"
            retry_limit = CONCISE_IDENTITY_RETRY_DAILY_LIMIT
        elif frontal_retry:
            retry_scope = "frontal_reference_retry"
            retry_limit = FRONTAL_REFERENCE_RETRY_DAILY_LIMIT
        else:
            retry_scope = "reference_model_retry"
            retry_limit = REFERENCE_KEYFRAME_RETRY_DAILY_LIMIT
        if reference_retry and (
            _keyframe_retry_budget_usage(config.pack_root, retry_scope) >= retry_limit
        ):
            job["keyframe_failure_code"] = "daily_keyframe_limit_reached"
            _write(manifest, payload)
            return None
        if automatic_delayed_retry:
            not_before = _utc(job.get("keyframe_retry_not_before"))
            if not_before is None or datetime.now(timezone.utc) < not_before:
                _write(manifest, payload)
                return None
        if not reference_retry and (
            _keyframe_budget_usage(config.pack_root) >= config.daily_keyframe_limit
        ):
            job["keyframe_failure_code"] = "daily_keyframe_limit_reached"
            _write(manifest, payload)
            return None
        original: ReferenceSelection | None = None
        identity_references: tuple[ReferenceSelection, ...] = ()
        if bool(scene.get("requires_naz_reference")):
            role = str(scene.get("reference_role") or "frontal_identity")
            selected_role = (
                str(job.get("keyframe_retry_reference_role"))
                if frontal_retry or concise_retry or automatic_frontal_retry else role
            )
            catalog = _reference_catalog(config.reference_path)
            original = catalog.get(selected_role)
            if not provider.supports_reference or original is None:
                job.update({
                    "keyframe_state": "terminal_failed",
                    "keyframe_failure_code": "approved_reference_invalid",
                    "state": "blocked_reference", "failure_code": "approved_reference_invalid",
                })
                _set_pack_status(payload)
                _write(manifest, payload)
                return None
            registry = _registry(config)
            identity_references = (
                (original,)
                if frontal_retry or concise_retry or automatic_frontal_retry
                else _legacy_identity_reference_set(catalog, selected_role)
                if legacy_retry
                else _identity_reference_set(
                    catalog,
                    selected_role,
                    registry=registry,
                )
            )
            if not legacy_retry:
                original = identity_references[0] if identity_references else None
            if not identity_references:
                job.update({
                    "keyframe_state": "terminal_failed",
                    "keyframe_failure_code": "approved_reference_invalid",
                    "state": "blocked_reference", "failure_code": "approved_reference_invalid",
                })
                _set_pack_status(payload)
                _write(manifest, payload)
                return None
        prompt_source = (
            _concise_identity_keyframe_prompt(scene)
            if concise_retry
            else story_production.validate_provider_prompt(
                str(scene.get("keyframe_prompt", ""))
            )
        )
        if automatic_delayed_retry:
            prompt_source = (
                f"{prompt_source} Render a fresh independent composition; "
                "do not reuse a previous latent arrangement."
            )
        identity_guidance = _identity_reference_guidance(identity_references)
        if identity_guidance:
            prompt_source = f"{identity_guidance} {prompt_source}"
        body_guidance = next(
            (
                reference.body_guidance
                for reference in identity_references
                if reference.body_guidance
            ),
            original.body_guidance if original else "",
        )
        if body_guidance:
            prompt_source = f"Body continuity: {body_guidance} {prompt_source}"
        prompt = append_prompt_guidance(
            prompt_source,
            "",
            too_long_code="keyframe_prompt_too_long",
        )
        attempt = int(job.get("keyframe_attempts", 0)) + 1
        created_at = datetime.now(timezone.utc).isoformat()
        previous = job.get("keyframe_submit_intent")
        if isinstance(previous, dict):
            job.setdefault("keyframe_submit_intent_history", []).append(dict(previous))
        intent_id = hashlib.sha256(
            f"{payload.get('plan_id')}|{job.get('scene_id')}|keyframe|{attempt}|{created_at}".encode()
        ).hexdigest()[:24]
        submit_intent = {
            "intent_id": intent_id, "model": "gen4_image",
            "created_at": created_at, "state": "submitting", "failure_code": None,
        }
        if reference_retry:
            submit_intent["approval_scope"] = retry_scope
        if identity_references:
            desired_view = str(scene.get("desired_view_role") or "frontal")
            observed_role = "frontal_identity"
            if (
                not (frontal_retry or concise_retry or automatic_frontal_retry)
                and desired_view == "three_quarter"
                and any(
                    item.role == "three_quarter_identity"
                    for item in identity_references
                )
            ):
                observed_role = "three_quarter_identity"
            route = _reference_route(identity_references, observed_role=observed_role)
            submit_intent.update({
                "reference_provider": route.provider_name,
                "reference_profile_version": route.reference_profile_version,
                "prompt_policy_version": route.prompt_policy_version,
                "identity_anchor_role": "frontal_identity",
                "desired_view_role": desired_view,
                "auxiliary_reference_roles": [
                    item.role for item in identity_references[1:]
                ],
                "quarantined_reference_roles": [
                    role
                    for role in ("three_quarter_identity", "full_body_identity")
                    if role in catalog
                    and role not in {item.role for item in identity_references}
                ],
                "observed_reference_role": route.reference_role,
                "observed_reference_digest": route.reference_digest,
                "reference_set_digest": route.reference_set_digest,
                "reference_health_policy_version": HEALTH_POLICY_VERSION,
                "reference_routing_decision": (
                    "automatic_delayed_corrected_prompt"
                    if automatic_delayed_retry
                    else "automatic_frontal_only"
                    if automatic_frontal_retry
                    else "explicit_frontal_only"
                    if frontal_retry or concise_retry
                    else "frontal_first_health_filtered"
                ),
            })
        job.update({
            "keyframe_state": "submitting", "keyframe_attempts": attempt,
            "keyframe_external_job_id": None, "keyframe_submitted_at": created_at,
            "keyframe_provider_status": "submit_intent_persisted",
            "keyframe_failure_code": None,
            "keyframe_submit_intent": submit_intent,
        })
        _write(manifest, payload)
        try:
            submitted = provider.submit_keyframe(KeyframeRequest(
                scene_id=str(job.get("scene_id")), prompt=prompt,
                reference_path=original.path if original else None,
                reference_paths=tuple(reference.path for reference in identity_references),
            ))
            if not str(submitted.external_job_id).strip():
                raise ProviderError("provider_job_id_missing", retryable=True)
        except ProviderError as exc:
            code = _failure_code(exc)
            if code not in DEFINITIVE_SUBMIT_FAILURE_CODES:
                _mark_keyframe_ambiguous(job, payload, manifest)
                return None
            intent = job.get("keyframe_submit_intent")
            if isinstance(intent, dict):
                intent.update({"state": "rejected", "failure_code": code})
            job.update({
                "keyframe_state": "terminal_failed", "keyframe_failure_code": code,
                "state": "terminal_failed", "failure_code": code,
            })
            _set_pack_status(payload)
            _write(manifest, payload)
            return None
        intent = job.get("keyframe_submit_intent")
        if isinstance(intent, dict):
            intent.update({
                "state": "accepted", "external_job_id": submitted.external_job_id,
                "failure_code": None,
            })
        job.update({
            "keyframe_state": "submitted", "keyframe_external_job_id": submitted.external_job_id,
            "keyframe_provider_status": submitted.status,
            "keyframe_submitted_at": datetime.now(timezone.utc).isoformat(),
        })
        _write(manifest, payload)
        return None
    external_id = str(job.get("keyframe_external_job_id") or "")
    if not external_id:
        job.update({
            "keyframe_state": "submit_ambiguous", "keyframe_failure_code": "provider_job_id_missing",
            "state": "submit_ambiguous", "failure_code": "provider_job_id_missing",
        })
        _set_pack_status(payload)
        _write(manifest, payload)
        return None
    submitted_at = _utc(job.get("keyframe_submitted_at"))
    if submitted_at and datetime.now(timezone.utc) - submitted_at > timedelta(
        seconds=config.poll_timeout_seconds
    ):
        try:
            provider.cancel(external_id)
        except ProviderError as exc:
            job["keyframe_state"] = "in_progress"
            job["keyframe_failure_code"] = _failure_code(exc)
            _write(manifest, payload)
            return None
        job.setdefault("keyframe_provider_job_history", []).append(external_id)
        job.update({
            "keyframe_state": "terminal_failed", "keyframe_failure_code": "provider_timeout",
            "state": "terminal_failed", "failure_code": "provider_timeout",
        })
        _set_pack_status(payload)
        _write(manifest, payload)
        return None
    try:
        current = provider.retrieve(external_id)
        job["keyframe_provider_status"] = current.status
        if current.status == "in_progress":
            job["keyframe_state"] = "in_progress"
            _write(manifest, payload)
            return None
        if current.status == "terminal_failed":
            category = (
                current.failure_category
                if current.failure_category in FAILURE_CATEGORIES
                else current.failure_code
                if current.failure_code in FAILURE_CATEGORIES
                else "unknown_terminal"
            )
            manifest_failure_code = (
                "provider_terminal_failure"
                if current.failure_code == "provider_terminal_failure"
                and current.provider_failure_code is None
                else category
            )
            job.update({
                "keyframe_state": "terminal_failed",
                "keyframe_failure_code": manifest_failure_code,
                "keyframe_provider_failure_code": current.provider_failure_code,
                "keyframe_failure_category": category,
                "keyframe_retry_policy": {
                    "automatic_retry": bool(current.automatic_retry_allowed),
                    "same_input_retry": bool(current.same_input_retry_allowed),
                    "delayed_retry_eligible": bool(current.delayed_retry_eligible),
                    "input_repair_required": bool(current.input_repair_required),
                    "corrected_input_required": bool(current.corrected_input_required),
                },
                "state": "terminal_failed",
                "failure_code": manifest_failure_code,
            })
            route = _route_from_intent(job.get("keyframe_submit_intent"))
            registry = _registry(config)
            if route is not None and registry is not None:
                try:
                    registry.record_terminal(
                        route,
                        plan_id=str(payload.get("plan_id")),
                        scene_id=str(job.get("scene_id")),
                        category=category,
                    )
                    quarantined = registry.role_is_quarantined(route)
                except ReferenceHealthError:
                    quarantined = False
                if (
                    _automatic_fallback_contract(payload)
                    and quarantined
                    and route.reference_role != "frontal_identity"
                ):
                    _queue_automatic_fallback(
                        job,
                        reason="reference_route_quarantined",
                        retry_phase="automatic_frontal",
                    )
            if _automatic_fallback_contract(payload) and current.delayed_retry_eligible:
                _queue_automatic_fallback(
                    job,
                    reason=category,
                    retry_phase="automatic_delayed",
                )
            _set_pack_status(payload)
            _write(manifest, payload)
            return None
        destination.parent.mkdir(parents=True, exist_ok=True)
        raw_fd, raw_name = tempfile.mkstemp(prefix=".keyframe-", suffix=".jpg", dir=destination.parent)
        os.close(raw_fd)
        temporary = Path(raw_name)
        try:
            provider.download_keyframe(current, temporary)
            os.replace(temporary, destination)
            ensure_private_group_access(destination, directory=False)
        finally:
            temporary.unlink(missing_ok=True)
        job.update({
            "keyframe_state": "ready", "keyframe_checksum": checksum(destination),
            "keyframe_failure_code": None, "keyframe_provider_status": "completed",
        })
        route = _route_from_intent(job.get("keyframe_submit_intent"))
        registry = _registry(config)
        if route is not None and registry is not None:
            try:
                registry.record_success(
                    route,
                    plan_id=str(payload.get("plan_id")),
                    scene_id=str(job.get("scene_id")),
                )
                if int(job.get("keyframe_automatic_fallbacks", 0)) == 1:
                    job["keyframe_fallback_state"] = "completed"
            except ReferenceHealthError:
                job.update({
                    "keyframe_state": "terminal_failed",
                    "keyframe_failure_code": "reference_health_write_failed",
                    "state": "terminal_failed",
                    "failure_code": "reference_health_write_failed",
                })
        _write(manifest, payload)
    except ProviderError as exc:
        job["keyframe_failure_code"] = _failure_code(exc)
        job["keyframe_state"] = "in_progress" if exc.retryable else "terminal_failed"
        if not exc.retryable:
            job.update({"state": "terminal_failed", "failure_code": _failure_code(exc)})
        _set_pack_status(payload)
        _write(manifest, payload)
    return None


def _process_scene(
    *, payload: dict[str, Any], job: dict[str, Any], manifest: Path,
    config: WorkerConfig, provider: VideoProvider, composer: MediaComposer,
) -> None:
    scene = _scene_plan(payload, str(job["scene_id"]))
    state = str(job.get("state", "queued"))
    if state in {
        "completed", "terminal_failed", "blocked_reference", "awaiting_secondary_approval",
        "submit_ambiguous",
    }:
        return
    if state == "submitting":
        # The process may have died after the POST left the host.  Without an
        # external id there is no safe way to distinguish accepted from lost,
        # so this intent is blocked permanently instead of being submitted twice.
        _mark_submit_ambiguous(job, payload, manifest)
        return
    keyframe = _process_keyframe(
        payload=payload, job=job, scene=scene, manifest=manifest,
        config=config, provider=provider,
    )
    if keyframe is None:
        return
    if state == "composed":
        clean = composer.safe_output(manifest.parent, str(job["clean_path"]))
        story = composer.safe_output(manifest.parent, str(job["story_path"]))
        try:
            story_probe = composer.overlay_story(
                clean, story, text=str(scene["story_overlay"]), safe_zone=str(scene["text_safe_zone"]),
            )
            job["story_checksum"] = checksum(story)
            job["story_media_probe"] = story_probe.to_dict()
            job["state"] = "completed"
            job["failure_code"] = None
            job["technical_qa"] = {"status": "accepted"}
        except MediaError as exc:
            job["failure_code"] = _failure_code(exc)
        _set_pack_status(payload)
        _write(manifest, payload)
        return
    if state in {"planned", "queued", "retryable_failed"}:
        # Identity was already resolved while producing the immutable keyframe.
        # Video generation must depend only on that approved scene image, not on
        # the original avatar remaining available for a second provider call.
        reference = ReferenceSelection(keyframe, "directed_keyframe", "")
        jobs, seconds = _budget_usage(config.pack_root)
        planned = int(job["planned_duration_seconds"])
        if jobs >= config.daily_job_limit or jobs + 1 > config.daily_job_limit:
            job["failure_code"] = "daily_job_limit_reached"
            _write(manifest, payload)
            return
        if seconds + planned > config.daily_seconds_limit:
            job["failure_code"] = "daily_seconds_limit_reached"
            _write(manifest, payload)
            return
        try:
            prompt = append_prompt_guidance(
                str(scene["provider_prompt"]),
                reference.body_guidance if reference else "",
            )
            request = SceneRequest(
                scene_id=str(job["scene_id"]),
                prompt=story_production.validate_provider_prompt(prompt),
                duration_seconds=planned,
                reference_path=reference.path if reference else None,
            )
        except story_production.StoryPlanError as exc:
            _mark_failure(job, exc, config)
            _set_pack_status(payload)
            _write(manifest, payload)
            return
        attempt_number = int(job.get("attempts", 0)) + 1
        created_at = datetime.now(timezone.utc).isoformat()
        previous_intent = job.get("submit_intent")
        if isinstance(previous_intent, dict):
            job.setdefault("submit_intent_history", []).append(dict(previous_intent))
        intent_id = hashlib.sha256(
            f"{payload.get('plan_id')}|{job.get('scene_id')}|{provider.model}|"
            f"{attempt_number}|{created_at}".encode("utf-8")
        ).hexdigest()[:24]
        job.update({
            "state": "submitting", "external_job_id": None,
            "provider_status": "submit_intent_persisted", "submitted_at": created_at,
            "attempts": attempt_number, "failure_code": None,
            "submit_intent": {
                "intent_id": intent_id, "model": provider.model,
                "created_at": created_at, "state": "submitting", "failure_code": None,
            },
        })
        attempts = job.setdefault("model_attempts", {})
        attempts[provider.model] = int(attempts.get(provider.model, 0)) + 1
        payload["provider"] = {"name": provider.name, "model": provider.model}
        _set_pack_status(payload)
        _write(manifest, payload)  # Durable before the paid POST leaves the host.
        try:
            submitted = provider.submit(request)
            if not str(submitted.external_job_id).strip():
                raise ProviderError("provider_job_id_missing", retryable=True)
        except (ProviderError, story_production.StoryPlanError) as exc:
            code = _failure_code(exc)
            if code not in DEFINITIVE_SUBMIT_FAILURE_CODES:
                _mark_submit_ambiguous(job, payload, manifest)
                return
            intent = job.get("submit_intent")
            if isinstance(intent, dict):
                intent.update({"state": "rejected", "failure_code": code})
            if code == "video_prompt_image_required":
                _request_secondary(job, payload, code, config, provider)
                _write(manifest, payload)
                return
            _mark_failure(job, exc, config)
            if job.get("state") == "terminal_failed":
                _request_secondary(job, payload, code, config, provider)
            _set_pack_status(payload)
            _write(manifest, payload)
            return
        except Exception:
            _mark_submit_ambiguous(job, payload, manifest)
            return
        intent = job.get("submit_intent")
        if isinstance(intent, dict):
            intent.update({
                "state": "accepted", "external_job_id": submitted.external_job_id,
                "failure_code": None,
            })
        job.update({
            "state": "submitted", "external_job_id": submitted.external_job_id,
            "provider_status": submitted.status, "submitted_at": datetime.now(timezone.utc).isoformat(),
            "failure_code": None,
        })
        _set_pack_status(payload)
        _write(manifest, payload)  # External ID is durable before any poll.
        return
    external_id = str(job.get("external_job_id") or "")
    if not external_id:
        job["state"] = "retryable_failed"
        job["failure_code"] = "provider_job_id_missing"
        _write(manifest, payload)
        return
    submitted_at = _utc(job.get("submitted_at"))
    if submitted_at and datetime.now(timezone.utc) - submitted_at > timedelta(seconds=config.poll_timeout_seconds):
        try:
            provider.cancel(external_id)
        except ProviderError as exc:
            # An uncertain cancellation must never trigger another paid submit.
            job["state"] = "in_progress"
            job["failure_code"] = _failure_code(exc)
            _set_pack_status(payload)
            _write(manifest, payload)
            return
        job.setdefault("provider_job_history", []).append(external_id)
        _mark_failure(job, ProviderError("provider_timeout", retryable=True), config)
        if job.get("state") == "terminal_failed":
            _request_secondary(job, payload, "provider_timeout", config, provider)
        _set_pack_status(payload)
        _write(manifest, payload)
        return
    try:
        current = provider.retrieve(external_id)
        job["provider_status"] = current.status
        if current.status == "in_progress":
            job["state"] = "in_progress"
            _set_pack_status(payload)
            _write(manifest, payload)
            return
        if current.status == "terminal_failed":
            code = current.failure_code or "provider_terminal_failure"
            job["state"] = "terminal_failed"
            job["failure_code"] = code
            _request_secondary(job, payload, code, config, provider)
            _set_pack_status(payload)
            _write(manifest, payload)
            return
        raw_fd, raw_name = tempfile.mkstemp(prefix=".provider-", suffix=".mp4", dir=manifest.parent / "stories")
        os.close(raw_fd)
        raw_path = Path(raw_name)
        try:
            provider.download(current, raw_path)
            job["state"] = "downloaded"
            _write(manifest, payload)
            clean = composer.safe_output(manifest.parent, str(job["clean_path"]))
            probe = composer.normalize(raw_path, clean, duration_seconds=float(job["planned_duration_seconds"]))
        finally:
            raw_path.unlink(missing_ok=True)
        job.update({
            "state": "composed", "actual_duration_seconds": probe.duration_seconds,
            "media_probe": probe.to_dict(), "clean_checksum": checksum(clean),
            "failure_code": None, "technical_qa": {"status": "accepted"},
        })
        payload["renderer"] = {"status": "available", "name": provider.name}
        payload["composer"] = {"status": "available", "name": "ffmpeg"}
        _set_pack_status(payload)
        _write(manifest, payload)
    except ProviderError as exc:
        # Poll/download transport failures do not invalidate the paid external
        # task. Keep its ID and retrieve the same task on the next run.
        job["failure_code"] = _failure_code(exc)
        if exc.retryable:
            job["state"] = "in_progress"
        else:
            job["state"] = "terminal_failed"
            _request_secondary(job, payload, _failure_code(exc), config, provider)
    except MediaError as exc:
        # A completed provider task can be downloaded/composed again without a
        # second submit. Bound local retries independently from provider jobs.
        job["compose_attempts"] = int(job.get("compose_attempts", 0)) + 1
        job["failure_code"] = _failure_code(exc)
        job["state"] = "submitted" if job["compose_attempts"] <= config.max_retries else "terminal_failed"
        if job["state"] == "terminal_failed":
            _request_secondary(job, payload, _failure_code(exc), config, provider)
    if job.get("failure_code"):
        _set_pack_status(payload)
        _write(manifest, payload)


MUSIC_ROTATION_SCHEMA = "naz-story-music-rotation-v1"


class MusicRotationLock:
    def __init__(self, state_path: Path) -> None:
        self.lock = AdvisoryFileLock(
            state_path.with_suffix(state_path.suffix + ".lock")
        )

    def __enter__(self) -> "MusicRotationLock":
        try:
            self.lock.__enter__()
        except StoryPackLockError as exc:
            raise MediaError("music_rotation_state_invalid") from exc
        return self

    def __exit__(self, *_: object) -> None:
        self.lock.__exit__()


def _rotation_path(config: WorkerConfig) -> Path:
    return (
        config.music_rotation_state_path
        or (config.pack_root / ".story_music_rotation.json")
    ).expanduser().resolve()


def _load_music_rotation(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"schema": MUSIC_ROTATION_SCHEMA, "recent": [], "reservations": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MediaError("music_rotation_state_invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != MUSIC_ROTATION_SCHEMA
        or not isinstance(payload.get("recent"), list)
        or not isinstance(payload.get("reservations"), dict)
    ):
        raise MediaError("music_rotation_state_invalid")
    payload["recent"] = [str(item) for item in payload["recent"]][-8:]
    payload["reservations"] = {
        str(key): str(value) for key, value in payload["reservations"].items()
    }
    return payload


def _music_tags(payload: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for raw in payload.get("music_plan", {}).get("tags", []):
        result.update(
            item.strip().casefold()
            for item in str(raw).split(",")
            if item.strip()
        )
    return result


def _reserve_track(
    *, tracks: list[Any], tags: set[str], state_path: Path, reservation_id: str,
    duration_seconds: float,
) -> Any:
    with MusicRotationLock(state_path):
        state = _load_music_rotation(state_path)
        by_id = {track.track_id: track for track in tracks}
        existing = state["reservations"].get(reservation_id)
        if existing in by_id:
            selected = by_id[existing]
            if not eligible_segment_starts(
                beat_grid=selected.beat_grid,
                beat_evidence=selected.beat_evidence,
                evidence_required=selected.evidence_required,
                track_duration_seconds=selected.duration_seconds,
                segment_duration_seconds=duration_seconds,
                beats_per_bar=selected.beats_per_bar,
            ):
                raise MediaError("licensed_music_segment_invalid")
            return selected
        recent = list(state["recent"])
        reserved = set(state["reservations"].values())

        def rank(track: Any) -> tuple[int, int, int, str]:
            overlap = len(tags.intersection(set(track.tags)))
            is_recent = track.track_id in recent
            recent_index = recent.index(track.track_id) if is_recent else -1
            return (1 if is_recent else 0, -overlap, recent_index, track.track_id)

        candidates = [
            track for track in tracks
            if (
                track.track_id not in reserved
                and eligible_segment_starts(
                    beat_grid=track.beat_grid,
                    beat_evidence=track.beat_evidence,
                    evidence_required=track.evidence_required,
                    track_duration_seconds=track.duration_seconds,
                    segment_duration_seconds=duration_seconds,
                    beats_per_bar=track.beats_per_bar,
                )
            )
        ]
        if not candidates:
            raise MediaError("licensed_music_segment_invalid")
        selected = min(candidates, key=rank)
        state["reservations"][reservation_id] = selected.track_id
        story_production.atomic_json(state_path, state)
        return selected


def _complete_track_rotation(state_path: Path, reservation_id: str, track_id: str) -> None:
    with MusicRotationLock(state_path):
        state = _load_music_rotation(state_path)
        if state["reservations"].get(reservation_id) not in {None, track_id}:
            raise MediaError("music_rotation_state_invalid")
        state["reservations"].pop(reservation_id, None)
        recent = [item for item in state["recent"] if item != track_id]
        recent.append(track_id)
        state["recent"] = recent[-8:]
        story_production.atomic_json(state_path, state)


def _segment_grid(track: Any, *, duration_seconds: float, seed: str) -> tuple[float, tuple[float, ...]]:
    candidates = eligible_segment_starts(
        beat_grid=track.beat_grid,
        beat_evidence=track.beat_evidence,
        evidence_required=track.evidence_required,
        track_duration_seconds=track.duration_seconds,
        segment_duration_seconds=duration_seconds,
        beats_per_bar=track.beats_per_bar,
    )
    if not candidates:
        raise MediaError("licensed_music_segment_invalid")
    index = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12], 16) % len(candidates)
    start = candidates[index]
    grid = tuple(
        round(float(beat) - start, 6)
        for beat in track.beat_grid
        if start - 0.001 <= float(beat) <= start + duration_seconds + 0.6
    )
    if not grid or abs(grid[0]) > 0.01:
        raise MediaError("licensed_music_segment_invalid")
    return round(start, 6), grid


def _align_shots(shots: list[dict[str, Any]], beat_grid: tuple[float, ...], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    elapsed = 0.0
    aligned: list[dict[str, Any]] = []
    jobs = {str(row["scene_id"]): row for row in payload.get("scene_jobs", [])}
    for shot in shots:
        maximum = min(2.0, float(jobs[str(shot["scene_id"])]["actual_duration_seconds"]) - float(shot.get("in_seconds", 0)))
        candidates = [beat for beat in beat_grid if 0.4 <= beat - elapsed <= maximum]
        if not candidates:
            raise MediaError("reel_cuts_not_on_beat_grid")
        planned_end = elapsed + float(shot["duration_seconds"])
        end = min(candidates, key=lambda beat: abs(beat - planned_end))
        item = dict(shot)
        item["duration_seconds"] = round(end - elapsed, 3)
        aligned.append(item)
        elapsed = end
    if not 12.0 <= elapsed <= 20.0:
        raise MediaError("licensed_music_segment_invalid")
    return aligned


def _compose_reels(payload: dict[str, Any], manifest: Path, config: WorkerConfig, composer: MediaComposer) -> None:
    if not payload.get("scene_jobs") or not all(row.get("state") == "completed" for row in payload["scene_jobs"]):
        return
    composition = payload.get("scout_composition")
    if isinstance(composition, dict) and composition.get("mode") == "voice_over_only":
        voice = payload.get("voice_over_plan")
        reels = payload.get("reel_jobs", [])
        edits = {str(row["edit_id"]): row for row in payload.get("reel_edits", [])}
        if (
            composition.get("schema_version") != "content-inbox-scout-voice-composition-v1"
            or composition.get("duration_seconds") != 15
            or composition.get("scene_count") != 5
            or composition.get("music_present") is not False
            or not isinstance(voice, dict)
            or voice.get("schema_version") != "content-inbox-scout-voice-over-v1"
            or voice.get("music_present") is not False
            or voice.get("calls") != 1
        ):
            raise MediaError("voice_over_contract_invalid")
        if voice.get("status") != "ready" or not isinstance(voice.get("audio_digest"), str):
            for reel in reels:
                if reel.get("state") != "completed":
                    reel.update({"state": "blocked_voice", "failure_code": "voice_over_unavailable"})
            _set_pack_status(payload)
            _write(manifest, payload)
            return
        voice_path = (manifest.parent / str(voice.get("path", ""))).resolve()
        if manifest.parent.resolve() not in voice_path.parents or checksum(voice_path) != voice["audio_digest"]:
            raise MediaError("voice_over_invalid")
        for reel in reels:
            if reel.get("state") == "completed":
                continue
            try:
                edit = edits[str(reel["edit_id"])]
                destination = composer.safe_output(manifest.parent, str(reel["path"]))
                probe = composer.compose_voice_reel(
                    pack_root=manifest.parent,
                    shots=[dict(row) for row in edit["shots"]],
                    destination=destination,
                    voice_path=voice_path,
                    target_duration_seconds=15.0,
                )
                reel.update({
                    "state": "completed",
                    "actual_edl": [dict(row) for row in edit["shots"]],
                    "media_probe": probe.to_dict(),
                    "checksum": checksum(destination),
                    "failure_code": None,
                    "audio_present": True,
                    "music_present": False,
                })
            except MediaError as exc:
                reel.update({"state": "blocked_voice", "failure_code": _failure_code(exc)})
            _set_pack_status(payload)
            _write(manifest, payload)
        return
    tracks = load_music_library(config.music_library_path, pack_root=config.pack_root) if config.music_library_path else []
    if not tracks:
        for reel in payload.get("reel_jobs", []):
            if reel.get("state") != "completed":
                reel.update({"state": "blocked_music", "failure_code": "licensed_music_invalid"})
        _set_pack_status(payload)
        _write(manifest, payload)
        return
    tracks_by_id = {track.track_id: track for track in tracks}
    rotation_path = _rotation_path(config)
    tags = _music_tags(payload)
    edits = {str(row["edit_id"]): row for row in payload.get("reel_edits", [])}
    for reel in payload.get("reel_jobs", []):
        if reel.get("state") == "completed":
            continue
        try:
            edit = edits[str(reel["edit_id"])]
            reservation_id = f"{payload.get('plan_id')}:{reel['edit_id']}"
            planned_duration = sum(float(row["duration_seconds"]) for row in edit["shots"])
            selection = reel.get("music_selection")
            if isinstance(selection, dict):
                track = tracks_by_id.get(str(selection.get("track_id")))
                if track is None or selection.get("checksum") != track.checksum:
                    raise MediaError("licensed_music_invalid")
                start_seconds = float(selection["start_seconds"])
                segment_grid = tuple(float(item) for item in selection["segment_beat_grid"])
            else:
                track = _reserve_track(
                    tracks=tracks, tags=tags, state_path=rotation_path,
                    reservation_id=reservation_id,
                    duration_seconds=planned_duration,
                )
                start_seconds, segment_grid = _segment_grid(
                    track, duration_seconds=planned_duration,
                    seed=reservation_id,
                )
                reel["music_selection"] = {
                    "track_id": track.track_id, "checksum": track.checksum,
                    "start_seconds": start_seconds,
                    "segment_beat_grid": list(segment_grid),
                    "reservation_id": reservation_id,
                }
                _write(manifest, payload)
            shots = _align_shots(
                [dict(row) for row in edit["shots"]], segment_grid, payload
            )
            destination = composer.safe_output(manifest.parent, str(reel["path"]))
            probe = composer.compose_reel(
                pack_root=manifest.parent, shots=shots, destination=destination,
                track=track, track_start_seconds=start_seconds,
                segment_beat_grid=segment_grid,
            )
            _complete_track_rotation(rotation_path, reservation_id, track.track_id)
            reel.update({
                "state": "completed", "actual_edl": shots, "media_probe": probe.to_dict(),
                "checksum": checksum(destination), "failure_code": None,
            })
            selected = {
                "track_id": track.track_id, "bpm": track.bpm,
                "lane": track.lane, "start_seconds": start_seconds,
                "license": track.license, "source": track.source,
                "checksum": track.checksum,
                "publication_rotation_consumed": False,
                "story_rotation_consumed": True,
            }
            payload["music_plan"]["selected_track"] = selected
            payload["music_plan"].setdefault("selected_tracks", []).append(selected)
        except MediaError as exc:
            reel.update({"state": "blocked_music", "failure_code": _failure_code(exc)})
        _set_pack_status(payload)
        _write(manifest, payload)


def _process_corrected_scene_revision(
    revision_plan_id: str, *, config: WorkerConfig,
    provider: VideoProvider | None = None,
    composer: MediaComposer | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Resume one separately approved corrected-input child plan."""
    child_dir = (config.pack_root / revision_plan_id).resolve()
    if config.pack_root not in child_dir.parents:
        raise RuntimeError("unsafe_plan_id")
    with PackLock(child_dir):
        _plan, payload, runtime_path = (
            story_pack_control.validate_corrected_scene_revision_for_worker(
                config.pack_root, revision_plan_id
            )
        )
        if payload.get("pack_status") == "completed":
            return "completed"
        if len(payload.get("scene_jobs", [])) != 5:
            raise RuntimeError("corrected_scene_runtime_invalid")
        corrected_jobs = [
            job for job in payload.get("scene_jobs", [])
            if job.get("corrected_revision_id")
        ]
        if (
            [job.get("scene_id") for job in corrected_jobs]
            != list(story_pack_control.CORRECTED_SCENE_IDS)
            or any(int(job.get("keyframe_attempts", 0)) > 1 for job in corrected_jobs)
            or any(int(job.get("attempts", 0)) > 1 for job in corrected_jobs)
        ):
            raise RuntimeError("corrected_scene_runtime_invalid")
        for job in payload.get("scene_jobs", []):
            state = str(job.get("state", ""))
            if state == "submitting":
                _mark_submit_ambiguous(job, payload, runtime_path)
                return "submit_ambiguous"
            if state not in story_production.TERMINAL_SCENE_STATES:
                model = _selected_model(job, config)
                if model != "gen4_turbo":
                    raise RuntimeError("video_model_route_mismatch")
                active_provider = provider or provider_from_environment(
                    env, model_override=model
                )
                if active_provider.name == "runway" and active_provider.model != model:
                    raise RuntimeError("video_model_route_mismatch")
                media = composer or MediaComposer(
                    ffmpeg=config.ffmpeg,
                    ffprobe=config.ffprobe,
                    font_path=config.font_path,
                    timeout_seconds=config.media_timeout_seconds,
                )
                _process_scene(
                    payload=payload,
                    job=job,
                    manifest=runtime_path,
                    config=config,
                    provider=active_provider,
                    composer=media,
                )
                return str(job.get("state"))
        media = composer or MediaComposer(
            ffmpeg=config.ffmpeg,
            ffprobe=config.ffprobe,
            font_path=config.font_path,
            timeout_seconds=config.media_timeout_seconds,
        )
        _compose_reels(payload, runtime_path, config, media)
        return str(payload.get("pack_status"))


def process_pack(
    plan_id: str, *, config: WorkerConfig, provider: VideoProvider | None = None,
    composer: MediaComposer | None = None, env: Mapping[str, str] | None = None,
) -> str:
    if not config.render_enabled:
        return "render_disabled"
    if (
        config.primary_model != CANONICAL_PRIMARY_MODEL
        or config.secondary_model != CANONICAL_SECONDARY_MODEL
        or config.model_priority != CANONICAL_MODEL_PRIORITY
    ):
        raise RuntimeError("video_model_priority_invalid")
    pack_dir = (config.pack_root / plan_id).resolve()
    if config.pack_root not in pack_dir.parents:
        raise RuntimeError("unsafe_plan_id")
    manifest = pack_dir / "story_manifest.json"
    if config.auto_fallback:
        raise RuntimeError("video_auto_fallback_forbidden")
    if (pack_dir / "revision-plan.json").is_file():
        return _process_corrected_scene_revision(
            plan_id,
            config=config,
            provider=provider,
            composer=composer,
            env=env,
        )
    with PackLock(pack_dir):
        payload = story_production.read_manifest(manifest)
        if payload.get("schema") not in {
            story_production.STORY_SCHEMA,
            story_production.PREVIOUS_STORY_SCHEMA,
        }:
            return "legacy_manifest_read_only"
        if (
            payload.get("schema") == story_production.PREVIOUS_STORY_SCHEMA
            and not _migrated_legacy_contract(
                payload, health_root=config.reference_health_root
            )
        ):
            return "legacy_manifest_read_only"
        if not _paid_manifest_contract(payload, config):
            raise RuntimeError("story_manifest_contract_stale")
        if str(payload.get("approval", {}).get("status")) != "approved":
            return "awaiting_approval"
        if len(payload.get("scene_jobs", [])) > config.max_scene_jobs:
            raise RuntimeError("scene_job_limit_exceeded")
        if any(
            str(job.get("state", "")) == "submit_ambiguous"
            for job in payload.get("scene_jobs", [])
        ):
            return "submit_ambiguous"
        # Concurrency=1: resume the first non-terminal job only.
        for job in payload.get("scene_jobs", []):
            state = str(job.get("state", ""))
            if state == "submitting":
                _mark_submit_ambiguous(job, payload, manifest)
                return "submit_ambiguous"
            if state not in story_production.TERMINAL_SCENE_STATES:
                model = _selected_model(job, config)
                if model is None:
                    _set_pack_status(payload)
                    _write(manifest, payload)
                    return "awaiting_secondary_approval"
                active_provider = provider or provider_from_environment(
                    env, model_override=model
                )
                if active_provider.name == "runway" and active_provider.model != model:
                    raise RuntimeError("video_model_route_mismatch")
                media = composer or MediaComposer(
                    ffmpeg=config.ffmpeg, ffprobe=config.ffprobe,
                    font_path=config.font_path,
                    timeout_seconds=config.media_timeout_seconds,
                )
                _process_scene(
                    payload=payload, job=job, manifest=manifest, config=config,
                    provider=active_provider, composer=media,
                )
                return str(job.get("state"))
        media = composer or MediaComposer(
            ffmpeg=config.ffmpeg, ffprobe=config.ffprobe, font_path=config.font_path,
            timeout_seconds=config.media_timeout_seconds,
        )
        _compose_reels(payload, manifest, config, media)
        return str(payload.get("pack_status"))


def _queued_plan_ids(
    root: Path, *, reference_health_root: Path | None = None
) -> list[str]:
    result = []
    if not root.is_dir():
        return result
    for manifest in sorted(root.glob("*/story_manifest.json"), key=lambda item: item.stat().st_mtime):
        try:
            payload = story_production.read_manifest(manifest)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not (
            story_production.manifest_has_current_production_contract(payload)
            or _migrated_legacy_contract(
                payload, health_root=reference_health_root
            )
        ):
            continue
        if str(payload.get("approval", {}).get("status")) != "approved":
            continue
        if any(
            str(job.get("state", "")) == "submit_ambiguous"
            for job in payload.get("scene_jobs", [])
        ):
            continue
        if payload.get("pack_status") in {
            "awaiting_approval", "awaiting_secondary_approval", "superseded", "completed",
        }:
            continue
        scene_jobs = payload.get("scene_jobs", [])
        first_pending = next(
            (
                str(job.get("state", ""))
                for job in scene_jobs
                if str(job.get("state", ""))
                not in story_production.TERMINAL_SCENE_STATES
            ),
            None,
        )
        if first_pending in {
            "planned", "queued", "submitting", "submitted", "in_progress", "downloaded",
            "composed", "retryable_failed",
        }:
            result.append(str(payload.get("plan_id")))
            continue
        if first_pending == "awaiting_secondary_approval":
            continue
        if scene_jobs and all(job.get("state") == "completed" for job in scene_jobs):
            if any(job.get("state") != "completed" for job in payload.get("reel_jobs", [])):
                result.append(str(payload.get("plan_id")))
    result.extend(
        revision_plan_id
        for revision_plan_id in story_pack_control.queued_corrected_scene_revision_ids(root)
        if revision_plan_id not in result
    )
    return result


def main(argv: list[str] | None = None, *, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Naz Story-first private media worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-config", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--plan-id")
    parser.add_argument("--dry-run", action="store_true", help="Inspect queue without provider/API calls")
    args = parser.parse_args(argv)
    try:
        config = load_config(env)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    if args.check_config:
        print(json.dumps(check_config(config, env), ensure_ascii=False, sort_keys=True))
        return 0
    plan_ids = [args.plan_id] if args.plan_id else _queued_plan_ids(
        config.pack_root,
        reference_health_root=config.reference_health_root,
    )[:1]
    if args.dry_run:
        print(json.dumps({"live_api_called": False, "plan_ids": plan_ids}, ensure_ascii=False))
        return 0
    if not config.render_enabled:
        print(json.dumps({"status": "render_disabled", "live_api_called": False}))
        return 0
    try:
        status = (
            process_pack(plan_ids[0], config=config, env=env)
            if plan_ids else "queue_empty"
        )
    except (ProviderError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        LOGGER.error("story worker stopped: %s", _failure_code(exc))
        print(json.dumps({"status": "failed", "reason_code": _failure_code(exc)}))
        return 1
    print(json.dumps({"status": status, "plan_id": plan_ids[0] if plan_ids else None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
