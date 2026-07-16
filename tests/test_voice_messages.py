import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import main


def fake_voice_update(data=b"voice-bytes"):
    telegram_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(data)))
    voice = SimpleNamespace(
        file_size=len(data),
        duration=8,
        get_file=AsyncMock(return_value=telegram_file),
    )
    message = SimpleNamespace(
        voice=voice,
        audio=None,
        reply_text=AsyncMock(),
        reply_voice=AsyncMock(),
        reply_document=AsyncMock(),
    )
    return SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=77),
        effective_chat=SimpleNamespace(send_action=AsyncMock()),
    )


class VoiceConfigurationTests(unittest.TestCase):
    def test_voice_client_is_separate_from_openrouter(self):
        client = Mock()
        with patch.object(main, "voice_openai_client", None), patch.object(
            main, "OPENAI_VOICE_API_KEY", "official-key"
        ), patch.object(main, "OPENAI_VOICE_BASE_URL", "https://api.openai.com/v1"), patch.object(
            main, "OpenAI", return_value=client
        ) as constructor:
            self.assertIs(main.ensure_voice_openai_client(), client)
        constructor.assert_called_once_with(
            api_key="official-key",
            base_url="https://api.openai.com/v1",
        )

    def test_missing_voice_key_is_clear(self):
        with patch.object(main, "voice_openai_client", None), patch.object(main, "OPENAI_VOICE_API_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "OPENAI_VOICE_API_KEY"):
                main.ensure_voice_openai_client()


class VoiceDownloadTests(unittest.TestCase):
    def test_telegram_voice_download_uses_ogg_name(self):
        update = fake_voice_update()
        data, filename = asyncio.run(main.download_telegram_audio(update.message))
        self.assertEqual(data, b"voice-bytes")
        self.assertEqual(filename, "telegram-voice.ogg")

    def test_oversized_voice_is_rejected_before_download(self):
        update = fake_voice_update()
        update.message.voice.file_size = main.VOICE_MAX_BYTES + 1
        with self.assertRaisesRegex(ValueError, "превышает лимит"):
            asyncio.run(main.download_telegram_audio(update.message))
        update.message.voice.get_file.assert_not_awaited()

    def test_unknown_audio_format_is_rejected(self):
        update = fake_voice_update()
        update.message.voice = None
        update.message.audio = SimpleNamespace(
            file_name="recording.bin",
            mime_type="application/octet-stream",
            file_size=10,
            duration=1,
        )
        with self.assertRaisesRegex(ValueError, "определить формат"):
            asyncio.run(main.download_telegram_audio(update.message))


class VoiceProviderTests(unittest.TestCase):
    def test_transcription_returns_clean_text(self):
        client = Mock()
        client.audio.transcriptions.create.return_value = SimpleNamespace(text="  Привет, Naz  ")
        with patch.object(main, "ensure_voice_openai_client", return_value=client), patch.object(
            main, "OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe"
        ):
            result = asyncio.run(main.transcribe_voice_bytes(b"audio", "voice.ogg"))
        self.assertEqual(result, "Привет, Naz")
        kwargs = client.audio.transcriptions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-4o-transcribe")
        self.assertEqual(kwargs["file"].name, "voice.ogg")

    def test_transcription_logs_do_not_contain_key_or_audio(self):
        client = Mock()
        client.audio.transcriptions.create.side_effect = RuntimeError("provider rejected")
        secret_audio = b"private-audio-payload"
        with patch.object(main, "OPENAI_VOICE_API_KEY", "secret-voice-key"), patch.object(
            main, "ensure_voice_openai_client", return_value=client
        ), self.assertLogs("Naz_AI_Bot", level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "распознать"):
                asyncio.run(main.transcribe_voice_bytes(secret_audio, "voice.ogg"))
        output = "\n".join(logs.output)
        self.assertNotIn("secret-voice-key", output)
        self.assertNotIn(secret_audio.decode(), output)

    def test_synthesis_uses_opus_and_sanitized_text(self):
        response = SimpleNamespace(read=Mock(return_value=b"opus-audio"))
        client = Mock()
        client.audio.speech.create.return_value = response
        with patch.object(main, "ensure_voice_openai_client", return_value=client), patch.object(
            main, "OPENAI_TTS_MODEL", "gpt-4o-mini-tts"
        ), patch.object(main, "OPENAI_TTS_VOICE", "marin"):
            audio = asyncio.run(main.synthesize_voice_bytes("**Привет**"))
        self.assertEqual(audio, b"opus-audio")
        kwargs = client.audio.speech.create.call_args.kwargs
        self.assertEqual(kwargs["input"], "Привет")
        self.assertEqual(kwargs["response_format"], "opus")
        self.assertEqual(kwargs["voice"], "marin")


class VoiceHandlerTests(unittest.TestCase):
    def test_disabled_voice_mode_does_not_download(self):
        update = fake_voice_update()
        with patch.object(main, "VOICE_MESSAGES_ENABLED", False), patch.object(
            main, "download_telegram_audio", new=AsyncMock()
        ) as download:
            asyncio.run(main.handle_voice_message(update, SimpleNamespace()))
        download.assert_not_awaited()
        self.assertIn("выключены", update.message.reply_text.await_args.args[0])

    def test_non_admin_is_blocked_before_api_usage(self):
        update = fake_voice_update()
        with patch.object(main, "VOICE_MESSAGES_ENABLED", True), patch.object(
            main, "VOICE_MESSAGES_ADMIN_ONLY", True
        ), patch.object(main, "is_admin", return_value=False), patch.object(
            main, "download_telegram_audio", new=AsyncMock()
        ) as download:
            asyncio.run(main.handle_voice_message(update, SimpleNamespace()))
        download.assert_not_awaited()
        self.assertIn("только владельцу", update.message.reply_text.await_args.args[0])

    def test_successful_voice_turn_reuses_dialogue_and_returns_voice(self):
        update = fake_voice_update()
        with patch.object(main, "VOICE_MESSAGES_ENABLED", True), patch.object(
            main, "VOICE_MESSAGES_ADMIN_ONLY", True
        ), patch.object(main, "OPENAI_VOICE_API_KEY", "official-key"), patch.object(
            main, "is_admin", return_value=True
        ), patch.object(
            main, "download_telegram_audio", new=AsyncMock(return_value=(b"voice", "voice.ogg"))
        ), patch.object(
            main, "transcribe_voice_bytes", new=AsyncMock(return_value="Как твои дела?")
        ), patch.object(
            main, "generate_answer", new=AsyncMock(return_value="**Отлично**, работаю.")
        ) as generate, patch.object(
            main, "synthesize_voice_bytes", new=AsyncMock(return_value=b"opus")
        ):
            asyncio.run(main.handle_voice_message(update, SimpleNamespace()))
        generate.assert_awaited_once_with(77, "Как твои дела?")
        update.message.reply_voice.assert_awaited_once()
        kwargs = update.message.reply_voice.await_args.kwargs
        self.assertEqual(kwargs["caption"], "AI-голос Naz")
        payload = kwargs["voice"]
        self.assertEqual(payload.read(), b"opus")

    def test_contact_command_stops_at_confirmation_preview(self):
        update = fake_voice_update()
        with patch.object(main, "VOICE_MESSAGES_ENABLED", True), patch.object(
            main, "VOICE_MESSAGES_ADMIN_ONLY", True
        ), patch.object(main, "OPENAI_VOICE_API_KEY", "official-key"), patch.object(
            main, "is_admin", return_value=True
        ), patch.object(
            main, "download_telegram_audio", new=AsyncMock(return_value=(b"voice", "voice.ogg"))
        ), patch.object(
            main,
            "transcribe_voice_bytes",
            new=AsyncMock(return_value="Напиши сыну сообщение тест связи"),
        ), patch.object(
            main, "prepare_contact_message_request", new=AsyncMock(return_value=True)
        ) as preview, patch.object(
            main, "generate_answer", new=AsyncMock()
        ) as generate, patch.object(
            main, "synthesize_voice_bytes", new=AsyncMock()
        ) as synthesize:
            asyncio.run(main.handle_voice_message(update, SimpleNamespace()))
        preview.assert_awaited_once_with(update, ANY, "Напиши сыну сообщение тест связи")
        generate.assert_not_awaited()
        synthesize.assert_not_awaited()
        update.message.reply_voice.assert_not_awaited()

    def test_tts_failure_falls_back_to_plain_text(self):
        update = fake_voice_update()
        with patch.object(main, "VOICE_MESSAGES_ENABLED", True), patch.object(
            main, "VOICE_MESSAGES_ADMIN_ONLY", True
        ), patch.object(main, "OPENAI_VOICE_API_KEY", "official-key"), patch.object(
            main, "is_admin", return_value=True
        ), patch.object(
            main, "download_telegram_audio", new=AsyncMock(return_value=(b"voice", "voice.ogg"))
        ), patch.object(
            main, "transcribe_voice_bytes", new=AsyncMock(return_value="Ответь")
        ), patch.object(
            main, "generate_answer", new=AsyncMock(return_value="**Текстовый ответ**")
        ), patch.object(
            main, "synthesize_voice_bytes", new=AsyncMock(side_effect=RuntimeError("tts down"))
        ), patch.object(main, "reply_long", new=AsyncMock()) as reply:
            asyncio.run(main.handle_voice_message(update, SimpleNamespace()))
        reply.assert_awaited_once()
        self.assertEqual(reply.await_args.args[1], "Текстовый ответ")
        update.message.reply_voice.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
