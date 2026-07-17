import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import main
import memory
import naz_vk_music


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

    def tearDown(self):
        self.db_patch.stop()
        self.db_directory.cleanup()

    def test_job_uses_approved_track_instead_of_post_topic(self):
        topic = "AI-бот и очередь публикаций"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            (root / "pending").mkdir(parents=True)
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
            (root / "pending").mkdir(parents=True)
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
            (root / "pending").mkdir(parents=True)
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
            instruction = generate.await_args.kwargs["extra_instruction"]
            self.assertIn("Игровая лаборатория VK", instruction)
            self.assertIn("Игровая вертикаль", instruction)
            self.assertIn(job["track_query"], naz_vk_music.APPROVED_QUERIES)
            self.assertIn("gaming", next(track.tags for track in naz_vk_music.APPROVED_TRACKS if track.query == job["track_query"]))
            record.assert_called_once()

    def test_required_image_failure_never_creates_draft_or_queue_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "queue"
            (root / "pending").mkdir(parents=True)
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
            (root / "pending").mkdir(parents=True)
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
