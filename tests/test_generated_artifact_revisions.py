import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
import memory


class GeneratedArtifactMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(memory, "DB_PATH", str(Path(self.temp.name) / "naz.sqlite3"))
        self.db_patch.start()
        memory.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def artifact(self, kind="text", file_id=""):
        artifact = memory.create_generated_artifact(
            42, kind, "original", mode="post", telegram_file_id=file_id
        )
        memory.bind_generated_artifact_message(artifact["artifact_id"], 1, 42, 100, 200)
        return artifact

    def test_exact_text_replacement_is_versioned_and_old_message_becomes_stale(self):
        artifact = self.artifact()
        pending = memory.create_pending_generated_revision(
            artifact["artifact_id"], 42, 1, "text_replace", "Мой точный новый текст"
        )
        applied = memory.apply_generated_revision(pending["revision_id"], 42)

        self.assertEqual(applied["version"], 2)
        self.assertEqual(applied["content"], "Мой точный новый текст")
        old = memory.get_generated_artifact_for_reply(100, 200, 42)
        self.assertEqual(old["bound_version"], 1)
        self.assertEqual(old["current_version"], 2)
        self.assertIsNone(memory.apply_generated_revision(pending["revision_id"], 42))

    def test_owner_and_cancel_are_enforced(self):
        artifact = self.artifact()
        self.assertIsNone(memory.get_generated_artifact_for_reply(100, 200, 99))
        pending = memory.create_pending_generated_revision(
            artifact["artifact_id"], 42, 1, "text_replace", "revision"
        )
        self.assertFalse(memory.cancel_pending_generated_revision(pending["revision_id"], 99))
        self.assertTrue(memory.cancel_pending_generated_revision(pending["revision_id"], 42))
        self.assertIsNone(memory.apply_generated_revision(pending["revision_id"], 42))

    def test_image_revision_must_be_claimed_before_apply(self):
        artifact = self.artifact("image", "telegram-original")
        pending = memory.create_pending_generated_revision(
            artifact["artifact_id"], 42, 1, "image_instruction", "Сделай фон холоднее"
        )
        self.assertIsNone(
            memory.apply_generated_revision(
                pending["revision_id"], 42, telegram_file_id="telegram-new"
            )
        )
        claimed = memory.begin_image_generated_revision(pending["revision_id"], 42)
        self.assertEqual(claimed["telegram_file_id"], "telegram-original")
        self.assertIsNone(memory.begin_image_generated_revision(pending["revision_id"], 42))
        applied = memory.apply_generated_revision(
            pending["revision_id"], 42, telegram_file_id="telegram-new"
        )
        self.assertEqual(applied["version"], 2)


class GeneratedArtifactTelegramTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(memory, "DB_PATH", str(Path(self.temp.name) / "naz.sqlite3"))
        self.db_patch.start()
        memory.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def make_artifact(self, kind="text", file_id=""):
        artifact = memory.create_generated_artifact(
            42, kind, "original", mode="post", telegram_file_id=file_id
        )
        memory.bind_generated_artifact_message(artifact["artifact_id"], 1, 42, 100, 200)
        return artifact

    def reply_update(self):
        message = SimpleNamespace(
            reply_to_message=SimpleNamespace(message_id=200),
            reply_text=AsyncMock(),
        )
        return SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100),
        )

    def callback_update(self, data):
        query = SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
            message=SimpleNamespace(chat_id=100),
        )
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42))

    def test_reply_only_requests_confirmation(self):
        self.make_artifact("image", "original-file")
        update = self.reply_update()
        with patch.object(main, "process_dialog_image_request", new=AsyncMock()) as provider:
            handled = asyncio.run(main.handle_generated_artifact_reply(update, "Сделай холоднее"))
        self.assertTrue(handled)
        provider.assert_not_awaited()
        update.message.reply_text.assert_awaited_once()
        self.assertIn("только после подтверждения", update.message.reply_text.await_args.args[0])

    def test_angle_control_message_is_not_registered_as_artifact(self):
        update = SimpleNamespace(
            message=SimpleNamespace(reply_text=AsyncMock()),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(id=100),
        )
        with patch.object(main, "is_angle_engine_message", return_value=True), patch.object(
            main, "reply_long", new=AsyncMock()
        ) as service_reply, patch.object(memory, "create_generated_artifact") as create:
            asyncio.run(
                main.reply_generated_text(
                    update, "service control", mode="post", keyboard=main.ANGLE_KEYBOARD
                )
            )
        service_reply.assert_awaited_once()
        create.assert_not_called()

    def test_text_apply_sends_exact_revision_without_publication(self):
        artifact = self.make_artifact()
        pending = memory.create_pending_generated_revision(
            artifact["artifact_id"], 42, 1, "text_replace", "Точный текст пользователя"
        )
        sent = SimpleNamespace(chat_id=100, message_id=300)
        bot = SimpleNamespace(send_message=AsyncMock(return_value=sent))
        update = self.callback_update(f"genrev_apply:{pending['revision_id']}")

        asyncio.run(main.generated_revision_callback(update, SimpleNamespace(bot=bot)))

        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.kwargs["text"], "Точный текст пользователя")
        update.callback_query.edit_message_text.assert_awaited_once()
        current = memory.get_generated_artifact_for_reply(100, 300, 42)
        self.assertEqual(current["current_version"], 2)

    def test_image_provider_runs_once_only_after_apply(self):
        artifact = self.make_artifact("image", "original-file")
        pending = memory.create_pending_generated_revision(
            artifact["artifact_id"], 42, 1, "image_instruction", "Сделай холоднее"
        )
        telegram_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"reference")))
        sent = SimpleNamespace(
            chat_id=100,
            message_id=301,
            photo=[SimpleNamespace(file_id="new-file")],
        )
        bot = SimpleNamespace(
            get_file=AsyncMock(return_value=telegram_file),
            send_photo=AsyncMock(return_value=sent),
            send_message=AsyncMock(),
        )
        update = self.callback_update(f"genrev_apply:{pending['revision_id']}")
        provider = AsyncMock(return_value=(b"new-image", "event"))

        with patch.object(main, "validate_reference_image", return_value="image/png"), patch.object(
            main, "process_dialog_image_request", new=provider
        ):
            asyncio.run(main.generated_revision_callback(update, SimpleNamespace(bot=bot)))
            asyncio.run(main.generated_revision_callback(update, SimpleNamespace(bot=bot)))

        provider.assert_awaited_once()
        bot.send_photo.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
