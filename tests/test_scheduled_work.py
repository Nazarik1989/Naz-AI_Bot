import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
import naz_vk_producer
import scheduled_work


class ScheduledWorkTests(unittest.TestCase):
    def test_marker_contract_enumerates_every_scheduled_route(self):
        expected = {
            "telegram_autopost",
            "crosspost_exchange",
            "source_monitor",
            "agent_content_sync",
            "vk_embedded_producer",
            "vk_systemd_producer",
            "vk_receipt_sync",
        }
        self.assertEqual(scheduled_work.SCHEDULED_WORK_LABELS, expected)
        tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
        decorated = {
            decorator.args[0].value
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for decorator in node.decorator_list
            if isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Name)
            and decorator.func.id == "scheduled_work_marker"
            and decorator.args
            and isinstance(decorator.args[0], ast.Constant)
        }
        self.assertEqual(decorated, expected - {"vk_systemd_producer"})
        producer_source = Path(naz_vk_producer.__file__).read_text(encoding="utf-8")
        self.assertIn('"vk_systemd_producer"', producer_source)

    def test_resolved_snapshot_contains_only_public_schedule_values(self):
        snapshot = scheduled_work.resolved_schedule_snapshot(
            telegram_timezone="Europe/Moscow",
            telegram_times="10:00,14:00,18:00,22:00",
            vk_timezone="Europe/Moscow",
            vk_daily_time="10:30",
            vk_gaming_time="16:30",
        )
        self.assertEqual(set(snapshot), {"telegram", "vk"})
        self.assertEqual(
            snapshot["telegram"],
            {
                "timezone": "Europe/Moscow",
                "slots": ("10:00", "14:00", "18:00", "22:00"),
            },
        )
        self.assertEqual(snapshot["vk"]["daily"], "10:30")
        self.assertEqual(snapshot["vk"]["gaming"], "16:30")
        rendered = repr(snapshot).casefold()
        for forbidden in ("token", "cookie", "password", "database", "prompt"):
            self.assertNotIn(forbidden, rendered)

    def test_runtime_snapshot_uses_resolved_naz_schedule(self):
        snapshot = main.resolved_naz_schedule_snapshot()
        self.assertEqual(snapshot["telegram"]["timezone"], main.BOT_TIMEZONE)
        self.assertEqual(snapshot["vk"]["timezone"], main.NAZ_VK_TIMEZONE)
        self.assertEqual(snapshot["vk"]["daily"], main.NAZ_VK_DAILY_TIME)
        self.assertEqual(snapshot["vk"]["gaming"], main.NAZ_VK_GAMING_TIME)

    def test_deploy_snapshot_matches_canonical_schedule_only_schema(self):
        snapshot = main.resolved_naz_deploy_schedule_snapshot()
        self.assertEqual(set(snapshot), {"naz.telegram", "naz.vk"})
        self.assertEqual(
            snapshot["naz.telegram"],
            {
                "daily_times": ("10:00", "14:00", "18:00", "22:00"),
                "weekly_times": (),
            },
        )
        self.assertEqual(
            snapshot["naz.vk"],
            {
                "daily_times": ("10:30",),
                "weekly_times": (((1, 3, 6), "16:30"),),
            },
        )
        rendered = repr(snapshot).casefold()
        for forbidden in ("token", "cookie", "password", "database", "prompt"):
            self.assertNotIn(forbidden, rendered)

    def test_marker_is_visible_only_while_work_is_in_flight(self):
        with tempfile.TemporaryDirectory() as root:
            marker_root = Path(root)
            self.assertEqual(scheduled_work.active_work(marker_root), ())
            with scheduled_work.work_marker(marker_root, "telegram_autopost"):
                active = scheduled_work.active_work(marker_root)
                self.assertEqual(len(active), 1)
                self.assertEqual(active[0]["label"], "telegram_autopost")
                self.assertIsInstance(active[0]["pid"], int)
            self.assertEqual(scheduled_work.active_work(marker_root), ())

    def test_main_exposes_only_safe_active_marker_fields(self):
        with tempfile.TemporaryDirectory() as root:
            marker_root = Path(root)
            original = main.NAZ_SCHEDULED_WORK_DIR
            main.NAZ_SCHEDULED_WORK_DIR = marker_root
            try:
                with scheduled_work.work_marker(marker_root, "source_monitor"):
                    active = main.active_naz_scheduled_work()
            finally:
                main.NAZ_SCHEDULED_WORK_DIR = original
            self.assertEqual(set(active[0]), {"label", "pid", "started_at"})

    def test_marker_is_removed_after_failure(self):
        with tempfile.TemporaryDirectory() as root:
            marker_root = Path(root)
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with scheduled_work.work_marker(marker_root, "vk_systemd_producer"):
                    raise RuntimeError("boom")
            self.assertEqual(scheduled_work.active_work(marker_root), ())

    def test_sigkill_stale_marker_is_not_reported_as_active(self):
        with tempfile.TemporaryDirectory() as root:
            marker_root = Path(root)
            marker = marker_root / ".scheduled-work-telegram_autopost.999999999.dead.json"
            marker.write_text(
                json.dumps(
                    {
                        "schema": "naz_scheduled_work.v2",
                        "label": "telegram_autopost",
                        "pid": 999999999,
                        "process_start_id": "linux:old",
                        "started_at": "2026-07-22T10:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(scheduled_work.active_work(marker_root), ())

    def test_pid_reuse_does_not_revive_stale_marker_owner(self):
        payload = {
            "pid": 1234,
            "process_start_id": "linux:old",
        }
        with (
            patch.object(scheduled_work, "_pid_is_alive", return_value=True),
            patch.object(scheduled_work, "_process_start_id", return_value="linux:new"),
        ):
            self.assertFalse(scheduled_work._live_owner(payload))


if __name__ == "__main__":
    unittest.main()
