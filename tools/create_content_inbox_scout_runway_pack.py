"""Create one immutable Scout Runway pack and deliver its approval card."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import content_inbox_scout_runway as runway_bridge  # noqa: E402
import main  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create/reuse one selected Scout Runway pack without paid providers."
    )
    parser.add_argument("--selection-id", required=True)
    parser.add_argument("--request-id", required=True)
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if not main.BOT_TOKEN or not main.ADMIN_ID:
        raise RuntimeError("scout_runway_production_configuration_invalid")
    await asyncio.to_thread(main.assert_content_inbox_scout_private_state)
    pack = await asyncio.to_thread(
        runway_bridge.create_runway_pack,
        main.NAZ_CONTENT_INBOX_SCOUT_REEL_ROOT,
        main.NAZ_CONTENT_INBOX_SCOUT_ROOT,
        main.NAZ_STORY_PACK_ROOT,
        args.selection_id,
        admin_id=main.ADMIN_ID,
        expected_admin_id=main.ADMIN_ID,
        bridge_request_id=args.request_id,
        risk_detector=main.detect_content_risks,
    )
    from telegram import Bot

    async with Bot(token=main.BOT_TOKEN) as bot:
        message = await bot.send_message(
            chat_id=main.ADMIN_ID,
            text=runway_bridge.approval_card_text(pack),
            reply_markup=main.inbox_scout_runway_keyboard(pack.plan_id),
        )
    return {
        "schema_version": runway_bridge.BRIDGE_SCHEMA,
        "selection_id": pack.selection_id,
        "story_pack_id": pack.plan_id,
        "bridge_digest": pack.bridge_digest,
        "duration_seconds": pack.duration_seconds,
        "scene_count": pack.scene_count,
        "model_routes": list(pack.model_routes),
        "keyframe_jobs": pack.keyframe_jobs,
        "video_jobs": pack.video_jobs,
        "credit_estimate": pack.credit_estimate,
        "created": pack.created,
        "telegram_message_id": message.message_id,
        "provider_calls": 0,
        "tts_calls": 0,
        "render_calls": 0,
        "publication_calls": 0,
    }


def cli() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        reason = getattr(exc, "reason_code", "scout_runway_pack_failed")
        print(json.dumps({"status": "blocked", "reason": reason}, sort_keys=True))
        return 1
    print(json.dumps({"status": "awaiting_approval", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
