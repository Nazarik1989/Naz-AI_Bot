"""Verify that no source image has changed since catalog creation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("images_curated/catalog/images_manifest.json"))
    parser.add_argument("--source", type=Path, default=Path("images"))
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    mismatches = []
    for index, item in enumerate(manifest["items"], start=1):
        path = args.source / str(item["source_path"])
        if not path.exists() or sha256(path) != item["sha256"]:
            mismatches.append(str(item["source_path"]))
        if index % 100 == 0:
            print(f"verified {index}/{len(manifest['items'])}", flush=True)
    print(f"verified={len(manifest['items'])} mismatches={len(mismatches)}")
    if mismatches:
        print("\n".join(mismatches[:20]))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
