import unittest

import character_state
import content_formats
import duo_relationship


class DuoRelationshipTests(unittest.TestCase):
    def test_private_thought_is_original_material_not_a_publication(self) -> None:
        relationship = duo_relationship.RelationshipState()
        payload = duo_relationship.build_private_thought_payload(
            speaker="void",
            thought="Скорость становится клеткой, когда человек перестаёт замечать направление движения.",
            topic="скорость",
            relationship=relationship,
        )
        self.assertTrue(payload["private"])
        self.assertFalse(payload["already_published"])
        self.assertFalse(payload["ready_to_publish"])
        self.assertTrue(payload["public_attribution_allowed"])
        self.assertFalse(payload["quotation_allowed"])

    def test_public_reflection_may_naturally_mention_conversation(self) -> None:
        relationship = duo_relationship.RelationshipState()
        payload = duo_relationship.build_private_thought_payload(
            speaker="void",
            thought="Скорость становится клеткой, когда человек перестаёт замечать направление движения.",
            topic="скорость",
            relationship=relationship,
        )
        brief = duo_relationship.reflection_brief(
            receiver="naz", payload=payload, relationship=relationship,
            receiver_character_context="Naz context",
        )
        self.assertIn("Мы тут с VOID спорили", brief)
        self.assertIn("never copy the thought verbatim", brief)

    def test_news_attitudes_are_character_specific(self) -> None:
        title = "Революционная платформа навсегда изменит внимание пользователей"
        self.assertEqual(duo_relationship.news_attitude("naz", title)["stance"], "protective_skeptic")
        self.assertEqual(duo_relationship.news_attitude("void", title)["stance"], "quiet_disgust")

    def test_simulation_does_not_mutate_saved_state(self) -> None:
        state = character_state.CharacterState()
        original = state.to_dict()
        plans = character_state.simulate(state, [], count=8)
        self.assertEqual(state.to_dict(), original)
        self.assertEqual(len(plans), 8)

    def test_conversation_format_requires_private_context(self) -> None:
        chosen = content_formats.choose_format([], platform="telegram", energy=80, seed_key="a")
        self.assertNotEqual(chosen["key"], "dialogue_reflection")

    def test_verbatim_private_thought_is_blocked(self) -> None:
        source = "Скорость становится клеткой, когда человек перестаёт замечать направление движения."
        ok, reason = duo_relationship.reflection_is_original(source, f"Думаю вот о чём. {source} И это важно для всех нас.")
        self.assertFalse(ok)
        self.assertIn("copied verbatim", reason)


if __name__ == "__main__":
    unittest.main()
