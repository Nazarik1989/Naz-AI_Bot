"""Bounded operator CLI for Naz's private original music library.

There is deliberately no timer or scheduler integration.  The only command
that may create paid Stability tasks requires both an environment feature flag
and an explicit per-run paid-call confirmation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from story_audio_library import (
    MAX_INITIAL_TRACKS,
    AudioLibraryError,
    generate_initial_library,
    library_plan,
    specs_as_safe_rows,
)


PROJECT_ROOT = Path(__file__).resolve().parent
SUPPORTED_AUDIO_MODEL = "stable-audio-3"


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class AudioCliConfig:
    library_path: Path | None
    generation_enabled: bool
    provider_name: str
    model: str
    api_key_configured: bool


def load_config(env: Mapping[str, str] | None = None) -> AudioCliConfig:
    values = os.environ if env is None else env
    raw_path = values.get("NAZ_STORY_MUSIC_LIBRARY", "").strip()
    return AudioCliConfig(
        library_path=Path(raw_path).expanduser().resolve() if raw_path else None,
        generation_enabled=_bool(values.get("NAZ_AUDIO_GENERATION_ENABLED"), False),
        provider_name=values.get("NAZ_AUDIO_PROVIDER", "disabled").strip().casefold(),
        model=values.get("NAZ_AUDIO_MODEL", "stable-audio-3").strip(),
        api_key_configured=bool(values.get("NAZ_AUDIO_API_KEY", "").strip()),
    )


def check_config(config: AudioCliConfig) -> dict[str, Any]:
    issues: list[str] = []
    if config.library_path is None:
        issues.append("audio_library_path_missing")
    elif config.library_path == PROJECT_ROOT or PROJECT_ROOT in config.library_path.parents:
        issues.append("audio_library_inside_repository")
    if config.generation_enabled:
        if config.provider_name != "stability":
            issues.append(
                "audio_provider_unknown"
                if config.provider_name != "disabled"
                else "audio_provider_disabled"
            )
        if not config.api_key_configured:
            issues.append("audio_api_key_missing")
        if config.model != SUPPORTED_AUDIO_MODEL:
            issues.append("audio_model_invalid")
    ready_count = 0
    if config.library_path is not None:
        ready_count = int(library_plan(config.library_path)["ready_count"])
    return {
        "ok": not issues,
        "generation_enabled": config.generation_enabled,
        "ready_for_paid_generation": config.generation_enabled and not issues,
        "provider": config.provider_name,
        "model": config.model,
        "library": "available" if ready_count else "empty_or_unavailable",
        "ready_count": ready_count,
        "issues": sorted(set(issues)),
        "live_api_called": False,
    }


def _provider_from_environment(env: Mapping[str, str] | None) -> Any:
    try:
        from story_audio_provider import provider_from_environment
    except ImportError as exc:
        raise AudioLibraryError("audio_provider_adapter_unavailable") from exc
    try:
        return provider_from_environment(env)
    except Exception as exc:
        code = str(getattr(exc, "code", "audio_provider_configuration_invalid"))
        safe = "".join(
            character for character in code
            if character.isascii() and (character.isalnum() or character == "_")
        )
        raise AudioLibraryError(safe[:80] or "audio_provider_configuration_invalid") from exc


def _print(value: Mapping[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def main(
    argv: list[str] | None = None, *, env: Mapping[str, str] | None = None,
    provider: Any | None = None, analyzer: Any | None = None,
) -> int:
    parser = argparse.ArgumentParser(description="Naz private original-audio library")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-config", action="store_true")
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--generate-initial-library", action="store_true")
    parser.add_argument(
        "--confirm-paid-calls", type=int, metavar="N",
        help="Explicit maximum number of new paid submissions (0..8)",
    )
    parser.add_argument(
        "--max-new-tracks", type=int, metavar="N",
        help="Optional stricter per-run new-submission cap (0..8)",
    )
    args = parser.parse_args(argv)
    config = load_config(env)

    if args.check_config:
        _print(check_config(config))
        return 0

    if config.library_path is None:
        _print({"ok": False, "reason_code": "audio_library_path_missing", "live_api_called": False})
        return 2

    if args.plan:
        plan = library_plan(config.library_path)
        plan["catalog"] = specs_as_safe_rows()
        _print(plan)
        return 0

    if args.confirm_paid_calls is None:
        _print({"ok": False, "reason_code": "audio_paid_call_confirmation_missing", "live_api_called": False})
        return 2
    maximum = args.confirm_paid_calls if args.max_new_tracks is None else args.max_new_tracks
    if (
        not 0 <= args.confirm_paid_calls <= MAX_INITIAL_TRACKS
        or not 0 <= maximum <= MAX_INITIAL_TRACKS
        or maximum > args.confirm_paid_calls
    ):
        _print({"ok": False, "reason_code": "audio_generation_limit_invalid", "live_api_called": False})
        return 2
    validation = check_config(config)
    if not config.generation_enabled:
        _print({"ok": False, "reason_code": "audio_generation_disabled", "live_api_called": False})
        return 2
    if not validation["ok"]:
        _print({"ok": False, "reason_code": validation["issues"][0], "live_api_called": False})
        return 2
    try:
        active_provider = provider if provider is not None else _provider_from_environment(env)
        result = generate_initial_library(
            root=config.library_path,
            provider=active_provider,
            confirmed_paid_calls=args.confirm_paid_calls,
            max_new_tracks=maximum,
            analyzer=analyzer,
        )
    except AudioLibraryError as exc:
        _print({"ok": False, "reason_code": exc.code, "live_api_called": False})
        return 1
    if int(result.get("failed_count", 0)) > 0:
        _print({"ok": False, "reason_code": "audio_generation_job_failed", **result})
        return 1
    if int(result.get("analysis_pending_count", 0)) > 0:
        _print({"ok": False, "reason_code": "audio_analysis_pending", **result})
        return 1
    _print({"ok": True, **result})
    return 0


if __name__ == "__main__":
    sys.exit(main())
