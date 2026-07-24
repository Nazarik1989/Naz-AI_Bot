"""Resumable CLI worker for private Naz Story-first media packs.

The worker has no publication code.  Production rendering is disabled unless
both the feature flag and an explicit provider are configured.
"""

from __future__ import annotations

import argparse
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
from story_media_composer import MediaComposer, MediaError, checksum, load_music_library
from story_video_provider import ProviderError, SceneRequest, VideoProvider, provider_from_environment


LOGGER = logging.getLogger("naz.story.worker")
PROJECT_ROOT = Path(__file__).resolve().parent
SAFE_FAILURE_CODES = {
    "approved_reference_invalid", "approved_reference_too_large", "cyrillic_font_missing",
    "daily_job_limit_reached", "daily_seconds_limit_reached", "licensed_music_invalid",
    "licensed_music_metadata_invalid", "media_codec_invalid", "media_duration_invalid",
    "media_frame_rate_invalid", "media_missing_or_empty", "media_motion_not_detected",
    "media_pixel_format_invalid", "media_probe_invalid", "media_resolution_invalid",
    "media_tool_failed", "media_tool_unavailable_or_timed_out", "overlay_text_unsafe",
    "provider_download_failed", "provider_download_not_mp4", "provider_job_id_missing",
    "provider_cancel_uncertain",
    "provider_output_url_missing", "provider_request_rejected", "provider_response_invalid",
    "provider_prompt_unsafe",
    "provider_status_unknown", "provider_temporarily_unavailable", "provider_terminal_failure",
    "provider_timeout", "provider_transport_error", "reel_crop_missing",
    "reel_cuts_not_on_beat_grid", "reel_fragment_duration_invalid", "reel_fragment_out_of_source",
    "unsafe_media_path", "video_api_key_missing", "video_provider_disabled", "video_provider_unknown",
}


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


