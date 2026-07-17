"""Filesystem producer for the shared VK Publisher queue.

This module deliberately contains no VK, browser, or authentication code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import errno
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional, Union

from naz_vk_music import APPROVED_QUERIES


SCHEMA = "vk_publish_job.v1"
PRODUCER = "naz"
CONSUMER_STATES = ("pending", "processing", "done", "failed")
JOB_FIELDS = frozenset({
    "schema", "job_id", "producer", "target_group_id", "text", "media",
    "track_query", "created_at", "not_before", "dedupe_key", "source_ref",
})
MAX_TEXT_LENGTH = 16_000
MAX_MEDIA_COUNT = 4
MAX_MEDIA_BYTES = 15 * 1024 * 1024
MAX_TRACK_QUERY_LENGTH = 300
MAX_DEDUPE_KEY_LENGTH = 256
_SAFE_DEDUPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


class QueueError(RuntimeError):
    pass


class DuplicateJobError(QueueError):
    pass


@dataclass(frozen=True)
class MediaInput:
    filename: str
    content: Union[bytes, Path]


def stable_dedupe_key(*, target_group_id: str, text: str, source_ref: str) -> str:
    canonical = json.dumps(
        [PRODUCER, str(target_group_id), text.strip(), source_ref.strip()],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(value: Optional[datetime]) -> str:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise QueueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_media_name(name: str) -> str:
    if not name or "\\" in name or "://" in name:
        raise QueueError(f"unsafe media path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or len(path.parts) != 1 or any(part in {"", ".", ".."} for part in path.parts):
        raise QueueError(f"unsafe media path: {name!r}")
    return name


def canonical_job_id(dedupe_key: str) -> str:
    digest = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()
    return f"naz-{digest[:24]}"


def normalize_track_query(value: str) -> str:
    """Match the shared consumer's semantic key for global track rotation."""
    return " ".join(re.findall(r"[0-9a-zа-яё]+", str(value).casefold()))


def _require_pending(queue_root: Path) -> Path:
    """Return the deployment-owned inbox without listing or mutating it."""
    pending = queue_root / "pending"
    if pending.is_symlink() or not pending.is_dir():
        raise QueueError(f"pending must be an existing real directory: {pending}")
    return pending


def _media_size(item: MediaInput) -> int:
    if isinstance(item.content, bytes):
        return len(item.content)
    source = Path(item.content)
    if source.is_symlink() or not source.is_file():
        raise QueueError(f"media source must be a regular file: {source}")
    return source.stat().st_size


def validate_canonical_job(
    job: dict,
    job_dir: Path,
    *,
    allowed_group_id: str | None = None,
    recent_track_keys: Iterable[str] = (),
) -> None:
    """Apply the shared VOID consumer contract to a materialized job."""
    if not isinstance(job, dict) or len(job) != 11 or frozenset(job) != JOB_FIELDS:
        raise QueueError("job must contain exactly 11 canonical fields")
    if job["schema"] != SCHEMA or job["producer"] != PRODUCER:
        raise QueueError("invalid schema or producer")
    if not isinstance(job["target_group_id"], str) or not job["target_group_id"]:
        raise QueueError("target_group_id must be a non-empty JSON string")
    if allowed_group_id is not None and job["target_group_id"] != str(allowed_group_id):
        raise QueueError("target_group_id is not allowed")
    if not isinstance(job["text"], str) or not job["text"] or len(job["text"]) > MAX_TEXT_LENGTH:
        raise QueueError("invalid text")
    if (
        not isinstance(job["track_query"], str)
        or not job["track_query"].strip()
        or len(job["track_query"]) > MAX_TRACK_QUERY_LENGTH
        or job["track_query"] not in APPROVED_QUERIES
    ):
        raise QueueError("invalid track_query")
    key = job["dedupe_key"]
    if not isinstance(key, str) or len(key) > MAX_DEDUPE_KEY_LENGTH or not _SAFE_DEDUPE.fullmatch(key):
        raise QueueError("invalid dedupe_key")
    if job["job_id"] != canonical_job_id(key):
        raise QueueError("job_id does not match dedupe_key")
    for field in ("created_at", "not_before"):
        value = job[field]
        if not isinstance(value, str) or not value:
            raise QueueError(f"invalid {field}")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise QueueError(f"invalid {field}") from exc
        if parsed.tzinfo is None:
            raise QueueError(f"{field} must include timezone")
    if not isinstance(job["source_ref"], str) or not job["source_ref"]:
        raise QueueError("source_ref is required")
    track_key = normalize_track_query(job["track_query"])
    if not track_key or track_key in {str(item) for item in recent_track_keys}:
        raise QueueError("track_query was used in the shared last 8")
    media = job["media"]
    if not isinstance(media, list) or len(media) > MAX_MEDIA_COUNT or len(media) != len(set(media)):
        raise QueueError("invalid media list")
    for raw_name in media:
        if not isinstance(raw_name, str):
            raise QueueError("media paths must be strings")
        name = _safe_media_name(raw_name)
        path = Path(job_dir) / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MEDIA_BYTES:
            raise QueueError(f"invalid media file: {name}")


