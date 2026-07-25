import dataclasses
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import naz_story_worker as worker
import story_pack_control as control
import story_production as story
from story_audio_evidence import eligible_segment_starts
from story_media_composer import LicensedTrack, MediaComposer, MediaError, MediaProbe, checksum, load_music_library
from story_pack_lock import StoryPackLock, StoryPackLockError
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
    def test_budget_counts_only_accepted_or_uncertain_provider_submissions(self):
        with tempfile.TemporaryDirectory() as root:
            _, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            now = datetime.now(timezone.utc).isoformat()
            jobs = payload["scene_jobs"]
            jobs[0]["submit_intent"] = {
                "created_at": now, "state": "rejected", "failure_code": "provider_input_invalid",
            }
            jobs[0]["attempts"] = 1
            jobs[1]["submit_intent"] = {
                "created_at": now, "state": "accepted", "external_job_id": "accepted-1",
            }
            jobs[1]["attempts"] = 1
            jobs[2]["submit_intent"] = {
                "created_at": now, "state": "ambiguous", "failure_code": "provider_submit_outcome_ambiguous",
            }
            jobs[2]["attempts"] = 1
            story.atomic_json(manifest, payload)

            self.assertEqual(worker._budget_usage(Path(root)), (2, 10))

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

    def test_approved_stale_v2_is_rejected_before_submit(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["scenes"][0]["duration_seconds"] = 4
            story.atomic_json(manifest, payload)
            provider = FakeVideoProvider()

            with self.assertRaisesRegex(RuntimeError, "story_manifest_contract_stale"):
                worker.process_pack(
                    pack.plan_id, config=config(root), provider=provider,
                    composer=DummyComposer(),
                )
            self.assertEqual(provider.submit_count, 0)

    def test_ambiguous_submit_outcomes_never_post_twice(self):
        class TimeoutSubmitProvider(FakeVideoProvider):
            def submit(self, request):
                self.submit_count += 1
                raise ProviderError("provider_transport_error", retryable=True)

        class AcceptedWithoutIdProvider(FakeVideoProvider):
            def submit(self, request):
                self.submit_count += 1
                return ProviderJob("", "submitted")

        for provider_type in (TimeoutSubmitProvider, AcceptedWithoutIdProvider):
            with self.subTest(provider=provider_type.__name__), tempfile.TemporaryDirectory() as root:
                pack, _, manifest = make_pack(root)
                provider = provider_type()
                first = worker.process_pack(
                    pack.plan_id, config=config(root), provider=provider,
                    composer=DummyComposer(),
                )
                second = worker.process_pack(
                    pack.plan_id, config=config(root), provider=provider,
                    composer=DummyComposer(),
                )
                current = json.loads(manifest.read_text(encoding="utf-8"))["scene_jobs"][0]
                self.assertEqual((first, second), ("submit_ambiguous", "submit_ambiguous"))
                self.assertEqual(current["state"], "submit_ambiguous")
                self.assertEqual(current["failure_code"], "provider_submit_outcome_ambiguous")
                self.assertEqual(current["submit_intent"]["state"], "ambiguous")
                self.assertEqual(provider.submit_count, 1)

    def test_crash_left_submitting_intent_is_blocked_without_post(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            job = payload["scene_jobs"][0]
            job.update({
                "state": "submitting",
                "attempts": 1,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "provider_status": "submit_intent_persisted",
                "submit_intent": {
                    "intent_id": "durable-intent-before-crash",
                    "model": "gen4_turbo",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "state": "submitting",
                    "failure_code": None,
                },
            })
            story.atomic_json(manifest, payload)
            provider = FakeVideoProvider()

            self.assertEqual(
                worker.process_pack(
                    pack.plan_id, config=config(root), provider=provider,
                    composer=DummyComposer(),
                ),
                "submit_ambiguous",
            )
            self.assertEqual(
                worker.process_pack(
                    pack.plan_id, config=config(root), provider=provider,
                    composer=DummyComposer(),
                ),
                "submit_ambiguous",
            )
            self.assertEqual(provider.submit_count, 0)
            current = json.loads(manifest.read_text(encoding="utf-8"))["scene_jobs"][0]
            self.assertEqual(current["state"], "submit_ambiguous")
            self.assertEqual(current["failure_code"], "provider_submit_outcome_ambiguous")

    def test_process_pack_enforces_canonical_model_roles_before_provider_use(self):
        invalid_routes = (
            {"primary_model": "gen4.5", "secondary_model": "gen4_turbo"},
            {"primary_model": "gen4.5"},
            {"secondary_model": "gen4_turbo"},
            {"model_priority": ("gen4.5", "gen4_turbo")},
        )
        for changes in invalid_routes:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as root:
                pack, _, _ = make_pack(root)
                provider = FakeVideoProvider()
                with self.assertRaisesRegex(RuntimeError, "video_model_priority_invalid"):
                    worker.process_pack(
                        pack.plan_id,
                        config=config(root, **changes),
                        provider=provider,
                        composer=DummyComposer(),
                    )
                self.assertEqual(provider.submit_count, 0)

        with tempfile.TemporaryDirectory() as root:
            pack, _, _ = make_pack(root)
            with self.assertRaisesRegex(RuntimeError, "video_model_priority_invalid"):
                worker.process_pack(
                    pack.plan_id,
                    config=config(root, primary_model="gen4.5"),
                    composer=DummyComposer(),
                    env={},
                )

    def test_manifest_cannot_override_canonical_model_roles(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["scene_jobs"][0]["model_route"].update({
                "primary_model": "gen4.5",
                "secondary_model": "gen4_turbo",
            })
            story.atomic_json(manifest, payload)
            provider = FakeVideoProvider()
            with self.assertRaisesRegex(RuntimeError, "video_model_route_mismatch"):
                worker.process_pack(
                    pack.plan_id,
                    config=config(root),
                    provider=provider,
                    composer=DummyComposer(),
                )
            self.assertEqual(provider.submit_count, 0)

    def test_manifest_cannot_self_approve_or_forge_secondary_route(self):
        tampered_routes = (
            {
                "tier": "secondary", "secondary_approved_at": "2026-07-25T00:00:00+00:00",
            },
            {
                "tier": "secondary", "secondary_requested_at": "2026-07-25T00:00:00+00:00",
                "secondary_approved_at": "2026-07-25T00:01:00+00:00",
                "primary_failure_code": "provider_timeout",
            },
            {
                "tier": "secondary", "secondary_requested_at": "2026-07-25T00:00:00+00:00",
                "primary_failure_code": "provider_terminal_failure",
            },
        )
        for route_changes in tampered_routes:
            with self.subTest(route=route_changes), tempfile.TemporaryDirectory() as root:
                pack, _, manifest = make_pack(root)
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload["scene_jobs"][0]["model_route"].update(route_changes)
                story.atomic_json(manifest, payload)
                provider = FakeVideoProvider()
                with self.assertRaisesRegex(RuntimeError, "video_model_route_mismatch"):
                    worker.process_pack(
                        pack.plan_id,
                        config=config(root),
                        provider=provider,
                        composer=DummyComposer(),
                    )
                self.assertEqual(provider.submit_count, 0)

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
            payload["scene_jobs"][0]["reference_role"] = "frontal_identity"
            payload["scenes"][0]["requires_naz_reference"] = True
            payload["scenes"][0]["reference_role"] = "frontal_identity"
            story.atomic_json(manifest, payload)
            provider = FakeVideoProvider()
            worker.process_pack(payload["plan_id"], config=config(root), provider=provider, composer=DummyComposer())
            worker.process_pack(payload["plan_id"], config=config(root), provider=provider, composer=DummyComposer())
            current = json.loads(manifest.read_text())["scene_jobs"]
            self.assertEqual(current[0]["state"], "blocked_reference")
            self.assertEqual(current[1]["state"], "submitted")

    def test_turbo_text_only_pack_waits_for_one_secondary_confirmation(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root)
            transport = MockTransport([])
            primary = RunwayVideoProvider(
                api_key="dedicated-key", model="gen4_turbo", transport=transport
            )
            cfg = dataclasses.replace(
                config(root), provider_name="runway", model="gen4_turbo",
                primary_model="gen4_turbo", secondary_model="gen4.5",
                model_priority=("gen4_turbo", "gen4.5"), auto_fallback=False,
            )
            status = worker.process_pack(
                pack.plan_id, config=cfg, provider=primary, composer=DummyComposer()
            )
            self.assertEqual(status, "awaiting_secondary_approval")
            self.assertEqual(transport.calls, [])
            pending = json.loads(manifest.read_text(encoding="utf-8"))["scene_jobs"]
            self.assertTrue(all(job["state"] == "awaiting_secondary_approval" for job in pending))
            self.assertTrue(all(job["attempts"] == 0 for job in pending))

            self.assertEqual(
                control.confirm_generation(Path(root), pack.plan_id), "secondary_approved"
            )
            approved = json.loads(manifest.read_text(encoding="utf-8"))["scene_jobs"]
            self.assertTrue(all(job["model_route"]["secondary_approved_at"] for job in approved))

            secondary_transport = MockTransport([
                (200, {"Content-Type": "application/json"}, b'{"id":"secondary-task"}')
            ])
            secondary = RunwayVideoProvider(
                api_key="dedicated-key", model="gen4.5", transport=secondary_transport
            )
            status = worker.process_pack(
                pack.plan_id, config=cfg, provider=secondary, composer=DummyComposer()
            )
            self.assertEqual(status, "submitted")
            self.assertEqual(len(secondary_transport.calls), 1)

    def test_secondary_escalation_has_exact_model_failure_allowlist(self):
        self.assertEqual(
            worker.SECONDARY_ESCALATION_CODES,
            frozenset({"video_prompt_image_required", "provider_terminal_failure"}),
        )
        cfg = config(
            Path.cwd(), provider_name="runway", model="gen4_turbo",
            primary_model="gen4_turbo", secondary_model="gen4.5",
            model_priority=("gen4_turbo", "gen4.5"),
        )
        primary = RunwayVideoProvider(
            api_key="dedicated-key", model="gen4_turbo", transport=MockTransport([])
        )
        excluded = (
            "provider_prompt_unsafe", "video_model_unsupported",
            "provider_transport_error", "provider_timeout",
            "provider_status_unknown", "provider_download_failed",
            "provider_download_not_mp4", "media_tool_failed",
            "cyrillic_font_missing", "overlay_text_unsafe",
            "approved_reference_invalid",
        )
        for code in excluded:
            with self.subTest(code=code):
                job = {"state": "terminal_failed", "model_route": {}}
                payload = {"pack_status": "failed"}
                self.assertFalse(
                    worker._request_secondary(job, payload, code, cfg, primary)
                )
                self.assertEqual(job["state"], "terminal_failed")
                self.assertIsNone(job["model_route"]["secondary_requested_at"])

        for code in worker.SECONDARY_ESCALATION_CODES:
            with self.subTest(allowed=code):
                job = {"state": "terminal_failed", "model_route": {}}
                payload = {"pack_status": "failed"}
                self.assertTrue(
                    worker._request_secondary(job, payload, code, cfg, primary)
                )
                self.assertEqual(job["state"], "awaiting_secondary_approval")
                self.assertIsNotNone(job["model_route"]["secondary_requested_at"])

    def test_reference_profile_selects_secondary_and_adds_body_guidance_only_at_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            ref_dir = Path(root) / "private-references"
            ref_dir.mkdir()
            (ref_dir / "naz-primary.jpg").write_bytes(b"primary")
            (ref_dir / "naz-secondary.jpg").write_bytes(b"secondary")
            (ref_dir / "naz-reference-profile.json").write_text(json.dumps({
                "schema": "naz-reference-profile.v1", "persona": "naz",
                "reference_files": {
                    "primary": "naz-primary.jpg", "secondary": "naz-secondary.jpg",
                },
                "body_profile": {
                    "height_cm": 185, "weight_kg": 80, "build": "tall, lean-athletic",
                    "visual_guidance": "Long balanced proportions; fit but not bulky.",
                },
            }), encoding="utf-8")
            _, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["scenes"][0].update({
                "requires_naz_reference": True,
                "reference_role": "three_quarter_identity",
            })
            payload["scene_jobs"][0].update({
                "requires_naz_reference": True,
                "reference_role": "three_quarter_identity",
            })
            story.atomic_json(manifest, payload)
            provider = FakeVideoProvider()
            worker.process_pack(
                payload["plan_id"],
                config=config(root, reference_path=ref_dir),
                provider=provider, composer=DummyComposer(),
            )
            request = provider.submissions[0]
            self.assertEqual(request.reference_path, (ref_dir / "naz-secondary.jpg").resolve())
            self.assertIn("185 cm", request.prompt)
            self.assertIn("80 kg", request.prompt)
            persisted = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertNotIn("185 cm", persisted["scenes"][0]["provider_prompt"])

    def test_reference_profile_rejects_filename_traversal(self):
        with tempfile.TemporaryDirectory() as root:
            ref_dir = Path(root) / "references"
            ref_dir.mkdir()
            (ref_dir / "naz-reference-profile.json").write_text(json.dumps({
                "schema": "naz-reference-profile.v1", "persona": "naz",
                "reference_files": {"primary": "../outside.jpg", "secondary": "naz-secondary.jpg"},
            }), encoding="utf-8")
            (Path(root) / "outside.jpg").write_bytes(b"outside")
            (ref_dir / "naz-secondary.jpg").write_bytes(b"secondary")
            catalog = worker._reference_catalog(ref_dir)
            self.assertNotIn("frontal_identity", catalog)
            self.assertIn("three_quarter_identity", catalog)

    def test_malformed_reference_profiles_fail_closed_without_type_errors(self):
        valid_base = {
            "schema": "naz-reference-profile.v1",
            "persona": "naz",
            "reference_files": {
                "primary": "naz-primary.jpg", "secondary": "naz-secondary.jpg",
            },
            "body_profile": {
                "height_cm": 185, "weight_kg": 80,
                "build": "tall, lean-athletic",
                "visual_guidance": "Long balanced proportions; fit but not bulky.",
            },
        }
        malformed = {
            "invalid_json": "{not-json",
            "profile_list": json.dumps([]),
            "profile_scalar": json.dumps(42),
            "reference_files_list": json.dumps({**valid_base, "reference_files": []}),
            "reference_files_scalar": json.dumps({**valid_base, "reference_files": 42}),
            "reference_filename_list": json.dumps({
                **valid_base,
                "reference_files": {"primary": [], "secondary": "naz-secondary.jpg"},
            }),
            "body_profile_list": json.dumps({**valid_base, "body_profile": []}),
            "body_profile_scalar": json.dumps({**valid_base, "body_profile": "185/80"}),
        }
        with tempfile.TemporaryDirectory() as root:
            ref_dir = Path(root) / "references"
            ref_dir.mkdir()
            (ref_dir / "naz-primary.jpg").write_bytes(b"primary")
            (ref_dir / "naz-secondary.jpg").write_bytes(b"secondary")
            profile = ref_dir / "naz-reference-profile.json"
            for case, raw in malformed.items():
                with self.subTest(case=case):
                    profile.write_text(raw, encoding="utf-8")
                    self.assertEqual(worker._reference_catalog(ref_dir), {})

    def test_auto_fallback_is_rejected_before_provider_use(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, _ = make_pack(root)
            cfg = dataclasses.replace(config(root), auto_fallback=True)
            provider = FakeVideoProvider()
            with self.assertRaisesRegex(RuntimeError, "video_auto_fallback_forbidden"):
                worker.process_pack(
                    pack.plan_id, config=cfg, provider=provider, composer=DummyComposer()
                )
            self.assertEqual(provider.submit_count, 0)

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
            self.assertEqual(current["state"], "terminal_failed")
            self.assertIsNone(current["model_route"]["secondary_requested_at"])

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

    def test_queue_ignores_old_waiting_and_superseded_packs_for_new_approved_pack(self):
        with tempfile.TemporaryDirectory() as root:
            plan = planned()
            waiting = story.plan_story_pack(plan, SAFE_FACTS, variant_index=0)
            story.persist_story_queue(waiting, Path(root))

            superseded = story.plan_story_pack(plan, SAFE_FACTS, variant_index=1)
            superseded_manifest = (
                story.persist_story_queue(superseded, Path(root)) / "story_manifest.json"
            )
            stale = json.loads(superseded_manifest.read_text(encoding="utf-8"))
            stale["approval"]["status"] = "superseded"
            stale["pack_status"] = "superseded"
            story.atomic_json(superseded_manifest, stale)

            approved = story.plan_story_pack(plan, SAFE_FACTS, variant_index=2)
            story.persist_story_queue(approved, Path(root))
            control.approve_pack(Path(root), approved.plan_id)

            self.assertEqual(worker._queued_plan_ids(Path(root)), [approved.plan_id])

    def test_blocked_music_pack_remains_queue_eligible(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            for job in payload["scene_jobs"]:
                job["state"] = "completed"
            for job in payload["reel_jobs"]:
                job["state"] = "blocked_music"
                job["failure_code"] = "licensed_music_invalid"
            payload["pack_status"] = "blocked_music"
            story.atomic_json(manifest, payload)

            self.assertEqual(worker._queued_plan_ids(Path(root)), [pack.plan_id])


class SharedQueuePermissionTests(unittest.TestCase):
    def test_persist_marks_every_shared_queue_path(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            story, "ensure_private_group_access"
        ) as shared_access:
            pack = story.plan_story_pack(planned(), SAFE_FACTS)
            pack_dir = story.persist_story_queue(pack, Path(root))

        calls = {(call.args[0], call.kwargs["directory"]) for call in shared_access.call_args_list}
        self.assertTrue({
            (pack_dir, True),
            (pack_dir / "stories", True),
            (pack_dir / "reels", True),
            (pack_dir / "story_manifest.json", False),
            (pack_dir / "caption_pack.md", False),
        }.issubset(calls))

    @unittest.skipIf(os.name == "nt", "Unix group permissions are deployment-specific")
    def test_manifest_and_lock_remain_private_but_group_writable(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            root_path.chmod(0o2770)
            pack = story.plan_story_pack(planned(), SAFE_FACTS)
            pack_dir = story.persist_story_queue(pack, root_path)
            manifest = pack_dir / "story_manifest.json"

            self.assertEqual(stat.S_IMODE(pack_dir.stat().st_mode), 0o2770)
            self.assertEqual(stat.S_IMODE((pack_dir / "stories").stat().st_mode), 0o2770)
            self.assertEqual(stat.S_IMODE((pack_dir / "reels").stat().st_mode), 0o2770)
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o660)
            self.assertEqual(stat.S_IMODE((pack_dir / "caption_pack.md").stat().st_mode), 0o660)

            payload = story.read_manifest(manifest)
            story.atomic_json(manifest, payload)
            self.assertEqual(stat.S_IMODE(manifest.stat().st_mode), 0o660)

            with StoryPackLock(pack_dir):
                self.assertEqual(
                    stat.S_IMODE((pack_dir / ".pack.lock").stat().st_mode), 0o660
                )


class ControlTests(unittest.TestCase):
    def test_control_and_worker_share_one_nonblocking_pack_lock(self):
        with tempfile.TemporaryDirectory() as root:
            pack_dir = Path(root) / "pack"
            with StoryPackLock(pack_dir):
                with self.assertRaises(StoryPackLockError):
                    with StoryPackLock(pack_dir):
                        pass
            with StoryPackLock(pack_dir):
                pass

    def test_approval_is_idempotent_and_never_calls_provider(self):
        with tempfile.TemporaryDirectory() as root:
            pack, _, manifest = make_pack(root, approved=False)
            self.assertEqual(control.approve_pack(Path(root), pack.plan_id), "approved")
            self.assertEqual(control.approve_pack(Path(root), pack.plan_id), "already_approved")
            payload = json.loads(manifest.read_text())
            self.assertEqual(payload["approval"]["status"], "approved")
            self.assertTrue(all(not job["external_job_id"] for job in payload["scene_jobs"]))

    def test_approval_controls_reject_stale_v2_before_mutation(self):
        for action in (control.approve_pack, control.confirm_generation):
            with self.subTest(action=action.__name__), tempfile.TemporaryDirectory() as root:
                pack, _, manifest = make_pack(root, approved=False)
                payload = json.loads(manifest.read_text(encoding="utf-8"))
                payload.pop("model_policy")
                story.atomic_json(manifest, payload)
                before = manifest.read_bytes()

                with self.assertRaisesRegex(
                    story.StoryPlanError, "story_manifest_contract_stale"
                ):
                    action(Path(root), pack.plan_id)
                self.assertEqual(manifest.read_bytes(), before)

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

    def test_generated_audio_sidecar_is_compatible_and_rights_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            music = Path(root) / "naz-midnight-wave-01.mp3"
            music.write_bytes(b"ID3" + b"a" * 128)
            sidecar = music.with_suffix(".mp3.json")
            row = {
                "schema": "naz-story-audio-track-v1",
                "track_id": "naz-midnight-wave-01", "lane": "midnight_wave",
                "tags": ["night", "focus"], "bpm": 120,
                "duration_seconds": 60, "beats_per_bar": 4,
                "beat_grid": [value * 0.5 for value in range(121)],
                "beat_grid_source": "actual-audio-derived-beat-track-v1",
                "beat_evidence": [True for _ in range(121)],
                "beat_evidence_source": "actual-audio-derived-onset-match-v1",
                "audio_analysis": {
                    "source": "actual-audio-derived-beat-track-v1",
                    "analyzer": "test-pcm-onset-v1",
                    "confidence": 1.0,
                    "peak_prominence": 1.0,
                    "onset_alignment_fraction": 1.0,
                    "grid_onset_coverage": 1.0,
                },
                "license": "stability-generated-output",
                "source": "naz-private-generated-library",
                "checksum": checksum(music),
                "rights": {
                    "origin": "text_to_audio", "provider": "stability-ai",
                    "model": "stable-audio-3", "third_party_audio_input": False,
                    "artist_or_track_reference": False,
                },
            }
            sidecar.write_text(json.dumps(row), encoding="utf-8")
            tracks = load_music_library(Path(root))
            self.assertEqual([track.track_id for track in tracks], ["naz-midnight-wave-01"])
            self.assertEqual(tracks[0].duration_seconds, 60)
            self.assertEqual(tracks[0].tags, ("night", "focus"))
            self.assertTrue(tracks[0].evidence_required)
            self.assertEqual(len(tracks[0].beat_evidence), len(tracks[0].beat_grid))

            expected_checksum = row.pop("checksum")
            sidecar.write_text(json.dumps(row), encoding="utf-8")
            self.assertEqual(load_music_library(Path(root)), [])
            row["checksum"] = expected_checksum
            row["rights"]["artist_or_track_reference"] = True
            sidecar.write_text(json.dumps(row), encoding="utf-8")
            self.assertEqual(load_music_library(Path(root)), [])

            row["rights"]["artist_or_track_reference"] = False
            row["beat_grid_source"] = "declared-tempo-grid-v1"
            sidecar.write_text(json.dumps(row), encoding="utf-8")
            self.assertEqual(load_music_library(Path(root)), [])

            row["beat_grid_source"] = "actual-audio-derived-beat-track-v1"
            row["beat_evidence"] = row["beat_evidence"][:-1]
            sidecar.write_text(json.dumps(row), encoding="utf-8")
            self.assertEqual(load_music_library(Path(root)), [])

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
                    duration = 12.0 if "reels" in str(args[-1]).replace("\\", "/") else 4.0
                    data = {"streams": [{"codec_name": "h264", "pix_fmt": "yuv420p", "width": 1080, "height": 1920, "avg_frame_rate": "30/1"}], "format": {"duration": duration}}
                    return subprocess.CompletedProcess(args, 0, json.dumps(data), "")
                if "-filter_complex" in args:
                    Path(args[-1]).write_bytes(b"0000ftyp" + b"y" * 32)
                return subprocess.CompletedProcess(args, 0, "lavfi.signalstats.YDIF=2.0", "")
            track = LicensedTrack(
                "licensed", music, 120.0,
                tuple(float(value) for value in range(0, 14, 2)),
                "owned", "local", checksum(music),
            )
            output = pack_root / "reels" / "reel.mp4"
            output.parent.mkdir()
            MediaComposer(runner=runner).compose_reel(
                pack_root=pack_root,
                shots=[
                    {"source": "stories/01_clean.mp4", "in_seconds": 0.0,
                     "duration_seconds": 2.0, "reel_crop": "tight-center"}
                    for _ in range(6)
                ],
                destination=output, track=track,
            )
            filter_value = next(call[call.index("-filter_complex") + 1] for call in calls if "-filter_complex" in call)
            self.assertIn("scale=ceil(iw*1.18", filter_value)
            self.assertIn("crop=1080:1920", filter_value)
            compose_call = next(call for call in calls if "-filter_complex" in call)
            self.assertIn("-ss", compose_call)
            self.assertNotIn("-stream_loop", compose_call)

    def test_composer_rejects_manually_selected_weak_generated_window(self):
        class GuardComposer(MediaComposer):
            def __init__(self):
                super().__init__()
                self.ffmpeg_called = False

            def probe(self, path, **kwargs):
                return MediaProbe(4.0, 1080, 1920, "h264", "yuv420p", "30/1", 2.0)

            def _run(self, args):
                self.ffmpeg_called = True
                raise AssertionError("ffmpeg must not run for a weak rhythm window")

        with tempfile.TemporaryDirectory() as root:
            pack_root = Path(root)
            source = pack_root / "stories" / "01_clean.mp4"
            source.parent.mkdir()
            source.write_bytes(b"0000ftyp" + b"x" * 32)
            music = pack_root / "generated.mp3"
            music.write_bytes(b"ID3" + b"a" * 128)
            grid = tuple(index * 0.5 for index in range(121))
            evidence = tuple(beat <= 15.0 for beat in grid)
            track = LicensedTrack(
                "generated", music, 120.0, grid,
                "stability-generated-output", "naz-private-generated-library",
                checksum(music), 60.0, (), "lane", 4,
                evidence, True,
            )
            composer = GuardComposer()
            with self.assertRaises(MediaError) as caught:
                composer.compose_reel(
                    pack_root=pack_root,
                    shots=[{
                        "source": "stories/01_clean.mp4", "in_seconds": 0.0,
                        "duration_seconds": 2.0, "reel_crop": "tight-center",
                    } for _ in range(6)],
                    destination=pack_root / "reels" / "reel.mp4",
                    track=track,
                    track_start_seconds=30.0,
                    segment_beat_grid=tuple(index * 0.5 for index in range(25)),
                )
            self.assertEqual(caught.exception.code, "licensed_music_segment_invalid")
            self.assertFalse(composer.ffmpeg_called)

    def test_story_music_rotation_uses_all_eight_tracks_before_reuse(self):
        with tempfile.TemporaryDirectory() as root:
            state_path = Path(root) / "rotation.json"
            tracks = [
                LicensedTrack(
                    f"track-{index}", Path(root) / f"track-{index}.mp3", 120.0,
                    tuple(index * 0.5 for index in range(121)),
                    "owned", "private", f"checksum-{index}", 60.0,
                    ("focus",) if index == 0 else ("night",), "lane", 4,
                )
                for index in range(8)
            ]
            selected = []
            for index in range(8):
                reservation = f"plan:{index}"
                track = worker._reserve_track(
                    tracks=tracks, tags={"focus"}, state_path=state_path,
                    reservation_id=reservation,
                    duration_seconds=14.0,
                )
                selected.append(track.track_id)
                worker._complete_track_rotation(state_path, reservation, track.track_id)
            self.assertEqual(len(set(selected)), 8)
            ninth = worker._reserve_track(
                tracks=tracks, tags=set(), state_path=state_path,
                reservation_id="plan:8",
                duration_seconds=14.0,
            )
            self.assertEqual(ninth.track_id, selected[0])

    def test_music_segment_is_bar_aligned_and_long_enough_for_reel(self):
        track = LicensedTrack(
            "track", Path("track.mp3"), 120.0,
            tuple(index * 0.5 for index in range(121)),
            "owned", "private", "checksum", 60.0, (), "lane", 4,
        )
        start, grid = worker._segment_grid(track, duration_seconds=14.0, seed="plan:edit")
        self.assertAlmostEqual(start % 2.0, 0.0, places=5)
        self.assertAlmostEqual(grid[0], 0.0, places=5)
        self.assertGreaterEqual(grid[-1], 14.0)

    def test_rhythm_only_in_first_quarter_excludes_weak_segment_windows(self):
        beat_grid = tuple(index * 0.5 for index in range(121))
        evidence = tuple(beat <= 15.0 for beat in beat_grid)
        starts = eligible_segment_starts(
            beat_grid=beat_grid,
            beat_evidence=evidence,
            evidence_required=True,
            track_duration_seconds=60.0,
            segment_duration_seconds=14.0,
            beats_per_bar=4,
        )
        self.assertTrue(starts)
        self.assertLessEqual(max(starts), 2.0)
        track = LicensedTrack(
            "generated", Path("track.mp3"), 120.0, beat_grid,
            "generated", "private", "checksum", 60.0, (), "lane", 4,
            evidence, True,
        )
        start, _ = worker._segment_grid(track, duration_seconds=14.0, seed="plan:edit")
        self.assertIn(start, starts)

    def test_new_reservation_filters_tracks_without_eligible_local_window(self):
        beat_grid = tuple(index * 0.5 for index in range(121))
        weak_evidence = tuple(index % 8 < 4 for index in range(len(beat_grid)))
        strong_evidence = tuple(True for _ in beat_grid)
        with tempfile.TemporaryDirectory() as root:
            weak = LicensedTrack(
                "weak", Path(root) / "weak.mp3", 120.0, beat_grid,
                "generated", "private", "weak", 60.0, ("focus",), "lane", 4,
                weak_evidence, True,
            )
            strong = LicensedTrack(
                "strong", Path(root) / "strong.mp3", 120.0, beat_grid,
                "generated", "private", "strong", 60.0, (), "lane", 4,
                strong_evidence, True,
            )
            state_path = Path(root) / "rotation.json"
            selected = worker._reserve_track(
                tracks=[weak, strong], tags={"focus"}, state_path=state_path,
                reservation_id="plan:new", duration_seconds=14.0,
            )
            self.assertEqual(selected.track_id, "strong")

            state_path.write_text(json.dumps({
                "schema": worker.MUSIC_ROTATION_SCHEMA,
                "recent": [],
                "reservations": {"plan:existing": "weak"},
            }), encoding="utf-8")
            with self.assertRaises(MediaError) as caught:
                worker._reserve_track(
                    tracks=[weak, strong], tags=set(), state_path=state_path,
                    reservation_id="plan:existing", duration_seconds=14.0,
                )
            self.assertEqual(caught.exception.code, "licensed_music_segment_invalid")

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