def load_config(env: Mapping[str, str] | None = None) -> WorkerConfig:
    values = os.environ if env is None else env
    reference = (
        values.get("NAZ_VIDEO_REFERENCE_DIR", "").strip()
        or values.get("NAZ_VIDEO_REFERENCE_PATH", "").strip()
    )
    music = values.get("NAZ_STORY_MUSIC_LIBRARY", "").strip()
    font = values.get("NAZ_STORY_FONT_PATH", "").strip()
    return WorkerConfig(
        pack_root=Path(values.get("NAZ_STORY_PACK_ROOT", "/var/lib/naz-ai-bot/story-packs")).expanduser().resolve(),
        render_enabled=_bool(values.get("NAZ_STORY_RENDER_ENABLED"), False),
        provider_name=values.get("NAZ_VIDEO_PROVIDER", "disabled").strip().casefold(),
        model=values.get("NAZ_VIDEO_MODEL", "gen4.5").strip(),
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
    if shutil.which(config.ffmpeg) is None:
        issues.append("ffmpeg_unavailable")
    if shutil.which(config.ffprobe) is None:
        issues.append("ffprobe_unavailable")
    if config.font_path is None or not config.font_path.is_file():
        issues.append("cyrillic_font_missing")
    reference_status = "available" if _approved_reference_path(config.reference_path) else "unavailable"
    music_status = (
        "available"
        if config.music_library_path
        and load_music_library(config.music_library_path, pack_root=config.pack_root)
        else "unavailable"
    )
    if config.reference_path and (config.reference_path == PROJECT_ROOT or PROJECT_ROOT in config.reference_path.parents):
        issues.append("approved_reference_inside_repository")
    return {
        "ok": not issues, "render_enabled": config.render_enabled,
        "provider": config.provider_name, "model": config.model,
        "reference": reference_status, "music_library": music_status,
        "issues": sorted(set(issues)), "live_api_called": False,
    }


class PackLock:
    def __init__(self, pack_dir: Path) -> None:
        self.path = pack_dir / ".worker.lock"
        self.fd: int | None = None

    def __enter__(self) -> "PackLock":
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise RuntimeError("pack_locked") from exc
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
        self.path.unlink(missing_ok=True)


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


def _budget_usage(root: Path) -> tuple[int, int]:
    now = datetime.now(timezone.utc).date()
    jobs = seconds = 0
    for manifest in root.glob("*/story_manifest.json"):
        try:
            payload = story_production.read_manifest(manifest)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("schema") != story_production.STORY_SCHEMA:
            continue
        for job in payload.get("scene_jobs", []):
            submitted = _utc(job.get("submitted_at"))
            if submitted and submitted.date() == now:
                jobs += int(job.get("attempts", 0))
                seconds += int(job.get("planned_duration_seconds", 0)) * int(job.get("attempts", 0))
    return jobs, seconds


def _write(manifest: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    story_production.atomic_json(manifest, payload)


def _scene_plan(payload: Mapping[str, Any], scene_id: str) -> Mapping[str, Any]:
    return next(row for row in payload.get("scenes", []) if row.get("scene_id") == scene_id)


def _set_pack_status(payload: dict[str, Any]) -> None:
    scenes = [str(row.get("state")) for row in payload.get("scene_jobs", [])]
    reels = [str(row.get("state")) for row in payload.get("reel_jobs", [])]
    if scenes and all(state == "completed" for state in scenes):
        if reels and all(state == "completed" for state in reels):
            payload["pack_status"] = "completed"
            delivery = payload.setdefault(
                "delivery", {"status": "not_ready", "sent_files": [], "completed_at": None}
            )
            if delivery.get("status") == "not_ready":
                delivery["status"] = "ready"
        elif "blocked_music" in reels:
            payload["pack_status"] = "blocked_music"
        else:
            payload["pack_status"] = "composing_reels"
    elif any(state in {"terminal_failed", "blocked_reference"} for state in scenes):
        payload["pack_status"] = "partially_blocked"
    elif any(state in {"submitted", "in_progress", "downloaded", "composed"} for state in scenes):
        payload["pack_status"] = "in_progress"
    else:
        payload["pack_status"] = "queued"


def _approved_reference_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    candidate = path.expanduser().resolve()
    if candidate == PROJECT_ROOT or PROJECT_ROOT in candidate.parents:
        return None
    if candidate.is_file() and candidate.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}:
        return candidate
    if not candidate.is_dir():
        return None
    images = [
        item for item in candidate.iterdir()
        if item.is_file() and item.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
    ]
    if not images:
        return None
    return sorted(
        images,
        key=lambda item: (0 if item.stem.casefold() == "naz-primary" else 1, item.name.casefold()),
    )[0]


def _reference_for(scene: Mapping[str, Any], config: WorkerConfig, provider: VideoProvider) -> Path | None:
    if not bool(scene.get("requires_naz_reference")):
        return None
    reference = _approved_reference_path(config.reference_path)
    if not provider.supports_reference or reference is None:
        raise ProviderError("approved_reference_invalid")
    return reference


def _mark_failure(job: dict[str, Any], exc: BaseException, config: WorkerConfig) -> None:
    code = _failure_code(exc)
    retryable = bool(getattr(exc, "retryable", isinstance(exc, MediaError)))
    job["failure_code"] = code
    if retryable and int(job.get("attempts", 0)) <= config.max_retries:
        job["state"] = "retryable_failed"
        job["external_job_id"] = None
    else:
        job["state"] = "terminal_failed"


def _process_scene(
    *, payload: dict[str, Any], job: dict[str, Any], manifest: Path,
    config: WorkerConfig, provider: VideoProvider, composer: MediaComposer,
) -> None:
    scene = _scene_plan(payload, str(job["scene_id"]))
    state = str(job.get("state", "queued"))
    if state in {"completed", "terminal_failed", "blocked_reference"}:
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
        try:
            reference = _reference_for(scene, config, provider)
        except ProviderError:
            job["state"] = "blocked_reference"
            job["failure_code"] = "approved_reference_invalid"
            _set_pack_status(payload)
            _write(manifest, payload)
            return
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
            submitted = provider.submit(SceneRequest(
                scene_id=str(job["scene_id"]), prompt=story_production.validate_provider_prompt(str(scene["provider_prompt"])),
                duration_seconds=planned, reference_path=reference,
            ))
        except (ProviderError, story_production.StoryPlanError) as exc:
            job["attempts"] = int(job.get("attempts", 0)) + 1
            _mark_failure(job, exc, config)
            _set_pack_status(payload)
            _write(manifest, payload)
            return
        job.update({
            "state": "submitted", "external_job_id": submitted.external_job_id,
            "provider_status": submitted.status, "submitted_at": datetime.now(timezone.utc).isoformat(),
            "attempts": int(job.get("attempts", 0)) + 1, "failure_code": None,
        })
        payload["provider"] = {"name": provider.name, "model": provider.model}
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
            _mark_failure(job, ProviderError(current.failure_code or "provider_terminal_failure"), config)
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
    except MediaError as exc:
        # A completed provider task can be downloaded/composed again without a
        # second submit. Bound local retries independently from provider jobs.
        job["compose_attempts"] = int(job.get("compose_attempts", 0)) + 1
        job["failure_code"] = _failure_code(exc)
        job["state"] = "submitted" if job["compose_attempts"] <= config.max_retries else "terminal_failed"
    if job.get("failure_code"):
        _set_pack_status(payload)
        _write(manifest, payload)


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
    return aligned


def _compose_reels(payload: dict[str, Any], manifest: Path, config: WorkerConfig, composer: MediaComposer) -> None:
    if not payload.get("scene_jobs") or not all(row.get("state") == "completed" for row in payload["scene_jobs"]):
        return
    tracks = load_music_library(config.music_library_path, pack_root=config.pack_root) if config.music_library_path else []
    if not tracks:
        for reel in payload.get("reel_jobs", []):
            if reel.get("state") != "completed":
                reel.update({"state": "blocked_music", "failure_code": "licensed_music_invalid"})
        _set_pack_status(payload)
        _write(manifest, payload)
        return
    track = tracks[0]
    payload["music_plan"]["selected_track"] = {
        "track_id": track.track_id, "path": str(track.path), "bpm": track.bpm,
        "beat_grid": list(track.beat_grid), "license": track.license,
        "source": track.source, "checksum": track.checksum,
        "publication_rotation_consumed": False,
    }
    edits = {str(row["edit_id"]): row for row in payload.get("reel_edits", [])}
    for reel in payload.get("reel_jobs", []):
        if reel.get("state") == "completed":
            continue
        try:
            edit = edits[str(reel["edit_id"])]
            shots = _align_shots([dict(row) for row in edit["shots"]], track.beat_grid, payload)
            destination = composer.safe_output(manifest.parent, str(reel["path"]))
            probe = composer.compose_reel(pack_root=manifest.parent, shots=shots, destination=destination, track=track)
            reel.update({
                "state": "completed", "actual_edl": shots, "media_probe": probe.to_dict(),
                "checksum": checksum(destination), "failure_code": None,
            })
        except MediaError as exc:
            reel.update({"state": "blocked_music", "failure_code": _failure_code(exc)})
        _set_pack_status(payload)
        _write(manifest, payload)


def process_pack(
    plan_id: str, *, config: WorkerConfig, provider: VideoProvider,
    composer: MediaComposer | None = None,
) -> str:
    if not config.render_enabled:
        return "render_disabled"
    pack_dir = (config.pack_root / plan_id).resolve()
    if config.pack_root not in pack_dir.parents:
        raise RuntimeError("unsafe_plan_id")
    manifest = pack_dir / "story_manifest.json"
    payload = story_production.read_manifest(manifest)
    if payload.get("schema") == story_production.LEGACY_STORY_SCHEMA:
        return "legacy_manifest_read_only"
    if str(payload.get("approval", {}).get("status")) != "approved":
        return "awaiting_approval"
    if len(payload.get("scene_jobs", [])) > config.max_scene_jobs:
        raise RuntimeError("scene_job_limit_exceeded")
    media = composer or MediaComposer(
        ffmpeg=config.ffmpeg, ffprobe=config.ffprobe, font_path=config.font_path,
        timeout_seconds=config.media_timeout_seconds,
    )
    with PackLock(pack_dir):
        # Concurrency=1: resume the first non-terminal job only.
        for job in payload.get("scene_jobs", []):
            if job.get("state") not in {"completed", "terminal_failed", "blocked_reference"}:
                _process_scene(payload=payload, job=job, manifest=manifest, config=config, provider=provider, composer=media)
                return str(job.get("state"))
        _compose_reels(payload, manifest, config, media)
        return str(payload.get("pack_status"))


def _queued_plan_ids(root: Path) -> list[str]:
    result = []
    if not root.is_dir():
        return result
    for manifest in sorted(root.glob("*/story_manifest.json"), key=lambda item: item.stat().st_mtime):
        try:
            payload = story_production.read_manifest(manifest)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if payload.get("schema") == story_production.STORY_SCHEMA and payload.get("pack_status") != "completed":
            result.append(str(payload.get("plan_id")))
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
    plan_ids = [args.plan_id] if args.plan_id else _queued_plan_ids(config.pack_root)[:1]
    if args.dry_run:
        print(json.dumps({"live_api_called": False, "plan_ids": plan_ids}, ensure_ascii=False))
        return 0
    if not config.render_enabled:
        print(json.dumps({"status": "render_disabled", "live_api_called": False}))
        return 0
    try:
        provider = provider_from_environment(env)
        status = process_pack(plan_ids[0], config=config, provider=provider) if plan_ids else "queue_empty"
    except (ProviderError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        LOGGER.error("story worker stopped: %s", _failure_code(exc))
        print(json.dumps({"status": "failed", "reason_code": _failure_code(exc)}))
        return 1
    print(json.dumps({"status": status, "plan_id": plan_ids[0] if plan_ids else None}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
