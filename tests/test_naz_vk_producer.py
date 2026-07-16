import asyncio
import importlib
import inspect
import sys
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import main


class StandaloneNazVkProducerTests(unittest.TestCase):
    def test_import_does_not_build_or_run_telegram(self):
        sys.modules.pop("naz_vk_producer", None)
        with patch.object(main, "build_application") as build:
            module = importlib.import_module("naz_vk_producer")
        build.assert_not_called()
        self.assertTrue(callable(module.produce_one))

    def test_one_invocation_calls_shared_creator_once(self):
        import naz_vk_producer

        create = AsyncMock(return_value={"job_id": "naz-one"})
        moment = datetime(2026, 7, 12, 10, 30, tzinfo=ZoneInfo("Europe/Moscow"))
        with patch.object(naz_vk_producer.naz, "create_naz_vk_job", create), patch.object(
            naz_vk_producer.naz, "NAZ_VK_SCHEDULER", "off"
        ):
            result = asyncio.run(naz_vk_producer.produce_one(moment))
        self.assertEqual(result["job_id"], "naz-one")
        create.assert_awaited_once()
        self.assertEqual(create.await_args.kwargs["source_ref"], "systemd:2026-07-12:daily:10:30")
        self.assertEqual(create.await_args.kwargs["rubric_kind"], "daily")

    def test_tuesday_gaming_slot_uses_gaming_rubric(self):
        import naz_vk_producer

        create = AsyncMock(return_value={"job_id": "naz-game"})
        moment = datetime(2026, 7, 14, 16, 30, tzinfo=ZoneInfo("Europe/Moscow"))
        with patch.object(naz_vk_producer.naz, "create_naz_vk_job", create), patch.object(
            naz_vk_producer.naz, "NAZ_VK_GAMING_TIME", "16:30"
        ):
            asyncio.run(naz_vk_producer.produce_one(moment))
        self.assertEqual(create.await_args.kwargs["rubric_kind"], "gaming")
        self.assertEqual(create.await_args.kwargs["source_ref"], "systemd:2026-07-14:gaming:16:30")

    def test_systemd_timer_has_only_requested_vk_schedule(self):
        from pathlib import Path

        timer = Path("deploy/systemd/naz-vk-producer.timer").read_text(encoding="utf-8")
        calendars = [line for line in timer.splitlines() if line.startswith("OnCalendar=")]
        self.assertEqual(
            calendars,
            [
                "OnCalendar=*-*-* 10:30:00 Europe/Moscow",
                "OnCalendar=Tue,Thu,Sun *-*-* 16:30:00 Europe/Moscow",
            ],
        )

    def test_entrypoint_has_no_browser_or_playwright_path(self):
        import naz_vk_producer

        source = inspect.getsource(naz_vk_producer).lower()
        self.assertNotIn("playwright", source)
        self.assertNotIn("browser_profile", source)
        self.assertNotIn("cookie", source)
        self.assertNotIn("run_polling", source)


if __name__ == "__main__":
    unittest.main()
