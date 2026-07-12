import asyncio
import base64
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import main


class OpenRouterImageTests(unittest.TestCase):
    def test_default_model_is_exact_gpt_image_2_id(self):
        example = Path(".env.example").read_text(encoding="utf-8")
        self.assertIn("OPENAI_IMAGE_MODEL=openai/gpt-image-2", example)

    def test_env_image_settings_are_loaded(self):
        env = os.environ.copy()
        env.update(
            OPENAI_BASE_URL="https://example.invalid/v1",
            OPENAI_IMAGE_MODEL="image-test-model",
            OPENAI_IMAGE_SIZE="1536x1024",
            OPENAI_IMAGE_QUALITY="high",
            IMAGE_PROVIDER="openai",
        )
        code = (
            "import main; print('|'.join([main.OPENAI_BASE_URL, main.OPENAI_IMAGE_MODEL, "
            "main.OPENAI_IMAGE_SIZE, main.OPENAI_IMAGE_QUALITY, main.IMAGE_PROVIDER]))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], env=env, check=True, capture_output=True, text=True
        )
        self.assertEqual(
            result.stdout.strip(),
            "https://example.invalid/v1|image-test-model|1536x1024|high|openai",
        )

    def test_model_size_quality_and_base64_response(self):
        payload = b"generated-image"
        client = Mock()
        client.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(payload).decode("ascii"), url=None)]
        )
        with patch.object(main, "OPENROUTER_API_KEY", "test-key"), patch.object(
            main, "OPENAI_IMAGE_MODEL", "gpt-image-test"
        ), patch.object(main, "OPENAI_IMAGE_SIZE", "1024x1024"), patch.object(
            main, "OPENAI_IMAGE_QUALITY", "medium"
        ), patch.object(main, "ensure_openai_client", return_value=client):
            result = asyncio.run(main.generate_openai_image_bytes("specific scene", variant=2))
        self.assertEqual(result, payload)
        kwargs = client.images.generate.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-image-test")
        self.assertEqual(kwargs["size"], "1024x1024")
        self.assertEqual(kwargs["quality"], "medium")
        self.assertNotIn("test-key", str(kwargs))

    def test_url_response_is_downloaded(self):
        client = Mock()
        client.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=None, url="https://images.example/result.png")]
        )
        with patch.object(main, "OPENROUTER_API_KEY", "test-key"), patch.object(
            main, "ensure_openai_client", return_value=client
        ), patch.object(main, "download_generated_image", new=AsyncMock(return_value=b"url-image")) as download:
            result = asyncio.run(main.generate_openai_image_bytes("specific scene"))
        self.assertEqual(result, b"url-image")
        download.assert_awaited_once_with("https://images.example/result.png")

    def test_openai_error_falls_through_to_bfl(self):
        calls = []

        async def provider(name, result):
            calls.append(name)
            return result

        async def openai(*args, **kwargs):
            return await provider("openai", None)

        async def bfl(*args, **kwargs):
            return await provider("bfl", b"bfl-image")

        async def hf(*args, **kwargs):
            return await provider("hf", b"hf-image")

        with patch.object(main, "IMAGE_PROVIDER", "openai"), patch.object(
            main, "generate_openai_image_bytes", new=openai
        ), patch.object(
            main, "generate_bfl_image_bytes", new=bfl
        ), patch.object(
            main, "generate_hf_image_bytes", new=hf
        ), patch.object(main, "fallback_image_bytes", new=AsyncMock(return_value=b"card")) as fallback:
            result = asyncio.run(main.generate_image_bytes("prompt"))
        self.assertEqual(result, b"bfl-image")
        self.assertEqual(calls, ["openai", "bfl"])
        fallback.assert_not_awaited()

    def test_branded_fallback_is_only_last(self):
        calls = []

        async def empty(name):
            calls.append(name)
            return None

        async def card():
            calls.append("card")
            return b"card"

        async def openai(*args, **kwargs):
            return await empty("openai")

        async def bfl(*args, **kwargs):
            return await empty("bfl")

        async def hf(*args, **kwargs):
            return await empty("hf")

        with patch.object(main, "IMAGE_PROVIDER", "openai"), patch.object(
            main, "ALLOW_IMAGE_FALLBACK", True
        ), patch.object(main, "generate_openai_image_bytes", new=openai), patch.object(
            main, "generate_bfl_image_bytes", new=bfl
        ), patch.object(main, "generate_hf_image_bytes", new=hf), patch.object(
            main, "fallback_image_bytes", new=card
        ):
            result = asyncio.run(main.generate_image_bytes("prompt"))
        self.assertEqual(result, b"card")
        self.assertEqual(calls, ["openai", "bfl", "hf", "card"])


if __name__ == "__main__":
    unittest.main()
