import dataclasses
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import naz_story_worker as worker
import story_pack_control as control
import story_production as story
from story_media_composer import LicensedTrack, MediaComposer, MediaError, MediaProbe, checksum, load_music_library
from story_video_provider import (
    FakeVideoProvider,
    ProviderError,
    ProviderJob,
    RunwayVideoProvider,
    SceneRequest,
)
from tests.test_story_production import SAFE_FACTS, planned


class MockTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        return self.responses.pop(0)


class DummyComposer:
    def safe_output(self, root, relative):
        target = (Path(root) / relative).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def normalize(self, source, destination, *, duration_seconds):
        destination.write_bytes(b"normalized-moving-media")
        return MediaProbe(float(duration_seconds), 1080, 1920, "h264", "yuv420p", "30/1", 2.0)

    def overlay_story(self, clean, destination, *, text, safe_zone):
        destination.write_bytes(clean.read_bytes() + b"-overlay")
        return MediaProbe(4.0, 1080, 1920, "h264", "yuv420p", "30/1", 2.0)

    def compose_reel(self, **kwargs):
        raise AssertionError("music-less test must not compose a Reel")


def config(root, **changes):
    value = worker.WorkerConfig(
        pack_root=Path(root).resolve(), render_enabled=True, provider_name="fake", model="fake",
        reference_path=None, music_library_path=None, ffmpeg="ffmpeg", ffprobe="ffprobe",
        font_path=None, max_scene_jobs=7, concurrency=1, poll_timeout_seconds=900,
        max_retries=2, daily_job_limit=7, daily_seconds_limit=56, media_timeout_seconds=30,
    )
    return dataclasses.replace(value, **changes)


def make_pack(root, *, approved=True):
    pack = story.plan_story_pack(planned(), SAFE_FACTS)
    pack_dir = story.persist_story_queue(pack, Path(root))
    if approved:
        control.approve_pack(Path(root), pack.plan_id)
    return pack, pack_dir, pack_dir / "story_manifest.json"


