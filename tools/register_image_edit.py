"""Register a derived image while proving that its original is unchanged."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--edited", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path("images_curated/catalog/images_manifest.json"))
    parser.add_argument("--queue", type=Path, default=Path("images_curated/catalog/edit_queue.json"))
    parser.add_argument("--candidates", type=Path, default=Path("images_curated/catalog/publication_candidates.json"))
    parser.add_argument("--source", type=Path, default=Path("images"))
    parser.add_argument("--curated-root", type=Path, default=Path("images_curated"))
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    item = next(entry for entry in manifest["items"] if str(entry["id"]) == args.id)
    original = args.source / str(item["source_path"])
    current_hash = sha256(original)
    if current_hash != item["sha256"]:
        raise SystemExit(f"original hash mismatch: {original}")
    if not args.edited.exists():
        raise SystemExit(f"edited file not found: {args.edited}")

    edited_path = args.edited.resolve()
    curated_root = args.curated_root.resolve()
    edited_record = {
        "path": edited_path.relative_to(curated_root).as_posix(),
        "sha256": sha256(args.edited),
        "edit": "remove_embedded_text_preserve_scene",
        "original_sha256_verified": True,
    }
    item.setdefault("edited_versions", []).append(edited_record)
    item["edit_status"] = "completed_v1"
    write_json(args.manifest, manifest)

    queue = json.loads(args.queue.read_text(encoding="utf-8"))
    queued = next(entry for entry in queue if str(entry["id"]) == args.id)
    queued["status"] = "completed_v1"
    queued["result"] = edited_record
    write_json(args.queue, queue)

    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    candidate = next(entry for entry in candidates if str(entry["id"]) == args.id)
    candidate.setdefault("edited_versions", []).append(edited_record)
    candidate["review_status"] = "edited_pending_review"
    write_json(args.candidates, candidates)
    print(json.dumps(edited_record, ensure_ascii=False))


if __name__ == "__main__":
    main()
