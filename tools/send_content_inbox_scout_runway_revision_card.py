"""Create/reuse one provider-free child revision and send its cost card."""

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
import story_pack_control  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send one corrected-scene revision cost-approval card."
    )
    parser.add_argument("--plan-id", required=True)
    return parser


async def _run(plan_id: str) -> dict[str, object]:
    if not main.BOT_TOKEN or not main.ADMIN_ID:
        raise RuntimeError("scout_runway_production_configuration_invalid")
    runway_bridge.load_bridge_for_plan(main.NAZ_STORY_PACK_ROOT, plan_id)
    revision = story_pack_control.create_corrected_scene_revision_plan(
        main.NAZ_STORY_PACK_ROOT,
        plan_id,
        admin_id=main.ADMIN_ID,
        expected_admin_id=main.ADMIN_ID,
    )
    revision_plan_id = str(revision["revision_plan_id"])
    from telegram import Bot

    async with Bot(token=main.BOT_TOKEN) as bot:
        message = await bot.send_message(
            chat_id=main.ADMIN_ID,
            text=story_pack_control.corrected_scene_revision_card(
                main.NAZ_STORY_PACK_ROOT, revision_plan_id
            ),
            reply_markup=main._corrected_revision_keyboard(revision_plan_id),
        )
    return {
        "status": "runway_scene_revision_awaiting_cost_approval",
        **revision,
        "telegram_message_id": message.message_id,
        "approval_card_delivered": True,
        "provider_calls": 0,
        "tts_calls": 0,
        "publication_calls": 0,
    }


def cli() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args.plan_id))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({
            "status": "blocked",
            "reason": str(
                getattr(exc, "reason_code", "scout_runway_revision_card_failed")
            ),
        }, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
