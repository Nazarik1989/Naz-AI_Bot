import ast
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import editorial_orchestrator as eo
import main
import memory
import naz_editorial_catalog
from tests.test_editorial_orchestrator import context


class CooldownRepairTests(unittest.TestCase):
    def test_cooldown_uses_rounded_persona_pool_size(self):
        expected = {3: 2, 6: 4, 8: 5, 13: 8, 17: 10, 36: 22}
        self.assertEqual(
            {size: eo.cooldown_depth(size) for size in expected},
            expected,
        )

    def test_constrained_exhaustion_selects_least_recently_used(self):
        history = (
            {"axis": "allowed-oldest"},
            {"axis": "allowed-newest"},
            {"axis": "outside-one"},
            {"axis": "outside-two"},
        )
        selected = eo._choose(
            plan_id="deterministic-plan",
            axis="axis",
            values=("allowed-newest", "allowed-oldest"),
            history=history,
            persona_pool_size=6,
        )
        self.assertEqual(selected, "allowed-oldest")

    def test_plan_uses_persona_pool_not_rubric_subset_for_axis_depth(self):
        base = context()
        constrained = dataclasses.replace(
            base.rubrics[0],
            constraints={"thesis_direction": ("thesis-0", "thesis-1")},
        )
        history = (
            {"thesis_direction": "thesis-0"},
            {"thesis_direction": "thesis-1"},
            {"thesis_direction": "thesis-8"},
            {"thesis_direction": "thesis-9"},
            {"thesis_direction": "thesis-10"},
            {"thesis_direction": "thesis-11"},
            {"thesis_direction": "thesis-12"},
            {"thesis_direction": "thesis-13"},
            {"thesis_direction": "thesis-14"},
            {"thesis_direction": "thesis-15"},
        )
        plan = eo.plan_release(
            dataclasses.replace(base, rubrics=(constrained,), published_history=history)
        )
        self.assertEqual(plan.thesis_direction, "thesis-0")

    def test_catalog_carries_full_rubric_and_source_sizes_into_constrained_plan(self):
        eligible_rubric = {
            "key": "eligible",
            "name": "Eligible slot rubric",
            "kind": "daily",
        }
        complete_rubrics = tuple(
            {
                "key": f"rubric-{index}",
                "name": f"Persona rubric {index}",
                "kind": "daily",
            }
            for index in range(6)
        ) + (eligible_rubric,)
        eligible_source = {
            "source_ref": "eligible-source",
            "topic": "A bounded eligible topic",
            "rubric_keys": ("eligible",),
        }
        complete_sources = tuple(
            {
                "source_ref": f"persona-source-{index}",
                "topic": f"Persona source {index}",
            }
            for index in range(16)
        ) + (eligible_source,)
        runtime = naz_editorial_catalog.build_context(
            platform="telegram",
            slot="constrained-slot",
            seed="complete-catalog",
            rubric_rows=(eligible_rubric,),
            source_rows=(eligible_source,),
            published_history=(),
            character=main.naz_character.CharacterState(),
            persona_rubric_rows=complete_rubrics,
            persona_source_rows=complete_sources,
        )
        with patch.object(eo, "_choose", wraps=eo._choose) as choose:
            eo.plan_release(runtime)
        sizes = {
            call.kwargs["axis"]: call.kwargs["persona_pool_size"]
            for call in choose.call_args_list
        }
        self.assertEqual(runtime.persona_pool_sizes["rubric"], 7)
        self.assertEqual(runtime.persona_pool_sizes["source_ref"], 17)
        self.assertEqual(sizes["rubric"], 7)
        self.assertEqual(sizes["source_ref"], 17)


class ReceiptSyncRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "naz.sqlite3"
        self.queue = self.root / "queue"
        (self.queue / "published").mkdir(parents=True)
        self.db_patch = patch.object(memory, "DB_PATH", str(self.db_path))
        self.db_patch.start()
        memory.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def _plan(self):
        return dataclasses.replace(
            eo.plan_release(context(seed="receipt-plan")),
            plan_id="29bfc68c87f3754ae60043a2",
            platform="vk",
            slot="10:30",
            source_ref="systemd:2026-07-22:daily:10:30",
        )

    def _draft_and_receipt(self, user_id=77):
        plan = self._plan()
        job_id = "naz-" + "a" * 24
        memory.save_generated_post(
            user_id=user_id,
            expert_mode="balanced",
            task="naz_vk_queue:daily:Daily",
            topic=plan.topic,
            content="bounded generated post",
            published_to_channel=False,
            external_job_id=job_id,
            plan_id=plan.plan_id,
            editorial_plan=plan.to_dict(),
        )
        receipt = {
            "schema": "vk_publication_receipt.v1",
            "job_id": job_id,
            "producer": "naz",
            "source_ref": plan.source_ref,
            "published_at": "2026-07-22T07:32:18Z",
        }
        (self.queue / "published" / f"{job_id}.json").write_text(
            json.dumps(receipt), encoding="utf-8"
        )
        return plan, job_id

    def test_valid_receipt_syncs_existing_plan_exactly_once(self):
        plan, job_id = self._draft_and_receipt()
        with patch.multiple(main, NAZ_VK_QUEUE_DIR=self.queue, ADMIN_ID=77):
            first = main.sync_completed_naz_vk_jobs()
            second = main.sync_completed_naz_vk_jobs()
        self.assertEqual(
            (first.receipts_seen, first.history_inserted, first.already_recorded, first.invalid_receipts),
            (1, 1, 0, 0),
        )
        self.assertEqual(
            (second.receipts_seen, second.history_inserted, second.already_recorded, second.invalid_receipts),
            (1, 0, 1, 0),
        )
        history = memory.get_recent_content_signatures(77)
        self.assertEqual([item["plan_id"] for item in history], [plan.plan_id])
        event = memory.get_editorial_release_event(77, plan.plan_id, "vk")
        self.assertEqual(event["vk_job_id"], job_id)
        self.assertEqual(event["vk_receipt_id"], job_id)
        self.assertEqual(event["history_commit_status"], "committed")

    def test_legacy_published_flag_repairs_missing_history_once(self):
        plan, job_id = self._draft_and_receipt()
        with memory.db() as conn:
            conn.execute(
                "UPDATE generated_posts SET published_to_channel=1 "
                "WHERE user_id=? AND external_job_id=?",
                (77, job_id),
            )
        with patch.multiple(main, NAZ_VK_QUEUE_DIR=self.queue, ADMIN_ID=77):
            first = main.sync_completed_naz_vk_jobs()
        self.assertEqual(first.history_inserted, 1)
        self.assertEqual(
            [item["plan_id"] for item in memory.get_recent_content_signatures(77)],
            [plan.plan_id],
        )

        # Simulate normal bounded-history pruning. The durable commit event
        # must stop an old receipt from reviving that cooldown entry.
        with memory.db() as conn:
            conn.execute(
                "DELETE FROM content_signatures WHERE user_id=? AND plan_id=?",
                (77, plan.plan_id),
            )
        with patch.multiple(main, NAZ_VK_QUEUE_DIR=self.queue, ADMIN_ID=77):
            second = main.sync_completed_naz_vk_jobs()
        self.assertEqual(second.history_inserted, 0)
        self.assertEqual(second.already_recorded, 1)
        self.assertEqual(memory.get_recent_content_signatures(77), [])

    def test_done_directory_without_receipt_never_spends_history(self):
        plan = self._plan()
        job_id = "naz-" + "b" * 24
        memory.save_generated_post(
            user_id=77,
            expert_mode="balanced",
            task="naz_vk_queue:daily:Daily",
            topic=plan.topic,
            content="draft",
            published_to_channel=False,
            external_job_id=job_id,
            plan_id=plan.plan_id,
            editorial_plan=plan.to_dict(),
        )
        (self.queue / "done" / job_id).mkdir(parents=True)
        with patch.multiple(main, NAZ_VK_QUEUE_DIR=self.queue, ADMIN_ID=77):
            result = main.sync_completed_naz_vk_jobs()
        self.assertEqual(result.receipts_seen, 0)
        self.assertEqual(memory.get_recent_content_signatures(77), [])

    def test_malformed_and_unknown_receipts_are_counted_invalid(self):
        (self.queue / "published" / "bad.json").write_text("{}", encoding="utf-8")
        unknown_id = "naz-" + "c" * 24
        (self.queue / "published" / f"{unknown_id}.json").write_text(
            json.dumps(
                {
                    "schema": "vk_publication_receipt.v1",
                    "job_id": unknown_id,
                    "producer": "naz",
                    "source_ref": "unknown:source",
                    "published_at": "2026-07-22T07:32:18Z",
                }
            ),
            encoding="utf-8",
        )
        with patch.multiple(main, NAZ_VK_QUEUE_DIR=self.queue, ADMIN_ID=77):
            result = main.sync_completed_naz_vk_jobs()
        self.assertEqual(result.receipts_seen, 1)
        self.assertEqual(result.invalid_receipts, 2)

    def test_same_plan_already_recorded_by_telegram_is_not_spent_twice(self):
        plan, job_id = self._draft_and_receipt()
        telegram_plan = dataclasses.replace(plan, platform="telegram")
        memory.record_content_signature(77, telegram_plan.to_dict(), plan.topic)
        memory.update_editorial_release_event(
            user_id=77,
            plan_id=plan.plan_id,
            platform="telegram",
            slot="10:00",
            generation_package_status="accepted",
            image_qa_status="not_run",
            telegram_chat_id="-10077",
            telegram_message_id="1234",
            history_commit_status="committed",
        )
        with patch.multiple(main, NAZ_VK_QUEUE_DIR=self.queue, ADMIN_ID=77):
            result = main.sync_completed_naz_vk_jobs()
        self.assertEqual(result.history_inserted, 0)
        self.assertEqual(result.already_recorded, 1)
        self.assertEqual(len(memory.get_recent_content_signatures(77)), 1)
        telegram_event = memory.get_editorial_release_event(
            77, plan.plan_id, "telegram"
        )
        vk_event = memory.get_editorial_release_event(77, plan.plan_id, "vk")
        self.assertEqual(telegram_event["telegram_message_id"], "1234")
        self.assertEqual(telegram_event["history_commit_status"], "committed")
        self.assertEqual(vk_event["vk_job_id"], job_id)
        self.assertEqual(vk_event["vk_receipt_id"], job_id)
        self.assertEqual(vk_event["history_commit_status"], "committed")

    def test_periodic_sync_registration_is_independent_of_producer_schedule(self):
        queue = SimpleNamespace(calls=[])

        def run_repeating(callback, **kwargs):
            queue.calls.append((callback, kwargs))

        queue.run_repeating = run_repeating
        app = SimpleNamespace(job_queue=queue)
        with patch.multiple(
            main,
            NAZ_VK_ENABLED=True,
            NAZ_VK_SCHEDULER="systemd",
            NAZ_VK_RECEIPT_SYNC_INTERVAL_SECONDS=300,
        ):
            main.setup_naz_vk_receipt_sync(app)
        self.assertEqual(len(queue.calls), 1)
        self.assertIs(queue.calls[0][0], main.naz_vk_receipt_sync_job)
        self.assertEqual(queue.calls[0][1]["interval"], 300)


