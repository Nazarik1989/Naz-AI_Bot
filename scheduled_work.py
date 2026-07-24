"""Safe in-flight markers for deploy coordination.

Markers contain only a bounded job name, PID and timestamp. They never contain
environment values, prompts, post text or queue payloads.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

try:  # Production is Linux; Windows tests use PID/start-identity fallback.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - platform dependent
    _fcntl = None


_SAFE_LABEL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SCHEDULED_WORK_LABELS = frozenset(
    {
        "telegram_autopost",
        "crosspost_exchange",
        "source_monitor",
        "agent_content_sync",
        "story_private_delivery",
        "vk_embedded_producer",
        "vk_systemd_producer",
        "vk_receipt_sync",
    }
)


def _process_start_id(pid: int) -> str:
    """Return Linux's non-secret process start tick to reject PID reuse."""
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return ""
    closing = raw.rfind(")")
    if closing < 0:
        return ""
    fields = raw[closing + 1 :].split()
    return f"linux:{fields[19]}" if len(fields) > 19 else ""


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


_PROCESS_START_ID = _process_start_id(os.getpid()) or f"runtime:{uuid.uuid4().hex}"


def _live_owner(payload: dict[str, object]) -> bool:
    pid = payload.get("pid")
    expected = payload.get("process_start_id")
    if not isinstance(pid, int) or isinstance(pid, bool) or not isinstance(expected, str):
        return False
    if not _pid_is_alive(pid):
        return False
    observed = _process_start_id(pid)
    if observed:
        return observed == expected
    if pid == os.getpid():
        return expected == _PROCESS_START_ID
    # On a platform without process-start metadata, a live foreign PID is the
    # conservative answer. Linux production always takes the branch above.
    return True


def _marker_lock_is_held(path: Path, payload: dict[str, object]) -> bool:
    if _fcntl is None:
        return _live_owner(payload)
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDWR)
        try:
            _fcntl.flock(descriptor, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        return False
    except OSError:
        # If the preflight user cannot inspect the lock, require a matching
        # live process identity instead of treating unknown as idle.
        return _live_owner(payload)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def resolved_schedule_snapshot(
    *,
    telegram_timezone: str,
    telegram_times: str,
    vk_timezone: str,
    vk_daily_time: str,
    vk_gaming_time: str,
) -> dict[str, object]:
    """Return the complete public cadence without exposing other env values."""
    telegram_slots = tuple(
        item.strip() for item in telegram_times.split(",") if item.strip()
    )
    return {
        "telegram": {
            "timezone": str(telegram_timezone),
            "slots": telegram_slots,
        },
        "vk": {
            "timezone": str(vk_timezone),
            "daily": str(vk_daily_time),
            "gaming": str(vk_gaming_time),
            "gaming_days": ("Tuesday", "Thursday", "Sunday"),
        },
    }


@contextmanager
def work_marker(root: Path, label: str) -> Iterator[Path]:
    clean_label = str(label).strip().lower()
    if (
        not _SAFE_LABEL.fullmatch(clean_label)
        or clean_label not in SCHEDULED_WORK_LABELS
    ):
        raise ValueError("invalid scheduled work label")
    marker_root = Path(root)
    marker_root.mkdir(parents=True, exist_ok=True)
    marker = marker_root / (
        f".scheduled-work-{clean_label}.{os.getpid()}.{uuid.uuid4().hex}.json"
    )
    payload = {
        "schema": "naz_scheduled_work.v2",
        "label": clean_label,
        "pid": os.getpid(),
        "process_start_id": _PROCESS_START_ID,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    descriptor, raw_path = tempfile.mkstemp(prefix=".scheduled-", dir=marker_root)
    os.close(descriptor)
    temporary = Path(raw_path)
    lock_descriptor = -1
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o640)
        if _fcntl is not None:
            lock_descriptor = os.open(temporary, os.O_RDWR)
            _fcntl.flock(lock_descriptor, _fcntl.LOCK_EX)
        os.replace(temporary, marker)
        yield marker
    finally:
        if lock_descriptor >= 0:
            _fcntl.flock(lock_descriptor, _fcntl.LOCK_UN)
            os.close(lock_descriptor)
        temporary.unlink(missing_ok=True)
        marker.unlink(missing_ok=True)


def active_work(root: Path) -> tuple[dict[str, object], ...]:
    """Read safe markers for a coordinated read-only deploy preflight."""
    marker_root = Path(root)
    if not marker_root.exists():
        return ()
    if marker_root.is_symlink() or not marker_root.is_dir():
        raise ValueError("scheduled work marker directory is unavailable")
    result = []
    for path in sorted(marker_root.glob(".scheduled-work-*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if (
            isinstance(payload, dict)
            and payload.get("schema") == "naz_scheduled_work.v2"
            and _SAFE_LABEL.fullmatch(str(payload.get("label") or ""))
            and isinstance(payload.get("pid"), int)
            and not isinstance(payload.get("pid"), bool)
            and isinstance(payload.get("process_start_id"), str)
            and isinstance(payload.get("started_at"), str)
            and _marker_lock_is_held(path, payload)
        ):
            result.append(
                {
                    "label": payload["label"],
                    "pid": payload["pid"],
                    "started_at": payload["started_at"],
                }
            )
    return tuple(result)
