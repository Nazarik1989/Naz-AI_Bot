import json
import hashlib
import os
import errno
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import vk_publish_queue as queue


class VkPublishQueueTests(unittest.TestCase):
    def enqueue(self, root: Path, **overrides):
        (root / "pending").mkdir(parents=True, exist_ok=True)
        values = dict(
            target_group_id="123",
            text="Безопасный текст Naz",
            source_ref="test:one",
            track_query="Tycho — Awake",
            media=[queue.MediaInput("image-1.png", b"png")],
            created_at=datetime(2026, 7, 12, tzinfo=timezone.utc),
        )
        values.update(overrides)
        return queue.enqueue(root, **values)

    def test_correct_naz_job_and_media_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = self.enqueue(root)
            saved = json.loads((root / "pending" / job["job_id"] / "job.json").read_text("utf-8"))
            self.assertEqual(saved["schema"], "vk_publish_job.v1")
            self.assertEqual(saved["producer"], "naz")
            self.assertEqual(saved["target_group_id"], "123")
            self.assertEqual(saved["media"], ["image-1.png"])
            self.assertEqual((root / "pending" / job["job_id"] / "image-1.png").read_bytes(), b"png")
            self.assertEqual(job["job_id"], "naz-" + hashlib.sha256(job["dedupe_key"].encode()).hexdigest()[:24])
            self.assertEqual(len(job), 11)
            self.assertIsInstance(job["target_group_id"], str)
            queue.validate_canonical_job(job, root / "pending" / job["job_id"])

    def test_atomic_enqueue_renames_completed_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch("vk_publish_queue.os.replace", wraps=queue.os.replace) as replace:
            root = Path(directory)
            job = self.enqueue(root)
            replace.assert_called_once()
            self.assertTrue((root / "pending" / job["job_id"] / "job.json").is_file())
            self.assertFalse(list((root / "pending").glob(".*")))

    def test_atomic_conflict_is_reported_as_duplicate_and_temp_is_removed(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "vk_publish_queue.os.replace", side_effect=OSError(errno.EEXIST, "race")
        ):
            root = Path(directory)
            with self.assertRaises(queue.DuplicateJobError):
                self.enqueue(root)
            self.assertFalse(list((root / "pending").glob(".*")))

    def test_duplicate_dedupe_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.enqueue(root)
            with self.assertRaises(queue.DuplicateJobError):
                self.enqueue(root)

    def test_producer_does_not_read_private_consumer_states(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pending").mkdir()
            for state in ("processing", "done", "failed"):
                (root / state).mkdir()
            original_iterdir = Path.iterdir

            def guarded_iterdir(path):
                if path.name in {"processing", "done", "failed"}:
                    raise AssertionError(f"producer read private state: {path}")
                return original_iterdir(path)

            with patch.object(Path, "iterdir", guarded_iterdir):
                self.enqueue(root)

    def test_existing_pending_is_not_chmodded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pending = root / "pending"
            pending.mkdir()
            with patch("vk_publish_queue.os.chmod", wraps=os.chmod) as chmod:
                self.enqueue(root)
            self.assertNotIn(pending, [Path(call.args[0]) for call in chmod.call_args_list])

    def test_enqueue_does_not_require_pending_directory_listing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pending").mkdir()
            with patch.object(Path, "iterdir", side_effect=PermissionError("listing denied")), patch.object(
                Path, "glob", side_effect=PermissionError("listing denied")
            ):
                job = self.enqueue(root)
            self.assertTrue((root / "pending" / job["job_id"] / "job.json").is_file())

    def test_canonical_limits_are_rejected_before_enqueue(self):
        cases = (
            {"text": "x" * (queue.MAX_TEXT_LENGTH + 1)},
            {"track_query": "x" * (queue.MAX_TRACK_QUERY_LENGTH + 1)},
            {"dedupe_key": "x" * (queue.MAX_DEDUPE_KEY_LENGTH + 1)},
            {"media": [queue.MediaInput(f"image-{i}.png", b"x") for i in range(5)]},
            {"media": [queue.MediaInput("image-1.png", b"x" * (queue.MAX_MEDIA_BYTES + 1))]},
        )
        for index, overrides in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(queue.QueueError):
                    self.enqueue(Path(directory), source_ref=f"limit:{index}", **overrides)
                pending = Path(directory) / "pending"
                self.assertFalse(pending.exists() and list(pending.iterdir()))

    def test_track_query_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(queue.QueueError, "track_query"):
                self.enqueue(Path(directory), track_query="")

    def test_track_query_must_come_from_approved_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(queue.QueueError, "approved"):
                self.enqueue(Path(directory), track_query="Тема поста как музыка")

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_consumer_can_read_group_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = self.enqueue(root)
            job_dir = root / "pending" / job["job_id"]
            self.assertEqual(job_dir.stat().st_mode & 0o777, 0o770)
            self.assertEqual((job_dir / "job.json").stat().st_mode & 0o777, 0o640)
            self.assertEqual((job_dir / "image-1.png").stat().st_mode & 0o777, 0o640)

    def test_void_compatibility_fixture(self):
        fixture_dir = Path(__file__).parent / "fixtures"
        job = json.loads((fixture_dir / "vk_publish_job_naz.json").read_text("utf-8"))
        queue.validate_canonical_job(job, fixture_dir)

    def test_forbidden_media_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("../x.png", "/x.png", "dir/x.png", "https://x/y.png", "C:\\x.png"):
                with self.subTest(name=name), self.assertRaises(queue.QueueError):
                    self.enqueue(Path(directory), source_ref=f"bad:{name}", media=[queue.MediaInput(name, b"x")])

    def test_symlink_source_is_forbidden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.png"
            link = root / "link.png"
            source.write_bytes(b"x")
            try:
                link.symlink_to(source)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaises(queue.QueueError):
                self.enqueue(root / "queue", media=[queue.MediaInput("image-1.png", link)])

    def test_producer_and_target_cannot_be_overridden(self):
        with tempfile.TemporaryDirectory() as directory:
            job = self.enqueue(Path(directory), target_group_id="configured")
            self.assertEqual(job["producer"], "naz")
            self.assertEqual(job["target_group_id"], "configured")
            with self.assertRaises(TypeError):
                self.enqueue(Path(directory), producer="attacker")

    def test_pending_must_exist_and_must_not_be_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(queue.QueueError):
                queue.enqueue(root, target_group_id="123", text="text", source_ref="missing")


if __name__ == "__main__":
    unittest.main()
