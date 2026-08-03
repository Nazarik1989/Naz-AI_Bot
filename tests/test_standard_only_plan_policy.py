import asyncio
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

import character_state
import editorial_orchestrator as eo
import main
import memory
from telegram.error import TelegramError


DATE_TEXT = "2026-07-22"
PROJECT = "Naz_AI_Bot_clean"
SOURCE_REF = f"agent_content:{DATE_TEXT}:synthetic-hash"

AXES = (
    "thesis_direction",
    "epistemic_state",
    "tension",
    "semantic_theme",
    "facet",
    "author_role",
    "emotional_arc",
    "reader_relation",
    "structure",
    "hook",
    "ending",
    "energy",
    "seriousness",
    "tempo",
    "length",
    "humor",
    "imagery",
    "visual_mode",
    "visual_subject_direction",
    "visual_relation",
    "track_tags",
)


def eligible_source_row(source_ref: str = SOURCE_REF) -> dict[str, object]:
    return {
        "source_ref": source_ref,
        "topic": f"synthetic work chronicle {DATE_TEXT}",
        "source_type": "work_chronicle",
        "safe_facts": (
            "The builder tested one bounded input before changing the pipeline.",
            "First the failing handoff was reproduced on a synthetic fixture.",
            "Then one validation rule was added before the next processing step.",
            "The test failed before the change and passed after the same check.",
        ),
        "source_verified": True,
        "concrete_action": True,
        "visualizable_process": True,
        "causal_bits": 4,
        "real_result": True,
        "contains_secrets": False,
        "contains_private_data": False,
    }


def policy_context(policy: str = eo.EditorialPlanPolicy.AUTO) -> eo.EditorialContext:
    pools = {axis: (f"{axis}-value",) for axis in AXES}
    pools["semantic_theme"] = ("theme",)
    pools["visual_subject_direction"] = (
        "one tested device with visible evidence",
    )
    pools["visual_relation"] = (
        "the object visibly carries the consequence named by the thesis",
    )
    pools["track_tags"] = ("daily,focus,builder,reflective",)
    source = eligible_source_row("agent_content:2026-07-22:synthetic")
    source_ref = str(source["source_ref"])
    return eo.EditorialContext(
        persona="naz",
        platform="telegram",
        slot="agent_content_sync",
        seed=source_ref,
        sources=(
            eo.EditorialSource(
                source_ref=source_ref,
                topic=str(source["topic"]),
                source_type="work_chronicle",
                safe_facts=tuple(source["safe_facts"]),
                source_verified=True,
                concrete_action=True,
                visualizable_process=True,
                causal_bits=4,
                real_result=True,
            ),
        ),
        rubrics=(
            eo.EditorialRubric(
                "agent_content",
                "Synthetic chronicle",
                "work_chronicle",
                "turn one verified synthetic episode into a release",
            ),
        ),
        pools=pools,
        semantic_cards={"theme": ("card",)},
        policy_versions={"content": "c1", "visual": "v1", "music": "m1"},
        production_policy=policy,
        source_metadata={
            source_ref: {"project": PROJECT, "date": DATE_TEXT},
        },
    )


def generated_package() -> eo.GenerationPackage:
    return eo.GenerationPackage(
        final_text="x" * 600,
        concrete_scene="A tested device changes state on a synthetic workbench.",
        visual_subject="The same tested device after the bounded check.",
        visual_relation_to_thesis="Its changed state demonstrates the consequence.",
        image_prompt_seed="A close view of the tested device after the check.",
        track_tags=("daily", "focus", "builder", "reflective"),
    )