class ScheduledCallGraphTests(unittest.TestCase):
    def test_all_scheduled_roots_are_transitively_isolated_from_legacy_gates(self):
        tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
        definitions = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def reachable(root):
            seen = set()
            pending = [root]
            while pending:
                name = pending.pop()
                if name in seen or name not in definitions:
                    continue
                seen.add(name)
                for call in ast.walk(definitions[name]):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                        pending.append(call.func.id)
            return seen

        roots = (
            "auto_post_job",
            "source_monitor_job",
            "naz_vk_queue_job",
            "agent_content_sync_job",
            "crosspost_exchange_job",
        )
        forbidden = {
            "generate_semantic_autopost_candidate",
            "select_naz_telegram_rubric",
            "select_naz_vk_rubric",
            "plan_content",
            "choose_format",
        }
        for root in roots:
            graph = reachable(root)
            self.assertIn("scheduled_plan", graph, root)
            self.assertTrue(forbidden.isdisjoint(graph), (root, forbidden & graph))
            self.assertNotIn("void_v14", graph)
            decorators = definitions[root].decorator_list
            self.assertTrue(
                any(
                    isinstance(item, ast.Call)
                    and isinstance(item.func, ast.Name)
                    and item.func.id == "scheduled_work_marker"
                    for item in decorators
                ),
                f"{root} has no scheduled-work marker",
            )


