import io
import json
import array
import random
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import naz_audio_library as cli
from story_audio_analysis import AudioAnalysis, AudioAnalysisError, FfmpegAudioAnalyzer
from story_audio_library import (
    GENERATION_STATE_FILE,
    GENERATION_STATE_SCHEMA,
    INITIAL_TRACK_SPECS,
    MAX_INITIAL_TRACKS,
    AudioLibraryError,
    LibraryLock,
    beat_grid,
    generate_initial_library,
    library_plan,
    load_generation_state,
    read_valid_sidecar,
    save_generation_state,
    sidecar_path,
)


def mp3_bytes(marker: bytes = b"a") -> bytes:
    return b"ID3" + marker * 256


class FakeAudioProvider:
    name = "stability"
    model = "stable-audio-3"

    def __init__(self) -> None:
        self.submissions = []
        self.jobs = {}

    def submit(self, request):
        self.submissions.append(request)
        job_id = f"audio-job-{len(self.submissions)}"
        self.jobs[job_id] = SimpleNamespace(
            external_job_id=job_id, status="in_progress", artifact=None,
        )
        return SimpleNamespace(external_job_id=job_id, status="submitted")

    def poll(self, external_job_id):
        return self.jobs[external_job_id]

    def complete(self, external_job_id, *, data=None):
        request_index = int(external_job_id.rsplit("-", 1)[-1]) - 1
        self.jobs[external_job_id] = SimpleNamespace(
            external_job_id=external_job_id,
            status="completed",
            artifact=SimpleNamespace(
                data=data or mp3_bytes(), content_type="audio/mpeg",
                output_format="mp3", seed=self.submissions[request_index].seed,
                finish_reason="SUCCESS",
                request_id=f"request-{external_job_id}",
            ),
        )


class FakeAudioAnalyzer:
    def __init__(self) -> None:
        self.paths = []
        self.preflight_calls = 0

    def preflight(self):
        self.preflight_calls += 1

    def analyze(self, path):
        self.paths.append(Path(path))
        if not Path(path).read_bytes().startswith(b"ID3"):
            raise AudioAnalysisError("audio_artifact_invalid")
        duration = 63.812
        step = 60.0 / 147.6
        grid = tuple(
            round(0.187 + index * step, 6)
            for index in range(int((duration - 0.187) / step) + 1)
        )
        return AudioAnalysis(
            duration, 147.6, grid, beat_evidence=tuple(True for _ in grid),
            analyzer="fake-waveform-analyzer-v1",
        )


def enabled_env(root: str) -> dict[str, str]:
    return {
        "NAZ_STORY_MUSIC_LIBRARY": root,
        "NAZ_AUDIO_GENERATION_ENABLED": "true",
        "NAZ_AUDIO_PROVIDER": "stability",
        "NAZ_AUDIO_MODEL": "stable-audio-3",
        "NAZ_AUDIO_API_KEY": "test-key-not-for-output",
    }


class CatalogTests(unittest.TestCase):
    def test_catalog_is_exactly_three_three_two(self):
        self.assertEqual(len(INITIAL_TRACK_SPECS), MAX_INITIAL_TRACKS)
        self.assertEqual(len({row.track_id for row in INITIAL_TRACK_SPECS}), 8)
        lanes = [row.lane for row in INITIAL_TRACK_SPECS]
        self.assertEqual(lanes.count("midnight_wave"), 3)
        self.assertEqual(lanes.count("dark_melodic_house"), 3)
        self.assertEqual(lanes.count("emotional_future_garage"), 2)
        self.assertTrue(all(45 <= row.duration_seconds <= 90 for row in INITIAL_TRACK_SPECS))

    def test_prompts_have_no_artist_title_or_audio_input_reference(self):
        forbidden = (
            "skeler", "tel aviv", "into you", "in the style of", "reference track",
            "audio input", "audio-to-audio", "uploaded audio", "sample this",
        )
        for spec in INITIAL_TRACK_SPECS:
            prompt = spec.prompt.casefold()
            with self.subTest(track_id=spec.track_id):
                self.assertTrue(all(term not in prompt for term in forbidden))
                self.assertIn("original instrumental", prompt)
                self.assertIn("no samples", prompt)
                self.assertIn("no imitation", prompt)

    def test_declared_beat_grids_are_finite_and_inside_master(self):
        for spec in INITIAL_TRACK_SPECS:
            grid = beat_grid(spec)
            with self.subTest(track_id=spec.track_id):
                self.assertEqual(grid[0], 0.0)
                self.assertLessEqual(grid[-1], spec.duration_seconds)
                self.assertTrue(all(left < right for left, right in zip(grid, grid[1:])))

    def test_safe_plan_never_contains_generation_prompts(self):
        with tempfile.TemporaryDirectory() as root:
            raw = json.dumps(library_plan(Path(root)), ensure_ascii=False)
        for spec in INITIAL_TRACK_SPECS:
            self.assertNotIn(spec.prompt, raw)


