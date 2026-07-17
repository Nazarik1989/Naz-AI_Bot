"""Standalone systemd oneshot producer for one Naz VK queue job."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import shutil
import stat
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import main as naz
import naz_vk_music
import vk_publish_queue


logger = logging.getLogger("NazVKProducer")
CONSUMER_ENV_FILE = Path("/etc/void-vk-publisher.env")
TIMER_UNIT_FILE = Path("/etc/systemd/system/naz-vk-producer.timer")
EXPECTED_QUEUE_DIR = Path("/var/lib/void-vk-publisher/queue")
EXPECTED_CALENDARS = (
    "OnCalendar=*-*-* 10:30:00 Europe/Moscow",
    "OnCalendar=Tue,Thu,Sun *-*-* 16:30:00 Europe/Moscow",
)


class PreflightError(RuntimeError):
    """A safe configuration error that contains no secret values."""


def _read_env_file(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise PreflightError("consumer environment must be a readable regular file")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PreflightError("consumer environment is not readable") from exc
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            values[key] = value.strip().strip("'\"")
    return values


def _permission_bits(path: Path, uid: int, gids: set[int]) -> int:
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    if info.st_uid == uid:
        return (mode >> 6) & 0o7
    if info.st_gid in gids:
        return (mode >> 3) & 0o7
    return mode & 0o7


def _service_identity(user_name: str) -> tuple[int, set[int]]:
    if os.name == "nt":
        raise PreflightError("POSIX queue permission check is unavailable")
    import grp
    import pwd

    try:
        user = pwd.getpwnam(user_name)
    except KeyError as exc:
        raise PreflightError("Naz service account does not exist") from exc
    gids = {user.pw_gid}
    gids.update(
        group.gr_gid
        for group in grp.getgrall()
        if user_name in group.gr_mem
    )
    return user.pw_uid, gids


def _validate_queue_permissions(queue_root: Path, user_name: str = "naz") -> None:
    uid, gids = _service_identity(user_name)
    pending = queue_root / "pending"
    if queue_root.is_symlink() or not queue_root.is_dir():
        raise PreflightError("shared queue must be a real directory")
    if pending.is_symlink() or not pending.is_dir():
        raise PreflightError("shared pending inbox must be a real directory")
    if _permission_bits(pending, uid, gids) & 0o3 != 0o3:
        raise PreflightError("Naz cannot write the shared pending inbox")
    if _permission_bits(queue_root, uid, gids) & 0o2:
        raise PreflightError("Naz must not write the shared queue root")
    for state_name in ("processing", "done", "failed"):
        state_path = queue_root / state_name
        if state_path.exists() and _permission_bits(state_path, uid, gids) & 0o2:
            raise PreflightError(f"Naz must not write the shared {state_name} state")
        if (
            state_name == "done"
            and state_path.exists()
            and not (_permission_bits(state_path, uid, gids) & 0o1)
        ):
            raise PreflightError(
                "Naz needs execute-only traversal of done to confirm its known job ids"
            )


def _validate_browser_isolation(profile_dir: Path, user_name: str = "naz") -> None:
    if not profile_dir.is_absolute() or not profile_dir.exists():
        raise PreflightError("publisher browser profile is missing")
    try:
        resolved = profile_dir.resolve(strict=True)
    except OSError as exc:
        raise PreflightError("publisher browser profile is unavailable") from exc
    if not resolved.is_dir():
        raise PreflightError("publisher browser profile must be a directory")
    uid, gids = _service_identity(user_name)
    chain = [resolved, *resolved.parents]
    if all(_permission_bits(path, uid, gids) & 0o1 for path in chain):
        raise PreflightError("Naz must not have access to the publisher browser profile")


def _validate_database_access(user_name: str = "naz") -> None:
    database = Path(naz.memory.DB_PATH)
    if not database.is_absolute():
        database = Path("/opt/naz-ai-bot") / database
    if database.is_symlink() or not database.is_file():
        raise PreflightError("Naz database must be an existing regular file")
    runuser = shutil.which("runuser")
    if not runuser:
        raise PreflightError("runuser is required for database permission checks")
    can_write_database = subprocess.run(
        [runuser, "-u", user_name, "--", "test", "-w", str(database)],
        check=False,
    ).returncode == 0
    can_write_parent = subprocess.run(
        [runuser, "-u", user_name, "--", "test", "-w", str(database.parent)],
        check=False,
    ).returncode == 0
    can_enter_parent = subprocess.run(
        [runuser, "-u", user_name, "--", "test", "-x", str(database.parent)],
        check=False,
    ).returncode == 0
    if not can_write_database:
        raise PreflightError("Naz service account cannot write the database")
    if not (can_write_parent and can_enter_parent):
        raise PreflightError("Naz service account cannot create SQLite sidecars")


def _validate_schedule(timer_unit: Path) -> None:
    try:
        ZoneInfo(naz.NAZ_VK_TIMEZONE)
        for value in (naz.NAZ_VK_DAILY_TIME, naz.NAZ_VK_GAMING_TIME):
            datetime.strptime(value, "%H:%M")
    except (ValueError, TypeError) as exc:
        raise PreflightError("Naz VK timezone or schedule is invalid") from exc
    if naz.NAZ_VK_TIMEZONE != "Europe/Moscow":
        raise PreflightError("Naz VK timezone must be Europe/Moscow")
    if (naz.NAZ_VK_DAILY_TIME, naz.NAZ_VK_GAMING_TIME) != ("10:30", "16:30"):
        raise PreflightError("Naz VK environment schedule does not match policy")
    if timer_unit.is_symlink() or not timer_unit.is_file():
        raise PreflightError("Naz VK timer unit must be a readable regular file")
    try:
        calendars = tuple(
            line.strip()
            for line in timer_unit.read_text(encoding="utf-8").splitlines()
            if line.strip().startswith("OnCalendar=")
        )
    except OSError as exc:
        raise PreflightError("Naz VK timer unit is not readable") from exc
    if calendars != EXPECTED_CALENDARS:
        raise PreflightError("Naz VK timer schedule does not match policy")


def check_config(
    *,
    consumer_env_file: Path = CONSUMER_ENV_FILE,
    timer_unit_file: Path = TIMER_UNIT_FILE,
) -> tuple[str, ...]:
    """Validate production readiness without generating or mutating state."""
    checks: list[str] = []
    if not naz.NAZ_VK_ENABLED:
        raise PreflightError("NAZ_VK_ENABLED is not enabled")
    if naz.NAZ_VK_SCHEDULER != "systemd":
        raise PreflightError("NAZ_VK_SCHEDULER must be systemd")
    public_id = naz.NAZ_VK_PUBLIC_ID
    if not public_id or not public_id.isdigit():
        raise PreflightError("NAZ_VK_PUBLIC_ID is missing or invalid")
    consumer_env = _read_env_file(Path(consumer_env_file))
    allowed_id = consumer_env.get("VK_GROUP_ID", "").strip()
    if not allowed_id or public_id != allowed_id:
        raise PreflightError("Naz target does not match the publisher allowlist")
    checks.append("publisher allowlist")
    profile_dir = Path(consumer_env.get("VK_BROWSER_PROFILE_DIR", "").strip())
    _validate_browser_isolation(profile_dir)
    checks.append("browser profile isolation")

    queue_root = Path(naz.NAZ_VK_QUEUE_DIR)
    if queue_root != EXPECTED_QUEUE_DIR:
        raise PreflightError("Naz queue path is not canonical")
    try:
        _validate_queue_permissions(queue_root)
    except OSError as exc:
        raise PreflightError("shared queue permissions are unavailable") from exc
    checks.append("queue write scope")
    try:
        _validate_database_access()
    except OSError as exc:
        raise PreflightError("Naz database permissions are unavailable") from exc
    checks.append("database write scope")

    if not naz.OPENROUTER_API_KEY:
        raise PreflightError("content API configuration is missing")
    if naz.IMAGE_PROVIDER not in {"openai", "bfl", "huggingface", "hf"}:
        raise PreflightError("image provider is invalid")
    if naz.IMAGE_PROVIDER == "bfl" and not naz.BFL_API_KEY:
        raise PreflightError("image API configuration is missing")
    if naz.IMAGE_PROVIDER in {"huggingface", "hf"} and not naz.HF_TOKEN:
        raise PreflightError("image API configuration is missing")
    checks.append("API configuration")

    if not naz_vk_music.APPROVED_TRACKS or not naz_vk_music.APPROVED_QUERIES:
        raise PreflightError("approved music catalog is empty")
    shared_history = queue_root / "recent-tracks.json"
    naz_vk_music.load_shared_recent(shared_history)
    private_history = Path(naz.NAZ_VK_TRACK_STATE_FILE)
    if private_history.exists():
        naz_vk_music._load_recent(private_history)
    elif not private_history.parent.is_dir():
        raise PreflightError("Naz track history parent does not exist")
    checks.append("music catalog and histories")

    if naz.NAZ_VK_IMAGE_POLICY not in {"required", "text_music"}:
        raise PreflightError("Naz VK image policy is invalid")
    if not 1 <= naz.NAZ_VK_IMAGE_ATTEMPTS <= 3:
        raise PreflightError("Naz VK image attempt limit is invalid")
    checks.append("bounded media policy")

    _validate_schedule(Path(timer_unit_file))
    checks.append("Europe/Moscow schedule")
    return tuple(checks)


def rubric_kind_for(moment: datetime) -> str:
    gaming_days = {1, 3, 6}  # datetime.weekday(): Tue, Thu, Sun
    if moment.strftime("%H:%M") == naz.NAZ_VK_GAMING_TIME and moment.weekday() in gaming_days:
        return "gaming"
    return "daily"


async def produce_one(now: datetime | None = None) -> dict:
    """Generate and enqueue exactly one job; never starts Telegram or VK clients."""
    current = now or datetime.now(ZoneInfo(naz.NAZ_VK_TIMEZONE))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo(naz.NAZ_VK_TIMEZONE))
    slot = current.strftime("%H:%M")
    rubric_kind = rubric_kind_for(current)
    source_ref = f"systemd:{current.date().isoformat()}:{rubric_kind}:{slot}"
    default_topic = (
        "Игровая лаборатория Naz VK: механика, мод, AI-инструмент или эксперимент для игроков"
        if rubric_kind == "gaming"
        else (
            "Оригинальная заметка Naz вокруг одной конкретной сцены, предмета, действия "
            "или встречи. Выбранная server-side смысловая ось задаёт предмет выпуска; "
            "AI, разработка и системы не являются обязательной темой."
        )
    )
    topic = os.getenv(
        "NAZ_VK_ONESHOT_TOPIC",
        default_topic,
    ).strip()
    return await naz.create_naz_vk_job(
        topic,
        source_ref=source_ref,
        not_before=current,
        rubric_kind=rubric_kind,
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="validate production configuration without generating or writing state",
    )
    arguments = parser.parse_args(argv)
    if arguments.check_config:
        try:
            checks = check_config()
        except (PreflightError, naz_vk_music.TrackSelectionError) as exc:
            logger.error("Naz VK preflight failed: %s", exc)
            return 1
        logger.info("Naz VK preflight OK | checks=%s", ", ".join(checks))
        return 0
    try:
        job = asyncio.run(produce_one())
    except vk_publish_queue.DuplicateJobError as exc:
        logger.info("Naz VK slot already queued: %s", exc)
        return 0
    except Exception:  # noqa: BLE001
        logger.exception("Naz VK standalone producer failed")
        return 1
    logger.info("Naz VK job queued | job_id=%s", job["job_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
