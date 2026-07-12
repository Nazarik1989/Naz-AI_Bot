"""Selection and state for image-first Naz publications."""

from __future__ import annotations

import json
import os
import random
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def preferred_image_path(root: Path, candidate: dict[str, Any]) -> Path:
    edited = candidate.get("edited_versions") or []
    if edited:
        path = root / str(edited[-1]["path"])
        if path.exists():
            return path
    return root / str(candidate["curated_original_copy"])


def choose_candidate(
    manifest_path: Path,
    state_path: Path,
    root: Path,
    *,
    require_approved: bool = True,
    rubric: str = "",
) -> dict[str, Any] | None:
    candidates = read_json(manifest_path, [])
    state = read_json(state_path, {"used_ids": []})
    used = set(str(item) for item in state.get("used_ids", []))
    available = []
    for candidate in candidates:
        if str(candidate.get("id")) in used:
            continue
        if candidate.get("publication_status") == "published":
            continue
        if require_approved and candidate.get("review_status") != "approved":
            continue
        if rubric and candidate.get("rubric") != rubric:
            continue
        image_path = preferred_image_path(root, candidate)
        if image_path.is_file():
            available.append(candidate)
    return random.choice(available) if available else None


def mark_used(state_path: Path, candidate_id: str) -> None:
    state = read_json(state_path, {"used_ids": []})
    used = [str(item) for item in state.get("used_ids", [])]
    if candidate_id not in used:
        used.append(candidate_id)
    state["used_ids"] = used[-5000:]
    write_json_atomic(state_path, state)


def claim_visual_turn(state_path: Path, every_n_posts: int) -> tuple[bool, int]:
    """Advance the persistent autopost cadence and claim every Nth slot."""
    state = read_json(state_path, {"used_ids": [], "slot_counter": 0})
    counter = int(state.get("slot_counter") or 0) + 1
    state["slot_counter"] = counter
    state.setdefault("used_ids", [])
    write_json_atomic(state_path, state)
    cadence = max(2, int(every_n_posts))
    return counter % cadence == 0, counter


def visual_topic(candidate: dict[str, Any]) -> str:
    text = " ".join(str(candidate.get("ocr_text") or "").split())
    rubric = str(candidate.get("rubric") or "visual_archive")
    return f"Рубрика: {rubric}. Смысл визуала: {text[:1200]}"
