"""Add Russian/English OCR text to an existing visual archive manifest."""

from __future__ import annotations

import argparse
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import pytesseract
from PIL import Image, ImageEnhance, ImageFilter, ImageOps


def normalize_ocr_text(value: str) -> str:
    lines = []
    for raw_line in value.replace("\x0c", "").splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def prepare_for_ocr(path: Path) -> Image.Image:
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("L")
        image.thumbnail((1800, 1800), Image.Resampling.LANCZOS)
        image = ImageOps.autocontrast(image, cutoff=1)
        image = ImageEnhance.Contrast(image).enhance(1.25)
        return image.filter(ImageFilter.SHARPEN)


def recognize(item: dict[str, Any], source_root: Path) -> tuple[str, str, str]:
    item_id = str(item["id"])
    path = source_root / str(item["source_path"])
    try:
        image = prepare_for_ocr(path)
        text = pytesseract.image_to_string(image, lang="rus+eng", config="--psm 6")
        return item_id, "done", normalize_ocr_text(text)
    except Exception as exc:  # noqa: BLE001
        return item_id, "failed", f"{type(exc).__name__}: {exc}"


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("images_curated/catalog/images_manifest.json"))
    parser.add_argument("--source", type=Path, default=Path("images"))
    parser.add_argument("--tesseract-cmd", type=Path, required=True)
    parser.add_argument("--tessdata-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--save-every", type=int, default=20)
    args = parser.parse_args()

    pytesseract.pytesseract.tesseract_cmd = str(args.tesseract_cmd)
    os.environ["TESSDATA_PREFIX"] = str(args.tessdata_dir.resolve())
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    items = manifest["items"]
    by_id = {str(item["id"]): item for item in items}
    pending = [item for item in items if item.get("ocr_status") not in {"done", "no_text"}]
    print(f"ocr pending={len(pending)} total={len(items)}", flush=True)

    completed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as executor:
        futures = {
            executor.submit(recognize, item, args.source.resolve()): str(item["id"])
            for item in pending
        }
        for future in as_completed(futures):
            item_id, status, value = future.result()
            item = by_id[item_id]
            if status == "done":
                item["ocr_text"] = value
                item["ocr_status"] = "done" if value else "no_text"
                item["text_char_count"] = len(value)
                item["has_text"] = bool(value)
            else:
                item["ocr_status"] = "failed"
                item["ocr_error"] = value
            completed += 1
            if completed % max(1, args.save_every) == 0 or completed == len(pending):
                save_manifest(args.manifest, manifest)
                print(f"ocr completed={completed}/{len(pending)}", flush=True)

    done = sum(item.get("ocr_status") == "done" for item in items)
    no_text = sum(item.get("ocr_status") == "no_text" for item in items)
    failed = sum(item.get("ocr_status") == "failed" for item in items)
    print(f"ocr done={done} no_text={no_text} failed={failed}")


if __name__ == "__main__":
    main()
