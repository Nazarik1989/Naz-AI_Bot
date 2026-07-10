"""Generate review-only image-first post drafts without Telegram publication."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import main
import visual_archive


async def run(candidates_path: Path, output: Path, count: int) -> None:
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    approved = [candidate for candidate in candidates if candidate.get("review_status") == "approved"]
    rubrics = ["tupye_kozyrki", "naz_core"]
    selected = []
    for rubric in rubrics:
        selected.extend(candidate for candidate in approved if candidate.get("rubric") == rubric)
    selected = selected[: max(1, count)]

    drafts = []
    for candidate in selected:
        visual_context = visual_archive.visual_topic(candidate)
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты редактор Naz. Пиши самостоятельный Telegram-пост вокруг идеи визуала. "
                    "Не описывай изображение как каталог, не упоминай OCR, не копируй надписи дословно. "
                    "Нужны конкретная сцена или конфликт, системный вывод и живой финал без успешного успеха."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"{visual_context}\n\n"
                    "Подготовь один пост длиной 700–1100 знаков. Без Markdown-заголовков и служебных комментариев."
                ),
            },
        ]
        text = await main.call_gpt(messages, max_tokens=520, model=main.CONTENT_MODEL_NAME)
        drafts.append(
            {
                "id": candidate["id"],
                "rubric": candidate["rubric"],
                "image": candidate["curated_original_copy"],
                "ocr_text": candidate.get("ocr_text", ""),
                "draft": text,
                "status": "review_pending",
            }
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"drafted {len(drafts)}/{len(selected)} | {candidate['id']}", flush=True)


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, default=Path("images_curated/catalog/publication_candidates.json"))
    parser.add_argument("--output", type=Path, default=Path("images_curated/drafts/image_post_drafts.json"))
    parser.add_argument("--count", type=int, default=3)
    args = parser.parse_args()
    asyncio.run(run(args.candidates, args.output, args.count))


if __name__ == "__main__":
    main_cli()
