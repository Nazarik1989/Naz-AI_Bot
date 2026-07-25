import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from story_video_provider import (
    RUNWAY_DATA_URI_BASE64_LIMIT,
    RUNWAY_PROMPT_MAX_UTF16_UNITS,
    ProviderError,
    ProviderJob,
    KeyframeRequest,
    RunwayVideoProvider,
    SceneRequest,
    append_prompt_guidance,
    provider_from_environment,
    utf16_code_units,
)


class MockTransport:
    def __init__(self, responses=()):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, body, timeout):
        self.calls.append((method, url, dict(headers), body, timeout))
        if not self.responses:
            raise AssertionError("unexpected transport call")
        return self.responses.pop(0)


def create_reference(path: Path, size=(576, 1280)) -> Path:
    Image.new("RGB", size, (180, 30, 20)).save(path, format="JPEG")
    return path


class RunwayModelContractTests(unittest.TestCase):
    def test_only_explicit_video_models_are_allowed(self):
        for model in ("gen4_turbo", "gen4.5", " GEN4_TURBO "):
            with self.subTest(model=model):
                provider = RunwayVideoProvider(api_key="secret-key", model=model)
                self.assertIn(provider.model, {"gen4_turbo", "gen4.5"})

        for model in ("", "gen3a_turbo", "seedance2", "gen4.5-preview"):
            with self.subTest(model=model), self.assertRaises(ProviderError) as raised:
                RunwayVideoProvider(api_key="secret-key", model=model)
            self.assertEqual(str(raised.exception), "video_model_unsupported")
            self.assertNotIn("secret-key", str(raised.exception))

    def test_environment_model_override_is_allowlisted_and_wins(self):
        env = {
            "NAZ_VIDEO_PROVIDER": "runway",
            "NAZ_VIDEO_API_KEY": "dedicated-secret",
            "NAZ_VIDEO_MODEL": "gen4.5",
        }
        provider = provider_from_environment(env, model_override="gen4_turbo")
        self.assertEqual(provider.model, "gen4_turbo")

        with self.assertRaises(ProviderError) as raised:
            provider_from_environment(env, model_override="untrusted-model")
        self.assertEqual(str(raised.exception), "video_model_unsupported")
        self.assertNotIn("dedicated-secret", str(raised.exception))

    def test_gen4_turbo_fails_closed_without_image_before_transport(self):
        transport = MockTransport()
        provider = RunwayVideoProvider(
            api_key="secret-key", model="gen4_turbo", transport=transport
        )
        with self.assertRaisesRegex(ProviderError, "video_prompt_image_required"):
            provider.submit(SceneRequest("01", "safe visible motion", 5))
        self.assertEqual(transport.calls, [])

    def test_model_duration_contract_is_checked_before_transport(self):
        turbo = RunwayVideoProvider(
            api_key="secret-key", model="gen4_turbo", transport=MockTransport()
        )
        gen45 = RunwayVideoProvider(
            api_key="secret-key", model="gen4.5", transport=MockTransport()
        )
        for duration in (2, 4, 6, 9, True, 5.0):
            with self.subTest(model="gen4_turbo", duration=duration), self.assertRaisesRegex(
                ProviderError, "video_duration_unsupported"
            ):
                turbo.submit(SceneRequest("01", "safe", duration))
        for duration in (1, 11, True, 5.0):
            with self.subTest(model="gen4.5", duration=duration), self.assertRaisesRegex(
                ProviderError, "video_duration_unsupported"
            ):
                gen45.submit(SceneRequest("01", "safe", duration))
        self.assertEqual(turbo._transport.calls, [])
        self.assertEqual(gen45._transport.calls, [])

    def test_gen45_keeps_current_text_to_video_compatibility(self):
        transport = MockTransport(
            [(200, {"Content-Type": "application/json"}, b'{"id":"task-1"}')]
        )
        provider = RunwayVideoProvider(
            api_key="secret-key", model="gen4.5", transport=transport
        )
        job = provider.submit(SceneRequest("01", "safe visible motion", 4))
        self.assertEqual(job.external_job_id, "task-1")
        self.assertTrue(transport.calls[0][1].endswith("/text_to_video"))

    def test_prompt_contract_is_enforced_before_transport_in_utf16_units(self):
        transport = MockTransport()
        provider = RunwayVideoProvider(
            api_key="secret-key", model="gen4.5", transport=transport
        )
        prompt = "a" * (RUNWAY_PROMPT_MAX_UTF16_UNITS - 1) + "😀"
        self.assertEqual(utf16_code_units(prompt), RUNWAY_PROMPT_MAX_UTF16_UNITS + 1)
        with self.assertRaisesRegex(ProviderError, "video_prompt_too_long"):
            provider.submit(SceneRequest("01", prompt, 5))
        self.assertEqual(transport.calls, [])

    def test_continuity_guidance_is_compacted_without_changing_base_prompt(self):
        base = "a" * 940
        result = append_prompt_guidance(base, "stable body proportions " * 20)
        self.assertTrue(result.startswith(base))
        self.assertLessEqual(utf16_code_units(result), RUNWAY_PROMPT_MAX_UTF16_UNITS)
        self.assertEqual(append_prompt_guidance("a" * 995, "stable body"), "a" * 995)

    def test_invalid_provider_input_has_safe_specific_code(self):
        transport = MockTransport([(400, {"Content-Type": "application/json"}, b"")])
        provider = RunwayVideoProvider(
            api_key="secret-key", model="gen4.5", transport=transport
        )
        with self.assertRaisesRegex(ProviderError, "provider_input_invalid"):
            provider.submit(SceneRequest("01", "safe visible motion", 5))


