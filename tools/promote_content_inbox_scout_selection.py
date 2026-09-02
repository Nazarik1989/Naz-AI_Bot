"""Promote one existing Russian Scout preference and deliver its build card."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import content_inbox_scout_reel as scout_reel  # noqa: E402
import main  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Promote exactly one existing Russian Scout selection without providers."
    )
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--title-fragment", default="Naz терял контекст")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if not main.BOT_TOKEN or not main.ADMIN_ID:
        raise RuntimeError("scout_production_configuration_invalid")
    await asyncio.to_thread(main.assert_content_inbox_scout_private_state)
    run_id, candidate_id = await asyncio.to_thread(
        scout_reel.locate_selected_material,
        main.NAZ_CONTENT_INBOX_SCOUT_ROOT,
        expected_admin_id=main.ADMIN_ID,
        risk_detector=main.detect_content_risks,
        title_fragment=args.title_fragment,
    )
    selected, _material, created = await asyncio.to_thread(
        scout_reel.promote_selection,
        main.NAZ_CONTENT_INBOX_SCOUT_REEL_ROOT,
        main.NAZ_CONTENT_INBOX_SCOUT_ROOT,
        run_id,
        candidate_id,
        admin_id=main.ADMIN_ID,
        expected_admin_id=main.ADMIN_ID,
        selection_request_id=args.request_id,
        risk_detector=main.detect_content_risks,
    )
    from telegram import Bot

    async with Bot(token=main.BOT_TOKEN) as bot:
        message = await bot.send_message(
            chat_id=main.ADMIN_ID,
            text=scout_reel.selection_card_text(selected),
            reply_markup=main.inbox_scout_selected_keyboard(selected),
        )
    return {
        "schema_version": "content-inbox-selected-material-delivery-v1",
        "run_id": selected.run_id,
        "candidate_id": selected.candidate_id,
        "selection_id": selected.selection_id,
        "title": selected.title,
        "duration_seconds": selected.duration_seconds,
        "scene_count": selected.scene_count,
        "ready_material_digest": selected.ready_material_artifact_digest,
        "selection_created": created,
        "telegram_message_id": message.message_id,
        "model_calls": 0,
        "tts_calls": 0,
        "render_calls": 0,
        "publication_calls": 0,
    }


def cli() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        reason = getattr(exc, "reason_code", "scout_selection_promotion_failed")
        print(json.dumps({"status": "blocked", "reason": reason}, sort_keys=True))
        return 1
    print(json.dumps({"status": "delivered", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
