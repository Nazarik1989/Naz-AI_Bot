import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import editorial_orchestrator
import main
import memory
import story_production
from tests.test_story_production import director_response


WORK_CHRONICLE = """
Сцена была маленькая: на рабочем столе лежали два открытых тикета поддержки и профиль GitHub, который я перешивал в витрину AI Systems Lab.
Сначала Codex попросил остановиться, а регистратор доменов после оплаченных лет внезапно решил изменить правила.
Я раздражался, как будто это могло что-то починить, но это не помогло.
Помогло другое: перестать изображать универсального гения и руками собрать карту того, что уже реально работает.
После этой мелкой поломки профиль впервые превратился из склада репозиториев в понятную систему.
Теперь на главном экране видна одна карта, по которой можно идти дальше без объяснений.
Работа продолжилась с тем, что уже проверено и выдерживает повторный тест.
""".strip()


RUBRICS = (
    {
        "key": "agent_content",
        "name": "Рабочая хроника Naz",
        "kind": "work_chronicle",
        "angle": "turn a verified work episode into one coherent release",
        "track_tags": "daily,focus,builder,reflective",
    },
)


class AgentContentReelsTriggerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(memory, "DB_PATH", str(Path(self.temp.name) / "naz.sqlite3"))
        self.db_patch.start()
        memory.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def source_row(self, risks=()):
        return main.chronicle_source_row(
            source_ref="agent_content:2026-07-25:fixture",
            safe_context=WORK_CHRONICLE,
            risks=risks,
            topic="Проверенный рабочий эпизод Naz",
        )

    def test_russian_inflected_actions_preserve_story_evidence(self):
        row = self.source_row()
        self.assertTrue(row["source_verified"])
        self.assertTrue(row["concrete_action"])
        self.assertTrue(row["visualizable_process"])
        self.assertGreaterEqual(row["causal_bits"], 4)
        self.assertTrue(row["real_result"])
        self.assertEqual(len(row["safe_facts"]), 7)
        source = editorial_orchestrator.EditorialSource(**row)
        self.assertTrue(editorial_orchestrator.story_first_eligible(source))

    def test_same_fixture_selects_story_first_plan(self):
        plan = main.scheduled_plan(
            user_id=42,
            platform="telegram",
            slot="agent_content_sync",
            seed="agent_content:2026-07-25:fixture",
            rubric_rows=RUBRICS,
            source_rows=(self.source_row(),),
            character=main.naz_character.CharacterState(),
        )
        self.assertEqual(plan.production_mode, "story_first")
        self.assertEqual(plan.content_format, "story_pack")

    def test_director_call_uses_structured_output_without_retry(self):
        plan = main.scheduled_plan(
            user_id=42,
            platform="telegram",
            slot="agent_content_sync",
            seed="agent_content:2026-07-25:fixture",
            rubric_rows=RUBRICS,
            source_rows=(self.source_row(),),
            character=main.naz_character.CharacterState(),
        )
        with patch.object(
            main,
            "call_gpt",
            new=AsyncMock(return_value=director_response(plan, self.source_row()["safe_facts"])),
        ) as call:
            treatment = asyncio.run(
                main.generate_reels_director_treatment(
                    plan,
                    tuple(self.source_row()["safe_facts"]),
                )
            )
        self.assertEqual(call.await_count, 1)
        response_format = call.await_args.kwargs["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertEqual(treatment.version, story_production.DIRECTOR_VERSION)

    def test_call_gpt_forwards_response_format_to_openrouter_sdk(self):
        client = Mock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))]
        )
        response_format = {"type": "json_object"}
        with patch.object(main, "ensure_openai_client", return_value=client):
            result = asyncio.run(
                main.call_gpt(
                    [{"role": "user", "content": "return json"}],
                    response_format=response_format,
                )
            )
        self.assertEqual(result, '{"ok":true}')
        self.assertEqual(
            client.chat.completions.create.call_args.kwargs["response_format"],
            response_format,
        )

    def test_safety_flags_still_force_standard_mode(self):
        row = self.source_row(("secret credential",))
        plan = main.scheduled_plan(
            user_id=42,
            platform="telegram",
            slot="agent_content_sync",
            seed="unsafe-agent-content",
            rubric_rows=RUBRICS,
            source_rows=(row,),
            character=main.naz_character.CharacterState(),
        )
        self.assertTrue(row["contains_secrets"])
        self.assertEqual(plan.production_mode, "standard")

    def test_unrelated_observations_do_not_become_story_first(self):
        context = "\n".join(
            f"Static observation number {index} describes a general interface without sequence or outcome."
            for index in range(1, 8)
        )
        row = main.chronicle_source_row(
            source_ref="agent_content:abstract",
            safe_context=context,
            risks=(),
            topic="Abstract notes",
        )
        self.assertFalse(row["concrete_action"])
        self.assertEqual(row["causal_bits"], 0)
        self.assertFalse(editorial_orchestrator.story_first_eligible(
            editorial_orchestrator.EditorialSource(**row)
        ))

    def test_transport_metadata_and_paths_never_become_director_facts(self):
        context = """
Folders: Naz_AI_Bot_clean/2026-07-25
User focus: ежедневный импорт content-agent
### Naz_AI_Bot_clean/2026-07-25/2026-07-25--episode.md
Проект: Naz_AI_Bot_clean
Тема диалога: лимиты закончились
Сначала команда воспроизвела ограничение на отдельном тестовом маршруте.
После проверки один конфликтующий параметр убрали из конфигурации.
Затем тот же сценарий запустили повторно на неизменном входе.
В итоге рабочий маршрут завершился и результат подтвердили отдельным тестом.
""".strip()
        row = main.chronicle_source_row(
            source_ref="agent_content:metadata-fixture",
            safe_context=context,
            risks=(),
            topic="Проверенный рабочий эпизод",
        )
        joined = "\n".join(row["safe_facts"]).casefold()
        self.assertNotIn("folders:", joined)
        self.assertNotIn("user focus:", joined)
        self.assertNotIn(".md", joined)
        self.assertNotIn("проект:", joined)
        self.assertEqual(len(row["safe_facts"]), 4)
        self.assertTrue(editorial_orchestrator.story_first_eligible(
            editorial_orchestrator.EditorialSource(**row)
        ))

    def test_story_first_trigger_uses_one_director_call_without_media_provider_calls(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        pack_dir = Path(self.temp.name) / "story-packs" / ("a" * 24)
        payload = {
            "schema": story_production.STORY_SCHEMA,
            "plan_id": "a" * 24,
            "variant_index": 0,
            "rubric": "Рабочая хроника Naz",
            "pack_status": "awaiting_approval",
            "approval": {"status": "awaiting_approval"},
            "scene_jobs": [],
        }
        with patch.object(main, "ADMIN_ID", 42), patch.object(
            main, "agent_content_dirs_for_date", return_value=[Path("fixture")]
        ), patch.object(main, "agent_content_hash_for_date", return_value="fixture-hash"), patch.object(
            main, "load_agent_content_seen", return_value={}
        ), patch.object(
            main, "collect_agent_materials", return_value=(WORK_CHRONICLE, [], "2026-07-25")
        ), patch.object(
            main,
            "generate_reels_director_treatment",
            new=AsyncMock(return_value=Mock()),
        ) as director, patch.object(main, "queue_story_first_pack", return_value=pack_dir), patch.object(
            main.story_production, "read_manifest", return_value=payload
        ), patch.object(main, "mark_agent_content_seen"), patch.object(
            main, "generate_scheduled_package", new=AsyncMock()
        ) as text_model, patch.object(
            main, "generate_images_with_retries", new=AsyncMock()
        ) as image_model, patch.object(main, "generate_image_bytes", new=AsyncMock()) as provider:
            result = asyncio.run(
                main.process_agent_content_date(
                    bot, 42, "2026-07-25", force=True, publish=False
                )
            )

        self.assertIn("Story-first plan awaits approval", result)
        bot.send_message.assert_awaited_once()
        keyboard = bot.send_message.await_args.kwargs["reply_markup"]
        callback_data = {
            button.callback_data
            for row in keyboard.inline_keyboard
            for button in row
        }
        self.assertEqual(
            callback_data,
            {
                "reels_confirm:" + "a" * 24,
                "reels_variant:" + "a" * 24,
                "reels_status:" + "a" * 24,
            },
        )
        director.assert_awaited_once()
        text_model.assert_not_awaited()
        image_model.assert_not_awaited()
        provider.assert_not_awaited()

    def test_invalid_director_plan_fails_closed_without_template_queue(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        with patch.object(main, "ADMIN_ID", 42), patch.object(
            main, "agent_content_dirs_for_date", return_value=[Path("fixture")]
        ), patch.object(main, "agent_content_hash_for_date", return_value="fixture-hash"), patch.object(
            main, "load_agent_content_seen", return_value={}
        ), patch.object(
            main, "collect_agent_materials", return_value=(WORK_CHRONICLE, [], "2026-07-25")
        ), patch.object(
            main,
            "generate_reels_director_treatment",
            new=AsyncMock(side_effect=story_production.StoryPlanError("director_json_invalid")),
        ), patch.object(main, "queue_story_first_pack") as queue, patch.object(
            main, "mark_agent_content_seen"
        ) as mark_seen:
            result = asyncio.run(
                main.process_agent_content_date(
                    bot, 42, "2026-07-25", force=True, publish=False
                )
            )

        self.assertIn("режиссёрский план отклонён", result)
        queue.assert_not_called()
        mark_seen.assert_not_called()
        self.assertIn("Рендер не запускался", bot.send_message.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
