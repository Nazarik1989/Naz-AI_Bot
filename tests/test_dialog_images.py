import asyncio
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from PIL import Image

import main


def png_bytes() -> bytes:
    from io import BytesIO

    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, "PNG")
    return output.getvalue()


def fake_update(text=""):
    message = AsyncMock()
    message.text = text
    message.caption = None
    message.photo = []
    message.reply_to_message = None
    return SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=77, first_name="Test", username="test"),
        effective_chat=SimpleNamespace(send_action=AsyncMock()),
    )


class DialogPlainTextTests(unittest.TestCase):
    def test_markdown_is_removed(self):
        source = "# Заголовок\n**Жирный** и _курсив_, `код`\n- пункт\n[Сайт](https://example.com)"
        result = main.sanitize_dialog_text(source)
        self.assertEqual(
            result,
            "Заголовок\nЖирный и курсив, код\n• пункт\nСайт — https://example.com",
        )

    def test_plain_url_and_punctuation_are_preserved(self):
        source = "Смотри: https://example.com/a-b?q=1 — нормально, да?"
        self.assertEqual(main.sanitize_dialog_text(source), source)


class DialogImageTests(unittest.TestCase):
    def test_normal_question_does_not_call_image_generator(self):
        update = fake_update("Как твои дела?")
        context = SimpleNamespace(args=[])
        with patch.object(main, "is_admin", return_value=True), patch.object(main, "handle_menu_button", new=AsyncMock(return_value=False)), patch.object(
            main, "handle_pending_action", new=AsyncMock(return_value=False)
        ), patch.object(main, "send_dialog_image", new=AsyncMock()) as image, patch.object(
            main, "generate_answer", new=AsyncMock(return_value="Нормально")
        ), patch.object(main, "send_typing", new=AsyncMock()), patch.object(main, "reply_long", new=AsyncMock()):
            asyncio.run(main.handle_message(update, context))
        image.assert_not_awaited()

    def test_image_intent_calls_generator_once(self):
        update = fake_update("Нарисуй зимний город")
        with patch.object(main, "is_admin", return_value=True), patch.object(main, "handle_menu_button", new=AsyncMock(return_value=False)), patch.object(
            main, "handle_pending_action", new=AsyncMock(return_value=False)
        ), patch.object(main, "send_dialog_image", new=AsyncMock()) as image:
            asyncio.run(main.handle_message(update, SimpleNamespace(args=[])))
        image.assert_awaited_once_with(update, "Нарисуй зимний город")

    def test_image_command_sends_real_photo(self):
        update = fake_update()
        with patch.object(
            main, "process_dialog_image_request", new=AsyncMock(return_value=(png_bytes(), "event"))
        ):
            asyncio.run(main.dialog_image_command(update, SimpleNamespace(args=["рыжий", "кот"])))
        update.message.reply_photo.assert_awaited_once()
        sent = update.message.reply_photo.await_args.kwargs["photo"]
        self.assertTrue(sent.read().startswith(b"\x89PNG"))

    def test_photo_caption_is_passed_as_reference(self):
        update = fake_update()
        update.message.caption = "Сделай в стиле киберпанк"
        update.message.photo = [Mock()]
        with patch.object(main, "download_telegram_photo", new=AsyncMock(return_value=b"reference")), patch.object(
            main, "send_dialog_image", new=AsyncMock()
        ) as send:
            asyncio.run(main.handle_photo_instruction(update, SimpleNamespace()))
        send.assert_awaited_once_with(update, "Сделай в стиле киберпанк", b"reference")

    def test_text_reply_uses_replied_photo(self):
        update = fake_update("Сохрани лицо, но поменяй фон")
        update.message.reply_to_message = SimpleNamespace(photo=[Mock()], message_id=123)
        with patch.object(main, "is_admin", return_value=True), patch.object(main, "handle_menu_button", new=AsyncMock(return_value=False)), patch.object(
            main, "handle_pending_action", new=AsyncMock(return_value=False)
        ), patch.object(main.memory, "name_contact_from_reply", return_value=None), patch.object(
            main, "download_telegram_photo", new=AsyncMock(return_value=b"reference")
        ), patch.object(
            main, "send_dialog_image", new=AsyncMock()
        ) as send:
            asyncio.run(main.handle_message(update, SimpleNamespace(args=[])))
        send.assert_awaited_once_with(update, "Сохрани лицо, но поменяй фон", b"reference")

    def test_reference_temp_directory_removed_after_success_and_error(self):
        reference = png_bytes()
        paths = []
        original = tempfile.TemporaryDirectory

        class TrackingTemporaryDirectory:
            def __init__(self, *args, **kwargs):
                self.inner = original(*args, **kwargs)
                paths.append(Path(self.inner.name))

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, *args):
                return self.inner.__exit__(*args)

        with patch.object(main.tempfile, "TemporaryDirectory", TrackingTemporaryDirectory), patch.object(
            main, "generate_reference_image_bytes", new=AsyncMock(return_value=b"result")
        ), patch.object(main.memory, "save_dialog_turn"):
            asyncio.run(main.process_dialog_image_request(1, "измени фон", reference_bytes=reference))
        self.assertTrue(all(not path.exists() for path in paths))

        paths.clear()
        with patch.object(main.tempfile, "TemporaryDirectory", TrackingTemporaryDirectory), patch.object(
            main, "generate_reference_image_bytes", new=AsyncMock(side_effect=RuntimeError("fail"))
        ), patch.object(main.memory, "save_dialog_turn"):
            with self.assertRaises(RuntimeError):
                asyncio.run(main.process_dialog_image_request(1, "измени фон", reference_bytes=reference))
        self.assertTrue(all(not path.exists() for path in paths))

    def test_request_and_compact_event_are_saved(self):
        with patch.object(main, "generate_image_bytes", new=AsyncMock(return_value=b"image")), patch.object(
            main.memory, "save_dialog_turn"
        ) as save:
            asyncio.run(main.process_dialog_image_request(9, "Нарисуй маяк"))
        save.assert_called_once_with(9, "Нарисуй маяк", "[Создано изображение: Нарисуй маяк]")

    def test_user_gets_clear_error(self):
        update = fake_update()
        with patch.object(
            main, "process_dialog_image_request", new=AsyncMock(side_effect=RuntimeError("provider down"))
        ):
            asyncio.run(main.send_dialog_image(update, "нарисуй кота"))
        text = update.message.reply_text.await_args.args[0]
        self.assertIn("генератор временно недоступен", text)

    def test_reference_logs_do_not_contain_key_or_base64(self):
        encoded = base64.b64encode(png_bytes()).decode("ascii")
        client = Mock()
        client.images.generate.side_effect = RuntimeError("rejected")
        with patch.object(main, "OPENROUTER_API_KEY", "secret-test-key"), patch.object(
            main, "openrouter_model_supports_reference", new=AsyncMock(return_value=True)
        ), patch.object(main, "ensure_openai_client", return_value=client), self.assertLogs(
            "Naz_AI_Bot", level="WARNING"
        ) as logs:
            with self.assertRaises(main.ReferenceImageUnsupportedError):
                asyncio.run(main.generate_reference_image_bytes("edit", f"data:image/png;base64,{encoded}"))
        output = "\n".join(logs.output)
        self.assertNotIn("secret-test-key", output)
        self.assertNotIn(encoded, output)


if __name__ == "__main__":
    unittest.main()
