import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

import editorial_orchestrator as eo
import story_production as story
from tests.test_editorial_orchestrator import context


SAFE_FACTS = (
    "A failing build exposed one reproducible configuration mismatch.",
    "The configuration was compared with the known working release.",
    "One bounded setting was corrected and the build was repeated.",
    "The repeated check completed and changed the observable result.",
    "A regression check confirmed the result on the same input.",
)


def source(**overrides):
    values = {
        "source_ref": "chronicle:verified:001",
        "topic": "A verified work experiment",
        "source_type": "work_chronicle",
        "safe_facts": SAFE_FACTS,
        "source_verified": True,
        "concrete_action": True,
        "visualizable_process": True,
        "causal_bits": 5,
        "real_result": True,
        "contains_secrets": False,
        "contains_private_data": False,
    }
    values.update(overrides)
    return eo.EditorialSource(**values)


def planned(candidate=None):
    candidate = candidate or source()
    ctx = dataclasses.replace(
        context(seed="story-pack"),
        sources=(candidate,),
        rubrics=(eo.EditorialRubric("daily", "Daily", "work_chronicle", "document the work"),),
    )
    return eo.plan_release(ctx)


class StoryFirstTests(unittest.TestCase):
    def test_suitable_chronicle_selects_story_first(self):
        plan = planned()
        self.assertEqual((plan.content_format, plan.production_mode), ("story_pack", "story_first"))
        pack = story.plan_story_pack(plan, SAFE_FACTS)
        self.assertGreaterEqual(pack.scene_count, 4)
        self.assertLessEqual(pack.scene_count, 7)
        self.assertEqual(pack.renderer, story.RENDERER_UNAVAILABLE)

    def test_abstract_chronicle_stays_a_standard_post(self):
        plan = planned(source(concrete_action=False, visualizable_process=False))
        self.assertEqual((plan.content_format, plan.production_mode), ("text_post", "standard"))

    def test_insufficient_facts_stays_a_standard_post(self):
        plan = planned(source(causal_bits=3, safe_facts=SAFE_FACTS[:3]))
        self.assertEqual(plan.production_mode, "standard")

    def test_secret_or_private_material_never_selects_story_first(self):
        self.assertEqual(planned(source(contains_secrets=True)).production_mode, "standard")
        self.assertEqual(planned(source(contains_private_data=True)).production_mode, "standard")

    def test_one_short_episode_stays_a_standard_post(self):
        plan = planned(source(causal_bits=1, safe_facts=SAFE_FACTS[:1], real_result=False))
        self.assertEqual(plan.production_mode, "standard")

    def test_full_experiment_has_clean_story_and_nonsequential_reel_contracts(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        for scene in pack.scenes:
            self.assertIn("9:16", scene.clean_prompt)
            self.assertIn("Real motion", scene.clean_prompt)
            self.assertIn("No text", scene.clean_prompt)
            self.assertTrue(scene.story_overlay)
            self.assertTrue(scene.text_safe_zone)
            self.assertIn(pack.continuity_id, " ".join(scene.continuity_constraints))
            self.assertEqual(scene.duration_seconds, 5)
        for edit in pack.reel_edits:
            self.assertTrue(edit.hook)
            self.assertTrue(edit.conclusion)
            self.assertNotEqual(
                [shot["scene_id"] for shot in edit.shots],
                [scene.scene_id for scene in pack.scenes][: len(edit.shots)],
            )
            self.assertTrue(all(0.4 <= float(shot["duration_seconds"]) <= 2.0 for shot in edit.shots))
            self.assertTrue(12.0 <= sum(float(shot["duration_seconds"]) for shot in edit.shots) <= 20.0)
            self.assertTrue(
                any(
                    shot["source_shot_size"] != shot["reel_shot_size"]
                    for shot in edit.shots
                )
            )
            for shot in edit.shots:
                self.assertIn(shot["source_shot_size"], story.SHOT_SIZES)
                self.assertIn(shot["reel_shot_size"], story.SHOT_SIZES)
                self.assertTrue(shot["source_scene_id"])
                self.assertTrue(shot["crop_scale_instruction"])

    def test_naz_reference_roles_follow_shot_semantics(self):
        plan = dataclasses.replace(
            planned(),
            visual_subject_direction="the canonical Naz presence because the thesis concerns him",
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS)
        for scene in pack.scenes:
            self.assertTrue(scene.requires_naz_reference)
            expected = (
                "three_quarter_identity"
                if scene.shot_size in {"wide", "medium"}
                else "frontal_identity"
            )
            self.assertEqual(scene.reference_role, expected)

    def test_object_only_scenes_never_receive_naz_reference(self):
        plan = dataclasses.replace(
            planned(), visual_subject_direction="an object-only scene with no invented human hero"
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS)
        self.assertTrue(all(not scene.requires_naz_reference for scene in pack.scenes))
        self.assertTrue(all(scene.reference_role == "none" for scene in pack.scenes))

    def test_every_reel_fragment_must_be_between_point_four_and_two_seconds(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        edit = pack.reel_edits[0]
        for index in (0, len(edit.shots) // 2, len(edit.shots) - 1):
            shots = [dict(item) for item in edit.shots]
            shots[index]["duration_seconds"] = 9.0
            broken_edit = dataclasses.replace(edit, shots=tuple(shots))
            broken = dataclasses.replace(
                pack,
                reel_edits=(broken_edit, *pack.reel_edits[1:]),
            )
            with self.subTest(index=index), self.assertRaises(story.StoryPlanError):
                story.validate_story_pack(broken)

    def test_reel_without_shot_size_change_is_rejected(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        edit = pack.reel_edits[0]
        shots = []
        for item in edit.shots:
            shot = dict(item)
            shot["reel_shot_size"] = shot["source_shot_size"]
            shot["shot_size"] = shot["source_shot_size"]
            shots.append(shot)
        broken_edit = dataclasses.replace(edit, shots=tuple(shots))
        broken = dataclasses.replace(pack, reel_edits=(broken_edit, *pack.reel_edits[1:]))
        with self.assertRaises(story.StoryPlanError):
            story.validate_story_pack(broken)

    def test_sequential_story_concatenation_is_rejected_even_when_reframed(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        edit = pack.reel_edits[0]
        shots = []
        for item, scene in zip(edit.shots, pack.scenes):
            shot = dict(item)
            shot["scene_id"] = scene.scene_id
            shot["source_scene_id"] = scene.scene_id
            shots.append(shot)
        broken_edit = dataclasses.replace(edit, shots=tuple(shots))
        broken = dataclasses.replace(pack, reel_edits=(broken_edit, *pack.reel_edits[1:]))
        with self.assertRaises(story.StoryPlanError):
            story.validate_story_pack(broken)

    def test_partial_renderer_failure_is_preserved_for_resume(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        with tempfile.TemporaryDirectory() as root:
            pack_dir = story.persist_dry_run(pack, Path(root))
            manifest = pack_dir / "story_manifest.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["expected_outputs"]["stories"][0]["status"] = "generated"
            payload["expected_outputs"]["stories"][1]["status"] = "renderer_failed"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            resumed = story.persist_dry_run(pack, Path(root))
            current = json.loads((resumed / "story_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(current["expected_outputs"]["stories"][0]["status"], "generated")
            self.assertEqual(current["expected_outputs"]["stories"][1]["status"], "renderer_failed")

    def test_legacy_manifest_is_upgraded_without_losing_render_status(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        with tempfile.TemporaryDirectory() as root:
            pack_dir = story.persist_dry_run(pack, Path(root))
            manifest = pack_dir / "story_manifest.json"
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["expected_outputs"]["stories"][0]["status"] = "generated"
            for edit in payload["reel_edits"]:
                for shot in edit["shots"]:
                    shot.pop("source_scene_id", None)
                    shot.pop("source_shot_size", None)
                    shot.pop("reel_shot_size", None)
                    shot.pop("crop_scale_instruction", None)
                    shot["shot_size"] = "over-shoulder"
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            story.persist_dry_run(pack, Path(root))
            upgraded = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertTrue(story._manifest_has_current_reel_contract(upgraded))
            self.assertEqual(
                upgraded["expected_outputs"]["stories"][0]["status"],
                "generated",
            )
            self.assertEqual(list(pack_dir.rglob("*.mp4")), [])

    def test_public_current_contract_rejects_stale_v2_shapes(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        with tempfile.TemporaryDirectory() as root:
            manifest = story.persist_dry_run(pack, Path(root)) / "story_manifest.json"
            current = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertTrue(story.manifest_has_current_production_contract(current))

        stale_duration = json.loads(json.dumps(current))
        stale_duration["scenes"][0]["duration_seconds"] = 4
        self.assertFalse(story.manifest_has_current_production_contract(stale_duration))

        short_reel = json.loads(json.dumps(current))
        short_reel["reel_edits"][0]["shots"] = short_reel["reel_edits"][0]["shots"][:3]
        self.assertFalse(story.manifest_has_current_production_contract(short_reel))

        for missing_contract in ("model_policy", "model_route"):
            stale_model = json.loads(json.dumps(current))
            if missing_contract == "model_policy":
                stale_model.pop("model_policy")
            else:
                stale_model["scene_jobs"][0].pop("model_route")
            with self.subTest(missing_contract=missing_contract):
                self.assertFalse(story.manifest_has_current_production_contract(stale_model))

    def test_repeated_dry_run_is_idempotent_and_creates_no_fake_video(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        with tempfile.TemporaryDirectory() as root:
            first = story.persist_dry_run(pack, Path(root))
            second = story.persist_dry_run(pack, Path(root))
            self.assertEqual(first, second)
            self.assertEqual(list(first.rglob("*.mp4")), [])
            self.assertTrue((first / "story_manifest.json").is_file())
            self.assertTrue((first / "caption_pack.md").is_file())

    def test_missing_music_does_not_invent_or_select_a_track(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        self.assertIsNone(pack.music_plan["selected_track"])
        self.assertTrue(pack.music_plan["allowlist_required"])
        self.assertFalse(pack.music_plan["consume_publication_rotation"])

    def test_every_reel_cut_has_a_real_reframe(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        for edit in pack.reel_edits:
            self.assertTrue(all(shot["crop_change_required"] for shot in edit.shots))
            self.assertTrue(all(shot["reel_crop"] for shot in edit.shots))
            self.assertTrue(all(str(shot["source"]).endswith("_clean.mp4") for shot in edit.shots))

    def test_one_invalid_reel_fragment_rejects_the_entire_edit(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        edit = pack.reel_edits[0]
        shots = [dict(shot) for shot in edit.shots]
        shots[-1]["duration_seconds"] = 2.1
        broken = dataclasses.replace(
            pack,
            reel_edits=(dataclasses.replace(edit, shots=tuple(shots)), pack.reel_edits[1]),
        )
        with self.assertRaises(story.StoryPlanError):
            story.validate_story_pack(broken)

    def test_continuity_violation_is_a_qa_failure(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        broken_scene = dataclasses.replace(pack.scenes[0], continuity_constraints=("different identity",))
        broken = dataclasses.replace(pack, scenes=(broken_scene, *pack.scenes[1:]))
        with self.assertRaises(story.StoryPlanError):
            story.validate_story_pack(broken)


if __name__ == "__main__":
    unittest.main()