class PolicyPlanningTests(unittest.TestCase):
    def test_auto_policy_preserves_story_first_selection(self) -> None:
        plan = eo.plan_release(policy_context())
        self.assertEqual(plan.production_mode, "story_first")
        self.assertEqual(plan.production_policy, "auto")
        self.assertEqual(plan.plan_id, "7fcb48340d8225932588dd64")

    def test_standard_only_forces_standard_before_plan_id(self) -> None:
        director = Mock()
        plan = eo.plan_release(policy_context(eo.EditorialPlanPolicy.STANDARD_ONLY))
        self.assertEqual(plan.production_mode, "standard")
        self.assertEqual(plan.content_format, "text_post")
        director.assert_not_called()

    def test_standard_only_has_distinct_deterministic_plan_id(self) -> None:
        auto = eo.plan_release(policy_context())
        first = eo.plan_release(policy_context(eo.EditorialPlanPolicy.STANDARD_ONLY))
        second = eo.plan_release(policy_context(eo.EditorialPlanPolicy.STANDARD_ONLY))
        self.assertNotEqual(auto.plan_id, first.plan_id)
        self.assertEqual(first.plan_id, second.plan_id)

    def test_existing_serialized_plans_without_policy_remain_readable(self) -> None:
        original = eo.plan_release(policy_context())
        payload = original.to_dict()
        payload.pop("production_policy")
        payload.pop("source_project")
        payload.pop("source_date")
        restored = eo.EditorialPlan.from_dict(payload)
        self.assertEqual(restored.production_policy, eo.EditorialPlanPolicy.AUTO)
        self.assertEqual(restored.plan_id, original.plan_id)
        with self.assertRaises(eo.EditorialPlanError):
            eo.plan_release(policy_context("unknown"))

    def test_policy_parser_normalizes_and_reserializes_canonical_value(self) -> None:
        plan = eo.plan_release(policy_context(eo.EditorialPlanPolicy.STANDARD_ONLY))
        payload = plan.to_dict()
        payload["production_policy"] = " STANDARD_ONLY "
        restored = eo.EditorialPlan.from_dict(payload)
        self.assertEqual(
            restored.production_policy,
            eo.EditorialPlanPolicy.STANDARD_ONLY,
        )
        self.assertEqual(
            restored.to_dict()["production_policy"],
            eo.EditorialPlanPolicy.STANDARD_ONLY,
        )
        self.assertEqual(restored.plan_id, plan.plan_id)


