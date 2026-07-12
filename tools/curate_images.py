"""Build a non-destructive catalog for the local Naz visual archive."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import imagehash
from PIL import Image, ImageStat


SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def aspect_kind(width: int, height: int) -> str:
    ratio = width / max(1, height)
    if 0.53 <= ratio <= 0.60:
        return "story_9x16"
    if 0.95 <= ratio <= 1.05:
        return "square"
    if ratio < 0.95:
        return "portrait"
    return "landscape"


def inspect_image(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        mean = ImageStat.Stat(rgb.resize((32, 32))).mean
        width, height = image.size
        return {
            "id": hashlib.sha1(str(path.relative_to(root)).encode("utf-8")).hexdigest()[:16],
            "source_path": path.relative_to(root).as_posix(),
            "source_role": "original_immutable",
            "edited_versions": [],
            "bytes": stat.st_size,
            "modified_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "format": image.format,
            "mode": image.mode,
            "width": width,
            "height": height,
            "aspect_kind": aspect_kind(width, height),
            "sha256": file_sha256(path),
            "phash": str(imagehash.phash(rgb)),
            "dhash": str(imagehash.dhash(rgb)),
            "mean_rgb": [round(value, 2) for value in mean],
            "ocr_text": "",
            "ocr_status": "pending",
            "series_id": None,
            "rubric": None,
            "review_status": "unreviewed",
            "edit_status": "not_requested",
        }


def grouped_duplicates(items: list[dict[str, Any]], key: str) -> list[list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        groups[str(item[key])].append(str(item["id"]))
    return [ids for ids in groups.values() if len(ids) > 1]


def visual_duplicate_groups(items: list[dict[str, Any]], threshold: int) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    used_pairs: set[tuple[str, str]] = set()
    for index, left in enumerate(items):
        left_hash = imagehash.hex_to_hash(str(left["phash"]))
        matches = []
        for right in items[index + 1 :]:
            pair = tuple(sorted((str(left["id"]), str(right["id"]))))
            if pair in used_pairs or left["sha256"] == right["sha256"]:
                continue
            distance = left_hash - imagehash.hex_to_hash(str(right["phash"]))
            if distance <= threshold:
                used_pairs.add(pair)
                matches.append({"id": right["id"], "distance": int(distance)})
        if matches:
            groups.append({"id": left["id"], "matches": matches})
    return groups


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("images"))
    parser.add_argument("--output", type=Path, default=Path("images_curated/catalog"))
    parser.add_argument("--visual-threshold", type=int, default=4)
    args = parser.parse_args()

    source = args.source.resolve()
    files = sorted(
        path for path in source.iterdir()
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    items = []
    failures = []
    for index, path in enumerate(files, start=1):
        try:
            items.append(inspect_image(path, source))
        except Exception as exc:  # noqa: BLE001
            failures.append({"path": path.name, "error": f"{type(exc).__name__}: {exc}"})
        if index % 50 == 0 or index == len(files):
            print(f"inspected {index}/{len(files)}", flush=True)

    exact = grouped_duplicates(items, "sha256")
    visual = visual_duplicate_groups(items, max(0, args.visual_threshold))
    manifest = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source),
        "source_policy": "immutable",
        "total_files": len(files),
        "catalogued_files": len(items),
        "failures": failures,
        "items": items,
    }
    write_json(args.output / "images_manifest.json", manifest)
    write_json(args.output / "duplicates_exact.json", exact)
    write_json(args.output / "duplicates_visual.json", visual)
    print(f"catalogued={len(items)} failures={len(failures)} exact_groups={len(exact)} visual_groups={len(visual)}")


if __name__ == "__main__":
    main()
