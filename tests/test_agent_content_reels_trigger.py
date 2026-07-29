import asyncio
import json
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
from tools import reels_director_dry_run


WORK_CHRONICLE = """
Сцена была маленькая: на рабочем столе лежали два открытых тикета поддержки и профиль GitHub, который я перешивал в витрину AI Systems Lab.
Сначала Codex попросил остановиться, а регистратор доменов после оплаченных лет внезапно решил изменить правила.
Я раздражался, как будто это могло что-то починить, но это не помогло.
Помогло другое: перестать изображать универсального гения и руками собрать карту того, что уже реально работает.
После этой мелкой поломки профиль впервые превратился из склада репозиториев в понятную систему.
Теперь на главном экране видна одна карта, по которой можно идти дальше без объяснений.
Работа продолжилась с тем, что уже проверено и выдерживает повторный тест.
""".strip()


JULY_11_ROUTE_CHRONICLE = """
Ветка Git: feature/reels-director
Источник заголовка: локальная рабочая хроника
Источник-хеш: cafe1234
Реплик источника: 1
Граница: локальный архив
Сначала генератор остановился до обращения к модели и сохранил исходное состояние.
Ошибка оказалась воспроизводимой на одном ограниченном локальном маршруте.
После проверки один конфликтующий параметр убрали из рабочей конфигурации.
Затем тот же ограниченный запуск повторили на неизменном входе.
Локальный прогон завершился без обращения к внешнему провайдеру.
Отдельный тест подтвердил исправление на той же последовательности шагов.
В итоге проверенная цепочка заработала и сохранила наблюдаемый результат.
""".strip()


JULY_11_PRODUCTION_STYLE_CHRONICLE = """
Работа началась с задачи: подготовить публикацию, сохранить изменение и перезапустить бота.
По ходу работы запрос уточнился: включить новую рубрику в равномерную ротацию контента.
К финалу работа получила подтверждённый результат.
Коммиты ad733c1 и c588c55 созданы и запушены.
Работа началась с задачи: проверить готовую публикацию перед выпуском.
В процессе зафиксировали: пост успешно опубликован, изображение 9390ef818e628e48 отмечено использованным и не повторится.
В процессе зафиксировали: повторная отправка отменена, чтобы не создать дубль.
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
        self.assertEqual(call.await_args.kwargs["temperature"], 0.45)
        self.assertEqual(treatment.version, story_production.DIRECTOR_VERSION)

    def test_director_validation_logs_all_safe_codes_as_one_contract_reject(self):
        error = story_production.DirectorValidationError((
            "director_visual_concept_cliche",
            "director_scene_2_action_recipe_invalid",
        ))

        self.assertEqual(
            main.reels_director_reason_codes(error),
            error.reason_codes,
        )
        self.assertEqual(
            main.reels_director_reason_code(error),
            "director_contract_invalid",
        )
        self.assertIn(
            "несколько полей",
            main.reels_director_reason_summary("director_contract_invalid"),
        )

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

    def test_unfilmable_director_actions_have_safe_russian_admin_summary(self):
        error = story_production.DirectorValidationError((
            "director_scene_1_interface_pantomime",
            "director_scene_2_multi_action",
        ))

        code = main.reels_director_reason_code(error)

        self.assertEqual(code, "director_action_unfilmable")
        self.assertIn("непригодное для съёмки", main.reels_director_reason_summary(code))

    def test_director_dry_run_validates_in_memory_without_queue_or_history_writes(self):
        async def accepted_treatment(plan, facts):
            return story_production.parse_reels_director_response(
                director_response(plan, facts), plan, facts
            )

        with patch.object(
            reels_director_dry_run.main,
            "collect_agent_materials",
            return_value=(WORK_CHRONICLE, [], "2026-07-08"),
        ), patch.object(
            reels_director_dry_run.main,
            "agent_content_hash_for_date",
            return_value="fixture-hash",
        ), patch.object(
            reels_director_dry_run.main,
            "generate_reels_director_treatment",
            new=AsyncMock(side_effect=accepted_treatment),
        ) as director, patch.object(
            reels_director_dry_run.main,
            "queue_story_first_pack",
        ) as queue, patch.object(
            reels_director_dry_run.main.memory,
            "update_editorial_release_event",
        ) as write_event:
            result = asyncio.run(reels_director_dry_run.run("2026-07-08"))

        self.assertEqual(result["status"], "accepted")
        self.assertFalse(result["persisted"])
        self.assertEqual(result["media_calls"], 0)
        director.assert_awaited_once()
        queue.assert_not_called()
        write_event.assert_not_called()

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

    def test_2026_07_11_structured_headers_never_become_director_beats(self):
        context = """
