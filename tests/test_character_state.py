import unittest

import character_state as character


class NazCharacterStateTests(unittest.TestCase):
    def test_events_change_state_without_changing_core(self) -> None:
        state = character.CharacterState()
        original_core = state.core_version
        for event in ("new_topic", "failure", "void_challenge", "success", "publish"):
            state = character.apply_event(state, event)

        self.assertEqual(state.core_version, original_core)
        self.assertIn(state.facet, character.FACETS)
        for axis in ("energy", "warmth", "tension", "curiosity", "confidence", "sociability"):
            self.assertGreaterEqual(getattr(state, axis), 0)
            self.assertLessEqual(getattr(state, axis), 100)

    def test_failure_can_reveal_honest_novice_instead_of_fake_confidence(self) -> None:
        state = character.CharacterState(energy=42, confidence=50, tension=40)
        state = character.apply_event(state, "failure")
        self.assertEqual(state.facet, "honest_novice")

    def test_void_challenge_keeps_warmth_inside_conflict(self) -> None:
        state = character.CharacterState()
        changed = character.apply_event(state, "void_challenge")
        self.assertGreater(changed.curiosity, 92)
        self.assertGreater(changed.warmth, 64)

    def test_planner_respects_recent_shape_cooldowns(self) -> None:
        recent = [{
            "intent": "исследовать",
            "format": "маленькая история",
            "hook": "сцена",
            "media": "редакционная иллюстрация",
            "facet": "explorer",
        }] * 4
        plan = character.plan_content(character.CharacterState(), recent, topic="AI agents", platform="telegram")
        self.assertNotEqual(plan["intent"], "исследовать")
        self.assertNotEqual(plan["format"], "маленькая история")
        self.assertNotEqual(plan["hook"], "сцена")
        self.assertNotEqual(plan["media"], "редакционная иллюстрация")

    def test_prompt_preserves_character_invariants(self) -> None:
        state = character.CharacterState()
        plan = character.plan_content(state, [], topic="test", platform="telegram")
        prompt = character.prompt_context(state, plan)
        self.assertIn("молодой талантливый билдер", prompt)
        self.assertIn("не превращай его в гуру", prompt)

    def test_admin_axis_correction_is_clamped_and_reselects_facet(self) -> None:
        state = character.set_axis(character.CharacterState(), "energy", -20)
        self.assertEqual(state.energy, 0)
        self.assertEqual(state.facet, "honest_novice")


if __name__ == "__main__":
    unittest.main()
