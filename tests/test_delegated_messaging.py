import unittest

import delegated_messaging as dm


class DelegatedMessagingTests(unittest.TestCase):
    def test_natural_request_and_russian_dative_alias(self):
        alias, purpose = dm.parse_delegation_request("Напиши Диману, чтобы позвонил насчёт тачки")
        self.assertEqual(alias, "Диману")
        self.assertIn("тачки", purpose)
        contact = dm.resolve_saved_contact([{"alias": "Диман", "chat_id": 42}], alias)
        self.assertEqual(contact["chat_id"], 42)

    def test_intro_discloses_identity_and_owner(self):
        delegation = dm.create_delegation(
            character_id="naz", owner_user_id=1, contact_chat_id=2,
            contact_name="Диман", purpose="попросить позвонить насчёт машины",
        )
        text = dm.introduction(delegation)
        self.assertIn("AI-помощник", text)
        self.assertIn("Назар попросил", text)
        self.assertIn("стоп", text)

    def test_risk_and_stop_guards(self):
        self.assertIn("money", dm.assess_risk("переведи деньги на карту"))
        self.assertTrue(dm.is_stop("Стоп"))
        self.assertFalse(dm.is_stop("продолжай"))

    def test_unknown_or_ambiguous_alias_is_not_guessed(self):
        contacts = [{"alias": "Диман", "chat_id": 1}, {"alias": "Диман", "chat_id": 2}]
        self.assertIsNone(dm.resolve_saved_contact(contacts, "Диману"))
        self.assertIsNone(dm.resolve_saved_contact(contacts, "Саше"))

    def test_one_off_contact_message_requires_colon(self):
        self.assertEqual(
            dm.parse_contact_message_request("Напиши Диману: Привет, созвонимся вечером?"),
            ("Диману", "Привет, созвонимся вечером?"),
        )
        self.assertIsNone(dm.parse_contact_message_request("Напиши Диману, чтобы позвонил вечером"))

    def test_empty_or_oversized_contact_message_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "пустое"):
            dm.parse_contact_message_request("Напиши Диману:   ")
        with self.assertRaisesRegex(ValueError, "3500"):
            dm.parse_contact_message_request("Напиши Диману: " + "я" * 3501)

    def test_spoken_message_resolves_known_dative_alias(self):
        contacts = [{"alias": "Сын", "chat_id": 42}]
        contact, message = dm.parse_saved_contact_message_request(
            contacts,
            "Напиши сыну сообщение тест связи, отвечать не обязательно",
        )
        self.assertEqual(contact["chat_id"], 42)
        self.assertEqual(message, "тест связи, отвечать не обязательно")

    def test_spoken_message_does_not_capture_delegation(self):
        contacts = [{"alias": "Сын", "chat_id": 42}]
        self.assertIsNone(
            dm.parse_saved_contact_message_request(contacts, "Напиши сыну, чтобы позвонил вечером")
        )

    def test_voice_delivery_request_is_explicit(self):
        contacts = [{"alias": "Сын", "chat_id": 42}]
        contact, message = dm.parse_saved_contact_voice_request(
            contacts,
            "Отправь сыну голосовое: привет, созвонимся вечером",
        )
        self.assertEqual(contact["chat_id"], 42)
        self.assertEqual(message, "привет, созвонимся вечером")
        contact, message = dm.parse_saved_contact_voice_request(
            contacts,
            "Запиши голосовое сыну тест связи",
        )
        self.assertEqual(contact["chat_id"], 42)
        self.assertEqual(message, "тест связи")

    def test_plain_message_is_not_misclassified_as_voice(self):
        contacts = [{"alias": "Сын", "chat_id": 42}]
        self.assertIsNone(dm.parse_saved_contact_voice_request(contacts, "Напиши сыну сообщение тест связи"))


if __name__ == "__main__":
    unittest.main()