Ветка Git: feature/reels-after-deploy
Источник заголовка: source_hash
Формат: markdown
Версия формата: 2
Хеш источника: deadbeef
Источник-хеш: cafe1234
Реплик источника: 1
Граница: локальный архив
Сначала генератор остановился до обращения к модели и сохранил исходное состояние.
После проверки один конфликтующий параметр убрали из локального маршрута.
Затем тот же ограниченный запуск повторили на неизменном входе.
В итоге проверенный маршрут заработал и отдельный тест подтвердил результат.
""".strip()


        row = main.chronicle_source_row(
            source_ref="agent_content:2026-07-11:sanitized-fixture",
            safe_context=context,
            risks=(),
            topic="Проверенный рабочий эпизод",
        )

        joined = "\n".join(row["safe_facts"]).casefold()
        for forbidden in (
            "ветка git:", "источник заголовка:", "формат:",
            "версия формата:", "хеш источника:", "источник-хеш:",
            "реплик источника:", "граница:",
        ):
            self.assertNotIn(forbidden, joined)
        self.assertEqual(len(row["safe_facts"]), 4)
        self.assertTrue(editorial_orchestrator.story_first_eligible(
            editorial_orchestrator.EditorialSource(**row)
        ))

    def test_metadata_cannot_supply_story_first_eligibility_signals(self):
        context = """
