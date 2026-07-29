import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import main
import memory
import naz_vk_music
import editorial_orchestrator


class NazVkJobTests(unittest.TestCase):
    def setUp(self):
        self.db_directory = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            memory,
            "DB_PATH",
            str(Path(self.db_directory.name) / "test.sqlite3"),
        )
        self.db_patch.start()
        memory.init_db()
        original_scheduled_plan = main.scheduled_plan

        def deterministic_plan(**kwargs):
            rows = list(kwargs["rubric_rows"])
            source = dict(list(kwargs["source_rows"])[0])
            source_ref = str(source.get("source_ref", ""))
            if "material" in source_ref:
                selected = next(
                    row for row in rows
                    if "material" in str(row["name"]).casefold()
                    or "матери" in str(row["name"]).casefold()
                )
            else:
                selected = rows[0]
            selected = dict(selected)
            key = main.naz_editorial_catalog.rubric_key(str(selected["name"]))
            selected["key"] = key
            source["rubric_keys"] = (key,)
            return original_scheduled_plan(
                **{**kwargs, "rubric_rows": [selected], "source_rows": [source]}
            )

        self.plan_patch = patch.object(main, "scheduled_plan", side_effect=deterministic_plan)
        self.plan_patch.start()

        async def generated_package(plan, character, **kwargs):
            return editorial_orchestrator.GenerationPackage(
                final_text="Готовый VK-пост " + "с конкретной сценой и выводом. " * 18,
                concrete_scene="Конкретный предмет меняет состояние после действия.",
                visual_subject="Тот же конкретный предмет.",
                visual_relation_to_thesis="Изменение предмета показывает выбранный тезис.",
                image_prompt_seed="Тот же предмет после конкретного действия.",
                track_tags=plan.track_tags,
            )

        self.generation_patch = patch.object(
            main, "generate_scheduled_package", new=AsyncMock(side_effect=generated_package)
        )
        self.generation_patch.start()

    def tearDown(self):
        self.generation_patch.stop()
        self.plan_patch.stop()
        self.db_patch.stop()
        self.db_directory.cleanup()

    @staticmethod
    def prepare_queue(root: Path) -> None:
        (root / "pending").mkdir(parents=True)
        (root / naz_vk_music.TRACK_HISTORY_BACKFILL_MARKER).write_text(
            "ready\n",
            encoding="utf-8",
        )

    def test_job_uses_approved_track_instead_of_post_topic(self):
        topic = "AI-бот и очередь публикаций"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            self.prepare_queue(root)
            state = Path(directory) / "rotation.json"
            with patch.multiple(
                main,
                NAZ_VK_ENABLED=True,
                NAZ_VK_PUBLIC_ID="123",
                NAZ_VK_QUEUE_DIR=root,
                NAZ_VK_TRACK_STATE_FILE=state,
            ), patch.object(
                main, "generate_content", new=AsyncMock(return_value="Готовый VK-пост")
            ), patch.object(
                main, "generate_images_with_retries", new=AsyncMock(return_value=([b"png"], "prompt"))
            ):
                job = asyncio.run(main.create_naz_vk_job(topic, source_ref="test:daily"))
            self.assertIn(job["track_query"], naz_vk_music.APPROVED_QUERIES)
            self.assertNotEqual(job["track_query"].casefold(), topic.casefold())
            self.assertTrue(state.is_file())

    def test_missing_catalog_match_does_not_enqueue_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            self.prepare_queue(root)
            enqueue = Mock()
            with patch.multiple(
                main,
                NAZ_VK_ENABLED=True,
                NAZ_VK_PUBLIC_ID="123",
                NAZ_VK_QUEUE_DIR=root,
                NAZ_VK_TRACK_STATE_FILE=Path(directory) / "rotation.json",
            ), patch.object(
                main, "generate_content", new=AsyncMock(return_value="Готовый VK-пост")
            ), patch.object(
                main, "generate_images_with_retries", new=AsyncMock(return_value=([b"png"], "prompt"))
            ), patch.object(
                main.naz_vk_music, "select_track", return_value=None
            ), patch.object(
                main.vk_publish_queue, "enqueue", enqueue
            ):
                with self.assertRaises(naz_vk_music.TrackSelectionError):
                    asyncio.run(main.create_naz_vk_job("Тема", source_ref="test:no-track"))
            enqueue.assert_not_called()
            self.assertEqual(list((root / "pending").iterdir()), [])

    def test_gaming_job_uses_vk_gaming_prompt_and_records_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            self.prepare_queue(root)
            generate = AsyncMock(return_value="Игровой VK-пост")
            record = Mock()
            with patch.multiple(
                main,
                NAZ_VK_ENABLED=True,
                NAZ_VK_PUBLIC_ID="123",
                NAZ_VK_QUEUE_DIR=root,
                NAZ_VK_TRACK_STATE_FILE=Path(directory) / "rotation.json",
            ), patch.object(
                main, "generate_content", new=generate
            ), patch.object(
                main, "generate_images_with_retries", new=AsyncMock(return_value=([b"png"], "prompt"))
            ), patch.object(
                main.memory, "get_recent_content_signatures", return_value=[]
            ), patch.object(
                main.memory, "record_content_signature", record
            ):
                job = asyncio.run(
                    main.create_naz_vk_job(
                        "Игровые AI-инструменты",
                        source_ref="test:gaming",
                        rubric_kind="gaming",
                    )
                )
            generate.assert_not_awaited()
            self.assertIn(job["track_query"], naz_vk_music.APPROVED_QUERIES)
            self.assertIn("gaming", next(track.tags for track in naz_vk_music.APPROVED_TRACKS if track.query == job["track_query"]))
            record.assert_not_called()

    def test_material_job_enqueues_three_frames_with_shared_music_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            self.prepare_queue(root)
            images = AsyncMock(return_value=([b"macro", b"form", b"active"], "material prompt"))
            with patch.multiple(
                main,
                NAZ_VK_ENABLED=True,
                NAZ_VK_PUBLIC_ID="123",
                NAZ_VK_QUEUE_DIR=root,
                NAZ_VK_TRACK_STATE_FILE=Path(directory) / "rotation.json",
            ), patch.object(
                main, "select_naz_vk_rubric", return_value=main.MATERIAL_RUBRIC
            ), patch.object(
                main, "generate_content", new=AsyncMock(return_value="Короткая мысль Naz")
            ), patch.object(
                main, "generate_images_with_retries", new=images
            ):
                job = asyncio.run(
                    main.create_naz_vk_job(
                        "Фрезерованный титановый прототип",
                        source_ref="test:material",
                    )
                )
            self.assertEqual(images.await_args.kwargs["count"], 3)
            self.assertIn("MATERIAL / МАТЕРИЯ", images.await_args.args[1])
            self.assertEqual(job["media"], ["image-1.png", "image-2.png", "image-3.png"])
            self.assertIn(job["track_query"], naz_vk_music.APPROVED_QUERIES)

    def test_incomplete_material_series_is_not_enqueued(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            self.prepare_queue(root)
            with patch.multiple(
                main,
                NAZ_VK_ENABLED=True,
                NAZ_VK_PUBLIC_ID="123",
                NAZ_VK_QUEUE_DIR=root,
                NAZ_VK_TRACK_STATE_FILE=Path(directory) / "rotation.json",
            ), patch.object(
                main, "select_naz_vk_rubric", return_value=main.MATERIAL_RUBRIC
            ), patch.object(
                main, "generate_content", new=AsyncMock(return_value="Короткая мысль Naz")
            ), patch.object(
                main,
                "generate_images_with_retries",
                new=AsyncMock(return_value=([b"macro", b"form"], "material prompt")),
            ):
                with self.assertRaisesRegex(main.vk_publish_queue.QueueError, "complete three-frame"):
                    asyncio.run(
                        main.create_naz_vk_job(
                            "Титановый прототип",
                            source_ref="test:material-partial",
                        )
                    )
            self.assertEqual(list((root / "pending").iterdir()), [])

    def test_required_image_failure_never_creates_draft_or_queue_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            self.prepare_queue(root)
            save = Mock()
            enqueue = Mock()
            with patch.multiple(
                main,
                NAZ_VK_ENABLED=True,
                NAZ_VK_PUBLIC_ID="123",
                NAZ_VK_QUEUE_DIR=root,
                NAZ_VK_TRACK_STATE_FILE=Path(directory) / "rotation.json",
                NAZ_VK_IMAGE_POLICY="required",
                NAZ_VK_IMAGE_ATTEMPTS=2,
            ), patch.object(
                main, "generate_content", new=AsyncMock(return_value="Готовый VK-пост")
            ), patch.object(
                main, "generate_images_with_retries", new=AsyncMock(return_value=([], "prompt"))
            ) as images, patch.object(
                main.memory, "save_generated_post", save
            ), patch.object(
                main.vk_publish_queue, "enqueue", enqueue
            ):
                with self.assertRaisesRegex(main.vk_publish_queue.QueueError, "requires media"):
                    asyncio.run(main.create_naz_vk_job("Тема", source_ref="test:no-image"))
            self.assertEqual(images.await_args.kwargs["attempts"], 2)
            save.assert_not_called()
            enqueue.assert_not_called()
            self.assertEqual(list((root / "pending").iterdir()), [])

    def test_text_music_policy_must_be_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            self.prepare_queue(root)
            with patch.multiple(
                main,
                NAZ_VK_ENABLED=True,
                NAZ_VK_PUBLIC_ID="123",
                NAZ_VK_QUEUE_DIR=root,
                NAZ_VK_TRACK_STATE_FILE=Path(directory) / "rotation.json",
                NAZ_VK_IMAGE_POLICY="text_music",
            ), patch.object(
                main, "generate_content", new=AsyncMock(return_value="Готовый VK-пост")
            ), patch.object(
                main, "generate_images_with_retries", new=AsyncMock(return_value=([], "prompt"))
            ):
                job = asyncio.run(main.create_naz_vk_job("Тема", source_ref="test:text-music"))
            self.assertEqual(job["media"], [])


if __name__ == "__main__":
    unittest.main()
