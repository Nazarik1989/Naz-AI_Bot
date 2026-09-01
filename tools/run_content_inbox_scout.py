"""Run one explicit admin Content Inbox Scout request without starting the bot."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import main  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deliver one private Content Inbox Scout ranking.")
    parser.add_argument("--count", type=int, default=3, choices=range(1, 6))
    parser.add_argument("--format", dest="format_hint", choices=("reel",), default="")
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--refresh", action="store_true")
    return parser


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if not main.BOT_TOKEN or not main.ADMIN_ID:
        raise RuntimeError("scout_production_configuration_invalid")
    from telegram import Bot

    async with Bot(token=main.BOT_TOKEN) as bot:
        return await main.run_content_inbox_scout(
            bot,
            main.ADMIN_ID,
            count=args.count,
            format_hint=args.format_hint,
            refresh=args.refresh,
            operator_request_id=args.request_id,
        )


def cli() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001
        reason = getattr(exc, "reason_code", "scout_cli_failure")
        print(json.dumps({"status": "blocked", "reason": reason}, sort_keys=True))
        return 1
    print(json.dumps({"status": "delivered", **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
