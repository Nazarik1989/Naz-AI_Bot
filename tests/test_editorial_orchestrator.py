import asyncio
import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import character_state
import editorial_orchestrator as eo
import main
import memory
import naz_editorial_catalog
import vk_publish_queue


AXES = (
    "thesis_direction", "epistemic_state", "tension", "semantic_theme", "facet",
    "author_role", "emotional_arc", "reader_relation", "structure", "hook",
    "ending", "energy", "seriousness", "tempo", "length", "humor", "imagery",
    "visual_mode", "visual_subject_direction", "visual_relation", "track_tags",
)


def context(*, seed="seed-0", history=(), crosspost_plan_id=""):
    pools = {axis: tuple(f"{axis}-{index}" for index in range(3)) for axis in AXES}
    pools["thesis_direction"] = tuple(f"thesis-{index}" for index in range(17))
    pools["semantic_theme"] = ("theme-a", "theme-b", "theme-c")
    pools["visual_subject_direction"] = (
        "one used tool at a real workbench",
        "one object showing a concrete changed state",
        "one tested device with visible evidence",
    )
    pools["visual_relation"] = (
        "the object visibly carries the consequence named by the thesis",
        "the changed state makes the conclusion physically legible",
        "the action and result are visible in the same scene",
    )
    pools["track_tags"] = ("daily,focus", "daily,warm", "daily,systems")
    return eo.EditorialContext(
        persona="naz",
        platform="telegram",
        slot="10:00",
        seed=seed,
        sources=(
            eo.EditorialSource("catalog:one", "A concrete topic", rubric_keys=("daily",)),
            eo.EditorialSource("catalog:two", "Another concrete topic", rubric_keys=("daily",)),
            eo.EditorialSource("catalog:three", "A third concrete topic", rubric_keys=("daily",)),
        ),
        rubrics=(eo.EditorialRubric("daily", "Daily", "daily", "one useful release"),),
        pools=pools,
        semantic_cards={
            "theme-a": ("a-1", "a-2"),
            "theme-b": ("b-1", "b-2"),
            "theme-c": ("c-1", "c-2"),
        },
        published_history=tuple(history),
        policy_versions={"content": "c1", "visual": "v1", "music": "m1"},
        crosspost_plan_id=crosspost_plan_id,
    )


