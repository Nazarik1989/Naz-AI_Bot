import unittest

from main import AUTOPOST_TIMES, select_naz_telegram_rubric


class TelegramScheduleTests(unittest.TestCase):
    def test_default_schedule_uses_requested_moscow_slots(self) -> None:
        self.assertEqual(AUTOPOST_TIMES, "10:00,14:00,18:00,22:00")

    def test_each_slot_keeps_its_editorial_rubric(self) -> None:
        expected = {
            "10:00": "Утренний дожим",
            "14:00": "AI без магии",
            "18:00": "Баг, который стал системой",
            "22:00": "Naz после смены",
        }
        for slot, name in expected.items():
            with self.subTest(slot=slot):
                self.assertEqual(select_naz_telegram_rubric(slot)["name"], name)


if __name__ == "__main__":
    unittest.main()
