import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import naz_vk_music as music


class NazVkMusicTests(unittest.TestCase):
    now = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)

    def queue_files(
        self,
        directory: str,
        history: list[str] | None = None,
        *,
        marker: bool = True,
    ) -> tuple[Path, Path, Path]:
        root = Path(directory) / "queue"
        (root / "pending").mkdir(parents=True)
        shared = root / "recent-tracks.json"
        shared.write_text(
            json.dumps({"tracks": [{"key": key} for key in history or []]}),
            encoding="utf-8",
        )
        if marker:
            (root / music.TRACK_HISTORY_BACKFILL_MARKER).write_text(
                "ready\n",
                encoding="utf-8",
            )
        return root, shared, Path(directory) / "rotation.json"

    @staticmethod
    def job_id(index: int) -> str:
        return f"naz-{index:024x}"

    @staticmethod
    def result(job_id: str):
        return lambda query: {"job_id": job_id, "track_query": query}

    def test_catalog_queries_are_explicit_unique_and_cover_both_rubrics(self):
        queries = [track.query for track in music.APPROVED_TRACKS]
        self.assertEqual(len(queries), len(set(queries)))
        self.assertTrue(all(" — " in query for query in queries))
        self.assertGreater(music.rotation_pool_size(["daily"]), music.SHARED_COLLISION_LIMIT)
        self.assertGreater(music.rotation_pool_size(["gaming"]), music.SHARED_COLLISION_LIMIT)

    def test_selection_uses_every_eligible_track_before_lru_reuse(self):
        pool = list(music.eligible_tracks(["daily"]))
        published: list[str] = []
        selected: list[str] = []

        for index in range(len(pool) + 1):
            track = music.select_track(
                ["daily"],
                published,
                seed=f"daily:{index}",
                hard_excluded_queries=published[-music.SHARED_COLLISION_LIMIT :],
            )
            self.assertIsNotNone(track)
            selected.append(track.query)
            if track.query in published:
                published.remove(track.query)
            published.append(track.query)

        self.assertEqual(len(set(selected[: len(pool)])), len(pool))
        self.assertEqual(selected[len(pool)], selected[0])

    def test_semantic_tags_are_soft_ranking_not_a_slot_blocker(self):
        humor = [track.query for track in music.APPROVED_TRACKS if "humor" in track.tags]
        selected = music.select_track(
            ["gaming", "humor"],
            humor,
            seed="gaming:no-fresh-humor",
        )
        self.assertIsNotNone(selected)
        self.assertIn("gaming", selected.tags)
        self.assertNotIn(selected.query, humor)

    def test_rubric_category_cannot_cross_from_gaming_to_daily(self):
        selected = music.select_track(["gaming", "builder"], [], seed="gaming:builder")
        self.assertIsNotNone(selected)
        self.assertIn("gaming", selected.tags)
        self.assertNotIn("daily", selected.tags)

    def test_shared_history_is_complete_and_strict(self):
        queries = [music._normal(track.query) for track in music.APPROVED_TRACKS]
        with tempfile.TemporaryDirectory() as directory:
            _, shared, _ = self.queue_files(directory, queries)
            self.assertEqual(music.load_shared_recent(shared), queries)

            for invalid in (
                {},
                {"tracks": "bad"},
                {"tracks": [{}]},
                {"tracks": [{"key": "Not Normalized"}]},
                {"tracks": [{"key": queries[0]}, {"key": queries[0]}]},
            ):
                with self.subTest(invalid=invalid):
                    shared.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaises(music.TrackSelectionError):
                        music.load_shared_recent(shared)

    def test_lru_rollover_never_reuses_shared_last_eight(self):
        daily = list(music.eligible_tracks(["daily"]))
        ordered = [*daily[1:], daily[0]]
        history = [music._normal(track.query) for track in ordered]
        selected = music.select_track(
            ["daily"],
            history,
            seed="daily:rollover",
            hard_excluded_queries=history[-music.SHARED_COLLISION_LIMIT :],
        )
        self.assertEqual(selected.query, daily[1].query)
        self.assertNotIn(
            music._normal(selected.query),
            history[-music.SHARED_COLLISION_LIMIT :],
        )

    def test_reservation_is_saved_before_enqueue_and_is_not_published_history(self):
        with tempfile.TemporaryDirectory() as directory:
            _, shared, state = self.queue_files(directory)
            job_id = self.job_id(1)

            def enqueue(query):
                payload = json.loads(state.read_text(encoding="utf-8"))
                self.assertEqual(payload["schema"], music.ROTATION_SCHEMA)
                self.assertEqual(
                    payload["reservation_schema"], music.RESERVATION_SCHEMA
                )
                self.assertEqual(payload["reservations"][0]["job_id"], job_id)
                self.assertEqual(payload["recent_queries"], [])
                return {"job_id": job_id, "track_query": query}

            with patch("naz_vk_music._utc_now", return_value=self.now):
                result = music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:one",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=job_id,
                    source_ref="systemd:2026-07-30:daily:10:30",
                    enqueue_job=enqueue,
                )

            self.assertEqual(result["job_id"], job_id)
            self.assertEqual(music.load_shared_recent(shared), [])

    def test_reservation_envelope_remains_readable_by_v1_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "rotation.json"
            reservation = music.TrackReservation(
                job_id=self.job_id(16),
                track_query=music.APPROVED_TRACKS[0].query,
                track_key=music._normal(music.APPROVED_TRACKS[0].query),
                source_ref="systemd:rollback-compatible",
                reserved_at=self.now.isoformat().replace("+00:00", "Z"),
            )
            music._save_reservations(state, [reservation])
            payload = json.loads(state.read_text(encoding="utf-8"))

        # This is the exact subset read by the previous v1 implementation.
        self.assertEqual(payload["schema"], music.LEGACY_ROTATION_SCHEMA)
        self.assertEqual(payload["recent_queries"], [])
        self.assertEqual(payload["reservation_schema"], music.RESERVATION_SCHEMA)

    def test_active_reservation_is_excluded_without_changing_published_lru(self):
        daily = list(music.eligible_tracks(["daily"]))
        history = [music._normal(track.query) for track in daily[:-1]]
        with tempfile.TemporaryDirectory() as directory:
            root, shared, state = self.queue_files(directory, history)
            old_job = self.job_id(2)
            reserved = music.TrackReservation(
                job_id=old_job,
                track_query=daily[-1].query,
                track_key=music._normal(daily[-1].query),
                source_ref="systemd:old",
                reserved_at=self.now.isoformat().replace("+00:00", "Z"),
            )
            music._save_reservations(state, [reserved])
            (root / "pending" / old_job).mkdir()
            new_job = self.job_id(3)

            with patch("naz_vk_music._utc_now", return_value=self.now):
                result = music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:reserved",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=new_job,
                    source_ref="systemd:new",
                    enqueue_job=self.result(new_job),
                )

            self.assertNotEqual(result["track_query"], reserved.track_query)
            self.assertEqual(music.load_shared_recent(shared), history)

    def test_failed_enqueue_rolls_back_only_the_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            _, shared, state = self.queue_files(directory)
            enqueue = Mock(side_effect=RuntimeError("queue unavailable"))
            job_id = self.job_id(4)
            with patch("naz_vk_music._utc_now", return_value=self.now), self.assertRaises(
                RuntimeError
            ):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:failure",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=job_id,
                    source_ref="systemd:failure",
                    enqueue_job=enqueue,
                )

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(
                payload,
                {
                    "schema": music.ROTATION_SCHEMA,
                    "recent_queries": [],
                    "reservation_schema": music.RESERVATION_SCHEMA,
                    "reservations": [],
                },
            )

    def test_crash_after_materializing_pending_job_keeps_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root, shared, state = self.queue_files(directory)
            job_id = self.job_id(5)

            def enqueue_then_crash(_query):
                (root / "pending" / job_id).mkdir()
                raise RuntimeError("crash after atomic enqueue")

            with patch("naz_vk_music._utc_now", return_value=self.now), self.assertRaises(
                RuntimeError
            ):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:crash",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=job_id,
                    source_ref="systemd:crash",
                    enqueue_job=enqueue_then_crash,
                )

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual([item["job_id"] for item in payload["reservations"]], [job_id])

    def test_terminal_unpublished_reservation_expires_without_spending_lru(self):
        daily = list(music.eligible_tracks(["daily"]))
        history = [music._normal(track.query) for track in daily]
        with tempfile.TemporaryDirectory() as directory:
            _, shared, state = self.queue_files(directory, history)
            stale = music.TrackReservation(
                job_id=self.job_id(6),
                track_query=daily[0].query,
                track_key=music._normal(daily[0].query),
                source_ref="systemd:failed",
                reserved_at=(self.now - timedelta(hours=3)).isoformat().replace("+00:00", "Z"),
            )
            music._save_reservations(state, [stale])
            job_id = self.job_id(7)

            with patch("naz_vk_music._utc_now", return_value=self.now):
                result = music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:after-failure",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=job_id,
                    source_ref="systemd:after-failure",
                    enqueue_job=self.result(job_id),
                )

            self.assertEqual(result["track_query"], daily[0].query)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual([item["job_id"] for item in payload["reservations"]], [job_id])

    def test_pending_reservation_does_not_expire(self):
        daily = list(music.eligible_tracks(["daily"]))
        with tempfile.TemporaryDirectory() as directory:
            root, shared, state = self.queue_files(directory)
            old_job = self.job_id(8)
            stale = music.TrackReservation(
                job_id=old_job,
                track_query=daily[0].query,
                track_key=music._normal(daily[0].query),
                source_ref="systemd:pending",
                reserved_at=(self.now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            )
            music._save_reservations(state, [stale])
            (root / "pending" / old_job).mkdir()
            job_id = self.job_id(9)

            with patch("naz_vk_music._utc_now", return_value=self.now):
                result = music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:pending",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=job_id,
                    source_ref="systemd:new",
                    enqueue_job=self.result(job_id),
                )

            self.assertNotEqual(result["track_query"], stale.track_query)

    def test_same_pending_job_preserves_reservation_and_duplicate_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            root, shared, state = self.queue_files(directory)
            job_id = self.job_id(15)
            reservation = music.TrackReservation(
                job_id=job_id,
                track_query=music.APPROVED_TRACKS[0].query,
                track_key=music._normal(music.APPROVED_TRACKS[0].query),
                source_ref="systemd:duplicate",
                reserved_at=self.now.isoformat().replace("+00:00", "Z"),
            )
            music._save_reservations(state, [reservation])
            (root / "pending" / job_id).mkdir()
            enqueue = Mock(side_effect=RuntimeError("duplicate pending job"))

            with patch("naz_vk_music._utc_now", return_value=self.now), self.assertRaisesRegex(
                RuntimeError, "duplicate pending"
            ):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:duplicate",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=job_id,
                    source_ref="systemd:duplicate",
                    enqueue_job=enqueue,
                )

            enqueue.assert_called_once_with(reservation.track_query)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["reservations"][0]["job_id"], job_id)

    def test_legacy_enqueue_history_is_discarded_only_after_v2_marker(self):
        daily = list(music.eligible_tracks(["daily"]))
        with tempfile.TemporaryDirectory() as directory:
            _, shared, state = self.queue_files(directory, marker=False)
            legacy = {
                "schema": music.LEGACY_ROTATION_SCHEMA,
                "recent_queries": [track.query for track in daily],
            }
            state.write_text(json.dumps(legacy), encoding="utf-8")
            original = state.read_bytes()
            enqueue = Mock()

            with self.assertRaisesRegex(music.TrackSelectionError, "not ready"):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:legacy",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=self.job_id(10),
                    source_ref="systemd:legacy",
                    enqueue_job=enqueue,
                )
            self.assertEqual(state.read_bytes(), original)
            enqueue.assert_not_called()

            (shared.parent / music.TRACK_HISTORY_BACKFILL_MARKER).write_text(
                "ready\n", encoding="utf-8"
            )
            job_id = self.job_id(10)
            with patch("naz_vk_music._utc_now", return_value=self.now):
                result = music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:legacy",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=job_id,
                    source_ref="systemd:legacy",
                    enqueue_job=self.result(job_id),
                )
            self.assertIn(result["track_query"], music.APPROVED_QUERIES)
            migrated = json.loads(state.read_text())
            self.assertEqual(migrated["schema"], music.ROTATION_SCHEMA)
            self.assertEqual(
                migrated["reservation_schema"], music.RESERVATION_SCHEMA
            )

    def test_state_write_failure_prevents_enqueue(self):
        with tempfile.TemporaryDirectory() as directory:
            _, shared, state = self.queue_files(directory)
            enqueue = Mock()
            with patch.object(
                music, "_save_reservations", side_effect=OSError("state read-only")
            ), self.assertRaises(OSError):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:state-failure",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=self.job_id(11),
                    source_ref="systemd:state-failure",
                    enqueue_job=enqueue,
                )
            enqueue.assert_not_called()

    def test_mismatched_enqueue_result_fails_closed_with_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            _, shared, state = self.queue_files(directory)
            job_id = self.job_id(12)
            with patch("naz_vk_music._utc_now", return_value=self.now), self.assertRaisesRegex(
                music.TrackSelectionError, "does not match"
            ):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:mismatch",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=job_id,
                    source_ref="systemd:mismatch",
                    enqueue_job=lambda query: {
                        "job_id": self.job_id(99),
                        "track_query": query,
                    },
                )
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["reservations"][0]["job_id"], job_id)

    def test_corrupt_state_and_same_post_topic_fail_closed(self):
        selected = music.APPROVED_TRACKS[0]
        with tempfile.TemporaryDirectory() as directory:
            _, shared, state = self.queue_files(directory)
            state.write_text("not-json", encoding="utf-8")
            enqueue = Mock()
            with self.assertRaises(music.TrackSelectionError):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=["daily"],
                    seed="daily:corrupt",
                    post_topic="AI systems",
                    shared_history_file=shared,
                    expected_job_id=self.job_id(13),
                    source_ref="systemd:corrupt",
                    enqueue_job=enqueue,
                )
            enqueue.assert_not_called()

            state.unlink()
            with patch.object(music, "select_track", return_value=selected), self.assertRaisesRegex(
                music.TrackSelectionError, "post topic"
            ):
                music.enqueue_with_track_rotation(
                    state,
                    requested_tags=selected.tags,
                    seed="same-query",
                    post_topic=selected.query,
                    shared_history_file=shared,
                    expected_job_id=self.job_id(14),
                    source_ref="systemd:same",
                    enqueue_job=enqueue,
                )
            enqueue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
