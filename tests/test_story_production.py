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


def director_response(plan, facts=SAFE_FACTS, *, variant_index=0, action_prefix="Calibrate"):
    count = max(4, min(7, len(facts)))
    story_plan_id = story._variant_plan_id(plan.plan_id, variant_index)
    roles = story._roles(story_plan_id, count)
    scenes = []
    for index, role in enumerate(roles, 1):
        scenes.append({
            "role": role,
            "setting": f"distinct physical validation chamber number {index}",
            "subject": "one physical optical-titanium prototype",
            "concrete_action": f"{action_prefix} mechanical coupling number {index} under controlled load",
            "start_state": f"coupling number {index} is visibly misaligned",
            "end_state": f"coupling number {index} completes one stable motion",
            "shot_size": story.SHOT_SIZES[(index - 1) % len(story.SHOT_SIZES)],
            "camera_motion": story.CAMERA_MOTIONS[(index - 1) % len(story.CAMERA_MOTIONS)],
        })
    return json.dumps({
        "director_version": story.DIRECTOR_VERSION,
        "visual_concept": "a failed configuration becoming one testable physical mechanism",
        "scenes": scenes,
    })


class StoryFirstTests(unittest.TestCase):
    def test_reference_detection_uses_words_not_face_substrings(self):
        self.assertFalse(story._requires_reference("one specific work surface at the result"))
        self.assertTrue(story._requires_reference("the canonical Naz in the laboratory"))
        self.assertTrue(story._requires_reference("a close portrait of the founder's face"))

    def test_director_response_format_requires_exact_scene_count_and_strict_json(self):
        response_format = story.reels_director_response_format(SAFE_FACTS)
        self.assertEqual(response_format["type"], "json_schema")
        contract = response_format["json_schema"]
        self.assertTrue(contract["strict"])
        scenes = contract["schema"]["properties"]["scenes"]
        self.assertEqual(scenes["minItems"], len(SAFE_FACTS))
        self.assertEqual(scenes["maxItems"], len(SAFE_FACTS))
        self.assertFalse(scenes["items"]["additionalProperties"])

    def test_neutral_work_surface_direction_allows_naz_and_object_scenes(self):
        plan = dataclasses.replace(
            planned(),
            visual_subject_direction="one specific work surface at the moment a result changes",
        )
        payload = json.loads(director_response(plan))
        payload["scenes"][0]["subject"] = "Naz, the same real adult human founder"
        treatment = story.parse_reels_director_response(
            json.dumps(payload), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS, director_treatment=treatment)
        self.assertTrue(pack.scenes[0].requires_naz_reference)
        self.assertFalse(pack.scenes[1].requires_naz_reference)

    def test_naz_ai_lab_brand_on_an_object_does_not_trigger_face_reference(self):
        self.assertFalse(story._is_naz_human_subject("one Naz AI Lab optical prototype"))
        self.assertTrue(story._is_naz_human_subject("Naz, the same real adult human founder"))

    def test_explicit_object_only_direction_rejects_naz_subject(self):
        plan = dataclasses.replace(
            planned(), visual_subject_direction="an object-only scene with no person"
        )
        payload = json.loads(director_response(plan))
        payload["scenes"][0]["subject"] = "Naz, the real adult human founder"
        with self.assertRaisesRegex(story.StoryPlanError, "director_subject_identity_invalid_1"):
            story.parse_reels_director_response(json.dumps(payload), plan, SAFE_FACTS)

    def test_director_roles_follow_causal_order_and_never_start_with_result(self):
        expected = {
            4: ["hook", "problem", "test", "result"],
            5: ["hook", "problem", "test", "result", "conclusion"],
            6: ["hook", "problem", "hypothesis", "test", "result", "conclusion"],
            7: list(story.DRAMATURGIC_ROLES),
        }
        for count, roles in expected.items():
            with self.subTest(count=count):
                self.assertEqual(story._roles("any-plan-id", count), roles)

    def test_semantic_director_response_becomes_the_immutable_scene_plan(self):
        plan = planned()
        treatment = story.parse_reels_director_response(
            director_response(plan), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(
            plan, SAFE_FACTS, director_treatment=treatment
        )
        self.assertEqual(pack.director_version, story.DIRECTOR_VERSION)
        self.assertEqual(pack.visual_concept, treatment.visual_concept)
        self.assertEqual(
            [scene.concrete_action for scene in pack.scenes],
            [scene.concrete_action for scene in treatment.scenes],
        )
        self.assertTrue(all("tied to fact" not in scene.setting for scene in pack.scenes))
        self.assertTrue(all("fact 1" not in scene.concrete_action for scene in pack.scenes))

    def test_semantic_director_rejects_metadata_shaped_scene_content(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][0]["setting"] = "A real setting tied to fact 1"
        with self.assertRaisesRegex(story.StoryPlanError, "director_setting_invalid"):
            story.parse_reels_director_response(json.dumps(payload), plan, SAFE_FACTS)

    def test_2026_07_08_route_accepts_concrete_technical_locations(self):
        plan = planned(source(source_ref="agent_content:2026-07-08:fixture"))
        payload = json.loads(director_response(plan))
        locations = (
            "Naz AI Lab",
            "server room",
            "terminal bay",
            "GitHub build bench",
            "code test chamber",
        )
        for scene, location in zip(payload["scenes"], locations):
            scene["setting"] = location

        treatment = story.parse_reels_director_response(
            json.dumps(payload), plan, SAFE_FACTS
        )

        self.assertEqual(
            [scene.setting for scene in treatment.scenes],
            list(locations),
        )

    def test_director_still_rejects_internal_transport_markers(self):
        plan = planned()
        for invalid_setting in (
            "setting tied to fact 2",
            "Folders: private episode",
            "project: Naz_AI_Bot_clean",
            "source_ref inside scene",
            "plan_id inside scene",
        ):
            with self.subTest(invalid_setting=invalid_setting):
                payload = json.loads(director_response(plan))
                payload["scenes"][0]["setting"] = invalid_setting
                with self.assertRaisesRegex(story.StoryPlanError, "director_setting_invalid"):
                    story.parse_reels_director_response(
                        json.dumps(payload), plan, SAFE_FACTS
                    )

    def test_director_still_rejects_cheap_visual_cliches(self):
        plan = planned()
        for invalid_setting in (
            "overloaded HUD control room",
            "random circuit chamber",
            "flowing code projection room",
        ):
            with self.subTest(invalid_setting=invalid_setting):
                payload = json.loads(director_response(plan))
                payload["scenes"][0]["setting"] = invalid_setting
                with self.assertRaisesRegex(story.StoryPlanError, "director_setting_invalid"):
                    story.parse_reels_director_response(
                        json.dumps(payload), plan, SAFE_FACTS
                    )

    def test_semantic_director_rejects_malformed_json_without_template_fallback(self):
        with self.assertRaisesRegex(story.StoryPlanError, "director_json_invalid"):
            story.parse_reels_director_response("not-json", planned(), SAFE_FACTS)

    def test_director_prompt_is_content_bound_and_contains_no_source_transport_fields(self):
        prompt = story.reels_director_prompt(planned(), SAFE_FACTS)
        self.assertIn(SAFE_FACTS[0], prompt)
        self.assertIn(story.DIRECTOR_VERSION, prompt)
        self.assertNotIn("source_ref", prompt)
        self.assertNotIn("plan_id", prompt)

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
            self.assertEqual(scene.identity_reference_usage, "identity_only")
            self.assertIn("@Naz", scene.keyframe_prompt)
            self.assertIn("replace the reference background", scene.keyframe_prompt)
            self.assertNotIn("tied to fact", scene.setting)

    def test_story_plan_id_is_schema_scoped_and_cannot_reuse_an_old_manifest(self):
        plan = planned()
        pack = story.plan_story_pack(plan, SAFE_FACTS)
        self.assertNotEqual(pack.plan_id, plan.plan_id)
        self.assertEqual(pack.base_plan_id, plan.plan_id)
        self.assertEqual(len(pack.plan_id), 24)

    def test_current_pack_is_created_beside_old_base_id_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as root:
            plan = planned()
            old_dir = Path(root) / plan.plan_id
            old_dir.mkdir()
            old_manifest = old_dir / "story_manifest.json"
            old_manifest.write_text(
                json.dumps({"schema": story.OLDER_STORY_SCHEMA, "plan_id": plan.plan_id}),
                encoding="utf-8",
            )
            before = old_manifest.read_bytes()

            pack = story.plan_story_pack(plan, SAFE_FACTS)
            current_dir = story.persist_story_queue(pack, Path(root))

            self.assertNotEqual(current_dir, old_dir)
            self.assertEqual(old_manifest.read_bytes(), before)
            self.assertEqual(
                story.read_manifest(current_dir / "story_manifest.json")["schema"],
                story.STORY_SCHEMA,
            )

    def test_visual_treatment_comes_from_episode_meaning_not_one_fixed_room(self):
        constrained = story.plan_story_pack(
            planned(),
            (
                "The generation credits were exhausted during a bounded build.",
                "The team continued with tools already available.",
                "One physical system route was selected.",
                "The route was tested without increasing the limit.",
            ),
        )
        field = story.plan_story_pack(
            planned(),
            (
                "A field prototype left the laboratory for a city rooftop.",
                "Wind exposed one unstable physical mounting.",
                "A bounded brace adjustment was selected.",
                "The outdoor relay remained stable after the test.",
            ),
        )
        self.assertEqual(
            constrained.visual_concept,
            story.VISUAL_TREATMENTS["constraint_recovery"]["label"],
        )
        self.assertEqual(
            field.visual_concept,
            story.VISUAL_TREATMENTS["field_experiment"]["label"],
        )
        self.assertNotEqual(constrained.visual_concept, field.visual_concept)
        self.assertGreaterEqual(len({scene.setting for scene in constrained.scenes}), 4)
        self.assertTrue(all("tied to fact" not in scene.setting for scene in constrained.scenes))
        self.assertTrue(all("Perform and reveal" not in scene.concrete_action for scene in constrained.scenes))

    def test_object_only_scenes_never_receive_naz_reference(self):
        plan = dataclasses.replace(
            planned(), visual_subject_direction="an object-only scene with no invented human hero"
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS)
        self.assertTrue(all(not scene.requires_naz_reference for scene in pack.scenes))
        self.assertTrue(all(scene.reference_role == "none" for scene in pack.scenes))
        self.assertTrue(all(scene.identity_reference_usage == "none" for scene in pack.scenes))
        self.assertTrue(all("Naz" not in scene.concrete_action for scene in pack.scenes))

    def test_video_prompts_animate_directed_keyframes_within_runway_limit(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        for scene in pack.scenes:
            self.assertIn("supplied directed keyframe", scene.provider_prompt)
            self.assertLessEqual(len(scene.provider_prompt.encode("utf-16-le")) // 2, 1000)
            self.assertLessEqual(len(scene.keyframe_prompt.encode("utf-16-le")) // 2, 1000)

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
