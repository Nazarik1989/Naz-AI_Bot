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
                ):
                    main.setup_naz_vk_schedule(app)
                self.assertEqual(queue.jobs, [])

    def test_enabled_schedule_registers_configured_times(self):
        queue = FakeJobQueue()
        app = SimpleNamespace(job_queue=queue)
        with patch.multiple(
            main,
            NAZ_VK_ENABLED=True,
            NAZ_VK_AUTO_ON=True,
            NAZ_VK_PUBLIC_ID="123",
            NAZ_VK_AUTO_TIMES="01:02,23:59",
        ):
            main.setup_naz_vk_schedule(app)
        self.assertEqual([job[1]["data"]["slot"] for job in queue.jobs], ["01:02", "23:59"])

    def test_schedule_cooldown_uses_same_daily_source_ref(self):
        context = SimpleNamespace(
            job=SimpleNamespace(data={"slot": "11:20"}),
            bot=SimpleNamespace(),
        )
        create = AsyncMock(side_effect=[{"job_id": "one"}, main.vk_publish_queue.DuplicateJobError()])
        with patch.multiple(main, NAZ_VK_ENABLED=True, NAZ_VK_AUTO_ON=True), patch.object(
            main, "create_naz_vk_job", create
        ):
            asyncio.run(main.naz_vk_queue_job(context))
            asyncio.run(main.naz_vk_queue_job(context))
        first = create.await_args_list[0].kwargs["source_ref"]
        second = create.await_args_list[1].kwargs["source_ref"]
        self.assertEqual(first, second)
        self.assertTrue(first.endswith(":11:20"))


if __name__ == "__main__":
    unittest.main()