class ProviderTests(unittest.TestCase):
    def test_runway_submit_poll_cancel_with_mock_transport(self):
        transport = MockTransport([
            (200, {"Content-Type": "application/json"}, b'{"id":"task-1"}'),
            (200, {"Content-Type": "application/json"}, b'{"status":"SUCCEEDED","output":["https://cdn.example/video.mp4"]}'),
            (204, {}, b""),
        ])
        provider = RunwayVideoProvider(api_key="dedicated-key", transport=transport)
        job = provider.submit(SceneRequest("01", "safe visible motion", 4))
        completed = provider.retrieve(job.external_job_id)
        provider.cancel(job.external_job_id)
        self.assertEqual(completed.status, "completed")
        self.assertEqual([call[0] for call in transport.calls], ["POST", "GET", "DELETE"])
        self.assertIn("/text_to_video", transport.calls[0][1])
        self.assertNotIn(b"dedicated-key", transport.calls[0][3])

    def test_html_or_corrupt_download_is_rejected(self):
        provider = RunwayVideoProvider(
            api_key="dedicated-key",
            transport=MockTransport([(200, {"Content-Type": "text/html"}, b"<html>no</html>")]),
        )
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ProviderError, "provider_download_not_mp4"):
                provider.download(ProviderJob("id", "completed", "https://cdn.example/out"), Path(root) / "x.mp4")

    def test_provider_errors_never_include_key_or_response_body(self):
        key = "super-secret-dedicated-key"
        provider = RunwayVideoProvider(api_key=key, transport=MockTransport([(401, {}, b'{"echo":"private"}')]))
        with self.assertRaises(ProviderError) as raised:
            provider.submit(SceneRequest("01", "safe", 4))
        self.assertNotIn(key, str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    def test_fake_provider_has_no_transport_and_no_network_path(self):
        provider = FakeVideoProvider()
        self.assertFalse(hasattr(provider, "_transport"))
        provider.submit(SceneRequest("01", "motion", 4))
        self.assertEqual(provider.submit_count, 1)


class WorkerTests(unittest.TestCase):
    def test_feature_flags_default_false_and_check_config_is_offline(self):
        cfg = worker.load_config({})
        self.assertFalse(cfg.render_enabled)
        self.assertEqual(cfg.provider_name, "disabled")
        result = worker.check_config(cfg, {})
        self.assertFalse(result["live_api_called"])

    def test_worker_waits_for_explicit_approval_without_provider_call(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root, approved=False)
            provider = FakeVideoProvider()
            status = worker.process_pack(
                pack.plan_id, config=config(root), provider=provider, composer=DummyComposer()
            )
            self.assertEqual(status, "awaiting_approval")
            self.assertEqual(provider.submit_count, 0)
            self.assertEqual(json.loads(manifest.read_text())["pack_status"], "awaiting_approval")

    def test_reference_inside_repository_is_not_approved(self):
        local_reference = Path("tests/fixtures/image-1.png").resolve()
        result = worker.check_config(config(Path.cwd(), reference_path=local_reference), {})
        self.assertIn("approved_reference_inside_repository", result["issues"])

    def test_repeat_run_reuses_external_job(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root)
            provider = FakeVideoProvider()
            worker.process_pack(json.loads(manifest.read_text())["plan_id"], config=config(root), provider=provider, composer=DummyComposer())
            first = json.loads(manifest.read_text())["scene_jobs"][0]
            worker.process_pack(json.loads(manifest.read_text())["plan_id"], config=config(root), provider=provider, composer=DummyComposer())
            second = json.loads(manifest.read_text())["scene_jobs"][0]
            self.assertEqual(provider.submit_count, 1)
            self.assertEqual(first["external_job_id"], second["external_job_id"])

    def test_clean_and_story_complete_from_one_provider_master(self):
        with tempfile.TemporaryDirectory() as root:
            _, pack_dir, manifest = make_pack(root)
            provider_media = Path(root) / "provider.mp4"
            provider_media.write_bytes(b"real-provider-motion")
            provider = FakeVideoProvider(provider_media)
            plan_id = json.loads(manifest.read_text())["plan_id"]
            worker.process_pack(plan_id, config=config(root), provider=provider, composer=DummyComposer())
            job_id = json.loads(manifest.read_text())["scene_jobs"][0]["external_job_id"]
            provider.complete(job_id)
            worker.process_pack(plan_id, config=config(root), provider=provider, composer=DummyComposer())
            worker.process_pack(plan_id, config=config(root), provider=provider, composer=DummyComposer())
            current = json.loads(manifest.read_text())["scene_jobs"][0]
            self.assertEqual(provider.submit_count, 1)
            self.assertEqual(current["state"], "completed")
            self.assertTrue((pack_dir / current["clean_path"]).is_file())
            self.assertTrue((pack_dir / current["story_path"]).is_file())
            self.assertIn("CLEAN master", current["master_relation"])

    def test_resume_after_partial_success_submits_next_scene_only(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text())
            payload["scene_jobs"][0]["state"] = "completed"
            story.atomic_json(manifest, payload)
            provider = FakeVideoProvider()
            worker.process_pack(payload["plan_id"], config=config(root), provider=provider, composer=DummyComposer())
            current = json.loads(manifest.read_text())
            self.assertEqual(current["scene_jobs"][0]["state"], "completed")
            self.assertEqual(current["scene_jobs"][1]["state"], "submitted")
            self.assertEqual(provider.submit_count, 1)

    def test_retryable_poll_failure_keeps_paid_job_id(self):
        class RetryProvider(FakeVideoProvider):
            def retrieve(self, external_job_id):
                raise ProviderError("provider_transport_error", retryable=True)

        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root)
            provider = RetryProvider()
            plan_id = json.loads(manifest.read_text())["plan_id"]
            worker.process_pack(plan_id, config=config(root), provider=provider, composer=DummyComposer())
            job_id = json.loads(manifest.read_text())["scene_jobs"][0]["external_job_id"]
            worker.process_pack(plan_id, config=config(root), provider=provider, composer=DummyComposer())
            current = json.loads(manifest.read_text())["scene_jobs"][0]
            self.assertEqual(current["state"], "in_progress")
            self.assertEqual(current["external_job_id"], job_id)
            self.assertEqual(provider.submit_count, 1)

    def test_timeout_cancels_before_retry(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root)
            provider = FakeVideoProvider()
            plan_id = json.loads(manifest.read_text())["plan_id"]
            worker.process_pack(plan_id, config=config(root), provider=provider, composer=DummyComposer())
            payload = json.loads(manifest.read_text())
            job_id = payload["scene_jobs"][0]["external_job_id"]
            payload["scene_jobs"][0]["submitted_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
            story.atomic_json(manifest, payload)
            worker.process_pack(plan_id, config=config(root, poll_timeout_seconds=30), provider=provider, composer=DummyComposer())
            current = json.loads(manifest.read_text())["scene_jobs"][0]
            self.assertEqual(current["state"], "retryable_failed")
            self.assertIsNone(current["external_job_id"])
            self.assertIn(job_id, current["provider_job_history"])

    def test_terminal_provider_failure_is_terminal(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root)
            provider = FakeVideoProvider()
            plan_id = json.loads(manifest.read_text())["plan_id"]
            worker.process_pack(plan_id, config=config(root), provider=provider, composer=DummyComposer())
            job_id = json.loads(manifest.read_text())["scene_jobs"][0]["external_job_id"]
            provider.fail(job_id)
            worker.process_pack(plan_id, config=config(root), provider=provider, composer=DummyComposer())
            self.assertEqual(json.loads(manifest.read_text())["scene_jobs"][0]["state"], "terminal_failed")

    def test_missing_reference_blocks_only_face_scene(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text())
            payload["scene_jobs"][0]["requires_naz_reference"] = True
            payload["scenes"][0]["requires_naz_reference"] = True
            story.atomic_json(manifest, payload)
            provider = FakeVideoProvider()
            worker.process_pack(payload["plan_id"], config=config(root), provider=provider, composer=DummyComposer())
            worker.process_pack(payload["plan_id"], config=config(root), provider=provider, composer=DummyComposer())
            current = json.loads(manifest.read_text())["scene_jobs"]
            self.assertEqual(current[0]["state"], "blocked_reference")
            self.assertEqual(current[1]["state"], "submitted")

    def test_edited_secret_prompt_never_reaches_provider(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text())
            payload["scenes"][0]["provider_prompt"] += " API key sk-1234567890abcdef"
            story.atomic_json(manifest, payload)
            provider = FakeVideoProvider()
            worker.process_pack(payload["plan_id"], config=config(root), provider=provider, composer=DummyComposer())
            current = json.loads(manifest.read_text())["scene_jobs"][0]
            self.assertEqual(provider.submit_count, 0)
            self.assertEqual(current["failure_code"], "provider_prompt_unsafe")

    def test_missing_music_preserves_scenes_and_blocks_reels(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text())
            for job in payload["scene_jobs"]:
                job["state"] = "completed"
                job["actual_duration_seconds"] = float(job["planned_duration_seconds"])
            story.atomic_json(manifest, payload)
            worker.process_pack(payload["plan_id"], config=config(root), provider=FakeVideoProvider(), composer=DummyComposer())
            current = json.loads(manifest.read_text())
            self.assertTrue(all(job["state"] == "completed" for job in current["scene_jobs"]))
            self.assertTrue(all(job["state"] == "blocked_music" for job in current["reel_jobs"]))
            self.assertEqual(current["pack_status"], "blocked_music")

    def test_legacy_manifest_is_read_only(self):
        with tempfile.TemporaryDirectory() as root:
            pack_dir = Path(root) / "legacy"
            pack_dir.mkdir()
            manifest = pack_dir / "story_manifest.json"
            original = {"schema": story.LEGACY_STORY_SCHEMA, "plan_id": "legacy"}
            manifest.write_text(json.dumps(original), encoding="utf-8")
            status = worker.process_pack("legacy", config=config(root), provider=FakeVideoProvider(), composer=DummyComposer())
            self.assertEqual(status, "legacy_manifest_read_only")
            self.assertEqual(json.loads(manifest.read_text()), original)


class ControlTests(unittest.TestCase):
    def test_approval_is_idempotent_and_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root, approved=False)
            self.assertEqual(control.approve_pack(Path(root), pack.plan_id), "approved")
            self.assertEqual(control.approve_pack(Path(root), pack.plan_id), "already_approved")
            payload = json.loads(manifest.read_text())
            self.assertEqual(payload["approval"]["status"], "approved")
            self.assertTrue(all(not job["external_job_id"] for job in payload["scene_jobs"]))

    def test_other_variant_is_free_and_supersedes_previous_plan(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root, approved=False)
            new_dir = control.create_next_variant(Path(root), pack.plan_id)
            old = json.loads(manifest.read_text())
            new = json.loads((new_dir / "story_manifest.json").read_text())
            self.assertEqual(old["pack_status"], "superseded")
            self.assertNotEqual(old["plan_id"], new["plan_id"])
            self.assertEqual(new["approval"]["status"], "awaiting_approval")
            self.assertNotEqual(old["scenes"], new["scenes"])

    def test_variant_is_forbidden_after_approval(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, _ = make_pack(root)
            with self.assertRaises(story.StoryPlanError):
                control.create_next_variant(Path(root), pack.plan_id)

    def test_safe_summary_does_not_expose_facts_or_prompts(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root, approved=False)
            payload = json.loads(manifest.read_text())
            summary = control.safe_summary(payload)
            self.assertNotIn(SAFE_FACTS[0], summary)
            self.assertNotIn(payload["scenes"][0]["provider_prompt"], summary)


class MediaTests(unittest.TestCase):
    def test_private_music_folder_requires_license_sidecar(self):
        with tempfile.TemporaryDirectory() as root:
            music = Path(root) / "licensed.m4a"
            music.write_bytes(b"licensed-audio")
            self.assertEqual(load_music_library(Path(root)), [])
            music.with_suffix(".m4a.json").write_text(
                json.dumps({"bpm": 120, "license": "owned", "source": "private-library"}),
                encoding="utf-8",
            )
            tracks = load_music_library(Path(root))
            self.assertEqual(len(tracks), 1)
            self.assertEqual(tracks[0].path, music.resolve())
            self.assertGreater(len(tracks[0].beat_grid), 10)

    def test_probe_rejects_resolution_duration_and_codec(self):
        cases = [
            ({"codec_name": "vp9", "pix_fmt": "yuv420p", "width": 1080, "height": 1920, "avg_frame_rate": "30/1"}, 4.0, "codec"),
            ({"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"}, 4.0, "resolution"),
            ({"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1080, "height": 1920, "avg_frame_rate": "30/1"}, 9.0, "duration"),
        ]
        for stream, duration, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as root:
                path = Path(root) / "clip.mp4"
                path.write_bytes(b"0000ftyp0000")
                def runner(args, **kwargs):
                    if "-show_entries" in args:
                        return subprocess.CompletedProcess(args, 0, json.dumps({"streams": [stream], "format": {"duration": duration}}), "")
                    return subprocess.CompletedProcess(args, 0, "lavfi.signalstats.YDIF=2.0", "")
                with self.assertRaises(MediaError):
                    MediaComposer(runner=runner).probe(path)

    def test_command_runner_receives_argument_list(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "clip.mp4"
            path.write_bytes(b"0000ftyp0000")
            seen = []
            def runner(args, **kwargs):
                seen.append(args)
                if "-show_entries" in args:
                    data = {"streams": [{"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1080, "height": 1920, "avg_frame_rate": "30/1"}], "format": {"duration": 4.0}}
                    return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
                return subprocess.CompletedProcess(args, 0, "lavfi.signalstats.YDIF=2.0", "")
            MediaComposer(runner=runner).probe(path)
            self.assertTrue(all(isinstance(args, list) for args in seen))

    def test_reel_composer_applies_real_crop_filter(self):
        with tempfile.TemporaryDirectory() as root:
            pack_root = Path(root)
            source = pack_root / "stories" / "01_clean.mp4"
            source.parent.mkdir()
            source.write_bytes(b"0000ftyp" + b"x" * 32)
            music = pack_root / "licensed.m4a"
            music.write_bytes(b"licensed-audio")
            calls = []
            def runner(args, **kwargs):
                calls.append(args)
                if "-show_entries" in args:
                    data = {"streams": [{"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1080, "height": 1920, "avg_frame_rate": "30/1"}], "format": {"duration": 4.0}}
                    return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
                if "-filter_complex" in args:
                    Path(args[-1]).write_bytes(b"0000ftyp" + b"y" * 32)
                return subprocess.CompletedProcess(args, 0, "lavfi.signalstats.YDIF=2.0", "")
            track = LicensedTrack("licensed", music, 75.0, (0.0, 0.8), "owned", "local", checksum(music))
            output = pack_root / "reels" / "reel.mp4"
            output.parent.mkdir()
            MediaComposer(runner=runner).compose_reel(
                pack_root=pack_root,
                shots=[{"source": "stories/01_clean.mp4", "in_seconds": 0.0,
                        "duration_seconds": 0.8, "reel_crop": "tight-center"}],
                destination=output, track=track,
            )
            filter_value = next(call[call.index("-filter_complex") + 1] for call in calls if "-filter_complex" in call)
            self.assertIn("scale=ceil(iw*1.18", filter_value)
            self.assertIn("crop=1080:1920", filter_value)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe unavailable")
    def test_ffmpeg_probe_accepts_synthetic_moving_portrait(self):
        with tempfile.TemporaryDirectory() as root:
            clip = Path(root) / "moving.mp4"
            subprocess.run([
                shutil.which("ffmpeg"), "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=270x480:rate=30", "-t", "4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(clip),
            ], check=True, timeout=60)
            probe = MediaComposer(timeout_seconds=60).probe(clip)
            self.assertGreater(probe.motion_score, 0.25)
            self.assertEqual((probe.codec, probe.pixel_format), ("h264", "yuv420p"))


class ManifestTests(unittest.TestCase):
    def test_manifest_contains_observability_and_one_master_relation(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root, approved=False)
            payload = json.loads(manifest.read_text())
            self.assertEqual(payload["schema"], story.STORY_SCHEMA)
            self.assertEqual(payload["pack_status"], "awaiting_approval")
            self.assertIn("created_at", payload)
            self.assertIn("updated_at", payload)
            self.assertEqual(payload["persona"], "naz")
            self.assertEqual(payload["destination"], "telegram")
            self.assertIn("content", payload["policy_versions"])
            self.assertEqual(len(payload["scene_jobs"]), pack.scene_count)
            for job in payload["scene_jobs"]:
                self.assertIn("local overlay", job["master_relation"])
                self.assertTrue(job["clean_path"].endswith("_clean.mp4"))
                self.assertTrue(job["story_path"].endswith("_story.mp4"))
                self.assertEqual(job["visual_identity_qa"]["status"], "not_run")
            for scene in payload["scenes"]:
                factual_sentence = scene["standalone_meaning"].partition(":")[2].strip()
                self.assertNotIn(factual_sentence, scene["provider_prompt"])

    def test_secrets_are_rejected_before_manifest_or_provider(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(story.StoryPlanError):
                story.plan_story_pack(planned(), ("API key=top-secret", *SAFE_FACTS[1:]))
            self.assertEqual(list(Path(root).rglob("story_manifest.json")), [])

    def test_schedules_unchanged(self):
        env_text = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn("AUTOPOST_TIMES=10:00,14:00,18:00,22:00", env_text)
        self.assertIn("NAZ_TELEGRAM_AUTO_TIMES=10:00,14:00,18:00,22:00", env_text)
        self.assertIn("NAZ_VK_DAILY_TIME=10:30", env_text)
        self.assertIn("NAZ_VK_GAMING_TIME=16:30", env_text)

    def test_worker_units_use_canonical_env_and_have_no_publication_command(self):
        service = Path("deploy/systemd/naz-story-worker.service").read_text(encoding="utf-8")
        timer = Path("deploy/systemd/naz-story-worker.timer").read_text(encoding="utf-8")
        self.assertIn("EnvironmentFile=/opt/naz-ai-bot/.env", service)
        self.assertIn("-m naz_story_worker --once", service)
        self.assertIn("ReadWritePaths=/var/lib/naz-ai-bot", service)
        self.assertNotIn("publish", service.casefold())
        self.assertNotIn("OnCalendar", timer)
        self.assertIn("OnUnitInactiveSec=2min", timer)


if __name__ == "__main__":
    unittest.main()
