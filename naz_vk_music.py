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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator


LEGACY_ROTATION_SCHEMA = "naz_vk_track_rotation.v1"
ROTATION_SCHEMA = LEGACY_ROTATION_SCHEMA
RESERVATION_SCHEMA = "naz_vk_track_reservations.v2"
TRACK_HISTORY_BACKFILL_MARKER = ".track-history-v2-complete"
SHARED_COLLISION_LIMIT = 8
RESERVATION_GRACE_SECONDS = 2 * 60 * 60
_PROCESS_LOCK = threading.RLock()


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


def _ordered_unique_queries(values: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    originals: dict[str, str] = {}
    for value in values:
        query = str(value).strip()
        key = _normal(query)
        if not key:
            continue
        if key in ordered:
            ordered.remove(key)
        ordered.append(key)
        originals[key] = query
    return [originals[key] for key in ordered]


def eligible_tracks(requested_tags: Iterable[str]) -> tuple[ApprovedTrack, ...]:
    tags = {str(tag).casefold().strip() for tag in requested_tags if str(tag).strip()}
    categories = tags.intersection({"daily", "gaming"})
    return tuple(
        track
        for track in APPROVED_TRACKS
        if not categories or track.tags.intersection(categories)
    )


def rotation_pool_size(requested_tags: Iterable[str]) -> int:
    return len(eligible_tracks(requested_tags))


def select_track(
    requested_tags: Iterable[str],
    recent_queries: Iterable[str],
    *,
    seed: str,
    hard_excluded_queries: Iterable[str] = (),
) -> ApprovedTrack | None:
    """Choose every eligible catalog track before least-recently-used reuse."""
    tags = {str(tag).casefold().strip() for tag in requested_tags if str(tag).strip()}
    semantic_tags = tags.difference({"daily", "gaming"})
    catalog = list(eligible_tracks(tags))
    history = [_normal(query) for query in _ordered_unique_queries(recent_queries)]
    hard_excluded = {
        _normal(query) for query in hard_excluded_queries if _normal(query)
    }
    used = set(history)
    candidates = [
        track
        for track in catalog
        if _normal(track.query) not in used
        and _normal(track.query) not in hard_excluded
    ]
    if not candidates:
        positions = {key: index for index, key in enumerate(history)}
        rollover_catalog = [
            track
            for track in catalog
            if _normal(track.query) not in hard_excluded
        ]
        if not rollover_catalog:
            return None
        oldest = min(
            positions.get(_normal(track.query), -1)
            for track in rollover_catalog
        )
        candidates = [
            track
            for track in rollover_catalog
            if positions.get(_normal(track.query), -1) == oldest
        ]
    semantic_matches = [
        track for track in candidates if track.tags.intersection(semantic_tags)
    ]
    ranked = semantic_matches or candidates
    return max(
        ranked,
        key=lambda track: hashlib.sha256(f"{seed}|{track.query}".encode("utf-8")).hexdigest(),
    )


@dataclass(frozen=True)
class TrackReservation:
    job_id: str
    track_query: str
    track_key: str
    source_ref: str
    reserved_at: str
    status: str = "pending"

    def to_dict(self) -> dict[str, str]:
        return {
            "job_id": self.job_id,
            "track_query": self.track_query,
            "track_key": self.track_key,
            "source_ref": self.source_ref,
            "reserved_at": self.reserved_at,
            "status": self.status,
        }


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise TrackSelectionError("VK track reservation timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise TrackSelectionError("VK track reservation timestamp needs timezone")
    return parsed.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _load_reservations(state_file: Path) -> tuple[list[TrackReservation], bool]:
    """Return active reservations and whether a legacy v1 file was read."""
    if not state_file.exists():
        return [], False
    if state_file.is_symlink() or not state_file.is_file():
        raise TrackSelectionError("VK track rotation state must be a regular file")
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TrackSelectionError("VK track rotation state is unreadable") from exc
    if not isinstance(payload, dict):
        raise TrackSelectionError("VK track rotation state has an invalid schema")

    if payload.get("schema") != LEGACY_ROTATION_SCHEMA:
        raise TrackSelectionError("VK track rotation state has an invalid schema")

    if "reservation_schema" not in payload:
        recent = payload.get("recent_queries")
        if (
            set(payload) != {"schema", "recent_queries"}
            or not isinstance(recent, list)
            or not all(isinstance(item, str) for item in recent)
        ):
            raise TrackSelectionError("legacy VK track rotation state is invalid")
        # v1 was enqueue-time history, not proof of publication. It must never
        # be promoted into the receipt-backed cooldown.
        return [], True

    if (
        payload.get("reservation_schema") != RESERVATION_SCHEMA
        or set(payload)
        != {"schema", "recent_queries", "reservation_schema", "reservations"}
        or payload.get("recent_queries") != []
        or not isinstance(payload.get("reservations"), list)
        or len(payload["reservations"]) > len(APPROVED_TRACKS)
    ):
        raise TrackSelectionError("VK track rotation state has an invalid schema")

    reservations: list[TrackReservation] = []
    job_ids: set[str] = set()
    track_keys: set[str] = set()
    required = {
        "job_id",
        "track_query",
        "track_key",
        "source_ref",
        "reserved_at",
        "status",
    }
    for raw in payload["reservations"]:
        if (
            not isinstance(raw, dict)
            or set(raw) != required
            or not all(isinstance(raw[field], str) for field in required)
        ):
            raise TrackSelectionError("VK track reservation is invalid")
        reservation = TrackReservation(**raw)
        if (
            not re.fullmatch(r"naz-[0-9a-f]{24}", reservation.job_id)
            or reservation.track_query not in APPROVED_QUERIES
            or reservation.track_key != _normal(reservation.track_query)
            or not reservation.source_ref
            or len(reservation.source_ref) > 1000
            or reservation.status != "pending"
            or reservation.job_id in job_ids
            or reservation.track_key in track_keys
        ):
            raise TrackSelectionError("VK track reservation is invalid")
        _parse_timestamp(reservation.reserved_at)
        job_ids.add(reservation.job_id)
        track_keys.add(reservation.track_key)
        reservations.append(reservation)
    return reservations, False


def validate_rotation_state(state_file: Path) -> None:
    _load_reservations(Path(state_file))


def ensure_full_history_ready(history_file: Path) -> None:
    marker = Path(history_file).parent / TRACK_HISTORY_BACKFILL_MARKER
    if marker.is_symlink() or not marker.is_file():
        raise TrackSelectionError("shared VK full track history is not ready")


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
    if (
        not isinstance(payload, dict)
        or set(payload) != {"tracks"}
        or not isinstance(payload["tracks"], list)
    ):
        raise TrackSelectionError("shared VK track history is invalid")
    keys: list[str] = []
    for item in payload["tracks"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"key"}
            or not isinstance(item["key"], str)
            or not item["key"]
            or _normal(item["key"]) != item["key"]
            or item["key"] in keys
        ):
            raise TrackSelectionError("shared VK track history is invalid")
        keys.append(item["key"])
    return keys


def _save_reservations(
    state_file: Path,
    reservations: Iterable[TrackReservation],
) -> None:
    payload = {
        "schema": ROTATION_SCHEMA,
        "recent_queries": [],
        "reservation_schema": RESERVATION_SCHEMA,
        "reservations": [item.to_dict() for item in reservations],
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


def _pending_job_exists(queue_root: Path, job_id: str) -> bool:
    pending = Path(queue_root) / "pending"
    if pending.is_symlink() or not pending.is_dir():
        raise TrackSelectionError("shared VK pending queue is unavailable")
    job_dir = pending / job_id
    if job_dir.is_symlink() or (job_dir.exists() and not job_dir.is_dir()):
        raise TrackSelectionError("reserved VK job path is invalid")
    return job_dir.is_dir()


def _reconcile_reservations(
    reservations: Iterable[TrackReservation],
    *,
    queue_root: Path,
    now: datetime,
) -> list[TrackReservation]:
    active: list[TrackReservation] = []
    grace = timedelta(seconds=RESERVATION_GRACE_SECONDS)
    for reservation in reservations:
        reserved_at = _parse_timestamp(reservation.reserved_at)
        if reserved_at > now + timedelta(minutes=5):
            raise TrackSelectionError("VK track reservation timestamp is in the future")
        if _pending_job_exists(queue_root, reservation.job_id):
            active.append(reservation)
            continue
        # processing is private to the consumer. Its systemd timeout is much
        # shorter than this grace window; afterward, only confirmed shared
        # history may spend the real cooldown.
        if now - reserved_at <= grace:
            active.append(reservation)
    return active


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
    shared_history_file: Path,
    expected_job_id: str,
    source_ref: str,
    enqueue_job: Callable[[str], dict],
) -> dict:
    """Reserve a track for enqueue without treating enqueue as publication."""
    state_file = Path(state_file)
    shared_history_file = Path(shared_history_file)
    expected_job_id = str(expected_job_id).strip()
    source_ref = str(source_ref).strip()
    if not re.fullmatch(r"naz-[0-9a-f]{24}", expected_job_id):
        raise TrackSelectionError("expected VK job_id is invalid")
    if not source_ref or len(source_ref) > 1000:
        raise TrackSelectionError("VK source_ref is invalid")
    ensure_full_history_ready(shared_history_file)

    with _rotation_lock(state_file):
        now = _utc_now()
        reservations, _legacy = _load_reservations(state_file)
        active = _reconcile_reservations(
            reservations,
            queue_root=shared_history_file.parent,
            now=now,
        )
        existing = next(
            (item for item in active if item.job_id == expected_job_id),
            None,
        )
        if existing is not None:
            _save_reservations(state_file, active)
            if existing.source_ref != source_ref:
                raise TrackSelectionError("VK job reservation source_ref mismatch")
            if not _pending_job_exists(shared_history_file.parent, expected_job_id):
                raise TrackSelectionError(
                    "VK job already has an active track reservation"
                )
            result = enqueue_job(existing.track_query)
            if (
                not isinstance(result, dict)
                or result.get("job_id") != expected_job_id
                or result.get("track_query") != existing.track_query
            ):
                raise TrackSelectionError(
                    "enqueued VK job does not match its reservation"
                )
            return result

        shared_recent = load_shared_recent(shared_history_file)
        track = select_track(
            requested_tags,
            shared_recent,
            seed=seed,
            hard_excluded_queries=[
                *shared_recent[-SHARED_COLLISION_LIMIT:],
                *(item.track_query for item in active),
            ],
        )
        if track is None:
            _save_reservations(state_file, active)
            raise TrackSelectionError("no approved VK music track is available")
        if _normal(track.query) == _normal(post_topic):
            raise TrackSelectionError("post topic cannot be used as track_query")

        reservation = TrackReservation(
            job_id=expected_job_id,
            track_query=track.query,
            track_key=_normal(track.query),
            source_ref=source_ref,
            reserved_at=now.isoformat().replace("+00:00", "Z"),
        )
        reserved = [*active, reservation]
        _save_reservations(state_file, reserved)
        try:
            result = enqueue_job(track.query)
        except Exception:
            if not _pending_job_exists(shared_history_file.parent, expected_job_id):
                _save_reservations(state_file, active)
            raise
        if (
            not isinstance(result, dict)
            or result.get("job_id") != expected_job_id
            or result.get("track_query") != track.query
        ):
            raise TrackSelectionError("enqueued VK job does not match its reservation")
        return result