class DurableGenerationTests(unittest.TestCase):
    def test_parallel_paid_cli_is_locked_out(self):
        with tempfile.TemporaryDirectory() as root:
            with LibraryLock(Path(root)):
                with self.assertRaises(AudioLibraryError) as caught:
                    with LibraryLock(Path(root)):
                        pass
            self.assertEqual(caught.exception.code, "audio_library_locked")

    def test_paid_preflight_failure_happens_before_journal_and_provider_post(self):
        class MissingToolsAnalyzer(FakeAudioAnalyzer):
            def preflight(self):
                raise AudioAnalysisError("audio_analysis_tool_unavailable")

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            provider = FakeAudioProvider()
            with self.assertRaises(AudioLibraryError) as caught:
                generate_initial_library(
                    root=root_path, provider=provider,
                    confirmed_paid_calls=1, max_new_tracks=1,
                    analyzer=MissingToolsAnalyzer(),
                )
            self.assertEqual(caught.exception.code, "audio_analysis_tool_unavailable")
            self.assertEqual(provider.submissions, [])
            self.assertEqual(load_generation_state(root_path)["jobs"], {})

    def test_submission_cap_is_eight_and_second_run_never_posts_again(self):
        with tempfile.TemporaryDirectory() as root:
            provider = FakeAudioProvider()
            first = generate_initial_library(
                root=Path(root), provider=provider,
                confirmed_paid_calls=8, max_new_tracks=8,
                analyzer=FakeAudioAnalyzer(),
            )
            self.assertEqual(first["submitted_now"], 8)
            self.assertEqual(len(provider.submissions), 8)
            state = load_generation_state(Path(root))
            for spec in INITIAL_TRACK_SPECS:
                self.assertEqual(state["jobs"][spec.track_id]["requested_seed"], spec.seed)
                self.assertEqual(
                    state["jobs"][spec.track_id]["result_contract_version"],
                    "audio-result.v2",
                )
            second = generate_initial_library(
                root=Path(root), provider=provider,
                confirmed_paid_calls=8, max_new_tracks=8,
            )
            self.assertEqual(second["submitted_now"], 0)
            self.assertEqual(second["polled_now"], 8)
            self.assertEqual(len(provider.submissions), 8)

    def test_completed_async_job_resumes_by_poll_without_second_post(self):
        with tempfile.TemporaryDirectory() as root:
            provider = FakeAudioProvider()
            generated = generate_initial_library(
                root=Path(root), provider=provider,
                confirmed_paid_calls=1, max_new_tracks=1,
                analyzer=FakeAudioAnalyzer(),
            )
            self.assertEqual(generated["submitted_now"], 1)
            job_id = next(iter(provider.jobs))
            provider.complete(job_id)
            resumed = generate_initial_library(
                root=Path(root), provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=FakeAudioAnalyzer(),
            )
            self.assertEqual(resumed["submitted_now"], 0)
            self.assertEqual(resumed["polled_now"], 1)
            self.assertEqual(resumed["ready_count"], 1)
            self.assertEqual(len(provider.submissions), 1)

    def test_legacy_v1_success_sidecar_without_seed_source_remains_readable(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            provider = FakeAudioProvider()
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=1, max_new_tracks=1,
                analyzer=FakeAudioAnalyzer(),
            )
            provider.complete(next(iter(provider.jobs)))
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=FakeAudioAnalyzer(),
            )
            first = INITIAL_TRACK_SPECS[0]
            metadata = sidecar_path(root_path, first)
            row = json.loads(metadata.read_text(encoding="utf-8"))
            row["generation"].pop("seed_source")
            metadata.write_text(json.dumps(row), encoding="utf-8")

            self.assertIsNotNone(read_valid_sidecar(root_path, first))

    def test_headerless_http_200_receipt_recovers_old_validation_failure_without_post(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            provider = FakeAudioProvider()
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=1, max_new_tracks=1,
                analyzer=FakeAudioAnalyzer(),
            )
            job_id = next(iter(provider.jobs))
            provider.complete(job_id)
            provider.jobs[job_id].artifact = SimpleNamespace(
                data=mp3_bytes(), content_type="audio/mpeg", output_format="mp3",
                seed=None, finish_reason="HTTP_200_AUDIO", request_id="request-fixture",
            )
            state = load_generation_state(root_path)
            first = INITIAL_TRACK_SPECS[0]
            state["jobs"][first.track_id].update({
                "state": "failed",
                "reason_code": "audio_result_finish_reason_invalid",
            })
            state["jobs"][first.track_id].pop("requested_seed", None)
            state["jobs"][first.track_id].pop("result_contract_version", None)
            save_generation_state(root_path, state)

            resumed = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=FakeAudioAnalyzer(),
            )

            self.assertEqual(resumed["submitted_now"], 0)
            self.assertEqual(resumed["polled_now"], 1)
            self.assertEqual(resumed["ready_count"], 1)
            self.assertEqual(len(provider.submissions), 1)
            sidecar = read_valid_sidecar(root_path, first)
            self.assertIsNotNone(sidecar)
            self.assertEqual(sidecar["generation"]["seed"], first.seed)
            self.assertEqual(sidecar["generation"]["seed_source"], "request")
            self.assertEqual(sidecar["generation"]["finish_reason"], "HTTP_200_AUDIO")
            self.assertEqual(
                load_generation_state(root_path)["jobs"][first.track_id]["result_contract_version"],
                "audio-result.v2",
            )

    def test_all_eight_legacy_receipts_recover_with_eight_gets_and_zero_posts(self):
        class CatalogAnalyzer(FakeAudioAnalyzer):
            def analyze(self, path):
                self.paths.append(Path(path))
                spec = next(
                    item for item in INITIAL_TRACK_SPECS
                    if item.track_id in Path(path).name
                )
                duration = float(spec.duration_seconds) - 0.1
                step = 60.0 / float(spec.bpm)
                grid = tuple(
                    round(0.1 + index * step, 6)
                    for index in range(int((duration - 0.1) / step) + 1)
                )
                return AudioAnalysis(
                    duration, float(spec.bpm), grid,
                    beat_evidence=tuple(True for _ in grid),
                    analyzer="catalog-fixture-analyzer-v1",
                )

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            provider = FakeAudioProvider()
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=8, max_new_tracks=8,
                analyzer=FakeAudioAnalyzer(),
            )
            for job_id in tuple(provider.jobs):
                provider.complete(job_id)
                provider.jobs[job_id].artifact = SimpleNamespace(
                    data=mp3_bytes(job_id.encode("ascii")[-1:]),
                    content_type="audio/mpeg", output_format="mp3",
                    seed=None, finish_reason="HTTP_200_AUDIO",
                    request_id=f"request-{job_id}",
                )
            state = load_generation_state(root_path)
            for spec in INITIAL_TRACK_SPECS:
                state["jobs"][spec.track_id].update({
                    "state": "failed",
                    "reason_code": "audio_result_finish_reason_invalid",
                })
                state["jobs"][spec.track_id].pop("requested_seed", None)
                state["jobs"][spec.track_id].pop("result_contract_version", None)
            save_generation_state(root_path, state)

            resumed = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=CatalogAnalyzer(),
            )

            self.assertEqual(resumed["submitted_now"], 0)
            self.assertEqual(resumed["polled_now"], 8)
            self.assertEqual(resumed["ready_count"], 8)
            self.assertEqual(len(provider.submissions), 8)

    def test_failed_revalidation_is_attempted_only_once_without_post(self):
        class StillInvalidProvider(FakeAudioProvider):
            def __init__(self):
                super().__init__()
                self.poll_calls = 0

            def poll(self, external_job_id):
                self.poll_calls += 1
                error = AudioLibraryError("audio_result_finish_reason_invalid")
                error.retryable = False
                raise error

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            first = INITIAL_TRACK_SPECS[0]
            state = {
                "schema": GENERATION_STATE_SCHEMA,
                "updated_at": "2026-07-24T00:00:00+00:00",
                "jobs": {
                    first.track_id: {
                        "track_id": first.track_id,
                        "state": "failed",
                        "external_job_id": "existing-receipt",
                        "submission_attempts": 1,
                        "request_fingerprint": first.prompt_sha256,
                        "reason_code": "audio_result_finish_reason_invalid",
                    },
                },
            }
            save_generation_state(root_path, state)
            provider = StillInvalidProvider()

            first_run = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=FakeAudioAnalyzer(),
            )
            second_run = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=FakeAudioAnalyzer(),
            )

            self.assertEqual(first_run["polled_now"], 1)
            self.assertEqual(second_run["polled_now"], 0)
            self.assertEqual(provider.poll_calls, 1)
            self.assertEqual(provider.submissions, [])

    def test_terminal_poll_error_is_not_polled_forever(self):
        class TerminalPollError(RuntimeError):
            code = "audio_job_not_found"
            retryable = False

        class MissingJobProvider(FakeAudioProvider):
            def poll(self, external_job_id):
                raise TerminalPollError(external_job_id)

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            provider = MissingJobProvider()
            generated = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=1, max_new_tracks=1,
                analyzer=FakeAudioAnalyzer(),
            )
            self.assertEqual(generated["submitted_now"], 1)

            first_poll = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
            )
            first = INITIAL_TRACK_SPECS[0]
            self.assertEqual(first_poll["statuses"][first.track_id], "failed")

            second_poll = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
            )
            self.assertEqual(second_poll["polled_now"], 0)
            self.assertEqual(len(provider.submissions), 1)

    def test_ambiguous_pre_post_journal_never_repeats_that_request(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            first = INITIAL_TRACK_SPECS[0]
            state = {
                "schema": GENERATION_STATE_SCHEMA,
                "updated_at": "2026-07-24T00:00:00+00:00",
                "jobs": {
                    first.track_id: {
                        "track_id": first.track_id,
                        "state": "submitting",
                        "external_job_id": None,
                        "submission_attempts": 1,
                    }
                },
            }
            save_generation_state(root_path, state)
            provider = FakeAudioProvider()
            result = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=8, max_new_tracks=8,
                analyzer=FakeAudioAnalyzer(),
            )
            self.assertEqual(result["statuses"][first.track_id], "blocked")
            self.assertEqual(len(provider.submissions), 7)
            self.assertTrue(all(request.prompt != first.prompt for request in provider.submissions))

    def test_failed_submission_is_not_hiddenly_retried(self):
        class RejectFirstProvider(FakeAudioProvider):
            def __init__(self):
                super().__init__()
                self.first_attempts = 0

            def submit(self, request):
                if request.prompt == INITIAL_TRACK_SPECS[0].prompt:
                    self.first_attempts += 1
                    raise AudioLibraryError("audio_provider_request_rejected")
                return super().submit(request)

        with tempfile.TemporaryDirectory() as root:
            provider = RejectFirstProvider()
            first_result = generate_initial_library(
                root=Path(root), provider=provider,
                confirmed_paid_calls=8, max_new_tracks=8,
                analyzer=FakeAudioAnalyzer(),
            )
            self.assertEqual(first_result["submitted_now"], 1)
            self.assertEqual(provider.submissions, [])
            generate_initial_library(
                root=Path(root), provider=provider,
                confirmed_paid_calls=8, max_new_tracks=8,
                analyzer=FakeAudioAnalyzer(),
            )
            self.assertEqual(provider.first_attempts, 1)
            state = load_generation_state(Path(root))
            self.assertEqual(state["jobs"][INITIAL_TRACK_SPECS[0].track_id]["state"], "failed")

    def test_invalid_artifact_fails_without_sidecar_or_retry(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            provider = FakeAudioProvider()
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=1, max_new_tracks=1,
                analyzer=FakeAudioAnalyzer(),
            )
            provider.complete(next(iter(provider.jobs)), data=b"not-audio")
            result = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
            )
            first = INITIAL_TRACK_SPECS[0]
            self.assertEqual(result["statuses"][first.track_id], "failed")
            self.assertFalse(sidecar_path(root_path, first).exists())
            self.assertEqual(len(provider.submissions), 1)

    def test_local_analysis_failure_stays_retryable_without_second_post(self):
        class UnavailableAnalyzer:
            def preflight(self):
                pass

            def analyze(self, path):
                raise AudioAnalysisError("audio_analysis_tool_unavailable")

        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            provider = FakeAudioProvider()
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=1, max_new_tracks=1,
                analyzer=FakeAudioAnalyzer(),
            )
            provider.complete(next(iter(provider.jobs)))
            result = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=UnavailableAnalyzer(),
            )
            first = INITIAL_TRACK_SPECS[0]
            self.assertEqual(result["statuses"][first.track_id], "analysis_pending")
            self.assertEqual(
                result["analysis_pending_reason_codes"], ["audio_analysis_tool_unavailable"],
            )
            receipt = load_generation_state(root_path)["jobs"][first.track_id]["external_job_id"]
            self.assertFalse((root_path / f"{first.track_id}.mp3").exists())
            self.assertFalse(sidecar_path(root_path, first).exists())
            resumed = generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=FakeAudioAnalyzer(),
            )
            self.assertEqual(resumed["statuses"][first.track_id], "completed")
            self.assertEqual(
                load_generation_state(root_path)["jobs"][first.track_id]["external_job_id"], receipt,
            )
            self.assertEqual(len(provider.submissions), 1)

    def test_tool_timeout_and_process_analysis_errors_are_all_retryable(self):
        for reason_code in (
            "audio_analysis_tool_unavailable",
            "audio_analysis_timeout",
            "audio_analysis_process_failed",
        ):
            with self.subTest(reason_code=reason_code), tempfile.TemporaryDirectory() as root:
                root_path = Path(root)
                provider = FakeAudioProvider()
                generate_initial_library(
                    root=root_path, provider=provider,
                    confirmed_paid_calls=1, max_new_tracks=1,
                    analyzer=FakeAudioAnalyzer(),
                )
                provider.complete(next(iter(provider.jobs)))

                class LocalFailureAnalyzer(FakeAudioAnalyzer):
                    def analyze(self, path):
                        raise AudioAnalysisError(reason_code)

                result = generate_initial_library(
                    root=root_path, provider=provider,
                    confirmed_paid_calls=0, max_new_tracks=0,
                    analyzer=LocalFailureAnalyzer(),
                )
                first = INITIAL_TRACK_SPECS[0]
                self.assertEqual(result["statuses"][first.track_id], "analysis_pending")
                self.assertEqual(len(provider.submissions), 1)

    def test_sidecar_is_atomic_rights_complete_and_contains_no_prompt_or_key(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            provider = FakeAudioProvider()
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=1, max_new_tracks=1,
                analyzer=FakeAudioAnalyzer(),
            )
            provider.complete(next(iter(provider.jobs)), data=mp3_bytes(b"z"))
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=FakeAudioAnalyzer(),
            )
            spec = INITIAL_TRACK_SPECS[0]
            row = read_valid_sidecar(root_path, spec)
            self.assertIsNotNone(row)
            self.assertEqual(row["beat_grid_source"], "actual-audio-derived-beat-track-v1")
            self.assertEqual(row["beat_evidence_source"], "actual-audio-derived-onset-match-v1")
            self.assertEqual(len(row["beat_evidence"]), len(row["beat_grid"]))
            self.assertTrue(all(type(value) is bool for value in row["beat_evidence"]))
            self.assertEqual(row["audio_analysis"]["source"], "actual-audio-derived-beat-track-v1")
            self.assertAlmostEqual(row["duration_seconds"], 63.812)
            self.assertNotEqual(row["beat_grid"][0], 0.0)
            self.assertFalse(row["rights"]["third_party_audio_input"])
            self.assertFalse(row["rights"]["artist_or_track_reference"])
            self.assertEqual(len(row["checksum"]), 64)
            sidecar_text = sidecar_path(root_path, spec).read_text(encoding="utf-8")
            state_text = (root_path / GENERATION_STATE_FILE).read_text(encoding="utf-8")
            self.assertNotIn(spec.prompt, sidecar_text)
            self.assertNotIn(spec.prompt, state_text)
            self.assertNotIn("test-key-not-for-output", sidecar_text + state_text)
            self.assertEqual(list(root_path.glob(".*.tmp")), [])

    def test_corrupt_checksum_or_rights_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            provider = FakeAudioProvider()
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=1, max_new_tracks=1,
                analyzer=FakeAudioAnalyzer(),
            )
            provider.complete(next(iter(provider.jobs)))
            generate_initial_library(
                root=root_path, provider=provider,
                confirmed_paid_calls=0, max_new_tracks=0,
                analyzer=FakeAudioAnalyzer(),
            )
            spec = INITIAL_TRACK_SPECS[0]
            sidecar = sidecar_path(root_path, spec)
            row = json.loads(sidecar.read_text(encoding="utf-8"))
            row["rights"]["third_party_audio_input"] = True
            sidecar.write_text(json.dumps(row), encoding="utf-8")
            self.assertIsNone(read_valid_sidecar(root_path, spec))
            row["rights"]["third_party_audio_input"] = False
            row["beat_evidence"] = row["beat_evidence"][:-1]
            sidecar.write_text(json.dumps(row), encoding="utf-8")
            self.assertIsNone(read_valid_sidecar(root_path, spec))

    def test_invalid_paid_caps_are_rejected_before_provider(self):
        with tempfile.TemporaryDirectory() as root:
            provider = FakeAudioProvider()
            for confirmed, maximum in ((9, 8), (8, 9), (2, 3), (-1, 0)):
                with self.subTest(confirmed=confirmed, maximum=maximum):
                    with self.assertRaises(AudioLibraryError):
                        generate_initial_library(
                            root=Path(root), provider=provider,
                            confirmed_paid_calls=confirmed, max_new_tracks=maximum,
                        )
            self.assertEqual(provider.submissions, [])


