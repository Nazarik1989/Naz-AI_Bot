"""Approved VK music catalog and shared recent-track rotation for Naz jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, TypeVar


ROTATION_SCHEMA = "naz_vk_track_rotation.v1"
RECENT_TRACK_LIMIT = 8
_PROCESS_LOCK = threading.RLock()
_T = TypeVar("_T")


class TrackSelectionError(RuntimeError):
    """Raised when a safe catalog-backed track cannot be selected."""


@dataclass(frozen=True)
class ApprovedTrack:
    artist: str
    title: str
    tags: frozenset[str]

    @property
    def query(self) -> str:
        return f"{self.artist} — {self.title}"


def _track(artist: str, title: str, *tags: str) -> ApprovedTrack:
    return ApprovedTrack(artist, title, frozenset(tag.casefold() for tag in tags))


# This is an explicit allowlist. A producer must select from it and must not turn
# an arbitrary post topic into a music search query.
APPROVED_TRACKS: tuple[ApprovedTrack, ...] = (
    _track("Tycho", "Awake", "daily", "focus", "builder", "calm"),
    _track("Bonobo", "Cirrus", "daily", "focus", "systems", "warm"),
    _track("ODESZA", "A Moment Apart", "daily", "builder", "reflective", "warm"),
    _track("M83", "Midnight City", "daily", "synth", "energy", "builder"),
    _track("Kiasmos", "Looped", "daily", "systems", "focus", "reflective"),
    _track("Moderat", "A New Error", "daily", "systems", "glitch", "energy"),
    _track("Jon Hopkins", "Open Eye Signal", "daily", "focus", "systems", "energy"),
    _track("Emancipator", "Minor Cause", "daily", "calm", "reflective", "warm"),
    _track("Boards of Canada", "Dayvan Cowboy", "daily", "reflective", "builder", "warm"),
    _track("Nils Frahm", "Says", "daily", "calm", "focus", "systems"),
    _track("Daft Punk", "Derezzed", "gaming", "cyber", "mechanic", "energy"),
    _track("The Glitch Mob", "We Can Make the World Stop", "gaming", "mechanic", "energy", "builder"),
    _track("Carpenter Brut", "Turbo Killer", "gaming", "arcade", "energy", "synth"),
    _track("Perturbator", "Future Club", "gaming", "cyber", "synth", "energy"),
    _track("Kavinsky", "Nightcall", "gaming", "synth", "identity", "reflective"),
    _track("Lorn", "Anvil", "gaming", "cyber", "reflective", "mechanic"),
    _track("HOME", "Resonance", "gaming", "arcade", "identity", "calm"),
    _track("Power Glove", "Motorcycle Cop", "gaming", "arcade", "energy", "humor"),
    _track("Justice", "Genesis", "gaming", "energy", "mechanic", "builder"),
    _track("Gesaffelstein", "Pursuit", "gaming", "cyber", "energy", "mechanic"),
    _track("Mick Gordon", "BFG Division", "gaming", "energy", "mechanic", "humor"),
    _track("C418", "Sweden", "gaming", "calm", "reflective", "identity"),
)

APPROVED_QUERIES = frozenset(track.query for track in APPROVED_TRACKS)


def _normal(value: str) -> str:
    return " ".join(re.findall(r"[0-9a-zа-яё]+", str(value).casefold()))


def select_track(
    requested_tags: Iterable[str],
    recent_queries: Iterable[str],
    *,
    seed: str,
) -> ApprovedTrack | None:
    """Choose a meaningful approved track outside the shared recent window."""
    tags = {str(tag).casefold().strip() for tag in requested_tags if str(tag).strip()}
    categories = tags.intersection({"daily", "gaming"})
    semantic_tags = tags.difference(categories)
    recent = {_normal(query) for query in list(recent_queries)[-RECENT_TRACK_LIMIT:]}
    candidates = [
        track
        for track in APPROVED_TRACKS
        if (not categories or track.tags.intersection(categories))
        and (not semantic_tags or track.tags.intersection(semantic_tags))
        and _normal(track.query) not in recent
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda track: hashlib.sha256(f"{seed}|{track.query}".encode("utf-8")).hexdigest(),
    )


def _load_recent(state_file: Path) -> list[str]:
    if not state_file.exists():
        return []
    if state_file.is_symlink() or not state_file.is_file():
        raise TrackSelectionError("VK track rotation state must be a regular file")
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrackSelectionError("VK track rotation state is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema") != ROTATION_SCHEMA:
        raise TrackSelectionError("VK track rotation state has an invalid schema")
    recent = payload.get("recent_queries")
    if not isinstance(recent, list) or not all(isinstance(item, str) for item in recent):
        raise TrackSelectionError("VK track rotation history is invalid")
    return recent[-RECENT_TRACK_LIMIT:]


def load_shared_recent(history_file: Path) -> list[str]:
    """Read the consumer-owned global rotation without mutating its state."""
    history_file = Path(history_file)
    if not history_file.exists():
        return []
    if history_file.is_symlink() or not history_file.is_file():
        raise TrackSelectionError("shared VK track history must be a regular file")
    try:
        payload = json.loads(history_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrackSelectionError("shared VK track history is unreadable") from exc
    tracks = payload.get("tracks", []) if isinstance(payload, dict) else []
    if not isinstance(tracks, list):
        raise TrackSelectionError("shared VK track history is invalid")
    keys = [
        str(item.get("key") or "")
        for item in tracks
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    ]
    return keys[-RECENT_TRACK_LIMIT:]


def _save_recent(state_file: Path, recent_queries: Iterable[str]) -> None:
    payload = {
        "schema": ROTATION_SCHEMA,
        "recent_queries": list(recent_queries)[-RECENT_TRACK_LIMIT:],
    }
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{state_file.name}-", dir=state_file.parent)
    os.close(descriptor)
    temp_file = Path(raw_temp)
    try:
        temp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(temp_file, 0o640)
        os.replace(temp_file, state_file)
    finally:
        temp_file.unlink(missing_ok=True)


@contextmanager
def _rotation_lock(state_file: Path) -> Iterator[None]:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = state_file.with_name(f".{state_file.name}.lock")
    with _PROCESS_LOCK, lock_file.open("a+b") as handle:
        if os.name != "nt":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name != "nt":
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def enqueue_with_track_rotation(
    state_file: Path,
    *,
    requested_tags: Iterable[str],
    seed: str,
    post_topic: str,
    shared_history_file: Path | None = None,
    enqueue_job: Callable[[str], _T],
) -> _T:
    """Select, enqueue and record one track as a single cross-process operation."""
    state_file = Path(state_file)
    with _rotation_lock(state_file):
        state_existed = state_file.exists()
        recent = _load_recent(state_file)
        shared_recent = load_shared_recent(shared_history_file) if shared_history_file else []
        track = select_track(requested_tags, [*recent, *shared_recent], seed=seed)
        if track is None:
            raise TrackSelectionError("no approved VK music track is available outside the last 8")
        if _normal(track.query) == _normal(post_topic):
            raise TrackSelectionError("post topic cannot be used as track_query")
        _save_recent(state_file, [*recent, track.query])
        try:
            return enqueue_job(track.query)
        except Exception:
            if state_existed:
                _save_recent(state_file, recent)
            else:
                state_file.unlink(missing_ok=True)
            raise
