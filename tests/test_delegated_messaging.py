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


if __name__ == "__main__":
    unittest.main()
