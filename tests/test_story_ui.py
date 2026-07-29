import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
import story_pack_control as control
import story_production as story
from tests.test_story_production import SAFE_FACTS, director_response, planned


def fake_update(user_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
        effective_user=SimpleNamespace(id=user_id),
    )


def fake_reels_reply(user_id: int, text: str, summary: str) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(
            text=text,
            reply_to_message=SimpleNamespace(text=summary, caption=None),
            reply_text=AsyncMock(),
        ),
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=user_id),
    )


def fake_reels_callback(user_id: int, data: str) -> SimpleNamespace:
    return SimpleNamespace(
        callback_query=SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            message=SimpleNamespace(reply_text=AsyncMock()),
        ),
        effective_user=SimpleNamespace(id=user_id),
    )


class StoryMenuTests(unittest.TestCase):
    def test_admin_reels_menu_has_only_three_process_actions(self):
        actions = {
            button.text
            for row in main.REELS_KEYBOARD.keyboard
            for button in row
            if button.text != main.BTN_BACK
        }
        self.assertEqual(actions, {
            main.BTN_REELS_CONFIRM, main.BTN_REELS_VARIANT, main.BTN_REELS_STATUS,
        })

    def test_plan_keyboard_binds_every_action_to_one_plan(self):
        plan_id = "a" * 24
        keyboard = main.reels_plan_keyboard(plan_id)
        callbacks = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }
        self.assertEqual(callbacks, {
            f"reels_confirm:{plan_id}",
            f"reels_variant:{plan_id}",
            f"reels_status:{plan_id}",
        })

    def test_legacy_confirm_button_only_returns_plan_scoped_card(self):
        with tempfile.TemporaryDirectory() as root:
            pack = story.plan_story_pack(planned(), SAFE_FACTS)
            story.persist_story_queue(pack, Path(root))
            update = fake_update(1, main.BTN_REELS_CONFIRM)
            with patch.object(main, "ADMIN_ID", 1), patch.object(
                main, "NAZ_STORY_PACK_ROOT", Path(root)
            ), patch.object(
                main, "NAZ_STORY_RENDER_ENABLED", True
            ):
                handled = asyncio.run(main.handle_menu_button(
                    update, SimpleNamespace(), main.BTN_REELS_CONFIRM
                ))
            self.assertTrue(handled)
            payload = story.read_manifest(Path(root) / pack.plan_id / "story_manifest.json")
            self.assertEqual(payload["approval"]["status"], "awaiting_approval")
            self.assertTrue(all(not job["external_job_id"] for job in payload["scene_jobs"]))
            self.assertIn("общая кнопка", update.message.reply_text.await_args.args[0])
            keyboard = update.message.reply_text.await_args.kwargs["reply_markup"]
            self.assertEqual(
                keyboard.inline_keyboard[0][0].callback_data,
                f"reels_confirm:{pack.plan_id}",
            )

    def test_legacy_variant_button_does_not_supersede_latest_plan(self):
        with tempfile.TemporaryDirectory() as root:
            pack = story.plan_story_pack(planned(), SAFE_FACTS)
            story.persist_story_queue(pack, Path(root))
            update = fake_update(1, main.BTN_REELS_VARIANT)
            with patch.object(main, "ADMIN_ID", 1), patch.object(
                main, "NAZ_STORY_PACK_ROOT", Path(root)
            ), patch.object(
                main, "NAZ_STORY_RENDER_ENABLED", True
            ):
                handled = asyncio.run(main.handle_menu_button(
                    update, SimpleNamespace(), main.BTN_REELS_VARIANT
                ))
            self.assertTrue(handled)
            payload = story.read_manifest(Path(root) / pack.plan_id / "story_manifest.json")
            self.assertEqual(payload["approval"]["status"], "awaiting_approval")
            self.assertEqual(payload["pack_status"], "awaiting_approval")
            self.assertEqual(len(control.list_manifests(Path(root))), 1)
            self.assertIn("общая кнопка", update.message.reply_text.await_args.args[0])

    def test_reply_confirmation_is_not_sent_to_chat_or_provider(self):
        with tempfile.TemporaryDirectory() as root:
            pack = story.plan_story_pack(planned(), SAFE_FACTS)
            pack_dir = story.persist_story_queue(pack, Path(root))
            summary = control.safe_summary(
                story.read_manifest(pack_dir / "story_manifest.json")
            )
            update = fake_reels_reply(1, "подтверждаю", summary)
            context = SimpleNamespace()
            with patch.object(main, "ADMIN_ID", 1), patch.object(
                main, "NAZ_STORY_PACK_ROOT", Path(root)
            ), patch.object(
                main, "NAZ_STORY_RENDER_ENABLED", False
            ), patch.object(
                main, "handle_delegated_reply", new=AsyncMock(return_value=False)
            ), patch.object(
                main, "generate_answer", new=AsyncMock()
            ) as chat_model, patch.object(
                main, "generate_image_bytes", new=AsyncMock()
            ) as provider:
                asyncio.run(main.handle_message(update, context))

            payload = story.read_manifest(pack_dir / "story_manifest.json")
            self.assertEqual(payload["approval"]["status"], "awaiting_approval")
            self.assertEqual(payload["pack_status"], "awaiting_approval")
            self.assertIn("выключена", update.message.reply_text.await_args.args[0])
            chat_model.assert_not_awaited()
            provider.assert_not_awaited()

    def test_callback_confirms_exact_plan_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            first = story.plan_story_pack(planned(), SAFE_FACTS, variant_index=0)
            second = story.plan_story_pack(planned(), SAFE_FACTS, variant_index=1)
            self.assertNotEqual(first.plan_id, second.plan_id)
            story.persist_story_queue(first, Path(root))
            story.persist_story_queue(second, Path(root))
            update = fake_reels_callback(1, f"reels_confirm:{first.plan_id}")
            with patch.object(main, "ADMIN_ID", 1), patch.object(
                main, "NAZ_STORY_PACK_ROOT", Path(root)
            ), patch.object(
                main, "NAZ_STORY_RENDER_ENABLED", True
            ), patch.object(
                main, "generate_image_bytes", new=AsyncMock()
            ) as provider:
                asyncio.run(main.reels_control_callback(update, SimpleNamespace()))
                asyncio.run(main.reels_control_callback(update, SimpleNamespace()))

            first_payload = story.read_manifest(
                Path(root) / first.plan_id / "story_manifest.json"
            )
            second_payload = story.read_manifest(
                Path(root) / second.plan_id / "story_manifest.json"
            )
            self.assertEqual(first_payload["approval"]["status"], "approved")
            self.assertEqual(second_payload["approval"]["status"], "awaiting_approval")
            self.assertTrue(all(
                not job["external_job_id"] for job in first_payload["scene_jobs"]
            ))
            self.assertEqual(update.callback_query.answer.await_count, 2)
            provider.assert_not_awaited()

    def test_scoped_status_returns_compact_manifest_progress(self):
        with tempfile.TemporaryDirectory() as root:
            pack = story.plan_story_pack(planned(), SAFE_FACTS)
            pack_dir = story.persist_story_queue(pack, Path(root))
            control.approve_pack(Path(root), pack.plan_id)
            payload = story.read_manifest(pack_dir / "story_manifest.json")
            payload["scene_jobs"][0].update({
                "keyframe_state": "ready",
                "state": "in_progress",
            })
            payload["pack_status"] = "in_progress"
            scene_count = len(payload["scene_jobs"])
            story.atomic_json(pack_dir / "story_manifest.json", payload)

            with patch.object(main, "NAZ_STORY_PACK_ROOT", Path(root)):
                response, keyboard = asyncio.run(
                    main.reels_control_response(
                        main.BTN_REELS_STATUS,
                        plan_id=pack.plan_id,
                    )
                )

            self.assertIn("Reels Maker · прогресс", response)
            self.assertIn(f"Ключевые кадры: 1/{scene_count}", response)
            self.assertIn(f"Видео сцен: 0/{scene_count}", response)
            self.assertNotIn("Режиссёрский план", response)
            self.assertEqual(
                keyboard.inline_keyboard[-1][0].callback_data,
                f"reels_status:{pack.plan_id}",
            )

    def test_scoped_variant_runs_semantic_director_before_superseding(self):
        with tempfile.TemporaryDirectory() as root:
            plan = planned()
            first = story.plan_story_pack(plan, SAFE_FACTS)
            story.persist_story_queue(first, Path(root))
            treatment = story.parse_reels_director_response(
                director_response(plan, variant_index=1),
                plan,
                SAFE_FACTS,
                variant_index=1,
            )
            with patch.object(main, "NAZ_STORY_PACK_ROOT", Path(root)), patch.object(
                main,
                "generate_reels_director_treatment",
                new=AsyncMock(return_value=treatment),
            ) as director:
                response, _ = asyncio.run(
                    main.reels_control_response(
                        main.BTN_REELS_VARIANT,
                        plan_id=first.plan_id,
                    )
                )
            director.assert_awaited_once()
            self.assertIn("другой режиссёрский вариант", response)
            manifests = control.list_manifests(Path(root))
            new_payload = next(
                story.read_manifest(path)
                for path in manifests
                if path.parent.name != first.plan_id
            )
            self.assertEqual(new_payload["director_version"], story.DIRECTOR_VERSION)

    def test_scoped_variant_rejects_stale_manifest_before_director_call(self):
        with tempfile.TemporaryDirectory() as root:
            first = story.plan_story_pack(planned(), SAFE_FACTS)
            pack_dir = story.persist_story_queue(first, Path(root))
            manifest = pack_dir / "story_manifest.json"
            payload = story.read_manifest(manifest)
            payload["safe_facts"][0] = "tampered source fact"
            story.atomic_json(manifest, payload)

            with patch.object(
                main, "NAZ_STORY_PACK_ROOT", Path(root)
            ), patch.object(
                main,
                "generate_reels_director_treatment",
                new=AsyncMock(),
            ) as director:
                response, _ = asyncio.run(
                    main.reels_control_response(
                        main.BTN_REELS_VARIANT,
                        plan_id=first.plan_id,
                    )
                )

            director.assert_not_awaited()
            self.assertIn("Текущий план не изменён", response)
            self.assertEqual(len(control.list_manifests(Path(root))), 1)

    def test_manifest_plan_id_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            first = story.plan_story_pack(planned(), SAFE_FACTS, variant_index=0)
            second = story.plan_story_pack(planned(), SAFE_FACTS, variant_index=1)
            first_dir = story.persist_story_queue(first, Path(root))
            second_dir = story.persist_story_queue(second, Path(root))
            first_manifest = first_dir / "story_manifest.json"
            first_payload = story.read_manifest(first_manifest)
            first_payload["plan_id"] = second.plan_id
            story.atomic_json(first_manifest, first_payload)
            update = fake_reels_callback(1, f"reels_confirm:{first.plan_id}")
            with patch.object(main, "ADMIN_ID", 1), patch.object(
                main, "NAZ_STORY_PACK_ROOT", Path(root)
            ), patch.object(main, "NAZ_STORY_RENDER_ENABLED", True):
                asyncio.run(main.reels_control_callback(update, SimpleNamespace()))

            corrupted = story.read_manifest(first_manifest)
            untouched = story.read_manifest(second_dir / "story_manifest.json")
            self.assertEqual(corrupted["approval"]["status"], "awaiting_approval")
            self.assertEqual(untouched["approval"]["status"], "awaiting_approval")
            self.assertIn(
                "недоступно",
                update.callback_query.message.reply_text.await_args.args[0],
            )

    def test_non_admin_callback_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            pack = story.plan_story_pack(planned(), SAFE_FACTS)
            pack_dir = story.persist_story_queue(pack, Path(root))
            update = fake_reels_callback(2, f"reels_confirm:{pack.plan_id}")
            with patch.object(main, "ADMIN_ID", 1), patch.object(
                main, "NAZ_STORY_PACK_ROOT", Path(root)
            ), patch.object(main, "NAZ_STORY_RENDER_ENABLED", True):
                asyncio.run(main.reels_control_callback(update, SimpleNamespace()))
            payload = story.read_manifest(pack_dir / "story_manifest.json")
            self.assertEqual(payload["approval"]["status"], "awaiting_approval")
            update.callback_query.message.reply_text.assert_not_awaited()
            self.assertTrue(update.callback_query.answer.await_args.kwargs["show_alert"])

    def test_malformed_callback_fails_closed(self):
        update = fake_reels_callback(1, "reels_confirm:../../secrets")
        with patch.object(main, "ADMIN_ID", 1):
            asyncio.run(main.reels_control_callback(update, SimpleNamespace()))
        update.callback_query.message.reply_text.assert_not_awaited()
        self.assertTrue(update.callback_query.answer.await_args.kwargs["show_alert"])

    def test_contact_reels_button_keeps_text_script_flow(self):
        update = fake_update(77, main.BTN_REELS)
        with patch.object(main, "is_admin", return_value=False), patch.object(
            main, "reject_unregistered_user", new=AsyncMock(return_value=False)
        ):
            asyncio.run(main.handle_menu_button(update, SimpleNamespace(), main.BTN_REELS))
        self.assertEqual(main.USER_PENDING_ACTIONS.pop(77), "script")


