import dataclasses
import hashlib
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
    "The repeated software check completed and changed the observable result.",
    "A regression check confirmed the result on the same input.",
)

JULY_11_PUBLICATION_FACTS = (
    "The approved draft entered the publication review queue.",
    "A duplicate-publication check found the same release identifier.",
    "The scheduled publish action was held before delivery.",
    "The operator kept the existing schedule unchanged.",
    "No publication was created from the rejected duplicate.",
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


def director_response(plan, facts=SAFE_FACTS, *, variant_index=0):
    plan = plan or planned()
    count = max(4, min(7, len(facts)))
    roles = story._roles(
        story._variant_plan_id(plan.plan_id, variant_index),
        count,
    )
    arc_names = story._story_arc_names_for_plan(plan)
    story_arc = (
        "automated_validation_cycle"
        if "automated_validation_cycle" in arc_names
        else arc_names[0]
    )
    arc_steps = story._story_arc_steps(story_arc, count)
    semantic_goals = (
        "Reveal failing build and reproducible configuration mismatch.",
        "Compare configuration with known working release.",
        "Correct bounded setting, then repeat build.",
        "Reveal changed observable result after repeated check.",
        "Confirm regression result on input.",
        "Reveal corrected state with physical check.",
        "Reveal seventh software result completed in causal chain.",
    )
    relations = (
        "opening",
        "Configuration comparison follows reproducible mismatch.",
        "Bounded setting correction follows working release comparison.",
        "Repeated check follows bounded setting correction.",
        "Regression check follows changed observable result.",
        "Physical check follows regression input and corrected state.",
        "Seventh software result follows corrected physical check.",
    )
    understandings = (
        "Viewer understands failing build and reproducible configuration mismatch.",
        "Viewer understands known release and configuration comparison.",
        "Viewer understands bounded setting correction and repeated build.",
        "Viewer understands repeated check and changed observable result.",
        "Viewer understands confirmed regression result on input.",
        "Viewer understands corrected state and physical check.",
        "Viewer understands seventh software result completed in causal chain.",
    )
    visual_relations = (
        "Physical mechanism maps failing build to reproducible configuration mismatch.",
        "Physical mechanism maps working release to configuration comparison.",
        "Physical mechanism maps bounded setting correction to repeated build.",
        "Physical mechanism maps repeated check to changed observable result.",
        "Physical mechanism maps regression check to confirmed result on input.",
        "Physical mechanism maps corrected state to physical check.",
        "Physical mechanism maps seventh software result to completed causal chain.",
    )
    scenes = []
    for index, role in enumerate(roles, start=1):
        motion_class = story.DIRECTOR_ACTION_RECIPES[arc_steps[index - 1][0]][1]
        scenes.append({
            "beat_id": f"beat-{index:02d}-{role}",
            "semantic_goal": semantic_goals[index - 1],
            "source_fact_refs": [f"fact-{index}"],
            "relation_to_previous": relations[index - 1],
            "expected_viewer_understanding": understandings[index - 1],
            "visualization_kind": "physical_metaphor",
            "visual_relation_to_beat": (
                f"The physical {motion_class} action maps this beat: "
                f"{visual_relations[index - 1]}"
            ),
            "shot_size": story.SHOT_SIZES[(index - 1) % len(story.SHOT_SIZES)],
            "camera_motion": story.CAMERA_MOTIONS[(index - 1) % len(story.CAMERA_MOTIONS)],
        })
    return json.dumps({
        "director_version": story.DIRECTOR_VERSION,
        "core_thesis": (
            "Configuration mismatch: bounded correction, repeated build, "
            "regression confirmation."
        ),
        "thesis_source_fact_refs": ["fact-1", "fact-3", "fact-5"],
        "viewer_problem": "The reproducible mismatch is exposed by a failing build.",
        "hook": "Reveal failing build and reproducible mismatch.",
        "hook_thesis_ref": "core_thesis",
        "payoff": "Show regression confirmation after bounded correction.",
        "payoff_thesis_ref": "core_thesis",
        "visual_concept": (
            "Physical mechanism maps configuration mismatch to regression confirmation."
        ),
        "story_spine": "Failing build, bounded correction, repeated regression check.",
        "story_arc": story_arc,
        "scenes": scenes,
    })


def select_story_arc(payload, story_arc):
    payload["story_arc"] = story_arc
    arc_steps = story._story_arc_steps(story_arc, len(payload["scenes"]))
    for scene, (recipe_name, _) in zip(payload["scenes"], arc_steps):
        relation_prefix, separator, relation_body = scene[
            "visual_relation_to_beat"
        ].partition(": ")
        if not separator:
            raise AssertionError(
                f"director fixture relation has no marker prefix: {relation_prefix}"
            )
        motion_class = story.DIRECTOR_ACTION_RECIPES[recipe_name][1]
        scene["visual_relation_to_beat"] = (
            f"The physical {motion_class} action maps this beat: {relation_body}"
        )
    return payload


def directed_pack(plan=None, facts=SAFE_FACTS, *, variant_index=0):
    plan = plan or planned()
    treatment = story.parse_reels_director_response(
        director_response(plan, facts, variant_index=variant_index),
        plan,
        facts,
        variant_index=variant_index,
    )
    return story.plan_story_pack(
        plan,
        tuple(facts),
        variant_index=variant_index,
        director_treatment=treatment,
    )


class StoryFirstTests(unittest.TestCase):
    def test_reference_detection_uses_words_not_face_substrings(self):
        self.assertFalse(story._requires_reference("one specific work surface at the result"))
        self.assertTrue(story._requires_reference("the canonical Naz in the laboratory"))
        self.assertTrue(story._requires_reference("a close portrait of the founder's face"))

    def test_director_response_format_requires_exact_scene_count_and_strict_json(self):
        response_format = story.reels_director_response_format(SAFE_FACTS, planned())
        self.assertEqual(response_format["type"], "json_schema")
        contract = response_format["json_schema"]
        self.assertTrue(contract["strict"])
        root_schema = contract["schema"]
        self.assertFalse(root_schema["additionalProperties"])
        self.assertEqual(set(root_schema["required"]), set(root_schema["properties"]))
        scenes = root_schema["properties"]["scenes"]
        self.assertEqual(scenes["minItems"], len(SAFE_FACTS))
        self.assertEqual(scenes["maxItems"], len(SAFE_FACTS))
        self.assertFalse(scenes["items"]["additionalProperties"])
        properties = scenes["items"]["properties"]
        self.assertEqual(set(scenes["items"]["required"]), set(properties))
        self.assertNotIn("role", properties)
        self.assertTrue({
            "beat_id",
            "semantic_goal",
            "source_fact_refs",
            "relation_to_previous",
            "expected_viewer_understanding",
            "visualization_kind",
            "visual_relation_to_beat",
        }.issubset(properties))
        self.assertEqual(
            properties["visualization_kind"]["enum"],
            ["literal", "physical_metaphor"],
        )
        self.assertNotIn("admin_summary_ru", properties)
        self.assertNotIn("admin_concept_ru", contract["schema"]["properties"])
        self.assertTrue({
            "core_thesis",
            "thesis_source_fact_refs",
            "viewer_problem",
            "hook",
            "hook_thesis_ref",
            "payoff",
            "payoff_thesis_ref",
        }.issubset(contract["schema"]["properties"]))
        self.assertEqual(
            contract["schema"]["properties"]["hook_thesis_ref"]["enum"],
            ["core_thesis"],
        )
        self.assertEqual(
            contract["schema"]["properties"]["payoff_thesis_ref"]["enum"],
            ["core_thesis"],
        )
        self.assertIn("story_spine", contract["schema"]["properties"])
        self.assertNotIn("continuity_anchor", contract["schema"]["properties"])
        self.assertIn("story_arc", contract["schema"]["properties"])
        self.assertNotIn("primary_setting", contract["schema"]["properties"])
        self.assertNotIn("initial_state", contract["schema"]["properties"])
        self.assertNotIn("goal_state", contract["schema"]["properties"])
        self.assertNotIn("setting", properties)
        self.assertEqual(
            contract["schema"]["properties"]["story_spine"]["maxLength"], 180
        )
        self.assertNotIn("concrete_action", properties)
        self.assertNotIn("subject_detail", properties)
        self.assertNotIn("action_object", properties)
        self.assertNotIn("subject_kind", properties)
        self.assertNotIn("motion_class", properties)
        self.assertNotIn("action_recipe", properties)
        self.assertNotIn("brand_marking", properties)
        self.assertNotIn("start_state", properties)
        self.assertNotIn("end_state", properties)
        self.assertEqual(
            contract["schema"]["properties"]["story_arc"]["enum"],
            list(story._story_arc_names_for_plan(planned())),
        )
        self.assertEqual(
            contract["schema"]["properties"]["visual_concept"]["maxLength"],
            1200,
        )

    def test_story_arc_selection_controls_identity_without_free_form_subjects(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        select_story_arc(payload, "module_recovery_mixed")
        treatment = story.parse_reels_director_response(
            json.dumps(payload), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS, director_treatment=treatment)

        self.assertTrue(any(scene.requires_naz_reference for scene in pack.scenes))
        self.assertTrue(any(not scene.requires_naz_reference for scene in pack.scenes))
        self.assertTrue(all(
            story._is_naz_human_subject(scene.subject)
            for scene in pack.scenes
            if scene.requires_naz_reference
        ))

    def test_explicit_identity_directions_bound_the_story_arc_enum(self):
        human_plan = dataclasses.replace(
            planned(), visual_subject_direction="the canonical Naz in the laboratory"
        )
        object_plan = dataclasses.replace(
            planned(), visual_subject_direction="an object-only scene with no person"
        )
        human_arcs = story._story_arc_names_for_plan(human_plan)
        object_arcs = story._story_arc_names_for_plan(object_plan)

        self.assertTrue(all(
            story.DIRECTOR_STORY_ARCS[name]["subject_mode"] == "human"
            for name in human_arcs
        ))
        self.assertTrue(all(
            story.DIRECTOR_STORY_ARCS[name]["subject_mode"] == "object"
            for name in object_arcs
        ))
        schema = story.reels_director_response_format(SAFE_FACTS, object_plan)
        self.assertEqual(
            schema["json_schema"]["schema"]["properties"]["story_arc"]["enum"],
            list(object_arcs),
        )

        payload = json.loads(director_response(object_plan))
        payload["story_arc"] = "module_recovery_human"
        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), object_plan, SAFE_FACTS
            )
        self.assertIn("director_story_arc_invalid", raised.exception.reason_codes)

    def test_model_cannot_supply_actions_props_settings_or_states(self):
        for field, value in (
            ("action_recipe", "naz_presses_mechanical_button"),
            ("concrete_action", "Naz presses a laptop button"),
            ("end_state", "powered"),
            ("start_state", "inactive"),
            ("brand_marking", "naz_ai_lab"),
            ("primary_setting", "server_aisle"),
        ):
            with self.subTest(field=field):
                payload = json.loads(director_response(planned()))
                if field == "primary_setting":
                    payload[field] = value
                    expected = "director_schema_invalid"
                else:
                    payload["scenes"][0][field] = value
                    expected = "director_scene_1_schema_invalid"
                with self.assertRaises(story.DirectorValidationError) as raised:
                    story.parse_reels_director_response(
                        json.dumps(payload), planned(), SAFE_FACTS
                    )
                self.assertIn(expected, raised.exception.reason_codes)

    def test_every_arc_expands_to_distinct_pre_vetted_actions_and_states(self):
        for arc_name in story.DIRECTOR_STORY_ARC_NAMES:
            for count in range(4, 8):
                with self.subTest(arc=arc_name, count=count):
                    steps = story._story_arc_steps(arc_name, count)
                    self.assertEqual(len(steps), count)
                    self.assertEqual(len({item[0] for item in steps}), count)
                    self.assertEqual(len({item[1] for item in steps}), count)
                    for recipe_name, end_state in steps:
                        recipe = story.DIRECTOR_ACTION_RECIPES[recipe_name]
                        action = story._build_atomic_action(
                            action_recipe=recipe_name, brand_marking="none"
                        )
                        self.assertTrue(action)
                        self.assertIn(end_state, story.DIRECTOR_STATE_CODES)
                        self.assertFalse(story._motion_contract_reason_codes(
                            subject_kind=recipe[0],
                            motion_class=recipe[1],
                            action=action,
                            start_state="before",
                            end_state="after",
                        ))

    def test_every_story_arc_has_semantic_axis_metadata(self):
        self.assertEqual(
            set(story.DIRECTOR_ARC_SEMANTIC_AXES),
            set(story.DIRECTOR_STORY_ARCS),
        )
        self.assertTrue(all(story.DIRECTOR_ARC_SEMANTIC_AXES.values()))

    def test_story_arc_supplies_one_location_anchor_and_causal_state_chain(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        select_story_arc(payload, "module_recovery_mixed")
        treatment = story.parse_reels_director_response(
            json.dumps(payload), plan, SAFE_FACTS
        )
        arc = story.DIRECTOR_STORY_ARCS[treatment.story_arc]
        expected_steps = story._story_arc_steps(treatment.story_arc, len(SAFE_FACTS))

        self.assertEqual(
            {scene.setting for scene in treatment.scenes},
            {story.DIRECTOR_PRIMARY_SETTINGS[arc["setting"]]},
        )
        self.assertEqual(treatment.continuity_anchor, arc["continuity_anchor"])
        self.assertEqual(treatment.initial_state_code, arc["initial_state"])
        self.assertEqual(treatment.goal_state_code, expected_steps[-1][1])
        self.assertEqual(treatment.admin_concept_ru, arc["description_ru"])
        self.assertEqual(
            [scene.admin_summary_ru for scene in treatment.scenes],
            [
                story.DIRECTOR_RECIPE_SUMMARIES_RU[recipe]
                for recipe, _ in expected_steps
            ],
        )
        for previous, current in zip(treatment.scenes, treatment.scenes[1:]):
            self.assertEqual(current.start_state, previous.end_state)

    def test_object_arc_injects_visible_mechanical_drive_into_provider_prompts(self):
        plan = planned()
        treatment = story.parse_reels_director_response(
            director_response(plan), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS, director_treatment=treatment)

        self.assertTrue(all(not scene.requires_naz_reference for scene in pack.scenes))
        self.assertTrue(all(
            "visible mechanical actuator" in scene.provider_prompt
            and "no self-animation" in scene.provider_prompt
            for scene in pack.scenes
        ))

    def test_new_manifest_routes_mixed_arc_naz_to_gen45_and_objects_to_turbo(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        select_story_arc(payload, "module_recovery_mixed")
        treatment = story.parse_reels_director_response(
            json.dumps(payload), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS, director_treatment=treatment)
        with tempfile.TemporaryDirectory() as root:
            manifest = story.persist_story_queue(pack, Path(root)) / "story_manifest.json"
            persisted = story.read_manifest(manifest)

        for scene, job in zip(persisted["scenes"], persisted["scene_jobs"]):
            self.assertEqual(
                job["model_route"]["selected_model"],
                "gen4.5" if scene["requires_naz_reference"] else "gen4_turbo",
            )

    def test_abstract_director_motion_classes_are_not_available(self):
        for motion_class in ("adjust", "calibrate", "test", "walk"):
            self.assertNotIn(motion_class, story.DIRECTOR_MOTION_CLASSES)

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
        self.assertEqual(pack.central_thesis, treatment.core_thesis)
        self.assertEqual(
            pack.thesis_source_fact_refs,
            treatment.thesis_source_fact_refs,
        )
        self.assertEqual(pack.viewer_problem, treatment.viewer_problem)
        self.assertEqual(pack.hook, treatment.hook)
        self.assertEqual(pack.payoff, treatment.payoff)
        self.assertEqual(pack.visual_concept, treatment.visual_concept)
        self.assertEqual(pack.story_spine, treatment.story_spine)
        self.assertEqual(pack.story_arc, treatment.story_arc)
        self.assertEqual(pack.continuity_anchor, treatment.continuity_anchor)
        self.assertEqual(pack.initial_state_code, treatment.initial_state_code)
        self.assertEqual(pack.goal_state_code, treatment.goal_state_code)
        self.assertEqual(pack.admin_concept_ru, treatment.admin_concept_ru)
        self.assertEqual(
            [scene.concrete_action for scene in pack.scenes],
            [scene.concrete_action for scene in treatment.scenes],
        )
        self.assertEqual(
            [scene.motion_class for scene in pack.scenes],
            [scene.motion_class for scene in treatment.scenes],
        )
        self.assertEqual(
            [scene.admin_summary_ru for scene in pack.scenes],
            [scene.admin_summary_ru for scene in treatment.scenes],
        )
        for scene_index, scene in enumerate(pack.scenes):
            directed = treatment.scenes[scene_index]
            self.assertEqual(scene.beat_id, directed.beat_id)
            self.assertEqual(scene.semantic_goal, directed.semantic_goal)
            self.assertEqual(scene.source_fact_refs, directed.source_fact_refs)
            self.assertEqual(
                scene.relation_to_previous,
                directed.relation_to_previous,
            )
            self.assertNotIn(scene.admin_summary_ru, scene.clean_prompt)
            self.assertNotIn(scene.admin_summary_ru, scene.keyframe_prompt)
            self.assertNotIn(scene.admin_summary_ru, scene.provider_prompt)
            self.assertIn(scene.semantic_goal, scene.clean_prompt)
            self.assertNotIn(pack.story_spine, scene.provider_prompt)
            self.assertIn(pack.continuity_anchor, scene.clean_prompt)
            self.assertIn(pack.continuity_anchor, scene.keyframe_prompt)
            self.assertIn(pack.continuity_anchor, scene.provider_prompt)
            self.assertIn(scene.semantic_goal, scene.clean_prompt)
            self.assertIn(scene.semantic_goal, scene.keyframe_prompt)
            self.assertIn(scene.semantic_goal, scene.provider_prompt)
            self.assertNotIn(scene.visual_relation_to_beat, scene.clean_prompt)
            self.assertNotIn(scene.visual_relation_to_beat, scene.keyframe_prompt)
            self.assertNotIn(scene.visual_relation_to_beat, scene.provider_prompt)
            for source_fact in SAFE_FACTS:
                self.assertNotIn(source_fact, scene.clean_prompt)
                self.assertNotIn(source_fact, scene.keyframe_prompt)
                self.assertNotIn(source_fact, scene.provider_prompt)
            self.assertTrue(scene.provider_prompt.startswith("Continuous seamless five-second shot"))
        for previous, current in zip(pack.scenes, pack.scenes[1:]):
            self.assertEqual(current.start_state, previous.end_state)
        self.assertTrue(all("tied to fact" not in scene.setting for scene in pack.scenes))
        self.assertTrue(all("fact 1" not in scene.concrete_action for scene in pack.scenes))

        tampered_scene = dataclasses.replace(
            pack.scenes[0],
            provider_prompt=pack.scenes[0].provider_prompt + " unrelated addition",
        )
        tampered = dataclasses.replace(
            pack,
            scenes=(tampered_scene, *pack.scenes[1:]),
        )
        with self.assertRaises(story.StoryPlanError):
            story.validate_story_pack(tampered)

    def test_grounded_core_thesis_replaces_editorial_axis_in_story_pack(self):
        plan = planned()
        treatment = story.parse_reels_director_response(
            director_response(plan), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(
            plan, SAFE_FACTS, director_treatment=treatment
        )

        self.assertNotEqual(treatment.core_thesis, plan.thesis_direction)
        self.assertEqual(pack.central_thesis, treatment.core_thesis)
        self.assertEqual(
            pack.thesis_source_fact_refs,
            treatment.thesis_source_fact_refs,
        )
        self.assertEqual(pack.viewer_problem, treatment.viewer_problem)
        self.assertEqual(pack.hook, treatment.hook)
        self.assertEqual(pack.payoff, treatment.payoff)

    def test_core_thesis_must_be_grounded_in_existing_source_refs(self):
        plan = planned()
        cases = {
            "missing_refs": (
                lambda payload: payload.__setitem__("thesis_source_fact_refs", []),
                "director_thesis_source_fact_refs_invalid",
            ),
            "unknown_ref": (
                lambda payload: payload.__setitem__(
                    "thesis_source_fact_refs", ["fact-99"]
                ),
                "director_thesis_source_fact_refs_invalid",
            ),
            "unsupported_claim": (
                lambda payload: payload.__setitem__(
                    "core_thesis",
                    "A spacecraft launch proved orbital tourism.",
                ),
                "director_core_thesis_unsupported",
            ),
        }
        for name, (mutate, reason_code) in cases.items():
            payload = json.loads(director_response(plan))
            mutate(payload)
            with self.subTest(name=name), self.assertRaises(
                story.DirectorValidationError
            ) as raised:
                story.parse_reels_director_response(
                    json.dumps(payload), plan, SAFE_FACTS
                )
            self.assertIn(reason_code, raised.exception.reason_codes)

    def test_hook_and_payoff_must_reference_the_same_core_thesis(self):
        plan = planned()
        for field in ("hook_thesis_ref", "payoff_thesis_ref"):
            payload = json.loads(director_response(plan))
            payload[field] = "another_thesis"
            with self.subTest(field=field), self.assertRaises(
                story.DirectorValidationError
            ) as raised:
                story.parse_reels_director_response(
                    json.dumps(payload), plan, SAFE_FACTS
                )
            self.assertIn(
                "director_hook_payoff_mismatch",
                raised.exception.reason_codes,
            )

    def test_every_scene_has_the_expected_beat_and_grounded_semantic_fields(self):
        plan = planned()
        treatment = story.parse_reels_director_response(
            director_response(plan), plan, SAFE_FACTS
        )
        expected_roles = story._roles(
            story._variant_plan_id(plan.plan_id, 0), len(SAFE_FACTS)
        )

        self.assertEqual(
            [scene.beat_id for scene in treatment.scenes],
            [
                f"beat-{index:02d}-{role}"
                for index, role in enumerate(expected_roles, start=1)
            ],
        )
        self.assertTrue(all(scene.semantic_goal for scene in treatment.scenes))
        self.assertTrue(all(scene.source_fact_refs for scene in treatment.scenes))
        self.assertEqual(treatment.scenes[0].relation_to_previous, "opening")
        self.assertTrue(all(
            scene.relation_to_previous
            for scene in treatment.scenes[1:]
        ))

    def test_scene_semantic_contract_fails_closed_field_by_field(self):
        plan = planned()
        cases = {
            "wrong_beat": (
                lambda scene: scene.__setitem__("beat_id", "beat-99-result"),
                "director_scene_1_beat_id_invalid",
            ),
            "unknown_fact": (
                lambda scene: scene.__setitem__("source_fact_refs", ["fact-99"]),
                "director_scene_1_source_fact_refs_invalid",
            ),
            "unsupported_goal": (
                lambda scene: scene.__setitem__(
                    "semantic_goal", "Celebrate an unrelated victory."
                ),
                "director_scene_1_semantic_goal_unsupported",
            ),
            "missing_transition": (
                lambda scene: scene.__setitem__("relation_to_previous", ""),
                "director_scene_2_relation_to_previous_missing",
            ),
            "dubious_visual_relation": (
                lambda scene: scene.__setitem__(
                    "visual_relation_to_beat", "A beautiful futuristic mechanism."
                ),
                "director_scene_1_visual_relation_unsupported",
            ),
        }
        for name, (mutate, reason_code) in cases.items():
            payload = json.loads(director_response(plan))
            scene_index = 1 if name == "missing_transition" else 0
            mutate(payload["scenes"][scene_index])
            with self.subTest(name=name), self.assertRaises(
                story.DirectorValidationError
            ) as raised:
                story.parse_reels_director_response(
                    json.dumps(payload), plan, SAFE_FACTS
                )
            self.assertIn(reason_code, raised.exception.reason_codes)

    def test_duplicate_semantic_goals_are_rejected(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][1]["semantic_goal"] = payload["scenes"][0][
            "semantic_goal"
        ]

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_semantic_goal_duplicate",
            raised.exception.reason_codes,
        )

    def test_unknown_numeric_claim_is_rejected(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][2]["semantic_goal"] += " It increased throughput by 99 percent."

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_unknown_numeric_claim",
            raised.exception.reason_codes,
        )

    def test_number_cannot_migrate_from_an_unrelated_fact(self):
        facts = (
            "A failing build exposed 99 reproducible configuration mismatches.",
            *SAFE_FACTS[1:],
        )
        plan = planned(source(safe_facts=facts))
        payload = json.loads(director_response(plan, facts))
        payload["scenes"][1]["semantic_goal"] = (
            "Compare 99 configuration with known working release."
        )

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, facts
            )

        self.assertIn(
            "director_unknown_numeric_claim",
            raised.exception.reason_codes,
        )

    def test_hook_and_payoff_must_share_thesis_content_not_connectors(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["core_thesis"] = "Build configuration causal chain."
        payload["hook"] = "Configuration mismatch causal hook."
        payload["payoff"] = "Regression check causal payoff."

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_hook_payoff_mismatch",
            raised.exception.reason_codes,
        )

    def test_transition_must_name_previous_and_current_content(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][1]["relation_to_previous"] = (
            "Known working release follows semantic transition."
        )

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_scene_2_relation_to_previous_missing",
            raised.exception.reason_codes,
        )

    def test_unknown_claim_cannot_hide_behind_source_token_overlap(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][0]["semantic_goal"] = (
            "Reveal configuration mismatch deleted customer database."
        )

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_scene_1_semantic_goal_unsupported",
            raised.exception.reason_codes,
        )

    def test_partial_and_short_raw_source_copy_is_rejected(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][0]["semantic_goal"] = (
            "A failing build exposed one reproducible"
        )

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn("director_raw_source_copy", raised.exception.reason_codes)
        self.assertTrue(
            story._raw_source_fragment_in_text("Build failed.", ("Build failed.",))
        )

    def test_scene_relation_cannot_add_a_second_physical_action(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        motion = story.DIRECTOR_ACTION_RECIPES[
            story._story_arc_steps(payload["story_arc"], len(SAFE_FACTS))[0][0]
        ][1]
        other_motion = "open" if motion != "open" else "close"
        payload["scenes"][0]["visual_relation_to_beat"] = (
            f"Physical {motion} action and then {other_motion} action map "
            "failing build to reproducible configuration mismatch."
        )

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_scene_1_multiple_actions", raised.exception.reason_codes
        )
        self.assertEqual(
            story._visual_relation_motion_classes(
                "Physical open action maps titanium door opens and closes."
            ),
            frozenset({"open", "close"}),
        )

    def test_unbounded_relation_verb_never_reaches_render_prompts(self):
        facts = (
            "A failing software build falls after one reproducible configuration mismatch.",
            *SAFE_FACTS[1:],
        )
        plan = planned(source(safe_facts=facts))
        payload = json.loads(director_response(plan, facts))
        payload["viewer_problem"] = "Reveal failing build and reproducible mismatch."
        motion = story.DIRECTOR_ACTION_RECIPES[
            story._story_arc_steps(payload["story_arc"], len(facts))[0][0]
        ][1]
        payload["scenes"][0]["visual_relation_to_beat"] = (
            f"Physical {motion} action falls and maps failing build to "
            "reproducible configuration mismatch."
        )
        treatment = story.parse_reels_director_response(
            json.dumps(payload), plan, facts
        )
        pack = story.plan_story_pack(
            plan, facts, director_treatment=treatment
        )

        for prompt in (
            pack.scenes[0].clean_prompt,
            pack.scenes[0].keyframe_prompt,
            pack.scenes[0].provider_prompt,
        ):
            self.assertNotIn("falls", prompt.casefold())
            self.assertIn(pack.scenes[0].concrete_action, prompt)
        self.assertIn("one physical action", pack.scenes[0].clean_prompt.casefold())
        self.assertIn("one physical action", pack.scenes[0].provider_prompt.casefold())

    def test_scene_must_cite_its_assigned_beat_fact(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][0]["source_fact_refs"] = ["fact-2"]

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_scene_1_source_fact_refs_invalid",
            raised.exception.reason_codes,
        )

    def test_extra_reference_cannot_replace_assigned_fact_grounding(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][0]["source_fact_refs"] = ["fact-1", "fact-2"]
        payload["scenes"][0]["semantic_goal"] = (
            "Compare configuration with known working release."
        )

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_scene_1_semantic_goal_unsupported",
            raised.exception.reason_codes,
        )

    def test_treatment_is_bound_to_the_exact_plan_variant(self):
        plan = planned()
        treatment = story.parse_reels_director_response(
            director_response(plan), plan, SAFE_FACTS
        )

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.plan_story_pack(
                plan,
                SAFE_FACTS,
                variant_index=1,
                director_treatment=treatment,
            )

        self.assertIn(
            "director_treatment_plan_binding_invalid",
            raised.exception.reason_codes,
        )

    def test_software_module_terms_cannot_authorize_literal_hardware(self):
        self.assertEqual(
            story._source_semantic_axis((
                "A Python module import failed during the software build.",
            )),
            "software_validation",
        )
        self.assertEqual(
            story._source_semantic_axis((
                "The .NET assembly build failed regression tests.",
            )),
            "software_validation",
        )
        self.assertEqual(
            story._source_semantic_axis(("A module changed position.",)),
            "unknown",
        )
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][0]["visualization_kind"] = "literal"

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_scene_1_literal_source_mismatch",
            raised.exception.reason_codes,
        )

    def test_physical_metaphor_requires_an_explicit_grounded_relation(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["scenes"][0]["visualization_kind"] = "physical_metaphor"
        payload["scenes"][0]["visual_relation_to_beat"] = ""

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_scene_1_visual_relation_to_beat_missing",
            raised.exception.reason_codes,
        )

    def test_unrelated_july_11_module_recovery_is_rejected_before_persistence(self):
        plan = planned(source(
            source_ref="agent_content:2026-07-11:synthetic-fixture",
            safe_facts=JULY_11_PUBLICATION_FACTS,
        ))
        payload = json.loads(director_response(plan))
        payload.update({
            "core_thesis": (
                "Duplicate publication: scheduled action held; existing schedule unchanged."
            ),
            "thesis_source_fact_refs": ["fact-2", "fact-3", "fact-4"],
            "viewer_problem": "Reveal duplicate publication before delivery.",
            "hook": "Reveal duplicate release identifier.",
            "payoff": "Show existing schedule unchanged.",
            "visual_concept": (
                "Physical mechanism maps duplicate publication to unchanged schedule."
            ),
            "story_spine": (
                "Duplicate publication, held publish action, unchanged schedule."
            ),
            "story_arc": "module_recovery_mixed",
        })
        goals = (
            "Reveal approved draft in publication review queue.",
            "Reveal duplicate release identifier in publication check.",
            "Show publish action held before scheduled delivery.",
            "Show unchanged schedule kept by operator.",
            "Reveal rejected duplicate and no created publication.",
        )
        transitions = (
            "opening",
            "Publication review transitions to duplicate release identifier.",
            "Publish action follows duplicate publication check.",
            "Unchanged schedule follows held publish action.",
            "Rejected duplicate follows unchanged schedule.",
        )
        for index, scene in enumerate(payload["scenes"]):
            scene["semantic_goal"] = goals[index]
            scene["source_fact_refs"] = [f"fact-{index + 1}"]
            scene["relation_to_previous"] = transitions[index]
            scene["expected_viewer_understanding"] = goals[index]
            scene["visualization_kind"] = "physical_metaphor"
            scene["visual_relation_to_beat"] = (
                f"Physical mechanism metaphor maps this semantic beat: {goals[index]}"
            )

        with tempfile.TemporaryDirectory() as root:
            pack_dir = Path(root) / "would-be-pack"
            with self.assertRaises(story.DirectorValidationError) as raised:
                story.parse_reels_director_response(
                    json.dumps(payload), plan, JULY_11_PUBLICATION_FACTS
                )
            self.assertIn(
                "director_story_arc_semantic_mismatch",
                raised.exception.reason_codes,
            )
            self.assertFalse(pack_dir.exists())

    def test_unknown_or_free_form_story_arc_is_rejected(self):
        for value in (
            "cover_closes_then_system_fills",
            "setting tied to fact 2",
            "random cyberpunk server room",
        ):
            with self.subTest(value=value):
                payload = json.loads(director_response(planned()))
                payload["story_arc"] = value
                with self.assertRaises(story.DirectorValidationError) as raised:
                    story.parse_reels_director_response(
                        json.dumps(payload), planned(), SAFE_FACTS
                    )
                self.assertIn(
                    "director_story_arc_invalid", raised.exception.reason_codes
                )

    def test_arc_contract_makes_absurd_action_state_pair_unrepresentable(self):
        schema = story.reels_director_response_format(SAFE_FACTS, planned())
        scene_properties = schema["json_schema"]["schema"]["properties"][
            "scenes"
        ]["items"]["properties"]
        self.assertNotIn("action_recipe", scene_properties)
        self.assertNotIn("end_state", scene_properties)
        self.assertNotIn("setting", scene_properties)
        self.assertNotIn("subject", scene_properties)

        payload = json.loads(director_response(planned()))
        payload["scenes"][0]["end_state"] = "filled"
        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), planned(), SAFE_FACTS
            )
        self.assertIn(
            "director_scene_1_schema_invalid", raised.exception.reason_codes
        )

    def test_2026_07_08_route_rejects_generic_visual_concept(self):
        plan = planned(source(source_ref="agent_content:2026-07-08:fixture"))
        payload = json.loads(director_response(plan))
        payload["visual_concept"] = "Lab"

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn(
            "director_visual_concept_generic",
            raised.exception.reason_codes,
        )

    def test_2026_07_08_route_accepts_detailed_visual_concept(self):
        plan = planned(source(source_ref="agent_content:2026-07-08:fixture"))
        payload = json.loads(director_response(plan))
        detailed_concept = " ".join(
            ["configuration mismatch maps to physical mechanism"] * 8
        )
        self.assertGreater(len(detailed_concept), 240)
        payload["visual_concept"] = detailed_concept

        treatment = story.parse_reels_director_response(
            json.dumps(payload), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(
            plan, SAFE_FACTS, director_treatment=treatment
        )

        self.assertEqual(treatment.visual_concept, detailed_concept)
        self.assertEqual(pack.visual_concept, detailed_concept)

    def test_director_rejects_unbounded_visual_concept(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["visual_concept"] = "x" * 1201

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )
        self.assertIn(
            "director_visual_concept_too_long",
            raised.exception.reason_codes,
        )

    def test_admin_display_fields_are_derived_and_cannot_be_model_supplied(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["admin_concept_ru"] = "English display concept"
        payload["scenes"][0]["admin_summary_ru"] = "English display scene"

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertIn("director_schema_invalid", raised.exception.reason_codes)
        self.assertIn(
            "director_scene_1_schema_invalid", raised.exception.reason_codes
        )

    def test_director_reports_all_typed_contract_errors_in_one_pass(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["visual_concept"] = "flowing code"
        payload["story_arc"] = "unknown_person_watches_screen"
        payload["scenes"][1]["concrete_action"] = "click a dashboard"

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.parse_reels_director_response(
                json.dumps(payload), plan, SAFE_FACTS
            )

        self.assertEqual(str(raised.exception), "director_contract_invalid")
        self.assertIn("director_visual_concept_cliche", raised.exception.reason_codes)
        self.assertIn("director_story_arc_invalid", raised.exception.reason_codes)
        self.assertIn("director_scene_2_schema_invalid", raised.exception.reason_codes)

    def test_semantic_director_rejects_malformed_json_without_template_fallback(self):
        with self.assertRaisesRegex(story.StoryPlanError, "director_json_invalid"):
            story.parse_reels_director_response("not-json", planned(), SAFE_FACTS)

    def test_director_prompt_is_content_bound_and_contains_no_source_transport_fields(self):
        prompt = story.reels_director_prompt(planned(), SAFE_FACTS)
        self.assertIn(SAFE_FACTS[0], prompt)
        self.assertIn(story.DIRECTOR_VERSION, prompt)
        self.assertIn("exact Russian admin summaries", prompt)
        self.assertIn("one concise story_spine", prompt)
        self.assertIn("supplies the unresolved initial state", prompt)
        self.assertIn("one continuity anchor", prompt)
        self.assertIn("viewer with sound off", prompt)
        self.assertIn("Choose exactly one story_arc", prompt)
        self.assertIn("Build one causal chain, not separate illustrations.", prompt)
        self.assertIn("Do not write concrete_action", prompt)
        self.assertIn("available_story_arcs", prompt)
        self.assertNotIn("brand_marking=naz_ai_lab", prompt)
        self.assertIn("visible mechanical drive", prompt)
        self.assertNotIn("not separate Define primary_setting", prompt)
        self.assertNotIn("complete micro-film. illustrations", prompt)
        self.assertIn("Never invent armour", prompt)
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

    def test_claimed_causality_cannot_override_too_few_director_facts(self):
        plan = planned(source(causal_bits=7, safe_facts=SAFE_FACTS[:3]))
        self.assertEqual(plan.production_mode, "standard")

    def test_secret_or_private_material_never_selects_story_first(self):
        self.assertEqual(planned(source(contains_secrets=True)).production_mode, "standard")
        self.assertEqual(planned(source(contains_private_data=True)).production_mode, "standard")

    def test_one_short_episode_stays_a_standard_post(self):
        plan = planned(source(causal_bits=1, safe_facts=SAFE_FACTS[:1], real_result=False))
        self.assertEqual(plan.production_mode, "standard")

    def test_full_experiment_has_clean_story_and_causal_reel_contracts(self):
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
            scene_order = {scene.scene_id: index for index, scene in enumerate(pack.scenes)}
            positions = [scene_order[str(shot["source_scene_id"])] for shot in edit.shots]
            self.assertEqual(positions, sorted(positions))
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
            self.assertIn("fitted matte-black technical overshirt", scene.keyframe_prompt)
            self.assertLessEqual(
                len(scene.keyframe_prompt.encode("utf-16-le")) // 2,
                1000,
            )
            self.assertNotIn("tied to fact", scene.setting)

    def test_story_plan_id_is_schema_scoped_and_cannot_reuse_an_old_manifest(self):
        plan = planned()
        pack = story.plan_story_pack(plan, SAFE_FACTS)
        old_v5_id = hashlib.sha256(
            (
                f"{plan.plan_id}|naz-story-pack-v5|reels-semantic-director-v3|"
                "story-variant|0"
            ).encode("utf-8")
        ).hexdigest()[:24]
        self.assertNotEqual(pack.plan_id, plan.plan_id)
        self.assertNotEqual(pack.plan_id, old_v5_id)
        self.assertEqual(pack.base_plan_id, plan.plan_id)
        self.assertEqual(len(pack.plan_id), 24)

    def test_all_legacy_story_schemas_remain_readable(self):
        with tempfile.TemporaryDirectory() as root:
            manifest = Path(root) / "story_manifest.json"
            for schema in story.SUPPORTED_STORY_SCHEMAS[1:]:
                with self.subTest(schema=schema):
                    payload = {"schema": schema, "plan_id": "legacy"}
                    manifest.write_text(
                        json.dumps(payload),
                        encoding="utf-8",
                    )
                    self.assertEqual(story.read_manifest(manifest)["schema"], schema)
                    self.assertFalse(
                        story.manifest_has_current_production_contract(payload)
                    )

    def test_template_treatment_is_dry_run_readable_but_never_production_renderable(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        self.assertEqual(pack.director_version, story.TEMPLATE_DIRECTOR_VERSION)

        with tempfile.TemporaryDirectory() as dry_root:
            manifest = (
                story.persist_dry_run(pack, Path(dry_root))
                / "story_manifest.json"
            )
            payload = story.read_manifest(manifest)
            self.assertEqual(
                payload["director_version"],
                story.TEMPLATE_DIRECTOR_VERSION,
            )
            self.assertFalse(
                story.manifest_has_current_production_contract(payload)
            )

        with tempfile.TemporaryDirectory() as production_root:
            with self.assertRaisesRegex(
                story.StoryPlanError,
                "template_treatment_production_forbidden",
            ):
                story.persist_story_queue(pack, Path(production_root))
            self.assertEqual(list(Path(production_root).iterdir()), [])

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

            pack = directed_pack(plan)
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
            self.assertIn("One physical action:", scene.provider_prompt)
            self.assertNotIn("Role:", scene.provider_prompt)
            self.assertNotIn("story spine", scene.provider_prompt.casefold())
            self.assertLessEqual(len(scene.provider_prompt.encode("utf-16-le")) // 2, 1000)
            self.assertLessEqual(len(scene.keyframe_prompt.encode("utf-16-le")) // 2, 1000)

    def test_maximum_story_spine_and_derived_anchor_fit_provider_prompts(self):
        plan = planned()
        payload = json.loads(director_response(plan))
        payload["story_spine"] = (
            "configuration " * 12 + "build result"
        ).strip()
        self.assertEqual(len(payload["story_spine"]), 180)
        treatment = story.parse_reels_director_response(
            json.dumps(payload), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS, director_treatment=treatment)

        for scene in pack.scenes:
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

    def test_reel_edit_preserves_the_directors_causal_scene_order(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        scene_order = {scene.scene_id: index for index, scene in enumerate(pack.scenes)}
        for edit in pack.reel_edits:
            positions = [scene_order[str(item["source_scene_id"])] for item in edit.shots]
            self.assertEqual(positions[0], 0)
            self.assertEqual(positions[-1], len(pack.scenes) - 1)
            self.assertEqual(positions, sorted(positions))

    def test_reel_edit_rejects_a_backwards_causal_jump(self):
        pack = story.plan_story_pack(planned(), SAFE_FACTS)
        edit = pack.reel_edits[0]
        shots = [dict(item) for item in edit.shots]
        shots[1], shots[4] = shots[4], shots[1]
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

    def test_legacy_dry_run_remains_readable_without_becoming_renderable(self):
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
            self.assertFalse(
                story.manifest_has_current_production_contract(upgraded)
            )
            self.assertEqual(
                upgraded["expected_outputs"]["stories"][0]["status"],
                "generated",
            )
            self.assertEqual(list(pack_dir.rglob("*.mp4")), [])

    def test_public_current_contract_rejects_stale_v2_shapes(self):
        pack = directed_pack()
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

        changed_thesis = json.loads(json.dumps(current))
        changed_thesis["central_thesis"] += " changed"
        self.assertFalse(story.manifest_has_current_production_contract(changed_thesis))

        for field in ("central_thesis", "hook"):
            wrong_type = json.loads(json.dumps(current))
            wrong_type[field] = [wrong_type[field]]
            wrong_type["immutable_plan_fingerprint"] = (
                story._immutable_plan_fingerprint(wrong_type)
            )
            with self.subTest(wrong_top_level_type=field):
                self.assertFalse(
                    story.manifest_has_current_production_contract(wrong_type)
                )

        wrong_scene_type = json.loads(json.dumps(current))
        wrong_scene_type["scenes"][0]["semantic_goal"] = [
            wrong_scene_type["scenes"][0]["semantic_goal"]
        ]
        wrong_scene_type["immutable_plan_fingerprint"] = (
            story._immutable_plan_fingerprint(wrong_scene_type)
        )
        self.assertFalse(
            story.manifest_has_current_production_contract(wrong_scene_type)
        )

        runtime_music = json.loads(json.dumps(current))
        runtime_music["music_plan"]["selected_track"] = {"track_id": "allowlisted-1"}
        runtime_music["music_plan"]["selected_tracks"] = [
            runtime_music["music_plan"]["selected_track"]
        ]
        self.assertTrue(story.manifest_has_current_production_contract(runtime_music))

        legacy_route = json.loads(json.dumps(current))
        legacy_route["model_policy"]["scene_route_policy"] = None
        for job in legacy_route["scene_jobs"]:
            job["model_route"]["scene_strategy"] = None
            job["model_route"]["selected_model"] = None
        self.assertFalse(story.manifest_has_current_production_contract(legacy_route))

        stale_director = json.loads(json.dumps(current))
        stale_director["director_version"] = "reels-semantic-director-v4"
        stale_director["immutable_plan_fingerprint"] = (
            story._immutable_plan_fingerprint(stale_director)
        )
        self.assertFalse(
            story.manifest_has_current_production_contract(stale_director)
        )

        stale_motion_contract = json.loads(json.dumps(current))
        stale_motion_contract["visual_strategy"]["motion_contract_version"] = (
            "single-physical-motion-v1"
        )
        self.assertFalse(
            story.manifest_has_current_production_contract(stale_motion_contract)
        )

    def test_seven_scene_directed_pack_has_current_production_contract(self):
        seven_facts = SAFE_FACTS + (
            "A sixth physical check preserved the corrected state.",
            "A seventh observable software result completed the same causal chain.",
        )
        plan = planned(source(safe_facts=seven_facts, causal_bits=7))
        treatment = story.parse_reels_director_response(
            director_response(plan, seven_facts),
            plan,
            seven_facts,
        )
        pack = story.plan_story_pack(
            plan,
            seven_facts,
            director_treatment=treatment,
        )

        with tempfile.TemporaryDirectory() as root:
            manifest = (
                story.persist_story_queue(pack, Path(root))
                / "story_manifest.json"
            )
            payload = story.read_manifest(manifest)

        self.assertEqual(len(payload["scenes"]), 7)
        self.assertTrue(all(
            scene["motion_class"] in story.DIRECTOR_SELF_MOTION_CLASSES
            for scene in payload["scenes"]
        ))
        self.assertEqual(
            [scene["motion_class"] for scene in payload["scenes"]],
            [scene.motion_class for scene in treatment.scenes],
        )
        self.assertTrue(story.manifest_has_current_production_contract(payload))
        scene_ids = [scene["scene_id"] for scene in payload["scenes"]]
        self.assertTrue(all(
            [shot["source_scene_id"] for shot in edit["shots"]] == scene_ids
            for edit in payload["reel_edits"]
        ))

        backward = json.loads(json.dumps(payload))
        backward["reel_edits"][0]["shots"][3]["source_scene_id"] = scene_ids[0]
        backward["immutable_plan_fingerprint"] = story._immutable_plan_fingerprint(backward)
        self.assertFalse(story.manifest_has_current_production_contract(backward))

        skipped = json.loads(json.dumps(payload))
        skipped["reel_edits"][0]["shots"][2]["source_scene_id"] = scene_ids[1]
        skipped["immutable_plan_fingerprint"] = story._immutable_plan_fingerprint(skipped)
        self.assertFalse(story.manifest_has_current_production_contract(skipped))

    def test_persisted_director_motion_contract_rejects_missing_or_mismatched_fields(self):
        plan = planned()
        treatment = story.parse_reels_director_response(
            director_response(plan), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS, director_treatment=treatment)
        with tempfile.TemporaryDirectory() as root:
            manifest = story.persist_story_queue(pack, Path(root)) / "story_manifest.json"
            current = story.read_manifest(manifest)

        for mutation in ("missing", "mismatch", "extra_motion"):
            tampered = json.loads(json.dumps(current))
            if mutation == "missing":
                tampered["scenes"][0].pop("motion_class")
            elif mutation == "mismatch":
                tampered["scenes"][0]["motion_class"] = "slide"
            else:
                tampered["scenes"][0]["concrete_action"] += " and rotates the housing"
            tampered["immutable_plan_fingerprint"] = story._immutable_plan_fingerprint(tampered)
            with self.subTest(mutation=mutation):
                self.assertFalse(story.manifest_has_current_production_contract(tampered))

    def test_persisted_director_state_contract_rejects_tampering(self):
        plan = planned()
        treatment = story.parse_reels_director_response(
            director_response(plan), plan, SAFE_FACTS
        )
        pack = story.plan_story_pack(plan, SAFE_FACTS, director_treatment=treatment)
        with tempfile.TemporaryDirectory() as root:
            manifest = story.persist_story_queue(pack, Path(root)) / "story_manifest.json"
            current = story.read_manifest(manifest)

        mutations = {
            "free_form_state": lambda payload: payload["scenes"][0].__setitem__(
                "end_state", "the browser refreshes and a robot explodes"
            ),
            "broken_handoff": lambda payload: payload["scenes"][1].__setitem__(
                "start_state", payload["scenes"][0]["start_state"]
            ),
            "early_goal": lambda payload: payload["scenes"][1].__setitem__(
                "end_state",
                story._bounded_state_phrase(
                    payload["continuity_anchor"], payload["goal_state_code"]
                ),
            ),
            "unknown_goal": lambda payload: payload.__setitem__(
                "goal_state_code", "browser_refreshed"
            ),
        }
        for name, mutate in mutations.items():
            tampered = json.loads(json.dumps(current))
            mutate(tampered)
            tampered["immutable_plan_fingerprint"] = story._immutable_plan_fingerprint(
                tampered
            )
            with self.subTest(name=name):
                self.assertFalse(
                    story.manifest_has_current_production_contract(tampered)
                )

    def test_hand_built_director_treatment_cannot_bypass_motion_contract(self):
        plan = planned()
        treatment = story.parse_reels_director_response(
            director_response(plan), plan, SAFE_FACTS
        )
        broken_scene = dataclasses.replace(treatment.scenes[0], motion_class="slide")
        broken = dataclasses.replace(
            treatment,
            scenes=(broken_scene, *treatment.scenes[1:]),
        )

        with self.assertRaises(story.DirectorValidationError) as raised:
            story.plan_story_pack(plan, SAFE_FACTS, director_treatment=broken)
        self.assertIn(
            "director_story_arc_semantic_mismatch",
            raised.exception.reason_codes,
        )

    def test_queue_collision_with_changed_immutable_plan_fails_closed(self):
        pack = directed_pack()
        changed = dataclasses.replace(
            pack,
            caption_plan={**pack.caption_plan, "main": pack.caption_plan["main"] + "!"},
        )
        with tempfile.TemporaryDirectory() as root:
            manifest = story.persist_story_queue(pack, Path(root)) / "story_manifest.json"
            before = manifest.read_bytes()
            with self.assertRaisesRegex(
                story.StoryPlanError,
                "stored_manifest_contract_mismatch",
            ):
                story.persist_story_queue(changed, Path(root))
            self.assertEqual(manifest.read_bytes(), before)

    def test_queue_preflight_rejects_invalid_pack_before_creating_directory(self):
        pack = directed_pack()
        invalid = dataclasses.replace(pack, reel_edits=())

        with tempfile.TemporaryDirectory() as root:
            pack_dir = Path(root) / pack.plan_id
            with self.assertRaisesRegex(
                story.StoryPlanError,
                "story_manifest_contract_stale",
            ):
                story.persist_story_queue(invalid, Path(root))

            self.assertFalse(pack_dir.exists())

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
