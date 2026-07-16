import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import naz_vk_music as music


class NazVkMusicTests(unittest.TestCase):
    def test_catalog_queries_are_explicit_unique_and_cover_both_rubrics(self):
        queries = [track.query for track in music.APPROVED_TRACKS]
        self.assertEqual(len(queries), len(set(queries)))
        self.assertTrue(all(" — " in query for query in queries))
        self.assertGreater(len([track for track in music.APPROVED_TRACKS if "daily" in track.tags]), 8)
        self.assertGreater(len([track for track in music.APPROVED_TRACKS if "gaming" in track.tags]), 8)

    def test_selection_excludes_the_shared_last_eight(self):
        recent = [track.query for track in music.APPROVED_TRACKS if "daily" in track.tags][:8]
        selected = music.select_track(["daily"], recent, seed="daily:slot")
        self.assertIsNotNone(selected)
        self.assertNotIn(selected.query, recent)

    def test_rubric_category_cannot_cross_from_gaming_to_daily(self):
        selected = music.select_track(["gaming", "builder"], [], seed="gaming:builder")
        self.assertIsNotNone(selected)
        self.assertIn("gaming", selected.tags)
        self.assertNotIn("daily", selected.tags)

    def test_rotation_is_shared_and_persists_only_last_eight(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "rotation.json"
            selected = []
            for index in range(9):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily", "gaming"],
                    seed=f"job:{index}",
                    post_topic=f"topic {index}",
                    enqueue_job=lambda query: selected.append(query) or {"track_query": query},
                )
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], music.ROTATION_SCHEMA)
            self.assertEqual(payload["recent_queries"], selected[-8:])
            self.assertEqual(len(set(selected[:9])), 9)

    def test_no_eligible_track_means_no_enqueue(self):
        humor = [track.query for track in music.APPROVED_TRACKS if "humor" in track.tags]
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "rotation.json"
            state.write_text(
                json.dumps({"schema": music.ROTATION_SCHEMA, "recent_queries": humor}),
                encoding="utf-8",
            )
            enqueue = Mock()
            with self.assertRaises(music.TrackSelectionError):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["humor"],
                    seed="gaming:no-track",
                    post_topic="игровой пост",
                    enqueue_job=enqueue,
                )
            enqueue.assert_not_called()

    def test_failed_enqueue_does_not_advance_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "rotation.json"
            enqueue = Mock(side_effect=RuntimeError("queue unavailable"))
            with self.assertRaises(RuntimeError):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:failure",
                    post_topic="AI-системы",
                    enqueue_job=enqueue,
                )
            self.assertFalse(state.exists())

    def test_rotation_write_failure_prevents_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            enqueue = Mock()
            with patch.object(music, "_save_recent", side_effect=OSError("state read-only")):
                with self.assertRaises(OSError):
                    music.enqueue_with_track_rotation(
                        Path(directory) / "rotation.json",
                        requested_tags=["daily"],
                        seed="daily:state-failure",
                        post_topic="AI-системы",
                        enqueue_job=enqueue,
                    )
            enqueue.assert_not_called()

    def test_corrupt_rotation_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "rotation.json"
            state.write_text("not-json", encoding="utf-8")
            enqueue = Mock()
            with self.assertRaises(music.TrackSelectionError):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:corrupt-state",
                    post_topic="AI-системы",
                    enqueue_job=enqueue,
                )
            enqueue.assert_not_called()

    def test_post_topic_cannot_be_used_as_track_query(self):
        selected = music.APPROVED_TRACKS[0]
        with tempfile.TemporaryDirectory() as directory:
            enqueue = Mock()
            with patch.object(music, "select_track", return_value=selected), self.assertRaisesRegex(
                music.TrackSelectionError, "post topic"
            ):
                music.enqueue_with_track_rotation(
                    Path(directory) / "rotation.json",
                    requested_tags=selected.tags,
                    seed="same-query",
                    post_topic=selected.query,
                    enqueue_job=enqueue,
                )
            enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