class StoryDeliveryTests(unittest.TestCase):
    def test_completed_pack_is_sent_only_to_admin_private_chat(self):
        with tempfile.TemporaryDirectory() as root:
            pack = story.plan_story_pack(planned(), SAFE_FACTS)
            pack_dir = story.persist_story_queue(pack, Path(root))
            manifest = pack_dir / "story_manifest.json"
            payload = story.read_manifest(manifest)
            story_job = payload["scene_jobs"][0]
            story_job["state"] = "completed"
            reel_job = payload["reel_jobs"][0]
            reel_job["state"] = "completed"
            for relative in (story_job["story_path"], reel_job["path"]):
                path = pack_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"0000ftyp-private-video")
            payload["pack_status"] = "completed"
            payload["delivery"] = {"status": "ready", "sent_files": [], "completed_at": None}
            story.atomic_json(manifest, payload)
            bot = SimpleNamespace(send_video=AsyncMock())
            context = SimpleNamespace(bot=bot)
            with patch.object(main, "ADMIN_ID", 123), patch.object(
                main, "NAZ_STORY_PACK_ROOT", Path(root)
            ):
                asyncio.run(main.story_private_delivery_job.__wrapped__(context))
            self.assertEqual(bot.send_video.await_count, 2)
            self.assertTrue(all(
                call.kwargs["chat_id"] == 123 for call in bot.send_video.await_args_list
            ))
            delivered = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(delivered["delivery"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