Ветка Git: сначала исправили build, затем deploy заработал и дал результат.
Формат: после теста всё стало работать.
Версия формата: 2
Первая справка перечисляет оттенки корпуса и материал панели.
Вторая справка перечисляет размеры стола рядом со стеной.
Третья справка перечисляет уровень освещения комнаты и положение лампы.
""".strip()
        row = main.chronicle_source_row(
            source_ref="agent_content:metadata-only-signals",
            safe_context=context,
            risks=(),
            topic="Неподходящие заметки",
        )

        self.assertFalse(row["concrete_action"])
        self.assertFalse(row["visualizable_process"])
        self.assertFalse(row["real_result"])
        self.assertEqual(row["causal_bits"], 0)
        self.assertFalse(editorial_orchestrator.story_first_eligible(
            editorial_orchestrator.EditorialSource(**row)
        ))

    def test_2026_07_11_production_style_facts_are_safe_and_story_first_eligible(self):
        row = main.chronicle_source_row(
            source_ref="agent_content:2026-07-11:production-style",
            safe_context=JULY_11_PRODUCTION_STYLE_CHRONICLE,
            risks=(),
            topic="Рабочая хроника Naz 2026-07-11",
        )

        self.assertEqual(len(row["safe_facts"]), 7)
        self.assertTrue(row["concrete_action"])
        self.assertTrue(row["visualizable_process"])
        self.assertGreaterEqual(row["causal_bits"], 4)
        self.assertTrue(row["real_result"])
        self.assertNotRegex(
            "\n".join(row["safe_facts"]),
            r"(?i)\b(?=[0-9a-f]{7,64}\b)(?=[0-9a-f]*\d)[0-9a-f]+\b",
        )
        self.assertTrue(editorial_orchestrator.story_first_eligible(
            editorial_orchestrator.EditorialSource(**row)
        ))

    def test_2026_07_11_full_route_queues_one_atomic_plan_without_media(self):
        row = main.chronicle_source_row(
            source_ref="agent_content:2026-07-11:fixture-hash",
            safe_context=JULY_11_PRODUCTION_STYLE_CHRONICLE,
            risks=(),
            topic="Рабочая хроника Naz 2026-07-11",
        )
        self.assertEqual(len(row["safe_facts"]), 7)
        raw_payload = json.loads(director_response(None, row["safe_facts"]))
        raw_payload["story_arc"] = "module_recovery_human"
        raw_director = json.dumps(raw_payload)
        bot = SimpleNamespace(send_message=AsyncMock())
        pack_root = Path(self.temp.name) / "story-packs"

        with patch.object(main, "ADMIN_ID", 42), patch.object(
            main, "NAZ_STORY_PACK_ROOT", pack_root
        ), patch.object(
            main, "agent_content_source_dirs_for_date", return_value=[Path("fixture")]
        ), patch.object(
            main, "agent_content_hash_for_date", return_value="fixture-hash"
        ), patch.object(
            main, "load_agent_content_seen", return_value={}
        ), patch.object(
            main,
            "collect_agent_materials",
            return_value=(JULY_11_PRODUCTION_STYLE_CHRONICLE, [], "2026-07-11"),
        ), patch.object(
            main, "call_gpt", new=AsyncMock(return_value=raw_director)
        ) as director_call, patch.object(
            main.story_production,
            "persist_story_queue",
            wraps=story_production.persist_story_queue,
        ) as persist, patch.object(
            main, "mark_agent_content_seen"
        ) as mark_seen, patch.object(
            main, "generate_scheduled_package", new=AsyncMock()
        ) as text_post, patch.object(
            main, "generate_images_with_retries", new=AsyncMock()
        ) as image_provider, patch.object(
            main, "generate_image_bytes", new=AsyncMock()
        ) as legacy_image_provider:
            result = asyncio.run(
                main.process_agent_content_date(
                    bot, 42, "2026-07-11", force=True, publish=False
                )
            )

        self.assertIn("Story-first plan awaits approval", result)
        self.assertEqual(director_call.await_count, 1)
        messages = director_call.await_args.args[0]
        actual_prompt = messages[1]["content"].casefold()
        for forbidden in (
            "ветка git:", "источник заголовка:", "источник-хеш:",
            "реплик источника:", "граница:",
        ):
            self.assertNotIn(forbidden, actual_prompt)
        response_schema = director_call.await_args.kwargs["response_format"]
        scene_schema = response_schema["json_schema"]["schema"]["properties"]["scenes"]
        self.assertEqual((scene_schema["minItems"], scene_schema["maxItems"]), (7, 7))
        persist.assert_called_once()
        mark_seen.assert_called_once_with("2026-07-11", "fixture-hash")
        manifests = list(pack_root.glob("*/story_manifest.json"))
        self.assertEqual(len(manifests), 1)
        payload = story_production.read_manifest(manifests[0])
        self.assertEqual(payload["pack_status"], "awaiting_approval")
        self.assertTrue(all(not job["external_job_id"] for job in payload["scene_jobs"]))
        text_post.assert_not_awaited()
        image_provider.assert_not_awaited()
        legacy_image_provider.assert_not_awaited()

    def test_2026_07_11_compound_actions_reject_once_before_queue(self):
        row = main.chronicle_source_row(
            source_ref="agent_content:2026-07-11:fixture-hash",
            safe_context=JULY_11_PRODUCTION_STYLE_CHRONICLE,
            risks=(),
            topic="Рабочая хроника Naz 2026-07-11",
        )
        raw_payload = json.loads(director_response(None, row["safe_facts"]))
        raw_payload["story_arc"] = "module_recovery_human"
        raw_payload["scenes"][4]["action_recipe"] = "coupling_to_rotate_housing"
        raw_payload["scenes"][6]["action_recipe"] = "latch_until_housing_opens"
        bot = SimpleNamespace(send_message=AsyncMock())

        with patch.object(main, "ADMIN_ID", 42), patch.object(
            main, "agent_content_source_dirs_for_date", return_value=[Path("fixture")]
        ), patch.object(
            main, "agent_content_hash_for_date", return_value="fixture-hash"
        ), patch.object(
            main, "load_agent_content_seen", return_value={}
        ), patch.object(
            main,
            "collect_agent_materials",
            return_value=(JULY_11_PRODUCTION_STYLE_CHRONICLE, [], "2026-07-11"),
        ), patch.object(
            main,
            "call_gpt",
            new=AsyncMock(return_value=json.dumps(raw_payload)),
        ) as director_call, patch.object(
            main, "queue_story_first_pack"
        ) as queue, patch.object(
            main, "mark_agent_content_seen"
        ) as mark_seen, patch.object(
            main, "generate_scheduled_package", new=AsyncMock()
        ) as text_post, patch.object(
            main, "generate_images_with_retries", new=AsyncMock()
        ) as image_provider, patch.object(
            main, "generate_image_bytes", new=AsyncMock()
        ) as legacy_image_provider, patch.object(
            main.logger, "warning"
        ) as warning:
            result = asyncio.run(
                main.process_agent_content_date(
                    bot, 42, "2026-07-11", force=True, publish=False
                )
            )

        self.assertIn("режиссёрский план отклонён", result)
        self.assertEqual(director_call.await_count, 1)
        rejection = next(
            call for call in warning.call_args_list
            if call.args and str(call.args[0]).startswith("REELS_DIRECTOR rejected")
        )
        self.assertEqual(rejection.args[2], "director_contract_invalid")
        self.assertEqual(
            rejection.args[3],
            "director_scene_5_schema_invalid,director_scene_7_schema_invalid",
        )
        queue.assert_not_called()
        mark_seen.assert_not_called()
        text_post.assert_not_awaited()
        image_provider.assert_not_awaited()
        legacy_image_provider.assert_not_awaited()

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
            main, "agent_content_source_dirs_for_date", return_value=[Path("fixture")]
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
            main, "agent_content_source_dirs_for_date", return_value=[Path("fixture")]
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