class RunwayReferenceContractTests(unittest.TestCase):
    def test_downloaded_keyframe_is_normalized_to_video_portrait_size(self):
        source = io.BytesIO()
        Image.new("RGB", (960, 720), (2, 3, 9)).save(source, format="PNG")
        provider = RunwayVideoProvider(
            api_key="secret-key",
            model="gen4_turbo",
            transport=MockTransport([
                (200, {"Content-Type": "image/png"}, source.getvalue())
            ]),
        )
        with tempfile.TemporaryDirectory() as root:
            destination = Path(root) / "keyframe.jpg"
            provider.download_keyframe(
                ProviderJob("keyframe-task", "completed", "https://cdn.example/keyframe.png"),
                destination,
            )
            with Image.open(destination) as image:
                self.assertEqual(image.size, (720, 1280))
                self.assertEqual(image.format, "JPEG")

    def test_directed_keyframe_uses_identity_reference_not_video_first_frame(self):
        with tempfile.TemporaryDirectory() as root:
            reference = create_reference(Path(root) / "naz-primary.jpg")
            transport = MockTransport([
                (200, {"Content-Type": "application/json"}, b'{"id":"keyframe-task"}')
            ])
            provider = RunwayVideoProvider(
                api_key="secret-key", model="gen4_turbo", transport=transport
            )
            job = provider.submit_keyframe(KeyframeRequest(
                "01", "@Naz inside the directed Naz AI Lab server room", reference,
            ))
            payload = json.loads(transport.calls[0][3].decode("utf-8"))
            self.assertEqual(job.external_job_id, "keyframe-task")
            self.assertTrue(transport.calls[0][1].endswith("/text_to_image"))
            self.assertEqual(payload["model"], "gen4_image_turbo")
            self.assertEqual(payload["ratio"], "720:960")
            self.assertEqual(payload["referenceImages"][0]["tag"], "Naz")
            self.assertTrue(payload["referenceImages"][0]["uri"].startswith("data:image/jpeg;base64,"))

    def test_object_keyframe_uses_standard_image_model_without_identity_reference(self):
        transport = MockTransport([
            (200, {"Content-Type": "application/json"}, b'{"id":"object-keyframe"}')
        ])
        provider = RunwayVideoProvider(
            api_key="secret-key", model="gen4_turbo", transport=transport
        )
        provider.submit_keyframe(KeyframeRequest("02", "A titanium prototype in the Naz AI Lab"))
        payload = json.loads(transport.calls[0][3].decode("utf-8"))
        self.assertEqual(payload["model"], "gen4_image")
        self.assertNotIn("referenceImages", payload)

    def test_identity_keyframe_requires_explicit_naz_tag_before_transport(self):
        transport = MockTransport()
        provider = RunwayVideoProvider(
            api_key="secret-key", model="gen4_turbo", transport=transport
        )
        with self.assertRaisesRegex(ProviderError, "keyframe_identity_tag_missing"):
            provider.submit_keyframe(KeyframeRequest(
                "01", "A generic lab scene", Path("not-opened.jpg"),
            ))
        self.assertEqual(transport.calls, [])

    def test_portrait_reference_is_normalized_in_memory_to_720x1280(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            reference = create_reference(directory / "naz-primary.jpg")
            transport = MockTransport(
                [(200, {"Content-Type": "application/json"}, b'{"id":"task-image"}')]
            )
            provider = RunwayVideoProvider(
                api_key="secret-key", model="gen4_turbo", transport=transport
            )

            before = sorted(path.name for path in directory.iterdir())
            provider.submit(SceneRequest("01", "safe visible motion", 5, reference))
            after = sorted(path.name for path in directory.iterdir())

            self.assertEqual(before, after)
            self.assertTrue(transport.calls[0][1].endswith("/image_to_video"))
            payload = json.loads(transport.calls[0][3].decode("utf-8"))
            self.assertIsInstance(payload["promptImage"], str)
            self.assertNotIsInstance(payload["promptImage"], list)
            prefix, encoded = payload["promptImage"].split(",", 1)
            self.assertEqual(prefix, "data:image/jpeg;base64")
            self.assertLessEqual(len(encoded.encode("ascii")), RUNWAY_DATA_URI_BASE64_LIMIT)
            with Image.open(io.BytesIO(base64.b64decode(encoded))) as normalized:
                self.assertEqual(normalized.size, (720, 1280))
                self.assertEqual(normalized.mode, "RGB")

    def test_encoded_reference_over_five_megabytes_is_rejected_before_transport(self):
        transport = MockTransport()
        provider = RunwayVideoProvider(
            api_key="secret-key", model="gen4_turbo", transport=transport
        )
        raw_size = (RUNWAY_DATA_URI_BASE64_LIMIT * 3 // 4) + 4
        with patch.object(
            RunwayVideoProvider,
            "_portrait_reference_bytes",
            return_value=b"x" * raw_size,
        ), self.assertRaisesRegex(ProviderError, "approved_reference_too_large"):
            provider.submit(SceneRequest("01", "safe", 5, Path("not-opened.jpg")))
        self.assertEqual(transport.calls, [])

    def test_invalid_reference_error_does_not_expose_path_or_contents(self):
        with tempfile.TemporaryDirectory() as root:
            secret = "private-reference-secret"
            reference = Path(root) / f"{secret}.jpg"
            reference.write_text(secret, encoding="utf-8")
            provider = RunwayVideoProvider(
                api_key="dedicated-secret", model="gen4_turbo", transport=MockTransport()
            )
            with self.assertRaises(ProviderError) as raised:
                provider.submit(SceneRequest("01", "safe", 5, reference))
            self.assertEqual(str(raised.exception), "approved_reference_invalid")
            self.assertNotIn(secret, str(raised.exception))
            self.assertNotIn("dedicated-secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
