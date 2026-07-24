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
from tests.test_story_production import SAFE_FACTS, planned


def fake_update(user_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        message=SimpleNamespace(text=text, reply_text=AsyncMock()),
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

    def test_confirm_button_approves_without_provider_call(self):
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
            self.assertEqual(payload["approval"]["status"], "approved")
            self.assertTrue(all(not job["external_job_id"] for job in payload["scene_jobs"]))

    def test_confirm_button_does_not_queue_when_rendering_is_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            pack = story.plan_story_pack(planned(), SAFE_FACTS)
            story.persist_story_queue(pack, Path(root))
            update = fake_update(1, main.BTN_REELS_CONFIRM)
            with patch.object(main, "ADMIN_ID", 1), patch.object(
                main, "NAZ_STORY_PACK_ROOT", Path(root)
            ), patch.object(
                main, "NAZ_STORY_RENDER_ENABLED", False
            ):
                handled = asyncio.run(main.handle_menu_button(
                    update, SimpleNamespace(), main.BTN_REELS_CONFIRM
                ))
            self.assertTrue(handled)
            payload = story.read_manifest(Path(root) / pack.plan_id / "story_manifest.json")
            self.assertEqual(payload["approval"]["status"], "awaiting_approval")
            self.assertIn("выключена", update.message.reply_text.await_args.args[0])

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