class CrosspostRepairTests(unittest.TestCase):
    def test_private_grounding_is_closed_vocabulary_and_pii_is_rejected(self):
        private_source = (
            "Alice described trust during a medical appointment and left contact data: "
            "alice@example.invalid, +7 (999) 000-00-00."
        )
        grounding = main._bounded_crosspost_source(
            {"topic": "Alice and a private medical detail"}, private_source
        )
        folded = grounding.casefold()
        for forbidden in ("alice", "example.invalid", "+7", "medical appointment"):
            self.assertNotIn(forbidden, folded)
        self.assertIn("trust", folded)
        self.assertTrue(main.detect_content_risks(private_source))
        self.assertTrue(
            main._crosspost_publication_privacy_risks(
                "A generated draft containing contact@example.invalid"
            )
        )

    def test_private_pii_payload_never_reaches_generation_or_send(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            exchange = base / "exchange"
            inbox = exchange / "void_to_naz" / "inbox"
            inbox.mkdir(parents=True)
            (inbox / "private.json").write_text(
                json.dumps(
                    {
                        "source": "void_entity",
                        "topic": "private conversation",
                        "text": (
                            "A private work observation with contact details "
                            "person@example.invalid and +7 (999) 000-00-00."
                        ),
                        "publish_mode": "auto",
                    }
                ),
                encoding="utf-8",
            )
            generate = AsyncMock()
            send = AsyncMock()
            with patch.object(memory, "DB_PATH", str(base / "naz.sqlite3")), patch.multiple(
                main,
                ADMIN_ID=77,
                CHANNEL_ID="@channel",
                CROSSPOST_EXCHANGE_ENABLED=True,
                CROSSPOST_EXCHANGE_DIR=exchange,
                NAZ_SCHEDULED_WORK_DIR=base / "markers",
            ), patch.object(
                main, "generate_scheduled_package", new=generate
            ), patch.object(main, "send_post_with_images", new=send):
                memory.init_db()
                import asyncio

                asyncio.run(
                    main.crosspost_exchange_job(SimpleNamespace(bot=SimpleNamespace()))
                )
            generate.assert_not_awaited()
            send.assert_not_awaited()
            self.assertEqual(memory.get_recent_content_signatures(77), [])
            self.assertTrue(
                (exchange / "void_to_naz" / "failed" / "private.json").is_file()
            )

    def test_scheduled_draft_uses_generation_package_and_spends_no_history(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            db_path = base / "naz.sqlite3"
            exchange = base / "exchange"
            inbox = exchange / "void_to_naz" / "inbox"
            inbox.mkdir(parents=True)
            source_text = (
                "A concrete private observation about work, consequence and a changed result. "
                * 30
            ).strip()
            payload_path = inbox / "one.json"
            payload_path.write_text(
                json.dumps(
                    {
                        "source": "void_entity",
                        "topic": "A bounded conversation topic",
                        "text": source_text,
                        "publish_mode": "draft",
                    }
                ),
                encoding="utf-8",
            )
            package = eo.GenerationPackage(
                final_text="A distinct Naz reflection with a concrete scene and a materially different conclusion. " * 8,
                concrete_scene="A used tool is placed beside the result it produced.",
                visual_subject="The used tool and the visible result.",
                visual_relation_to_thesis="The changed state proves the consequence.",
                image_prompt_seed="A close view of one used tool beside its result.",
                track_tags=("daily", "reflective"),
            )
            with patch.object(memory, "DB_PATH", str(db_path)), patch.multiple(
                main,
                ADMIN_ID=77,
                CHANNEL_ID="@channel",
                CROSSPOST_EXCHANGE_ENABLED=True,
                CROSSPOST_EXCHANGE_AUTO_PUBLISH=False,
                CROSSPOST_EXCHANGE_DIR=exchange,
                NAZ_SCHEDULED_WORK_DIR=base / "markers",
            ), patch.object(
                main, "generate_scheduled_package", new=AsyncMock(return_value=package)
            ) as generate_package, patch.object(
                main, "generate_semantic_autopost_candidate", new=AsyncMock()
            ) as old_gate, patch.object(
                main, "generate_void_crosspost", new=AsyncMock()
            ) as old_generator, patch.object(
                main.duo_relationship,
                "reflection_is_original",
                return_value=(True, ""),
            ), patch.object(main, "notify_admin", new=AsyncMock()):
                memory.init_db()
                import asyncio

                asyncio.run(
                    main.crosspost_exchange_job(
                        SimpleNamespace(bot=SimpleNamespace())
                    )
                )
                history = memory.get_recent_content_signatures(77)
                drafts = memory.get_recent_generated_posts(
                    77, task="exchange_void_to_naz_draft", limit=5
                )
            old_gate.assert_not_awaited()
            old_generator.assert_not_awaited()
            self.assertEqual(history, [])
            self.assertEqual(len(drafts), 1)
            self.assertEqual(drafts[0]["content"], package.final_text)
            grounding = generate_package.await_args.kwargs["source_material"]
            self.assertNotEqual(grounding, source_text)
            self.assertLess(len(grounding), len(source_text))
            self.assertTrue((exchange / "void_to_naz" / "processed" / "one.json").is_file())

    def test_duplicate_auto_payload_plan_is_sent_once_and_spends_history_once(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            db_path = base / "naz.sqlite3"
            exchange = base / "exchange"
            inbox = exchange / "void_to_naz" / "inbox"
            inbox.mkdir(parents=True)
            payload = {
                "source": "void_entity",
                "plan_id": "shared-crosspost-plan-01",
                "topic": "trust and responsibility",
                "text": (
                    "A private observation about trust, work and the consequence "
                    "of one concrete choice."
                ),
                "publish_mode": "auto",
            }
            for name in ("one.json", "two.json"):
                (inbox / name).write_text(json.dumps(payload), encoding="utf-8")
            package = eo.GenerationPackage(
                final_text=(
                    "A distinct Naz reflection built around a public workbench scene "
                    "and a materially different conclusion. " * 8
                ),
                concrete_scene="A used tool is placed beside the result it produced.",
                visual_subject="The used tool and the visible result.",
                visual_relation_to_thesis="The changed state proves the consequence.",
                image_prompt_seed="A close view of one used tool beside its result.",
                track_tags=("daily", "reflective"),
            )
            receipt = main.TelegramPublicationReceipt("-10077", "4321")
            send = AsyncMock(return_value=receipt)
            generate_package = AsyncMock(return_value=package)
            with patch.object(memory, "DB_PATH", str(db_path)), patch.multiple(
                main,
                ADMIN_ID=77,
                CHANNEL_ID="@channel",
                CROSSPOST_EXCHANGE_ENABLED=True,
                CROSSPOST_EXCHANGE_AUTO_PUBLISH=True,
                CROSSPOST_EXCHANGE_MAX_PER_RUN=2,
                CROSSPOST_EXCHANGE_DIR=exchange,
                NAZ_SCHEDULED_WORK_DIR=base / "markers",
            ), patch.object(
                main, "generate_scheduled_package", new=generate_package
            ), patch.object(
                main,
                "generate_images_with_retries",
                new=AsyncMock(return_value=([b"image"], "safe visual brief")),
            ), patch.object(
                main, "send_post_with_images", new=send
            ), patch.object(
                main.duo_relationship,
                "reflection_is_original",
                return_value=(True, ""),
            ), patch.object(main, "notify_admin", new=AsyncMock()):
                memory.init_db()
                import asyncio

                asyncio.run(
                    main.crosspost_exchange_job(SimpleNamespace(bot=SimpleNamespace()))
                )
                history = memory.get_recent_content_signatures(77)
                event = memory.get_editorial_release_event(
                    77, "shared-crosspost-plan-01", "telegram"
                )
            self.assertEqual(send.await_count, 1)
            self.assertEqual(generate_package.await_count, 1)
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["plan_id"], "shared-crosspost-plan-01")
            self.assertEqual(event["history_commit_status"], "committed")
            self.assertEqual(event["telegram_message_id"], "4321")
            for name in ("one.json", "two.json"):
                self.assertTrue(
                    (exchange / "void_to_naz" / "processed" / name).is_file()
                )

    def test_ambiguous_in_flight_delivery_is_never_sent_again(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            exchange = base / "exchange"
            inbox = exchange / "void_to_naz" / "inbox"
            inbox.mkdir(parents=True)
            plan_id = "ambiguous-crosspost-plan-01"
            (inbox / "retry.json").write_text(
                json.dumps(
                    {
                        "source": "void_entity",
                        "plan_id": plan_id,
                        "topic": "trust and responsibility",
                        "text": (
                            "A private observation about trust, responsibility and "
                            "one concrete consequence at work."
                        ),
                        "publish_mode": "auto",
                    }
                ),
                encoding="utf-8",
            )
            generate = AsyncMock()
            send = AsyncMock()
            with patch.object(memory, "DB_PATH", str(base / "naz.sqlite3")), patch.multiple(
                main,
                ADMIN_ID=77,
                CHANNEL_ID="@channel",
                CROSSPOST_EXCHANGE_ENABLED=True,
                CROSSPOST_EXCHANGE_DIR=exchange,
                NAZ_SCHEDULED_WORK_DIR=base / "markers",
            ), patch.object(
                main, "generate_scheduled_package", new=generate
            ), patch.object(main, "send_post_with_images", new=send):
                memory.init_db()
                memory.update_editorial_release_event(
                    user_id=77,
                    plan_id=plan_id,
                    platform="telegram",
                    slot="crosspost_exchange",
                    history_commit_status="sending",
                )
                import asyncio

                asyncio.run(
                    main.crosspost_exchange_job(SimpleNamespace(bot=SimpleNamespace()))
                )
            generate.assert_not_awaited()
            send.assert_not_awaited()
            self.assertTrue(
                (exchange / "void_to_naz" / "failed" / "retry.json").is_file()
            )


class TelegramReceiptTests(unittest.TestCase):
    def test_delivery_claim_is_atomic_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            memory, "DB_PATH", str(Path(root) / "naz.sqlite3")
        ):
            memory.init_db()
            identity = {
                "user_id": 1,
                "plan_id": "delivery-claim-plan-01",
                "platform": "telegram",
            }
            memory.update_editorial_release_event(
                **identity, history_commit_status="pending"
            )
            self.assertEqual(
                memory.claim_editorial_delivery(**identity), "claimed"
            )
            self.assertEqual(
                memory.claim_editorial_delivery(**identity), "blocked"
            )
            memory.update_editorial_release_event(
                **identity, history_commit_status="committed"
            )
            self.assertEqual(
                memory.claim_editorial_delivery(**identity), "committed"
            )

    def test_send_returns_bounded_chat_and_message_receipt(self):
        import asyncio

        message = SimpleNamespace(chat_id=-100123, message_id=456)
        bot = SimpleNamespace(send_photo=AsyncMock(return_value=message))
        receipt = asyncio.run(
            main.send_post_with_images(bot, "@channel", "published text", [b"image"])
        )
        self.assertEqual(receipt.chat_id, "-100123")
        self.assertEqual(receipt.message_id, "456")

    def test_observability_enums_reject_false_image_qa_status(self):
        with tempfile.TemporaryDirectory() as root, patch.object(
            memory, "DB_PATH", str(Path(root) / "naz.sqlite3")
        ):
            memory.init_db()
            with self.assertRaises(ValueError):
                memory.update_editorial_release_event(
                    user_id=1,
                    plan_id="planned-release-0001",
                    platform="telegram",
                    image_qa_status="assumed_accepted",
                )
            memory.update_editorial_release_event(
                user_id=1,
                plan_id="planned-release-0001",
                platform="telegram",
                generation_package_status="accepted",
                image_qa_status="not_run",
                history_commit_status="pending",
            )
            event = memory.get_editorial_release_event(
                1, "planned-release-0001", "telegram"
            )
            self.assertEqual(event["image_qa_status"], "not_run")


if __name__ == "__main__":
    unittest.main()
