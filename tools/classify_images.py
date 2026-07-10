"""Classify the Naz visual archive and recover likely story sequences."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont, ImageOps


RUBRIC_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("tupye_kozyrki", ("шелби", "shelby", "томас", "козыр", "peaky", "манипулятор контекста")),
    ("naz_core", ("naz", "назаи", "naz_ai", "promptordie", "промптордай")),
    ("ai_agents", ("агент", "agents", "agent", "мультиагент", "gpt-агент", "gpt агент")),
    ("content_system", ("контент", "пост", "сторис", "reels", "автопост", "копирайт")),
    ("automation_systems", ("автоматизац", "архитектур", "контекст", "систем", "бот", "интеграц")),
    ("ai_general", ("нейросет", "искусственн", "chatgpt", "gpt", " ии ", " ai ")),
]


def normalize(value: str) -> str:
    return " " + re.sub(r"\s+", " ", (value or "").lower()).strip() + " "


def choose_rubric(item: dict[str, Any]) -> str:
    text = normalize(str(item.get("ocr_text") or ""))
    for rubric, keywords in RUBRIC_RULES:
        if any(keyword in text for keyword in keywords):
            return rubric
    return "visual_archive"


def text_kind(item: dict[str, Any]) -> str:
    length = int(item.get("text_char_count") or len(str(item.get("ocr_text") or "")))
    if length == 0:
        return "no_text"
    if length < 80:
        return "short_hook"
    if length < 250:
        return "post_card"
    return "dense_story"


def build_series(items: list[dict[str, Any]], max_gap_seconds: int) -> list[dict[str, Any]]:
    stories = sorted(
        (item for item in items if item.get("aspect_kind") == "story_9x16"),
        key=lambda item: str(item["modified_utc"]),
    )
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous_time: datetime | None = None
    for item in stories:
        timestamp = datetime.fromisoformat(str(item["modified_utc"]))
        gap = (timestamp - previous_time).total_seconds() if previous_time else None
        same_rubric = not current or current[-1].get("rubric") == item.get("rubric")
        if current and (gap is None or gap > max_gap_seconds or not same_rubric or len(current) >= 20):
            if len(current) >= 2:
                groups.append(current)
            current = []
        current.append(item)
        previous_time = timestamp
    if len(current) >= 2:
        groups.append(current)

    output = []
    for number, group in enumerate(groups, start=1):
        series_id = f"series-{number:03d}"
        for position, item in enumerate(group, start=1):
            item["series_id"] = series_id
            item["series_position"] = position
        output.append(
            {
                "series_id": series_id,
                "rubric": group[0].get("rubric"),
                "count": len(group),
                "start_utc": group[0]["modified_utc"],
                "end_utc": group[-1]["modified_utc"],
                "item_ids": [item["id"] for item in group],
                "ocr_preview": [str(item.get("ocr_text") or "")[:180] for item in group],
            }
        )
    return output


def font(size: int) -> ImageFont.ImageFont:
    candidates = [
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def contact_sheet(items: list[dict[str, Any]], source: Path, output: Path, title: str, limit: int = 80) -> None:
    selected = items[:limit]
    if not selected:
        return
    cell_width, cell_height = 240, 340
    columns = 4
    rows = (len(selected) + columns - 1) // columns
    sheet = Image.new("RGB", (columns * cell_width, 70 + rows * cell_height), (15, 17, 24))
    draw = ImageDraw.Draw(sheet)
    draw.text((20, 18), f"{title} ({len(items)})", fill=(235, 238, 246), font=font(28))
    small_font = font(15)
    for index, item in enumerate(selected):
        column, row = index % columns, index // columns
        x, y = column * cell_width, 70 + row * cell_height
        try:
            with Image.open(source / str(item["source_path"])) as image:
                thumb = ImageOps.contain(image.convert("RGB"), (220, 260), Image.Resampling.LANCZOS)
            sheet.paste(thumb, (x + (cell_width - thumb.width) // 2, y + 5))
        except Exception:  # noqa: BLE001
            pass
        name = Path(str(item["source_path"])).name[:24]
        draw.text((x + 8, y + 272), name, fill=(180, 188, 205), font=small_font)
        preview = re.sub(r"\s+", " ", str(item.get("ocr_text") or ""))[:30]
        draw.text((x + 8, y + 298), preview, fill=(112, 255, 190), font=small_font)
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, format="JPEG", quality=88, optimize=True)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("images_curated/catalog/images_manifest.json"))
    parser.add_argument("--source", type=Path, default=Path("images"))
    parser.add_argument("--output", type=Path, default=Path("images_curated"))
    parser.add_argument("--series-gap-seconds", type=int, default=720)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    items = manifest["items"]
    for item in items:
        item["rubric"] = choose_rubric(item)
        item["text_kind"] = text_kind(item)
        item["edit_status"] = "candidate" if item["text_kind"] in {"short_hook", "post_card", "dense_story"} else "not_needed"

    series = build_series(items, max(60, args.series_gap_seconds))
    manifest["classification"] = {
        "rubric_counts": dict(Counter(str(item["rubric"]) for item in items)),
        "text_kind_counts": dict(Counter(str(item["text_kind"]) for item in items)),
        "series_count": len(series),
    }
    write_json(args.manifest, manifest)
    write_json(args.output / "catalog" / "series_manifest.json", series)

    by_rubric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_rubric[str(item["rubric"])].append(item)
    for rubric, rubric_items in sorted(by_rubric.items()):
        write_json(args.output / "indexes" / f"{rubric}.json", [item["id"] for item in rubric_items])
        contact_sheet(rubric_items, args.source.resolve(), args.output / "contact_sheets" / f"{rubric}.jpg", rubric)

    print(json.dumps(manifest["classification"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
