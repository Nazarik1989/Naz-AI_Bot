import asyncio
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

import editorial_policy as policy
import main


RUBRICS = {
    "AI без магии",
    "Игровая лаборатория VK",
    "MATERIAL / МАТЕРИЯ",
    "visual_archive",
    "canonical_story",
}


def make_brief(**overrides):
    values = {
        "destination": "telegram",
        "scheduled_slot": "14:00",
        "source_type": "scheduled_rubric",
        "source_reference": "schedule:2026-07-21:14:00",
        "rubric": "AI без магии",
        "thesis": "A useful AI tool begins with a concrete responsibility boundary.",
        "context_reason": "The configured 14:00 rubric selected this approved subject.",
        "visual_subject": "a physical prototype beside one clearly labelled control",
        "visual_relation": "the single control makes the responsibility boundary visible",
        "allowed_rubrics": RUBRICS,
        "required_elements": ("physical prototype",),
        "forbidden_elements": ("luxury showroom",),
        "music_required": False,
    }
    values.update(overrides)
    return policy.build_brief(**values)


class EditorialPolicyContractTests(unittest.TestCase):
    def test_naz_ai_dev_snapshot(self):
        brief = make_brief()
        compiled = policy.render_text_instructions(brief, "Naz persona rules v2.4")
        snapshot = hashlib.sha256(compiled.encode("utf-8")).hexdigest()
        self.assertEqual(snapshot, "5cccec915e9a5bec20f702283177024e916218ae42d3773f67a8af8df6f36d4e")
        self.assertLess(compiled.index("Security and access control"), compiled.index("Creative variation"))
        self.assertIn(brief.post_id, compiled)

    def test_naz_gaming_and_material_visual_snapshots(self):
        gaming = make_brief(
            destination="vk",
            scheduled_slot="vk:gaming",
            source_reference="schedule:2026-07-21:gaming",
            rubric="Игровая лаборатория VK",
            thesis="A game choice matters when it closes another path.",
            visual_subject="a tangible controller prototype with one disabled route",
            visual_relation="the disabled route makes the cost of the choice literal",
            music_required=True,
        )
        material = make_brief(
            destination="vk",
            scheduled_slot="vk:material",
            source_reference="schedule:2026-07-21:material",
            rubric="MATERIAL / МАТЕРИЯ",
            thesis="Tool marks reveal how a prototype was actually made.",
            visual_subject="machined titanium surface with visible tool marks",
            visual_relation="the marks are direct evidence of the making process",
            music_required=True,
        )
        snapshots = [
            hashlib.sha256(policy.render_visual_instructions(brief, "Naz visual v2").encode("utf-8")).hexdigest()
            for brief in (gaming, material)
        ]
        self.assertEqual(snapshots, [
            "54036351698468c67274e835dda690d8c2c47b854c2ad47400bb8e01e2d7afbd",
            "6fac95c0a3abea138b4b9d261ab768ae5ada035d7d9e3953e09072655b9e9f28",
        ])
        self.assertTrue(gaming.music_required and material.music_required)

    def test_approved_backstage_and_canonical_story_are_typed(self):
        backstage = make_brief(
            source_type="approved_backstage_seed",
            source_reference="archive:approved:42",
            rubric="visual_archive",
        )
        story = make_brief(
            source_type="canonical_story",
            source_reference="story:canonical:7",
            rubric="canonical_story",
        )
        self.assertEqual(backstage.source_type, "approved_backstage_seed")
        self.assertEqual(story.source_type, "canonical_story")

    def test_random_untyped_topic_and_missing_source_are_rejected(self):
        with self.assertRaisesRegex(policy.BriefValidationError, "source type"):
            make_brief(source_type="random_topic")
        with self.assertRaisesRegex(policy.BriefValidationError, "reference"):
            make_brief(source_reference="")

    def test_people_default_and_cliches_are_forbidden(self):
        brief = make_brief()
        self.assertFalse(brief.people_allowed)
        forbidden = " ".join(brief.forbidden_elements).casefold()
        self.assertIn("elderly", forbidden)
        self.assertIn("humanoid robot", forbidden)
        with self.assertRaisesRegex(policy.BriefValidationError, "who, action and why"):
            make_brief(
                people_allowed=True,
                allowed_people_description="engineer near prototype",
            )

    def test_conflicting_visual_rules_and_unknown_rubric_fail_closed(self):
        with self.assertRaisesRegex(policy.BriefValidationError, "conflict"):
            make_brief(required_elements=("same",), forbidden_elements=("same",))
        with self.assertRaisesRegex(policy.BriefValidationError, "registered"):
            make_brief(rubric="random")

    def test_image_gate_rejects_unrelated_subject(self):
        raw = json.dumps({
            "accepted": False,
            "reason_code": "image_subject_mismatch",
            "literal_description": "an unexplained elderly person at a window",
            "subject_matches": False,
            "thesis_supported": False,
            "unexplained_people": True,
            "unexplained_elements": False,
            "visual_bible_matches": False,
            "why_here": True,
        })
        decision = policy.parse_image_gate_response(raw)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason_code, "image_subject_mismatch")

    def test_two_failed_regenerations_skip_and_keep_same_brief(self):
        brief = make_brief()
        seen = []

        async def generate(instruction, received):
            seen.append((instruction, received))
            return f"candidate-{len(seen)}"

        async def reject(_candidate, _brief):
            return False, "image_subject_mismatch"

        result = asyncio.run(policy.generate_with_relevance_gate(
            brief=brief,
            generate=generate,
            validate=reject,
        ))
        self.assertFalse(result.accepted)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(received is brief for _, received in seen))
        self.assertIn("reason_code=image_subject_mismatch", seen[1][0])

    def test_publication_fallback_is_forbidden(self):
        with patch.object(main, "generate_openai_image_bytes", new=AsyncMock(return_value=None)), patch.object(
            main, "generate_bfl_image_bytes", new=AsyncMock(return_value=None)
        ), patch.object(main, "generate_hf_image_bytes", new=AsyncMock(return_value=None)), patch.object(
            main, "fallback_image_bytes", new=AsyncMock(return_value=b"fallback")
        ) as fallback:
            result = asyncio.run(main.generate_image_bytes("prompt", allow_fallback=False))
        self.assertIsNone(result)
        fallback.assert_not_awaited()

    def test_required_media_never_falls_back_to_text(self):
        bot = AsyncMock()
        with self.assertRaisesRegex(RuntimeError, "required editorial image"):
            asyncio.run(main.send_post_with_images(bot, "@channel", "post", [], require_images=True))
        bot.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
