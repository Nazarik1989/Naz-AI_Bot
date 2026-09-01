"""Import one editorial operator package and optionally deliver its preview."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import operator_content_package as ocp  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import a closed operator-content-package-v1")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--source-markdown", type=Path)
    source.add_argument("--package-json", type=Path)
    parser.add_argument("--operator-request-id", required=True)
    parser.add_argument("--operator-id", type=int)
    parser.add_argument("--store-root", type=Path)
    parser.add_argument("--send-preview", action="store_true")
    return parser


def _keyboard(imported: ocp.ImportedOperatorPackage) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Собрать Reel", callback_data=ocp.callback_data(imported, "build")),
                InlineKeyboardButton("Показать сценарий", callback_data=ocp.callback_data(imported, "script")),
            ],
            [InlineKeyboardButton("Пропустить", callback_data=ocp.callback_data(imported, "skip"))],
        ]
    )


async def _send_preview(token: str, operator_id: int, package: dict, imported: ocp.ImportedOperatorPackage) -> None:
    async with Bot(token=token) as bot:
        await bot.send_message(
            chat_id=operator_id,
            text=ocp.preview_text(package),
            reply_markup=_keyboard(imported),
        )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.getenv("NAZ_ENV_LOADED_BY_SYSTEMD") != "1":
        load_dotenv(ROOT / ".env")
    expected_operator_id = int(os.getenv("ADMIN_ID", "0") or "0")
    operator_id = args.operator_id if args.operator_id is not None else expected_operator_id
    store_root = args.store_root or Path(
        os.getenv("NAZ_OPERATOR_CONTENT_PACKAGE_ROOT", "/var/lib/naz-ai-bot/operator-content-packages")
    )
    if args.source_markdown:
        package = ocp.package_from_editorial_markdown(
            args.source_markdown.read_bytes(), args.operator_request_id,
        )
    else:
        package = ocp.parse_json_package(args.package_json.read_bytes())
        if package["operator_request_id"] != args.operator_request_id:
            raise ocp.OperatorPackageError("operator_request_id_mismatch")
    imported = ocp.import_package(
        store_root,
        package,
        operator_id=operator_id,
        expected_operator_id=expected_operator_id,
    )
    delivered = False
    if args.send_preview:
        token = os.getenv("BOT_TOKEN", "").strip()
        if not token:
            raise ocp.OperatorPackageError("operator_preview_bot_token_missing")
        asyncio.run(_send_preview(token, operator_id, package, imported))
        delivered = True
    print(json.dumps({
        "status": "operator_content_package_imported",
        "package_id": imported.package_id,
        "package_digest": imported.package_digest,
        "created": imported.created,
        "preview_delivered": delivered,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
