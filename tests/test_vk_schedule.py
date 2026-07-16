import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main


class FakeJobQueue:
    def __init__(self):
        self.jobs = []

    def run_daily(self, callback, **kwargs):
        self.jobs.append((callback, kwargs))


class VkScheduleTests(unittest.TestCase):
    def test_disabled_flags_do_not_schedule_jobs(self):
        for enabled, auto_on in ((False, False), (True, False), (False, True)):
            with self.subTest(enabled=enabled, auto_on=auto_on):
                queue = FakeJobQueue()
                app = SimpleNamespace(job_queue=queue)
                with patch.multiple(
                    main,
                    NAZ_VK_ENABLED=enabled,
                    NAZ_VK_AUTO_ON=auto_on,
                    NAZ_VK_PUBLIC_ID="123",
                    NAZ_VK_SCHEDULER="telegram",
                ):
                    main.setup_naz_vk_schedule(app)
                self.assertEqual(queue.jobs, [])

    def test_enabled_schedule_registers_daily_and_gaming_slots(self):
        queue = FakeJobQueue()
        app = SimpleNamespace(job_queue=queue)
        with patch.multiple(
            main,
            NAZ_VK_ENABLED=True,
            NAZ_VK_AUTO_ON=True,
            NAZ_VK_PUBLIC_ID="123",
            NAZ_VK_DAILY_TIME="10:30",
            NAZ_VK_GAMING_TIME="16:30",
            NAZ_VK_SCHEDULER="telegram",
        ):
            main.setup_naz_vk_schedule(app)
        self.assertEqual(
            [job[1]["data"] for job in queue.jobs],
            [
                {"slot": "10:30", "rubric_kind": "daily"},
                {"slot": "16:30", "rubric_kind": "gaming"},
            ],
        )
        self.assertNotIn("days", queue.jobs[0][1])
        self.assertEqual(queue.jobs[1][1]["days"], (2, 4, 0))
        self.assertEqual(str(queue.jobs[0][1]["time"].tzinfo), "Europe/Moscow")
        self.assertEqual(str(queue.jobs[1][1]["time"].tzinfo), "Europe/Moscow")

    def test_schedule_cooldown_uses_same_daily_source_ref(self):
        context = SimpleNamespace(
            job=SimpleNamespace(data={"slot": "10:30", "rubric_kind": "daily"}),
            bot=SimpleNamespace(),
        )
        create = AsyncMock(side_effect=[{"job_id": "one"}, main.vk_publish_queue.DuplicateJobError()])
        with patch.multiple(
            main, NAZ_VK_ENABLED=True, NAZ_VK_AUTO_ON=True, NAZ_VK_SCHEDULER="telegram"
        ), patch.object(
            main, "create_naz_vk_job", create
        ):
            asyncio.run(main.naz_vk_queue_job(context))
            asyncio.run(main.naz_vk_queue_job(context))
        first = create.await_args_list[0].kwargs["source_ref"]
        second = create.await_args_list[1].kwargs["source_ref"]
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(":daily:10:30"))
        self.assertEqual(create.await_args_list[0].kwargs["rubric_kind"], "daily")

    def test_systemd_mode_disables_embedded_schedule(self):
        queue = FakeJobQueue()
        with patch.multiple(
            main,
            NAZ_VK_ENABLED=True,
            NAZ_VK_AUTO_ON=True,
            NAZ_VK_PUBLIC_ID="123",
            NAZ_VK_SCHEDULER="systemd",
        ):
            main.setup_naz_vk_schedule(SimpleNamespace(job_queue=queue))
        self.assertEqual(queue.jobs, [])


if __name__ == "__main__":
    unittest.main()
