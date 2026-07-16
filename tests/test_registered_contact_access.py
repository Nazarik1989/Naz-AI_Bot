import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ApplicationHandlerStop

import main
import memory


def fake_update(user_id: int = 77, text: str = "") -> SimpleNamespace:
    message = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(),
        reply_photo=AsyncMock(),
        reply_document=AsyncMock(),
        reply_to_message=None,
    )
    return SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=user_id, first_name="Контакт", username="contact"),
        effective_chat=SimpleNamespace(id=user_id, send_action=AsyncMock()),
    )


def button_texts(markup) -> set[str]:
    return {button.text for row in markup.keyboard for button in row}


class RegisteredContactAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(memory, "DB_PATH", str(Path(self.tmp.name) / "access.sqlite3"))
        self.admin_patch = patch.object(main, "ADMIN_ID", 1)
        self.db_patch.start()
        self.admin_patch.start()
        memory.init_db()
        memory.save_named_contact(1, 77, "Контакт", "Друг")

    def tearDown(self):
        main.USER_PENDING_ACTIONS.clear()
        self.admin_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_owner_and_saved_contact_have_access_but_unknown_user_does_not(self):
        self.assertTrue(main.has_registered_access(1))
        self.assertTrue(main.has_registered_access(77))
        self.assertFalse(main.has_registered_access(99))

    def test_contact_menu_contains_only_user_functions(self):
        buttons = button_texts(main.main_keyboard_for(77))
        self.assertEqual(buttons, {main.BTN_AI, main.BTN_CONTENT, main.BTN_HELP})
        self.assertNotIn(main.BTN_LINKS, buttons)
        self.assertNotIn(main.BTN_CONTROL, buttons)
        self.assertIn(main.BTN_CONTROL, button_texts(main.main_keyboard_for(1)))

    def test_saved_contact_can_open_content_buttons(self):
        update = fake_update(text=main.BTN_CONTENT)
        handled = asyncio.run(main.handle_menu_button(update, SimpleNamespace(), main.BTN_CONTENT))
        self.assertTrue(handled)
        self.assertIs(update.message.reply_text.await_args.kwargs["reply_markup"], main.CONTENT_KEYBOARD)

    def test_saved_contact_can_generate_content_when_admin_only_flag_is_on(self):
        update = fake_update(text="/post тест")
        context = SimpleNamespace(args=["тест"])
        with patch.object(main, "ADMIN_ONLY_CONTENT", True), patch.object(
            main, "generate_content", new=AsyncMock(return_value="Готовый пост")
        ) as generate, patch.object(main, "send_typing", new=AsyncMock()), patch.object(
            main, "reply_long", new=AsyncMock()
        ):
            asyncio.run(main.content_command(update, context, "post"))
        generate.assert_awaited_once()

    def test_saved_contact_can_generate_dialog_image(self):
        update = fake_update(text="/image маяк")
        with patch.object(
            main, "process_dialog_image_request", new=AsyncMock(return_value=(b"image", "event"))
        ):
            asyncio.run(main.send_dialog_image(update, "маяк"))
        update.message.reply_photo.assert_awaited_once()
        markup = update.message.reply_photo.await_args.kwargs["reply_markup"]
        self.assertEqual(button_texts(markup), {main.BTN_AI, main.BTN_CONTENT, main.BTN_HELP})

    def test_admin_button_stays_closed_and_returns_contact_menu(self):
        update = fake_update(text=main.BTN_CONTROL)
        handled = asyncio.run(main.handle_menu_button(update, SimpleNamespace(), main.BTN_CONTROL))
        self.assertTrue(handled)
        markup = update.message.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(button_texts(markup), {main.BTN_AI, main.BTN_CONTENT, main.BTN_HELP})

    def test_contact_help_does_not_advertise_admin_commands(self):
        text = main.help_commands_for(77)
        self.assertIn("/image", text)
        self.assertNotIn("/publish", text)
        self.assertNotIn("/contacts", text)

    def test_unknown_user_is_stopped_before_handlers(self):
        update = fake_update(user_id=99, text="Нарисуй кота")
        with patch.object(main, "ensure_contact_named", new=AsyncMock()) as notify_owner:
            with self.assertRaises(ApplicationHandlerStop):
                asyncio.run(main.registered_access_guard(update, SimpleNamespace()))
        notify_owner.assert_awaited_once()
        self.assertIn("сохранённым контактам", update.message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
