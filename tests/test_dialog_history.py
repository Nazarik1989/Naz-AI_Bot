import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
import memory


class DialogHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "dialog.sqlite3")
        self.db_patch = patch.object(memory, "DB_PATH", self.db_path)
        self.db_patch.start()
        memory.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_second_message_receives_first_pair_in_role_order_without_duplicate(self):
        captured = []

        async def fake_call(messages, **kwargs):
            captured.append(messages)
            return "Первый ответ" if len(captured) == 1 else "Второй ответ"

        with patch.object(main, "call_gpt", side_effect=fake_call):
            asyncio.run(main.generate_answer(101, "Первая реплика"))
            asyncio.run(main.generate_answer(101, "Вторая реплика"))

        second = captured[1]
        self.assertEqual([item["role"] for item in second], ["system", "user", "assistant", "user"])
        self.assertEqual([item["content"] for item in second[1:]], ["Первая реплика", "Первый ответ", "Вторая реплика"])
        self.assertEqual(sum(item["content"] == "Вторая реплика" for item in second), 1)

    def test_user_histories_are_isolated(self):
        memory.save_dialog_turn(1, "user one", "answer one")
        memory.save_dialog_turn(2, "user two", "answer two")
        self.assertEqual([item["content"] for item in memory.get_history(1, 20)], ["user one", "answer one"])
        self.assertEqual([item["content"] for item in memory.get_history(2, 20)], ["user two", "answer two"])

    def test_dialog_reset_preserves_state_and_content_memory(self):
        state = memory.load_state(7)
        state["content_goal"] = "engagement"
        state["goal"] = "engagement"
        memory.save_state(7, state)
        memory.add_memory_item(7, "note", "keep this")
        memory.save_dialog_turn(7, "remove user", "remove assistant")

        memory.clear_dialog_history(7)

        self.assertEqual(memory.get_history(7, 20), [])
        self.assertIn("keep this", memory.get_memory_context(7))
        self.assertEqual(memory.load_state(7)["content_goal"], "engagement")

    def test_model_error_does_not_save_half_turn(self):
        with patch.object(main, "call_gpt", new=AsyncMock(side_effect=RuntimeError("model failed"))):
            with self.assertRaises(RuntimeError):
                asyncio.run(main.generate_answer(33, "Не сохраняй меня"))
        self.assertEqual(memory.get_history(33, 20), [])

    def test_content_generation_does_not_enter_dialog_history(self):
        with patch.object(main, "call_gpt", new=AsyncMock(return_value="готовый пост")):
            asyncio.run(main.generate_answer(44, "служебный prompt", task="post", source_topic="topic"))
        self.assertEqual(memory.get_history(44, 20), [])


if __name__ == "__main__":
    unittest.main()