class ReadOnlyPlanQueryTests(unittest.TestCase):
    @staticmethod
    def schema_snapshot(db_path: Path) -> list[tuple[object, ...]]:
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
            ).fetchall()
        finally:
            conn.close()

    def test_standard_only_query_never_initializes_or_writes_schema(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            with patch.object(memory, "DB_PATH", str(db_path)):
                memory.init_db()
                plan = eo.plan_release(
                    policy_context(eo.EditorialPlanPolicy.STANDARD_ONLY)
                )
                memory.save_generated_post(
                    user_id=7,
                    expert_mode="balanced",
                    task="agent_content_standard_only",
                    topic=plan.topic,
                    content="synthetic",
                    plan_id=plan.plan_id,
                    editorial_plan=plan.to_dict(),
                )
                before_bytes = db_path.read_bytes()
                before_schema = self.schema_snapshot(db_path)
                statements: list[str] = []
                real_connect = sqlite3.connect

                def traced_connect(*args, **kwargs):
                    conn = real_connect(*args, **kwargs)
                    conn.set_trace_callback(statements.append)
                    return conn

                with patch.object(memory, "init_db") as init, patch.object(
                    memory.sqlite3,
                    "connect",
                    side_effect=traced_connect,
                ):
                    rows = memory.get_standard_only_plan_records(7, plan.plan_id)
                    event = memory.get_editorial_release_event_read_only(
                        7, plan.plan_id, "telegram"
                    )

                self.assertEqual(len(rows), 1)
                self.assertIsNone(event)
                init.assert_not_called()
                forbidden = ("CREATE", "ALTER", "DROP", "REINDEX", "COMMIT")
                self.assertFalse(
                    any(
                        statement.lstrip().upper().startswith(forbidden)
                        for statement in statements
                    ),
                    statements,
                )
                self.assertEqual(db_path.read_bytes(), before_bytes)
                self.assertEqual(self.schema_snapshot(db_path), before_schema)

    def test_missing_database_is_not_created_by_query(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "missing.sqlite3"
            with patch.object(memory, "DB_PATH", str(db_path)), self.assertRaises(
                memory.StandardOnlyPlanQueryError
            ):
                memory.get_standard_only_plan_records(7, "a" * 24)
            self.assertFalse(db_path.exists())


class StandardOnlyRouteTests(unittest.IsolatedAsyncioTestCase):
    def runtime_stack(
        self,
        db_path: Path,
        *,
        generation: AsyncMock | None = None,
        mode: str = "off",
        mock_draft: bool = True,
    ) -> tuple[ExitStack, dict[str, object]]:
        stack = ExitStack()
        stack.enter_context(patch.object(memory, "DB_PATH", str(db_path)))
        memory.init_db()
        generation_mock = generation or AsyncMock(return_value=generated_package())
        draft = (
            stack.enter_context(
                patch.object(
                    main,
                    "send_generated_text_to_chat",
                    new=AsyncMock(),
                )
            )
            if mock_draft
            else None
        )
        director = stack.enter_context(
            patch.object(main, "generate_reels_director_treatment", new=AsyncMock())
        )
        story_pack = stack.enter_context(
            patch.object(main, "queue_story_first_pack")
        )
        binding = stack.enter_context(
            patch.object(main.operator_events, "bind_plan_to_operator_events")
        )
        images = stack.enter_context(
            patch.object(main, "generate_images_with_retries", new=AsyncMock())
        )
        public_send = stack.enter_context(
            patch.object(main, "send_observed_scheduled_post", new=AsyncMock())
        )
        crosspost = stack.enter_context(patch.object(main, "queue_naz_post_for_void"))
        mark_seen = stack.enter_context(patch.object(main, "mark_agent_content_seen"))
        stack.enter_context(
            patch.object(
                main,
                "agent_content_source_dirs_for_date",
                return_value=[Path("synthetic") / PROJECT / DATE_TEXT],
            )
        )
        stack.enter_context(
            patch.object(main, "agent_content_hash_for_date", return_value="synthetic-hash")
        )
        stack.enter_context(patch.object(main, "load_agent_content_seen", return_value={}))
        stack.enter_context(
            patch.object(
                main,
                "collect_agent_materials",
                return_value=("synthetic safe material", [], DATE_TEXT),
            )
        )
        stack.enter_context(
            patch.object(
                main,
                "chronicle_source_row",
                side_effect=lambda **values: eligible_source_row(values["source_ref"]),
            )
        )
        stack.enter_context(
            patch.object(
                memory,
                "load_character_state",
                return_value=character_state.CharacterState(),
            )
        )
        stack.enter_context(patch.object(main, "get_user_expert_mode", return_value="balanced"))
        stack.enter_context(patch.object(main, "NAZ_CHARACTER_REELS_MODE", mode))
        stack.enter_context(
            patch.object(main, "generate_scheduled_package", new=generation_mock)
        )
        return stack, {
            "generation": generation_mock,
            "draft": draft,
            "director": director,
            "story_pack": story_pack,
            "binding": binding,
            "images": images,
            "public_send": public_send,
            "crosspost": crosspost,
            "mark_seen": mark_seen,
        }

    async def run_standard(self, user_id: int = 7, bot: object | None = None) -> str:
        return await main.process_agent_content_date(
            bot or object(),
            user_id,
            DATE_TEXT,
            force=True,
            publish=False,
            production_policy=eo.EditorialPlanPolicy.STANDARD_ONLY,
        )

    def expected_plan(self, user_id: int = 7) -> eo.EditorialPlan:
        return main.scheduled_plan(
            user_id=user_id,
            platform="telegram",
            slot="agent_content_sync",
            seed=SOURCE_REF,
            rubric_rows=(
                {
                    "key": "agent_content",
                    "name": "Рабочая хроника Naz",
                    "kind": "work_chronicle",
                    "angle": "turn a verified work episode into one coherent release without exposing private material",
                    "track_tags": "daily,focus,builder,reflective",
                },
            ),
            source_rows=(eligible_source_row(),),
            character=character_state.CharacterState(),
            production_policy=eo.EditorialPlanPolicy.STANDARD_ONLY,
            source_metadata={
                SOURCE_REF: {"project": PROJECT, "date": DATE_TEXT},
            },
        )

    def counts(self, db_path: Path) -> tuple[int, int, int]:
        conn = sqlite3.connect(db_path)
        try:
            posts = conn.execute("SELECT COUNT(*) FROM generated_posts").fetchone()[0]
            artifacts = conn.execute("SELECT COUNT(*) FROM generated_artifacts").fetchone()[0]
            versions = conn.execute("SELECT COUNT(*) FROM generated_artifact_versions").fetchone()[0]
        finally:
            conn.close()
        return int(posts), int(artifacts), int(versions)

    def persist_plan(
        self,
        plan: eo.EditorialPlan,
        payload: dict[str, object] | None = None,
    ) -> None:
        memory.save_generated_post(
            user_id=7,
            expert_mode="balanced",
            task="agent_content_standard_only",
            topic=plan.topic,
            content="synthetic",
            plan_id=plan.plan_id,
            editorial_plan=payload if payload is not None else plan.to_dict(),
        )

    async def test_standard_only_policy_is_persisted_in_plan_payload(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            stack, _ = self.runtime_stack(db_path)
            with stack:
                self.assertEqual(await self.run_standard(), main.STANDARD_ONLY_PLAN_CREATED)
                plan = memory.get_standard_only_plan_records(7, self.expected_plan().plan_id)[0][
                    "editorial_plan"
                ]
            self.assertEqual(plan["production_policy"], "standard_only")
            self.assertEqual(plan["production_mode"], "standard")
            self.assertEqual(plan["source_ref"], SOURCE_REF)
            self.assertEqual(plan["source_project"], PROJECT)
            self.assertEqual(plan["source_date"], DATE_TEXT)

    async def test_standard_only_route_uses_standard_model_only(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            stack, calls = self.runtime_stack(Path(root) / "naz.sqlite3")
            with stack:
                self.assertEqual(await self.run_standard(), main.STANDARD_ONLY_PLAN_CREATED)
            self.assertEqual(calls["generation"].await_count, 1)
            calls["director"].assert_not_awaited()
            calls["story_pack"].assert_not_called()
            calls["images"].assert_not_awaited()

    async def test_standard_only_route_never_publicly_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            stack, calls = self.runtime_stack(Path(root) / "naz.sqlite3")
            with stack:
                await self.run_standard()
            calls["public_send"].assert_not_awaited()
            calls["crosspost"].assert_not_called()
            self.assertEqual(calls["draft"].await_count, 1)

    async def test_delivery_failure_persists_once_and_reports_one_safe_status(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            stack, calls = self.runtime_stack(db_path, mock_draft=False)
            reply_text = AsyncMock()
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=7),
                message=SimpleNamespace(reply_text=reply_text),
            )
            bot_send = AsyncMock(side_effect=TelegramError("synthetic transport"))
            context = SimpleNamespace(
                args=[DATE_TEXT],
                bot=SimpleNamespace(send_message=bot_send),
            )
            with stack, patch.object(main, "is_admin", return_value=True):
                await main.sync_agent_content_standard_command(update, context)
                plan = self.expected_plan()
                first_counts = self.counts(db_path)
                first_records = memory.get_standard_only_plan_records(
                    7, plan.plan_id
                )
                first_generation = calls["generation"].await_count
                first_draft_sends = bot_send.await_count
                first_status = str(reply_text.await_args.args[0])

                self.assertEqual(first_counts, (1, 1, 1))
                self.assertEqual(len(first_records), 1)
                self.assertEqual(
                    first_records[0]["editorial_plan"],
                    plan.to_dict(),
                )
                self.assertEqual(first_generation, 1)
                self.assertEqual(first_draft_sends, 1)
                self.assertEqual(reply_text.await_count, 1)
                self.assertIn(
                    main.STANDARD_ONLY_PLAN_CREATED_DELIVERY_FAILED,
                    first_status,
                )
                self.assertIn(plan.plan_id, first_status)
                self.assertIn("canonical plan was saved", first_status)

                reply_text.reset_mock()
                await main.sync_agent_content_standard_command(update, context)
                second_counts = self.counts(db_path)
                second_status = str(reply_text.await_args.args[0])

            self.assertEqual(calls["generation"].await_count - first_generation, 0)
            self.assertEqual(bot_send.await_count - first_draft_sends, 0)
            self.assertEqual(second_counts, first_counts)
            self.assertEqual(reply_text.await_count, 1)
            self.assertIn(main.STANDARD_ONLY_PLAN_ALREADY_EXISTS, second_status)
            self.assertIn(plan.plan_id, second_status)
            calls["public_send"].assert_not_awaited()
            calls["mark_seen"].assert_not_called()

    async def test_partial_and_conflict_commands_send_one_status_each(self) -> None:
        for state in ("partial", "conflict"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as root:
                db_path = Path(root) / "naz.sqlite3"
                stack, calls = self.runtime_stack(db_path)
                reply_text = AsyncMock()
                update = SimpleNamespace(
                    effective_user=SimpleNamespace(id=7),
                    message=SimpleNamespace(reply_text=reply_text),
                )
                context = SimpleNamespace(args=[DATE_TEXT], bot=object())
                with stack, patch.object(main, "is_admin", return_value=True):
                    plan = self.expected_plan()
                    if state == "partial":
                        memory.update_editorial_release_event(
                            user_id=7,
                            plan_id=plan.plan_id,
                            platform="telegram",
                            slot=plan.slot,
                            generation_package_status="not_run",
                            history_commit_status="not_run",
                        )
                        expected = main.STANDARD_ONLY_PLAN_PARTIAL_EXISTING
                    else:
                        payload = plan.to_dict()
                        payload["topic"] = "conflicting topic"
                        self.persist_plan(plan, payload)
                        expected = main.STANDARD_ONLY_PLAN_IDENTITY_CONFLICT
                    before = self.counts(db_path)
                    await main.sync_agent_content_standard_command(update, context)
                    after = self.counts(db_path)
                    status = str(reply_text.await_args.args[0])
                self.assertEqual(reply_text.await_count, 1)
                self.assertIn(expected, status)
                self.assertIn(plan.plan_id, status)
                self.assertEqual(after, before)
                calls["generation"].assert_not_awaited()
                calls["draft"].assert_not_awaited()
                calls["mark_seen"].assert_not_called()

    async def test_standard_only_rerun_reuses_existing_plan(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            stack, calls = self.runtime_stack(db_path)
            with stack:
                first = await self.run_standard()
                first_counts = self.counts(db_path)
                second = await self.run_standard()
                second_counts = self.counts(db_path)
            self.assertEqual(first, main.STANDARD_ONLY_PLAN_CREATED)
            self.assertEqual(second, main.STANDARD_ONLY_PLAN_ALREADY_EXISTS)
            self.assertEqual(calls["generation"].await_count, 1)
            self.assertEqual(calls["draft"].await_count, 1)
            self.assertEqual(first_counts, (1, 0, 0))
            self.assertEqual(second_counts, first_counts)
            calls["mark_seen"].assert_not_called()

    async def test_identical_canonical_payload_is_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            stack, calls = self.runtime_stack(db_path)
            with stack:
                plan = self.expected_plan()
                self.persist_plan(plan)
                result = await self.run_standard()
            self.assertEqual(result, main.STANDARD_ONLY_PLAN_ALREADY_EXISTS)
            self.assertEqual(getattr(result, "plan_id", ""), plan.plan_id)
            calls["generation"].assert_not_awaited()
            calls["draft"].assert_not_awaited()

    async def test_any_canonical_payload_difference_is_identity_conflict(self) -> None:
        mutations = {
            "topic": lambda payload: payload.__setitem__("topic", "changed topic"),
            "rubric": lambda payload: payload.__setitem__("rubric", "changed rubric"),
            "visual": lambda payload: payload.__setitem__(
                "visual_subject_direction", ""
            ),
            "nested_track_tags": lambda payload: payload.__setitem__(
                "track_tags", ["different"]
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                db_path = Path(root) / "naz.sqlite3"
                stack, calls = self.runtime_stack(db_path)
                with stack:
                    plan = self.expected_plan()
                    payload = plan.to_dict()
                    mutate(payload)
                    self.persist_plan(plan, payload)
                    result = await self.run_standard()
                    counts = self.counts(db_path)
                self.assertEqual(
                    result,
                    main.STANDARD_ONLY_PLAN_IDENTITY_CONFLICT,
                )
                self.assertEqual(counts, (1, 0, 0))
                calls["generation"].assert_not_awaited()
                calls["draft"].assert_not_awaited()

    async def test_malformed_canonical_payload_is_never_already_exists(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            stack, calls = self.runtime_stack(db_path)
            with stack:
                plan = self.expected_plan()
                self.persist_plan(plan)
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        "UPDATE generated_posts SET editorial_plan_json = ?",
                        ("{malformed",),
                    )
                    conn.commit()
                finally:
                    conn.close()
                result = await self.run_standard()
            self.assertEqual(result, main.STANDARD_ONLY_PLAN_IDENTITY_CONFLICT)
            calls["generation"].assert_not_awaited()
            calls["draft"].assert_not_awaited()

    async def test_multiple_identical_canonical_records_are_identity_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            stack, calls = self.runtime_stack(db_path)
            with stack:
                plan = self.expected_plan()
                self.persist_plan(plan)
                self.persist_plan(plan)
                result = await self.run_standard()
            self.assertEqual(result, main.STANDARD_ONLY_PLAN_IDENTITY_CONFLICT)
            calls["generation"].assert_not_awaited()
            calls["draft"].assert_not_awaited()

    async def test_concurrent_standard_only_attempts_create_one_draft(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            stack, calls = self.runtime_stack(db_path)
            with stack:
                results = await asyncio.gather(
                    self.run_standard(),
                    self.run_standard(),
                )
                persisted_counts = self.counts(db_path)
            self.assertCountEqual(
                results,
                (
                    main.STANDARD_ONLY_PLAN_CREATED,
                    main.STANDARD_ONLY_PLAN_ALREADY_EXISTS,
                ),
            )
            self.assertEqual(persisted_counts, (1, 0, 0))
            calls["generation"].assert_awaited_once()
            calls["draft"].assert_awaited_once()

    async def test_partial_identity_blocks_retry(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            stack, calls = self.runtime_stack(db_path)
            with stack:
                plan = self.expected_plan()
                memory.update_editorial_release_event(
                    user_id=7,
                    plan_id=plan.plan_id,
                    platform="telegram",
                    slot=plan.slot,
                    generation_package_status="not_run",
                    history_commit_status="not_run",
                )
                before = self.counts(db_path)
                result = await self.run_standard()
                after = self.counts(db_path)
            self.assertEqual(result, main.STANDARD_ONLY_PLAN_PARTIAL_EXISTING)
            self.assertEqual(before, after)
            calls["generation"].assert_not_awaited()
            calls["draft"].assert_not_awaited()
            calls["mark_seen"].assert_not_called()

    async def test_conflicting_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            db_path = Path(root) / "naz.sqlite3"
            stack, calls = self.runtime_stack(db_path)
            with stack:
                plan = self.expected_plan()
                conflict = plan.to_dict()
                conflict["production_policy"] = "auto"
                memory.save_generated_post(
                    user_id=7,
                    expert_mode="balanced",
                    task="agent_content_standard_only",
                    topic=plan.topic,
                    content="synthetic",
                    plan_id=plan.plan_id,
                    editorial_plan=conflict,
                )
                before = self.counts(db_path)
                result = await self.run_standard()
                after = self.counts(db_path)
            self.assertEqual(result, main.STANDARD_ONLY_PLAN_IDENTITY_CONFLICT)
            self.assertEqual(before, after)
            calls["generation"].assert_not_awaited()
            calls["draft"].assert_not_awaited()
            calls["mark_seen"].assert_not_called()

    async def test_standard_only_does_not_bind_operator_events(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            stack, calls = self.runtime_stack(
                Path(root) / "naz.sqlite3", mode="shadow"
            )
            with stack:
                await self.run_standard()
            calls["binding"].assert_not_called()

    async def test_generation_failure_leaves_partial_and_blocks_rerun(self) -> None:
        generation = AsyncMock(side_effect=RuntimeError("synthetic timeout"))
        with tempfile.TemporaryDirectory() as root:
            stack, calls = self.runtime_stack(
                Path(root) / "naz.sqlite3", generation=generation
            )
            with stack:
                first = await self.run_standard()
                second = await self.run_standard()
            self.assertEqual(first, "standard_only_plan_generation_unavailable")
            self.assertEqual(second, main.STANDARD_ONLY_PLAN_PARTIAL_EXISTING)
            self.assertEqual(calls["generation"].await_count, 1)
            calls["draft"].assert_not_awaited()
            calls["binding"].assert_not_called()
            calls["public_send"].assert_not_awaited()
            calls["mark_seen"].assert_not_called()
            self.assertEqual(self.counts(Path(root) / "naz.sqlite3"), (0, 0, 0))

    async def test_ambiguous_database_state_blocks_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            stack, calls = self.runtime_stack(Path(root) / "naz.sqlite3")
            with stack, patch.object(
                memory,
                "get_standard_only_plan_records",
                side_effect=memory.StandardOnlyPlanQueryError("synthetic"),
            ):
                result = await self.run_standard()
            self.assertEqual(result, main.STANDARD_ONLY_PLAN_IDENTITY_CONFLICT)
            calls["generation"].assert_not_awaited()
            calls["draft"].assert_not_awaited()
            calls["mark_seen"].assert_not_called()

    async def test_cancellation_releases_lock_and_preserves_partial_identity(self) -> None:
        generation = AsyncMock(side_effect=asyncio.CancelledError())
        with tempfile.TemporaryDirectory() as root:
            stack, calls = self.runtime_stack(
                Path(root) / "naz.sqlite3", generation=generation
            )
            with stack:
                with self.assertRaises(asyncio.CancelledError):
                    await self.run_standard()
                second = await asyncio.wait_for(self.run_standard(), timeout=1)
            self.assertEqual(second, main.STANDARD_ONLY_PLAN_PARTIAL_EXISTING)
            self.assertEqual(calls["generation"].await_count, 1)
            calls["draft"].assert_not_awaited()
            calls["mark_seen"].assert_not_called()

    async def test_admin_only_and_explicit_date_required(self) -> None:
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            message=object(),
        )
        plan_id = "a" * 24
        process = AsyncMock(
            return_value=main.standard_only_plan_result(
                main.STANDARD_ONLY_PLAN_ALREADY_EXISTS,
                plan_id,
            )
        )
        reply = AsyncMock()

        with patch.object(main, "reply_long", new=reply), patch.object(
            main, "process_agent_content_date", new=process
        ), patch.object(main, "is_admin", return_value=False):
            await main.sync_agent_content_standard_command(
                update, SimpleNamespace(args=[DATE_TEXT], bot=object())
            )
        process.assert_not_awaited()
        reply.assert_awaited_once()

        final_reply = AsyncMock()
        with patch.object(main, "reply_long", new=final_reply), patch.object(
            main, "process_agent_content_date", new=process
        ), patch.object(main, "is_admin", return_value=True):
            await main.sync_agent_content_standard_command(
                update, SimpleNamespace(args=[DATE_TEXT], bot=object())
            )
        process.assert_awaited_once_with(
            ANY,
            7,
            DATE_TEXT,
            force=True,
            publish=False,
            production_policy=eo.EditorialPlanPolicy.STANDARD_ONLY,
        )
        final_reply.assert_awaited_once()
        self.assertIn(plan_id, str(final_reply.await_args.args[1]))

    async def test_invalid_command_inputs_stop_before_all_route_work(self) -> None:
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=7),
            message=object(),
        )
        invalid_args = (
            (),
            ("not-a-date",),
            (main.current_bot_date(),),
            ("2999-12-31",),
            ("../2026-07-22",),
            ("C:\\2026-07-22",),
            (f" {DATE_TEXT}",),
            (f"{DATE_TEXT} ",),
            (DATE_TEXT, "extra"),
        )
        for args in invalid_args:
            with self.subTest(args=args):
                process = AsyncMock()
                reply = AsyncMock()
                source_read = Mock()
                db_write = Mock()
                with patch.object(main, "reply_long", new=reply), patch.object(
                    main, "process_agent_content_date", new=process
                ), patch.object(
                    main, "agent_content_source_dirs_for_date", new=source_read
                ), patch.object(
                    memory, "update_editorial_release_event", new=db_write
                ), patch.object(main, "is_admin", return_value=True):
                    await main.sync_agent_content_standard_command(
                        update,
                        SimpleNamespace(args=list(args), bot=object()),
                    )
                process.assert_not_awaited()
                source_read.assert_not_called()
                db_write.assert_not_called()
                reply.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
