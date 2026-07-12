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
        values = dict(
            target_group_id="123",
            text="Безопасный текст Naz",
            source_ref="test:one",
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

    def test_duplicate_is_found_in_every_consumer_state(self):
        for state in queue.STATES:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                job = self.enqueue(root)
                source = root / "pending" / job["job_id"]
                destination = root / state / job["job_id"]
                if state != "pending":
                    destination.parent.mkdir()
                    source.rename(destination)
                with self.assertRaises(queue.DuplicateJobError):
                    self.enqueue(root)

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

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_consumer_can_read_group_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            job = self.enqueue(root)
            job_dir = root / "pending" / job["job_id"]
            self.assertEqual(job_dir.stat().st_mode & 0o777, 0o750)
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


if __name__ == "__main__":
    unittest.main()