def enqueue(
    queue_root: Path,
    *,
    target_group_id: str,
    text: str,
    source_ref: str,
    media: Iterable[MediaInput] = (),
    track_query: str = "",
    created_at: Optional[datetime] = None,
    not_before: Optional[datetime] = None,
    dedupe_key: Optional[str] = None,
) -> dict:
    """Validate and atomically add one Naz job to ``queue_root/pending``."""
    queue_root = Path(queue_root)
    target_group_id = str(target_group_id).strip()
    text = text.strip()
    source_ref = source_ref.strip()
    track_query = track_query.strip()
    if not target_group_id or not text or not source_ref or not track_query:
        raise QueueError("target_group_id, text, source_ref and track_query are required")
    if len(text) > MAX_TEXT_LENGTH:
        raise QueueError(f"text exceeds {MAX_TEXT_LENGTH} characters")
    if len(track_query) > MAX_TRACK_QUERY_LENGTH:
        raise QueueError(f"track_query exceeds {MAX_TRACK_QUERY_LENGTH} characters")
    if track_query not in APPROVED_QUERIES:
        raise QueueError("track_query is not in the approved Naz VK music catalog")
    items = list(media)
    if len(items) > MAX_MEDIA_COUNT:
        raise QueueError(f"media exceeds {MAX_MEDIA_COUNT} files")
    names = [_safe_media_name(item.filename) for item in items]
    if len(names) != len(set(names)):
        raise QueueError("media filenames must be unique")
    for item in items:
        if _media_size(item) > MAX_MEDIA_BYTES:
            raise QueueError(f"media file exceeds {MAX_MEDIA_BYTES} bytes")
    key = dedupe_key or stable_dedupe_key(
        target_group_id=target_group_id, text=text, source_ref=source_ref
    )
    if len(key) > MAX_DEDUPE_KEY_LENGTH:
        raise QueueError(f"dedupe_key exceeds {MAX_DEDUPE_KEY_LENGTH} characters")
    if not _SAFE_DEDUPE.fullmatch(key):
        raise QueueError("dedupe_key contains unsafe characters")
    job_id = canonical_job_id(key)
    if os.name != "nt":
        os.umask(0o027)
    pending = _require_pending(queue_root)
    final_dir = pending / job_id
    if final_dir.exists():
        raise DuplicateJobError(f"job {job_id} already exists in pending")
    temp_dir = Path(tempfile.mkdtemp(prefix=f".{job_id}-", dir=pending))
    try:
        # Do not chmod the inherited SGID directory before creating files:
        # RestrictSUIDSGID forbids setting SGID, while clearing it would make
        # media and job.json inherit the producer's private primary group.
        for item, name in zip(items, names):
            destination = temp_dir / name
            if isinstance(item.content, bytes):
                destination.write_bytes(item.content)
            else:
                source = Path(item.content)
                shutil.copyfile(source, destination, follow_symlinks=False)
            if destination.stat().st_size > MAX_MEDIA_BYTES:
                raise QueueError(f"media file exceeds {MAX_MEDIA_BYTES} bytes")
            os.chmod(destination, 0o640)
        job = {
            "schema": SCHEMA,
            "job_id": job_id,
            "producer": PRODUCER,
            "target_group_id": target_group_id,
            "text": text,
            "media": names,
            "track_query": track_query,
            "created_at": _iso(created_at),
            "not_before": _iso(not_before or created_at),
            "dedupe_key": key,
            "source_ref": source_ref,
        }
        job_file = temp_dir / "job.json"
        if frozenset(job) != JOB_FIELDS or len(job) != 11:
            raise QueueError("job must contain exactly 11 canonical fields")
        job_file.write_text(
            json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(job_file, 0o640)
        validate_canonical_job(job, temp_dir)
        # Files already inherited the shared group. Open the completed hidden
        # directory to the consumer immediately before the atomic rename.
        os.chmod(temp_dir, 0o770)
        try:
            os.replace(temp_dir, final_dir)
        except OSError as exc:
            if final_dir.exists() or exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                raise DuplicateJobError(f"job {job_id} was enqueued concurrently") from exc
            raise
        return job
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def queue_status(queue_root: Path) -> dict:
    root = Path(queue_root)
    pending = _require_pending(root)
    return {"path": str(pending), "ready": True}


def completed_naz_job(queue_root: Path, job_id: str) -> dict | None:
    """Confirm one known Naz job without listing any private consumer state."""
    clean_job_id = str(job_id or "").strip()
    if not re.fullmatch(r"naz-[0-9a-f]{24}", clean_job_id):
        return None
    done = Path(queue_root) / "done"
    job_dir = done / clean_job_id
    job_file = job_dir / "job.json"
    if (
        done.is_symlink()
        or job_dir.is_symlink()
        or not job_dir.is_dir()
        or job_file.is_symlink()
        or not job_file.is_file()
    ):
        return None
    try:
        job = json.loads(job_file.read_text(encoding="utf-8"))
        validate_canonical_job(job, job_dir)
    except (OSError, ValueError, QueueError):
        return None
    if job.get("producer") != PRODUCER or job.get("job_id") != clean_job_id:
        return None
    return job
