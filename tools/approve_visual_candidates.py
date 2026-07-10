"""Explicitly approve reviewed visual archive rubrics for publication use."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, default=Path("images_curated/catalog/publication_candidates.json"))
    parser.add_argument("--rubric", action="append", required=True)
    args = parser.parse_args()
    allowed = set(args.rubric)
    candidates = json.loads(args.candidates.read_text(encoding="utf-8"))
    approved = 0
    for candidate in candidates:
        if candidate.get("rubric") in allowed:
            candidate["review_status"] = "approved"
            approved += 1
    temporary = args.candidates.with_suffix(args.candidates.suffix + ".tmp")
    temporary.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.candidates)
    print(f"approved={approved} rubrics={','.join(sorted(allowed))}")


if __name__ == "__main__":
    main()
