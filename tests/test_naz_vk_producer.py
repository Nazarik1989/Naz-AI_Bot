import asyncio
import importlib
import inspect
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
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
        timer = Path("deploy/systemd/naz-vk-producer.timer").read_text(encoding="utf-8")
        calendars = [line for line in timer.splitlines() if line.startswith("OnCalendar=")]
        self.assertEqual(
            calendars,
            [
                "OnCalendar=*-*-* 10:30:00 Europe/Moscow",
                "OnCalendar=Tue,Thu,Sun *-*-* 16:30:00 Europe/Moscow",
            ],
        )

    def test_service_reads_only_canonical_naz_environment(self):
        service = Path("deploy/systemd/naz-vk-producer.service").read_text(encoding="utf-8")
        environment_files = [
            line for line in service.splitlines() if line.startswith("EnvironmentFile=")
        ]
        self.assertEqual(environment_files, ["EnvironmentFile=/opt/naz-ai-bot/.env"])
        self.assertNotIn("/etc/naz-ai-bot/naz.env", service)
        self.assertIn("Environment=NAZ_ENV_LOADED_BY_SYSTEMD=1", service)

    def test_check_config_is_read_only_and_complete(self):
        import naz_vk_producer

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            queue = base / "queue"
            for state in ("pending", "processing", "done", "failed"):
                (queue / state).mkdir(parents=True, exist_ok=True)
            (queue / "recent-tracks.json").write_text(
                '{"tracks":[{"key":"already used"}]}\n',
                encoding="utf-8",
            )
            (queue / main.naz_vk_music.TRACK_HISTORY_BACKFILL_MARKER).write_text(
                "ready\n",
                encoding="utf-8",
            )
            consumer_env = base / "consumer.env"
            consumer_env.write_text(
                f"VK_GROUP_ID=123\nVK_BROWSER_PROFILE_DIR={base / 'profile'}\n",
                encoding="utf-8",
            )
            (base / "profile").mkdir()
            timer = base / "naz-vk-producer.timer"
            timer.write_text(
                Path("deploy/systemd/naz-vk-producer.timer").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            track_state = base / "naz-data" / "rotation.json"
            track_state.parent.mkdir()
            before = {
                path.relative_to(base).as_posix(): path.read_bytes()
                for path in base.rglob("*")
                if path.is_file()
            }
            with patch.multiple(
                naz_vk_producer.naz,
                NAZ_VK_ENABLED=True,
                NAZ_VK_SCHEDULER="systemd",
                NAZ_VK_PUBLIC_ID="123",
                NAZ_VK_QUEUE_DIR=queue,
                NAZ_VK_TRACK_STATE_FILE=track_state,
                NAZ_VK_TIMEZONE="Europe/Moscow",
                NAZ_VK_DAILY_TIME="10:30",
                NAZ_VK_GAMING_TIME="16:30",
                OPENROUTER_API_KEY="configured",
                IMAGE_PROVIDER="openai",
                NAZ_VK_IMAGE_POLICY="required",
                NAZ_VK_IMAGE_ATTEMPTS=2,
            ), patch.object(
                naz_vk_producer, "EXPECTED_QUEUE_DIR", queue
            ), patch.object(
                naz_vk_producer, "_validate_queue_permissions"
            ) as permissions, patch.object(
                naz_vk_producer, "_validate_browser_isolation"
            ) as browser_isolation, patch.object(
                naz_vk_producer, "_validate_database_access"
            ) as database_access, patch.object(
                naz_vk_producer.naz.memory, "init_db"
            ) as init_db, patch.object(
                naz_vk_producer.naz, "create_naz_vk_job", new=AsyncMock()
            ) as create_job:
                checks = naz_vk_producer.check_config(
                    consumer_env_file=consumer_env,
                    timer_unit_file=timer,
                )
            permissions.assert_called_once_with(queue)
            browser_isolation.assert_called_once_with(base / "profile")
            database_access.assert_called_once_with()
            init_db.assert_not_called()
            create_job.assert_not_awaited()
            self.assertEqual(
                checks,
                (
                    "publisher allowlist",
                    "browser profile isolation",
                    "queue write scope",
                    "database write scope",
                    "API configuration",
                    "music catalog and histories",
                    "bounded media policy",
                    "Europe/Moscow schedule",
                ),
            )
            after = {
                path.relative_to(base).as_posix(): path.read_bytes()
                for path in base.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_check_config_blocks_missing_public_id(self):
        import naz_vk_producer

        with patch.object(naz_vk_producer.naz, "NAZ_VK_ENABLED", True), patch.object(
            naz_vk_producer.naz, "NAZ_VK_SCHEDULER", "systemd"
        ), patch.object(naz_vk_producer.naz, "NAZ_VK_PUBLIC_ID", ""):
            with self.assertRaisesRegex(naz_vk_producer.PreflightError, "PUBLIC_ID"):
                naz_vk_producer.check_config()

    def test_check_config_cli_never_calls_producer(self):
        import naz_vk_producer

        with patch.object(
            naz_vk_producer, "check_config", return_value=("safe",)
        ), patch.object(
            naz_vk_producer, "produce_one", new=AsyncMock()
        ) as produce:
            self.assertEqual(naz_vk_producer.main(["--check-config"]), 0)
        produce.assert_not_awaited()

    def test_entrypoint_has_no_browser_or_playwright_path(self):
        import naz_vk_producer

        source = inspect.getsource(naz_vk_producer).lower()
        self.assertNotIn("playwright", source)
        self.assertNotIn("cookie", source)
        self.assertNotIn("run_polling", source)
        service = Path("deploy/systemd/naz-vk-producer.service").read_text(encoding="utf-8")
        writable = [
            line for line in service.splitlines() if line.startswith("ReadWritePaths=")
        ]
        self.assertEqual(len(writable), 1)
        self.assertIn("/opt/naz-ai-bot", writable[0])
        self.assertIn("/var/lib/void-vk-publisher/queue/pending", writable[0])
        self.assertNotIn("profile", writable[0].casefold())


if __name__ == "__main__":
    unittest.main()
