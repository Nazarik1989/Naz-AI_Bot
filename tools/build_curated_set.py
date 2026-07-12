"""Copy a reviewable, non-destructive shortlist from the visual archive."""

from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

import imagehash


RUBRIC_LIMITS = {
    "tupye_kozyrki": 20,
    "naz_core": 20,
    "ai_agents": 24,
    "content_system": 24,
    "automation_systems": 20,
    "ai_general": 12,
}


def score(item: dict[str, Any]) -> tuple[int, int, int, str]:
    text_length = int(item.get("text_char_count") or 0)
    readable_text = 3 if 35 <= text_length <= 450 else 1 if text_length else 0
    resolution = int(item.get("width") or 0) * int(item.get("height") or 0)
    story_bonus = 1 if item.get("aspect_kind") == "story_9x16" else 0
    return readable_text, story_bonus, resolution, str(item.get("modified_utc") or "")


def visually_distinct(items: list[dict[str, Any]], limit: int, distance: int = 6) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    hashes: list[imagehash.ImageHash] = []
    for item in sorted(items, key=score, reverse=True):
        current = imagehash.hex_to_hash(str(item["phash"]))
        if any(current - previous <= distance for previous in hashes):
            continue
        selected.append(item)
        hashes.append(current)
        if len(selected) >= limit:
            break
    return selected


def copy_original(source_root: Path, target_root: Path, item: dict[str, Any]) -> Path:
    source = source_root / str(item["source_path"])
    target = target_root / str(item["rubric"]) / source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        shutil.copy2(source, target)
    return target


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("images_curated/catalog/images_manifest.json"))
    parser.add_argument("--series", type=Path, default=Path("images_curated/catalog/series_manifest.json"))
    parser.add_argument("--source", type=Path, default=Path("images"))
    parser.add_argument("--output", type=Path, default=Path("images_curated"))
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    items = manifest["items"]
    by_id = {str(item["id"]): item for item in items}
    by_rubric: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_rubric[str(item.get("rubric") or "visual_archive")].append(item)

    selected: list[dict[str, Any]] = []
    for rubric, limit in RUBRIC_LIMITS.items():
        selected.extend(visually_distinct(by_rubric.get(rubric, []), limit))

    selected_ids = {str(item["id"]) for item in selected}
    series = json.loads(args.series.read_text(encoding="utf-8"))
    selected_series = sorted(
        (entry for entry in series if 3 <= int(entry["count"]) <= 12),
        key=lambda entry: (entry["rubric"] in RUBRIC_LIMITS, int(entry["count"])),
        reverse=True,
    )[:12]
    for entry in selected_series:
        for item_id in entry["item_ids"]:
            if str(item_id) not in selected_ids:
                selected.append(by_id[str(item_id)])
                selected_ids.add(str(item_id))

    candidates = []
    edit_queue = []
    for item in selected:
        copied = copy_original(args.source.resolve(), args.output / "selected_originals", item)
        record = {
            "id": item["id"],
            "rubric": item["rubric"],
            "source_original": str((args.source / str(item["source_path"])).as_posix()),
            "curated_original_copy": copied.relative_to(args.output).as_posix(),
            "ocr_text": item.get("ocr_text", ""),
            "aspect_kind": item.get("aspect_kind"),
            "series_id": item.get("series_id"),
            "review_status": "pending",
            "publication_status": "not_published",
            "edited_versions": [],
        }
        candidates.append(record)
        if item.get("has_text") and item.get("rubric") != "tupye_kozyrki":
            edit_queue.append(
                {
                    "id": item["id"],
                    "edit_target": copied.relative_to(args.output).as_posix(),
                    "source_original": record["source_original"],
                    "requested_edit": "remove_embedded_text_preserve_scene",
                    "output_path": f"edited/{item['rubric']}/{Path(str(item['source_path'])).stem}-textless-v1.jpg",
                    "invariants": [
                        "never overwrite source_original",
                        "preserve people, faces, pose, lighting, composition and visual style",
                        "remove only embedded typography and reconstruct the background",
                    ],
                    "status": "queued",
                }
            )

    write_json(args.output / "catalog" / "publication_candidates.json", candidates)
    write_json(args.output / "catalog" / "edit_queue.json", edit_queue)
    write_json(args.output / "catalog" / "selected_series.json", selected_series)
    print(f"selected={len(candidates)} edit_queue={len(edit_queue)} series={len(selected_series)}")


if __name__ == "__main__":
    main()