class CliTests(unittest.TestCase):
    def call(self, args, env, provider=None, analyzer=None):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(args, env=env, provider=provider, analyzer=analyzer)
        return code, json.loads(output.getvalue())

    def test_check_and_plan_are_read_only_and_make_no_live_call(self):
        with tempfile.TemporaryDirectory() as root:
            env = enabled_env(root)
            code, checked = self.call(["--check-config"], env)
            self.assertEqual(code, 0)
            self.assertTrue(checked["ok"])
            self.assertFalse(checked["live_api_called"])
            code, planned = self.call(["--plan"], env)
            self.assertEqual(code, 0)
            self.assertEqual(planned["track_count"], 8)
            self.assertFalse(planned["live_api_called"])
            raw = json.dumps(planned, ensure_ascii=False)
            self.assertTrue(all(spec.prompt not in raw for spec in INITIAL_TRACK_SPECS))

    def test_paid_mode_requires_explicit_confirmation_and_enabled_flag(self):
        with tempfile.TemporaryDirectory() as root:
            provider = FakeAudioProvider()
            env = enabled_env(root)
            code, result = self.call(["--generate-initial-library"], env, provider)
            self.assertEqual(code, 2)
            self.assertEqual(result["reason_code"], "audio_paid_call_confirmation_missing")
            env["NAZ_AUDIO_GENERATION_ENABLED"] = "false"
            code, result = self.call(
                ["--generate-initial-library", "--confirm-paid-calls", "1"], env, provider,
            )
            self.assertEqual(code, 2)
            self.assertEqual(result["reason_code"], "audio_generation_disabled")
            self.assertEqual(provider.submissions, [])

    def test_cli_never_allows_more_than_eight_paid_calls(self):
        with tempfile.TemporaryDirectory() as root:
            provider = FakeAudioProvider()
            code, result = self.call(
                ["--generate-initial-library", "--confirm-paid-calls", "9"],
                enabled_env(root), provider,
            )
            self.assertEqual(code, 2)
            self.assertEqual(result["reason_code"], "audio_generation_limit_invalid")
            self.assertFalse(result["live_api_called"])
            self.assertEqual(provider.submissions, [])

    def test_cli_submits_only_the_confirmed_number(self):
        with tempfile.TemporaryDirectory() as root:
            provider = FakeAudioProvider()
            code, result = self.call(
                [
                    "--generate-initial-library", "--confirm-paid-calls", "3",
                    "--max-new-tracks", "2",
                ],
                enabled_env(root), provider, FakeAudioAnalyzer(),
            )
            self.assertEqual(code, 0)
            self.assertTrue(result["ok"])
            self.assertEqual(result["submitted_now"], 2)
            self.assertEqual(len(provider.submissions), 2)

    def test_cli_returns_failure_when_submission_finishes_failed(self):
        class RejectedProvider(FakeAudioProvider):
            def submit(self, request):
                raise AudioLibraryError("audio_provider_request_rejected")

        with tempfile.TemporaryDirectory() as root:
            code, result = self.call(
                ["--generate-initial-library", "--confirm-paid-calls", "1"],
                enabled_env(root), RejectedProvider(), FakeAudioAnalyzer(),
            )
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason_code"], "audio_generation_job_failed")
            self.assertEqual(result["failure_reason_codes"], ["audio_provider_request_rejected"])

    def test_cli_returns_failure_when_poll_finishes_failed(self):
        class TerminalPollError(RuntimeError):
            code = "audio_job_not_found"
            retryable = False

        class MissingJobProvider(FakeAudioProvider):
            def poll(self, external_job_id):
                raise TerminalPollError(external_job_id)

        with tempfile.TemporaryDirectory() as root:
            provider = MissingJobProvider()
            first_code, _ = self.call(
                ["--generate-initial-library", "--confirm-paid-calls", "1"],
                enabled_env(root), provider, FakeAudioAnalyzer(),
            )
            self.assertEqual(first_code, 0)
            code, result = self.call(
                ["--generate-initial-library", "--confirm-paid-calls", "0"],
                enabled_env(root), provider,
            )
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason_code"], "audio_generation_job_failed")
            self.assertEqual(result["failure_reason_codes"], ["audio_job_not_found"])

    def test_cli_reports_retryable_analysis_pending_without_second_post(self):
        class MissingToolsAnalyzer(FakeAudioAnalyzer):
            def analyze(self, path):
                raise AudioAnalysisError("audio_analysis_process_failed")

        with tempfile.TemporaryDirectory() as root:
            provider = FakeAudioProvider()
            first_code, _ = self.call(
                ["--generate-initial-library", "--confirm-paid-calls", "1"],
                enabled_env(root), provider, FakeAudioAnalyzer(),
            )
            self.assertEqual(first_code, 0)
            provider.complete(next(iter(provider.jobs)))
            code, result = self.call(
                ["--generate-initial-library", "--confirm-paid-calls", "0"],
                enabled_env(root), provider, MissingToolsAnalyzer(),
            )
            self.assertEqual(code, 1)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason_code"], "audio_analysis_pending")
            self.assertEqual(len(provider.submissions), 1)


