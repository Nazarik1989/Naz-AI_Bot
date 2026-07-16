import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
import memory
from telegram.error import Forbidden


def fake_message_update(text: str = ""):
    message = SimpleNamespace(text=text, reply_text=AsyncMock())
    return SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=77),
        callback_query=None,
    )


def fake_callback_update(data: str):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        message=None,
        effective_user=SimpleNamespace(id=77),
        callback_query=query,
    )


class ContactMessageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "contact-messages.sqlite3")
        self.db_patch = patch.object(memory, "DB_PATH", self.db_path)
        self.db_patch.start()
        memory.init_db()
        memory.save_named_contact(77, 88, "Дмитрий", "Диман")

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def prepare_draft(self) -> tuple[SimpleNamespace, dict]:
        update = fake_message_update("Напиши Диману: Привет, созвонимся вечером?")
        with patch.object(main, "is_admin", return_value=True):
            handled = asyncio.run(
                main.prepare_contact_message_request(update, SimpleNamespace(), update.message.text)
            )
        self.assertTrue(handled)
        update.message.reply_text.assert_awaited_once()
        preview = update.message.reply_text.await_args.args[0]
        self.assertIn("Контакт: Диман", preview)
        self.assertIn("Привет, созвонимся вечером?", preview)
        markup = update.message.reply_text.await_args.kwargs["reply_markup"]
        callback_data = markup.inline_keyboard[0][0].callback_data
        message_id = int(callback_data.rsplit(":", 1)[1])
        draft = memory.get_pending_contact_message(message_id, 77)
        self.assertIsNotNone(draft)
        return update, draft

    def test_preview_creates_pending_draft_without_sending(self):
        _, draft = self.prepare_draft()
        self.assertEqual(draft["contact_chat_id"], 88)
        self.assertEqual(draft["status"], "pending")

    def test_owner_confirmation_sends_exact_message_once(self):
        _, draft = self.prepare_draft()
        update = fake_callback_update(f"contact_send:{draft['id']}")
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(main, "is_admin", return_value=True):
            asyncio.run(main.contact_message_callback(update, SimpleNamespace(bot=bot)))
        bot.send_message.assert_awaited_once_with(
            chat_id=88,
            text="Сообщение от Назара:\n\nПривет, созвонимся вечером?",
            disable_web_page_preview=True,
        )
        self.assertIsNone(memory.get_pending_contact_message(draft["id"], 77))
        update.callback_query.edit_message_text.assert_awaited_once()

    def test_cancel_does_not_send(self):
        _, draft = self.prepare_draft()
        update = fake_callback_update(f"contact_cancel:{draft['id']}")
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(main, "is_admin", return_value=True):
            asyncio.run(main.contact_message_callback(update, SimpleNamespace(bot=bot)))
        bot.send_message.assert_not_awaited()
        self.assertIsNone(memory.get_pending_contact_message(draft["id"], 77))
        self.assertIn("не отправлено", update.callback_query.edit_message_text.await_args.args[0])

    def test_other_user_cannot_confirm(self):
        _, draft = self.prepare_draft()
        update = fake_callback_update(f"contact_send:{draft['id']}")
        update.effective_user.id = 999
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(main, "is_admin", return_value=False):
            asyncio.run(main.contact_message_callback(update, SimpleNamespace(bot=bot)))
        bot.send_message.assert_not_awaited()
        self.assertIsNotNone(memory.get_pending_contact_message(draft["id"], 77))

    def test_telegram_failure_keeps_draft_and_retry_buttons(self):
        _, draft = self.prepare_draft()
        update = fake_callback_update(f"contact_send:{draft['id']}")
        bot = SimpleNamespace(send_message=AsyncMock(side_effect=Forbidden("blocked")))
        with patch.object(main, "is_admin", return_value=True):
            asyncio.run(main.contact_message_callback(update, SimpleNamespace(bot=bot)))
        self.assertIsNotNone(memory.get_pending_contact_message(draft["id"], 77))
        kwargs = update.callback_query.edit_message_text.await_args.kwargs
        callbacks = [button.callback_data for button in kwargs["reply_markup"].inline_keyboard[0]]
        self.assertEqual(
            callbacks,
            [f"contact_send:{draft['id']}", f"contact_cancel:{draft['id']}"],
        )


if __name__ == "__main__":
    unittest.main()