class EditorialOrchestratorTests(unittest.TestCase):
    def test_contract_and_required_cooldown(self):
        self.assertEqual(eo.cooldown_depth(17), 10)
        plan = eo.plan_release(context())
        self.assertEqual(set(plan.to_dict()), set(eo.EditorialPlan.__dataclass_fields__))
        self.assertEqual(plan.orchestrator_version, "editorial-orchestrator-v1")

    def test_fifty_offline_plans_respect_cooldown_and_compatibility(self):
        history = []
        cards = context().semantic_cards
        for index in range(50):
            plan = eo.plan_release(context(seed=f"simulation-{index}", history=history))
            self.assertNotIn(plan.thesis_direction, [item["thesis_direction"] for item in history[-10:]])
            self.assertIn(plan.semantic_card, cards[plan.semantic_theme])
            self.assertTrue(plan.visual_relation)
            self.assertTrue(plan.track_tags)
            history.append(plan.to_dict())
        self.assertEqual(len(history), 50)

    def test_fifty_plans_from_runtime_catalog_are_compatible(self):
        rubric_rows = []
        source_rows = []
        for rubric in main.NAZ_TELEGRAM_RUBRICS:
            row = dict(rubric)
            key = naz_editorial_catalog.rubric_key(str(row["name"]))
            row["key"] = key
            rubric_rows.append(row)
            for index, topic in enumerate(row.get("topics", ())):
                source_rows.append(
                    {
                        "source_ref": f"simulation:{key}:{index}",
                        "topic": str(topic),
                        "rubric_keys": (key,),
                    }
                )
        history = []
        for index in range(50):
            runtime = naz_editorial_catalog.build_context(
                platform="telegram",
                slot="simulation",
                seed=f"runtime-{index}",
                rubric_rows=rubric_rows,
                source_rows=source_rows,
                published_history=history,
                character=character_state.CharacterState(),
            )
            plan = eo.plan_release(runtime)
            self.assertIn(plan.semantic_card, runtime.semantic_cards[plan.semantic_theme])
            self.assertEqual(plan.production_mode, "standard")
            history.append(plan.to_dict())
        self.assertEqual(len(history), 50)

    def test_diversity_exhaustion_never_drops_slot(self):
        first = eo.plan_release(context(seed="exhaust-1"))
        second = eo.plan_release(context(seed="exhaust-2", history=(first.to_dict(),)))
        third = eo.plan_release(
            context(seed="exhaust-3", history=(first.to_dict(), second.to_dict()))
        )
        self.assertTrue(third.plan_id)

    def test_crosspost_reuses_exact_plan_id(self):
        plan = eo.plan_release(context(crosspost_plan_id="crosspost-plan-0001"))
        self.assertEqual(plan.plan_id, "crosspost-plan-0001")

    def test_prompt_and_visual_are_derived_from_same_plan(self):
        plan = eo.plan_release(context())
        package = eo.GenerationPackage(
            final_text="x" * 500,
            concrete_scene="A tested device changes state on the workbench.",
            visual_subject="The same tested device.",
            visual_relation_to_thesis="Its changed state demonstrates the stated consequence.",
            image_prompt_seed="Close view of the tested device after the action.",
            track_tags=plan.track_tags,
        )
        prompt = eo.generation_prompt(plan, persona_direction="Naz direction")
        visual = eo.package_visual_brief(plan, package)
        self.assertIn(plan.plan_id, prompt)
        self.assertIn(plan.plan_id, visual)
        self.assertIn(package.visual_subject, visual)
        self.assertNotIn("random person", visual.casefold())

    def test_runtime_visual_uses_package_brief_without_a_second_prompt_model(self):
        plan = eo.plan_release(context())
        brief = f"Plan ID: {plan.plan_id}. One exact workbench subject tied to the thesis."
        with patch.object(main, "build_image_prompt", new=AsyncMock()) as legacy_prompt, patch.object(
            main, "generate_image_bytes", new=AsyncMock(return_value=b"image")
        ) as image_model:
            images, prompt = asyncio.run(
                main.generate_images_for_post(
                    1, "planned topic", "planned post", editorial_visual_brief=brief
                )
            )
        legacy_prompt.assert_not_awaited()
        self.assertEqual(images, [b"image"])
        self.assertIn(plan.plan_id, prompt)
        self.assertIn(plan.plan_id, image_model.await_args.args[0])

    def test_diag_and_unrelated_visual_are_rejected(self):
        plan = eo.plan_release(context())
        payload = {
            "final_text": "DIAG: internal exception " + "x" * 500,
            "concrete_scene": "A concrete scene exists here.",
            "visual_subject": "A specific device on the workbench.",
            "visual_relation_to_thesis": "The state proves the consequence.",
            "image_prompt_seed": "A specific workbench and tested device.",
            "track_tags": list(plan.track_tags),
        }
        with self.assertRaises(eo.GenerationPackageError):
            eo.parse_generation_package(json.dumps(payload), plan)

    def test_migrated_routes_do_not_call_legacy_selectors(self):
        forbidden = (
            "select_naz_telegram_rubric(", "select_naz_vk_rubric(",
            "generate_semantic_autopost_candidate(", "plan_content(", "choose_format(",
            "random.choice(",
        )
        for function in (
            main.auto_post_job,
            main.source_monitor_job,
            main.create_naz_vk_job,
            main.process_agent_content_date,
        ):
            source = inspect.getsource(function)
            self.assertIn("scheduled_plan(", source)
            for token in forbidden:
                self.assertNotIn(token, source, f"{function.__name__}: {token}")

    def test_vk_draft_does_not_enter_history_until_confirmed_receipt(self):
        plan = eo.plan_release(context())
        with tempfile.TemporaryDirectory() as root, patch.object(memory, "DB_PATH", str(Path(root) / "naz.sqlite3")):
            memory.init_db()
            memory.save_generated_post(
                user_id=7,
                expert_mode="balanced",
                task="naz_vk_queue:daily:Daily",
                topic=plan.topic,
                content="draft content",
                published_to_channel=False,
                external_job_id="naz-1234567890abcdef12345678",
                plan_id=plan.plan_id,
                editorial_plan=plan.to_dict(),
            )
            self.assertEqual(memory.get_recent_content_signatures(7), [])
            row_id = memory.get_unpublished_vk_jobs(7)[0]["id"]
            self.assertTrue(memory.mark_vk_generated_post_published(7, row_id))
            self.assertEqual(len(memory.get_recent_content_signatures(7)), 1)
            memory.record_content_signature(7, plan.to_dict(), plan.topic)
            self.assertEqual(len(memory.get_recent_content_signatures(7)), 1)

    def test_naz_queue_carries_safe_plan_metadata_and_keeps_legacy_compatible(self):
        with tempfile.TemporaryDirectory() as root:
            queue_root = Path(root)
            (queue_root / "pending").mkdir()
            legacy = vk_publish_queue.enqueue(
                queue_root,
                target_group_id="123",
                text="legacy",
                source_ref="source:legacy",
                track_query="Tycho — Awake",
                dedupe_key="legacy-job",
            )
            planned = vk_publish_queue.enqueue(
                queue_root,
                target_group_id="123",
                text="planned",
                source_ref="source:planned",
                track_query="Tycho — Awake",
                dedupe_key="planned-job",
                plan_id="planned-release-0001",
                editorial={"persona": "naz", "track_tags": ["daily", "focus"]},
            )
            self.assertNotIn("plan_id", legacy)
            self.assertEqual(planned["plan_id"], "planned-release-0001")


class GenerationRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_keeps_plan_id_and_all_axes(self):
        plan = eo.plan_release(context())
        valid = json.dumps(
            {
                "final_text": "x" * 500,
                "concrete_scene": "A tested device changes state on the workbench.",
                "visual_subject": "The same tested device on the workbench.",
                "visual_relation_to_thesis": "Its state demonstrates the planned consequence.",
                "image_prompt_seed": "A close view of the exact tested device after the action.",
                "track_tags": list(plan.track_tags),
            }
        )
        model = AsyncMock(side_effect=["not-json", valid])
        with patch.object(main, "call_gpt", model):
            package = await main.generate_scheduled_package(plan, character_state.CharacterState())
        self.assertEqual(package.track_tags, plan.track_tags)
        self.assertEqual(model.await_count, 2)
        first = model.await_args_list[0].args[0][1]["content"]
        second = model.await_args_list[1].args[0][1]["content"]
        for key, value in plan.to_dict().items():
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
            self.assertIn(rendered, first)
            self.assertIn(rendered, second)


if __name__ == "__main__":
    unittest.main()