class ActualAudioAnalysisTests(unittest.TestCase):
    @staticmethod
    def probe_payload(duration=20.0, *, codec="mp3", sample_rate=44_100, channels=2):
        return json.dumps({
            "streams": [{
                "codec_name": codec,
                "sample_rate": str(sample_rate),
                "channels": channels,
                "duration": str(duration),
            }],
            "format": {"duration": str(duration)},
        }).encode()

    def test_paid_preflight_checks_both_ffprobe_and_ffmpeg(self):
        calls = []

        def runner(command, timeout_seconds, output_limit):
            calls.append(tuple(command))
            return b"tool version"

        FfmpegAudioAnalyzer(runner=runner).preflight()
        self.assertEqual([command[0] for command in calls], ["ffprobe", "ffmpeg"])

    def test_analyzer_derives_nonzero_phase_grid_from_decoded_waveform(self):
        sample_rate = FfmpegAudioAnalyzer.sample_rate
        duration = 20.0
        samples = array.array("h", [0]) * int(sample_rate * duration)
        first = int(0.18 * sample_rate)
        step = int(0.5 * sample_rate)
        for onset in range(first, len(samples) - 200, step):
            for index in range(onset, onset + 160):
                samples[index] = 20_000

        calls = []

        def runner(command, timeout_seconds, output_limit):
            calls.append(tuple(command))
            if command[0] == "ffprobe":
                return self.probe_payload(duration)
            return samples.tobytes()

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "track.mp3"
            source.write_bytes(mp3_bytes())
            result = FfmpegAudioAnalyzer(runner=runner).analyze(source)
        self.assertAlmostEqual(result.duration_seconds, duration)
        self.assertTrue(115.0 <= result.bpm <= 125.0)
        self.assertGreater(len(result.beat_grid), 20)
        self.assertGreater(result.beat_grid[0], 0.0)
        self.assertEqual(len(result.beat_evidence), len(result.beat_grid))
        self.assertTrue(all(type(value) is bool for value in result.beat_evidence))
        self.assertGreaterEqual(result.confidence, 0.2)
        self.assertGreaterEqual(result.peak_prominence, 0.25)
        self.assertGreaterEqual(result.onset_alignment_fraction, 0.25)
        self.assertEqual(result.source, "actual-audio-derived-beat-track-v1")
        self.assertEqual([call[0] for call in calls], ["ffprobe", "ffmpeg"])

    def test_analyzer_fails_closed_when_audio_has_no_detectable_onsets(self):
        def runner(command, timeout_seconds, output_limit):
            if command[0] == "ffprobe":
                return self.probe_payload()
            return b"\x00\x00" * (FfmpegAudioAnalyzer.sample_rate * 20)

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "track.mp3"
            source.write_bytes(mp3_bytes())
            with self.assertRaises(AudioAnalysisError) as caught:
                FfmpegAudioAnalyzer(runner=runner).analyze(source)
        self.assertEqual(caught.exception.code, "audio_beat_analysis_failed")

    def test_random_nonperiodic_impulses_do_not_pass_as_a_beat_grid(self):
        sample_rate = FfmpegAudioAnalyzer.sample_rate
        duration = 30.0
        samples = array.array("h", [0]) * int(sample_rate * duration)
        rng = random.Random(20260725)
        positions = sorted(rng.sample(range(1_000, len(samples) - 500), 70))
        for onset in positions:
            for index in range(onset, onset + 120):
                samples[index] = 18_000

        def runner(command, timeout_seconds, output_limit):
            if command[0] == "ffprobe":
                return self.probe_payload(duration)
            return samples.tobytes()

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "random.mp3"
            source.write_bytes(mp3_bytes())
            with self.assertRaises(AudioAnalysisError) as caught:
                FfmpegAudioAnalyzer(runner=runner).analyze(source)
        self.assertEqual(caught.exception.code, "audio_beat_analysis_failed")

    def test_source_stream_must_be_expected_codec_44100_stereo_before_downmix(self):
        def runner(command, timeout_seconds, output_limit):
            if command[0] == "ffprobe":
                return self.probe_payload(sample_rate=48_000, channels=1)
            self.fail("decoder must not run after a rejected source stream")

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "track.mp3"
            source.write_bytes(mp3_bytes())
            with self.assertRaises(AudioAnalysisError) as caught:
                FfmpegAudioAnalyzer(runner=runner).analyze(source)
        self.assertEqual(caught.exception.code, "audio_stream_contract_invalid")

    def test_wav_source_contract_accepts_pcm_44100_stereo(self):
        def runner(command, timeout_seconds, output_limit):
            return self.probe_payload(codec="pcm_s16le")

        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "track.wav"
            source.write_bytes(b"RIFF" + b"\x00" * 64)
            duration = FfmpegAudioAnalyzer(runner=runner)._probe(source)
        self.assertEqual(duration, 20.0)


if __name__ == "__main__":
    unittest.main()
