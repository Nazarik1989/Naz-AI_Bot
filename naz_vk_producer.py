"""Standalone systemd oneshot producer for one Naz VK queue job."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import main as naz
import vk_publish_queue


logger = logging.getLogger("NazVKProducer")


async def produce_one(now: datetime | None = None) -> dict:
    """Generate and enqueue exactly one job; never starts Telegram or VK clients."""
    current = now or datetime.now(ZoneInfo(naz.BOT_TIMEZONE))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo(naz.BOT_TIMEZONE))
    slot = current.strftime("%H:%M")
    source_ref = f"systemd:{current.date().isoformat()}:{slot}"
    topic = os.getenv(
        "NAZ_VK_ONESHOT_TOPIC",
        f"Naz VK, слот {slot}: практическая заметка об AI, разработке или контент-системах",
    ).strip()
    return await naz.create_naz_vk_job(topic, source_ref=source_ref, not_before=current)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
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
