import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import character_state
import controller
import main
import memory
import prompts
import semantic_autopost as semantic
import vk_publish_queue


def rejected(reason: str = "same meaning") -> semantic.SemanticDecision:
    return semantic.SemanticDecision(
        False,
        reason,
        "контроль инструмента важнее его автономности",
        "без человеческой проверки автоматизация опасна",
        "сбой → проверка → правило",
        ("контроль", "проверка", "автоматизация"),
    )


def accepted() -> semantic.SemanticDecision:
    return semantic.SemanticDecision(
        True,
        "distinct",
        "забота проявляется в понятном сообщении об ошибке",
        "надёжность уважает чужое время",
        "сцена → последствие → вывод",
        ("забота", "ясность", "время"),
    )


class SemanticAutopostTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            memory,
            "DB_PATH",
            str(Path(self.directory.name) / "semantic.sqlite3"),
        )
        self.db_patch.start()
        memory.init_db()

    def test_controller_keeps_character_voice_without_forcing_builder_moral(self):
        prompt = controller.build_clean_gpt_input(
            "Сделай пост о вечернем городе",
            controller.normalize_state({}),
            "post",
            "вечерний город",
        )
        self.assertIn("Бери конкретную сцену из выбранной смысловой оси", prompt)
        self.assertIn("только когда сам материал действительно про разработку", prompt)
        self.assertNotIn(
            "Показывай путь через бардак, баги, кривой код, сломанные интеграции",
            prompt,
        )

    def test_experiential_axis_owns_thesis_not_technical_rubric(self):
        body_prompt = semantic.theme_instruction(
            semantic.THEMES_BY_KEY["body"],
            platform="vk",
            rubric_name="AI без успешного успеха",
        )
        work_prompt = semantic.theme_instruction(
            semantic.THEMES_BY_KEY["work"],
            platform="vk",
            rubric_name="AI без успешного успеха",
        )
        self.assertIn("опытная ось обязана владеть центральным тезисом", body_prompt)
        self.assertIn("не подменяет вывод универсальным советом", body_prompt)
        self.assertNotIn("опытная ось обязана владеть центральным тезисом", work_prompt)

    def tearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    def test_cooldown_is_applied_before_generation_and_shared_across_platforms(self):
        events = []
        recent = ["work", "care", "conflict", "practical_future", "attention"]

        def recent_themes(*args, **kwargs):
            events.append("themes")
            return recent

        def recent_posts(*args, **kwargs):
            events.append("posts")
            return []

        async def generate(instruction):
            events.append("generate")
            return "Новый самостоятельный текст"

        with patch.object(
            main.memory, "get_recent_semantic_theme_keys", side_effect=recent_themes
        ), patch.object(
            main.memory, "get_recent_posts_for_semantic_gate", side_effect=recent_posts
        ):
            theme, result = asyncio.run(
                main.generate_semantic_autopost_candidate(
                    user_id=1,
                    platform="vk",
                    rubric_name="AI без успешного успеха",
                    seed="slot",
                    generate=generate,
                )
            )

        self.assertEqual(events[:3], ["themes", "posts", "generate"])
        self.assertNotIn(theme.key, recent)
        self.assertTrue(result.accepted)

    def test_meaning_repeat_is_rejected_without_literal_word_match(self):
        history = [
            {
                "semantic_theme": "care",
                "content": "Хороший автопилот оставляет человеку понятный способ остановить действие.",
            }
        ]
        candidate = "Умная машина ценна, пока последнее решение остаётся за живым оператором."
        response = json.dumps(
            {
                "accepted": False,
                "reason": "тот же тезис о человеческом контроле",
                "central_thesis": "автоматизация должна оставлять окончательный выбор человеку",
                "conclusion": "полная автономность без контроля опасна",
                "narrative_shape": "пример → риск → граница",
                "key_meanings": ["автоматизация", "человеческий выбор", "граница"],
            },
            ensure_ascii=False,
        )
        with patch.object(main, "call_gpt", new=AsyncMock(return_value=response)) as judge:
            decision = asyncio.run(main.evaluate_autopost_candidate(candidate, history))
        self.assertFalse(decision.accepted)
        sent_prompt = judge.await_args.args[0][1]["content"]
        self.assertIn(history[0]["content"], sent_prompt)
        self.assertIn(candidate, sent_prompt)

    def test_legacy_generated_post_without_semantic_theme_remains_readable(self):
        legacy_path = Path(self.directory.name) / "legacy.sqlite3"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.execute(
                """
                CREATE TABLE generated_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    expert_mode TEXT NOT NULL,
                    task TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    content TEXT NOT NULL,
                    image_count INTEGER NOT NULL DEFAULT 0,
                    published_to_channel INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO generated_posts(
                    user_id, expert_mode, task, topic, content,
                    image_count, published_to_channel, created_at
                ) VALUES (1, 'copywriter', 'naz_telegram_autopost', 'old', 'Старый пост', 0, 1, '2026-01-01')
                """
            )
            conn.commit()
        finally:
            conn.close()
        with patch.object(memory, "DB_PATH", str(legacy_path)):
            memory.init_db()
            posts = memory.get_recent_posts_for_semantic_gate(1, limit=8)
            with memory.db() as conn:
                columns = memory._table_columns(conn, "generated_posts")
        self.assertIn("semantic_theme", columns)
        self.assertEqual(posts[-1]["content"], "Старый пост")
        self.assertEqual(posts[-1]["semantic_theme"], "")

    def test_generation_stops_after_one_correction(self):
        generate = AsyncMock(side_effect=["Первый вариант", "Совсем другая сцена"])
        first_rejection = rejected("first duplicate")
        evaluate = AsyncMock(side_effect=[first_rejection, rejected("second duplicate")])
        correction_theme = semantic.THEMES_BY_KEY["city"]
        result = asyncio.run(
            semantic.generate_with_gate(
                generate=generate,
                evaluate=evaluate,
                theme=semantic.THEMES_BY_KEY["care"],
                correction_theme=correction_theme,
                platform="telegram",
                rubric_name="Naz после смены",
                is_model_warning=lambda text: False,
            )
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(result.theme_key, "city")
        self.assertEqual(generate.await_count, 2)
        second_prompt = generate.await_args_list[1].args[0]
        self.assertIn("(city)", second_prompt)
        self.assertIn("first duplicate", second_prompt)
        self.assertIn(first_rejection.central_thesis, second_prompt)
        self.assertIn(first_rejection.conclusion, second_prompt)
        self.assertIn(first_rejection.narrative_shape, second_prompt)
        self.assertIn("Обязательная новая конкретная сцена", second_prompt)
        self.assertIn("существенно другой самостоятельный вывод", second_prompt)
        for canned_conclusion in correction_theme.conclusions:
            self.assertNotIn(canned_conclusion, second_prompt)

    def test_correction_axis_excludes_initial_and_full_cooldown(self):
        recent = ["relationships", "work", "care", "memory", "conflict"]
        initial = semantic.select_theme(
            "AI без магии",
            recent,
            platform="vk",
            seed="slot",
        )
        correction = semantic.select_correction_theme(
            "AI без магии",
            recent,
            initial_theme_key=initial.key,
            platform="vk",
            seed="slot:correction",
        )
        self.assertNotEqual(initial.key, correction.key)
        self.assertNotIn(correction.key, recent)
        self.assertIn(
            correction.key,
            semantic.DIVERGENT_THEME_KEYS[initial.key],
        )

    def test_rejected_theme_cooldown_is_persistent_but_not_accepted_history(self):
        memory.record_rejected_semantic_theme(
            user_id=7,
            platform="vk",
            semantic_theme="memory",
            source_ref="timer:test",
        )
        self.assertEqual(
            memory.get_recent_rejected_semantic_theme_keys(7),
            ["memory"],
        )
        self.assertEqual(memory.get_recent_semantic_theme_keys(7), [])
        memory.init_db()
        self.assertEqual(
            memory.get_recent_rejected_semantic_theme_keys(7),
            ["memory"],
        )

    def test_rejected_theme_cooldown_keeps_last_ten_axes(self):
        for index, theme in enumerate(
            (
                "relationships",
                "work",
                "care",
                "memory",
                "conflict",
                "practical_future",
                "attention",
                "creativity",
                "body",
                "music",
                "game",
            )
        ):
            memory.record_rejected_semantic_theme(
                user_id=9,
                platform="vk",
                semantic_theme=theme,
                source_ref=f"timer:{index}",
            )
        recent = memory.get_recent_rejected_semantic_theme_keys(9)
        self.assertEqual(len(recent), 10)
        self.assertNotIn("relationships", recent)
        self.assertIn("game", recent)

    def test_vk_rubric_selector_skips_fully_exhausted_theme_set(self):
        dev_log_keys = {
            theme.key
            for theme in semantic.compatible_themes("Naz Dev Log")
        }
        with patch.object(main.random, "choice", side_effect=lambda items: items[0]):
            rubric = main.select_naz_vk_rubric(
                "daily",
                excluded_theme_keys=dev_log_keys,
            )
        self.assertNotEqual(rubric["name"], "Naz Dev Log")
        self.assertTrue(
            any(
                theme.key not in dev_log_keys
                for theme in semantic.compatible_themes(str(rubric["name"]))
            )
        )

    def test_two_rejections_create_no_vk_draft_or_queue_job(self):
        theme = semantic.THEMES_BY_KEY["work"]
        result = semantic.GenerationResult(False, "", 2, rejected("duplicate twice"))
        root = Path(self.directory.name) / "queue"
        (root / "pending").mkdir(parents=True)
        with patch.multiple(
            main,
            NAZ_VK_ENABLED=True,
            NAZ_VK_PUBLIC_ID="123",
            NAZ_VK_QUEUE_DIR=root,
        ), patch.object(
            main,
            "generate_semantic_autopost_candidate",
            new=AsyncMock(return_value=(theme, result)),
        ), patch.object(
            main.memory, "save_generated_post"
        ) as save_draft, patch.object(
            main, "commit_accepted_autopost_state"
        ) as save_theme, patch.object(
            main.vk_publish_queue, "enqueue"
        ) as enqueue:
            with self.assertRaises(vk_publish_queue.QueueError):
                asyncio.run(main.create_naz_vk_job("Тема", source_ref="test:reject"))
        save_draft.assert_not_called()
        save_theme.assert_not_called()
        enqueue.assert_not_called()
        self.assertEqual(list((root / "pending").iterdir()), [])

    def test_two_rejections_do_not_publish_telegram_or_save_draft(self):
        theme = semantic.THEMES_BY_KEY["work"]
        result = semantic.GenerationResult(False, "", 2, rejected("duplicate twice"))
        context = SimpleNamespace(
            job=SimpleNamespace(data={"slot": "10:00"}),
            bot=SimpleNamespace(),
        )
        with patch.multiple(
            main,
            CHANNEL_ID="@test",
            ADMIN_ID=1,
            AUTOPOST_INSIGHT_CHANCE=0.0,
        ), patch.object(
            main, "try_visual_archive_autopost", new=AsyncMock(return_value=None)
        ), patch.object(
            main, "read_naz_stories", return_value=""
        ), patch.object(
            main,
            "generate_semantic_autopost_candidate",
            new=AsyncMock(return_value=(theme, result)),
        ), patch.object(
            main, "send_post_with_images", new=AsyncMock()
        ) as publish, patch.object(
            main.memory, "save_generated_post"
        ) as save_draft, patch.object(
            main, "queue_naz_post_for_void"
        ) as exchange, patch.object(
            main, "notify_autopost_skip_once", new=AsyncMock()
        ):
            asyncio.run(main.auto_post_job(context))
        publish.assert_not_awaited()
        save_draft.assert_not_called()
        exchange.assert_not_called()

    def test_only_accepted_theme_enters_history(self):
        theme = semantic.THEMES_BY_KEY["care"]
        result = semantic.GenerationResult(True, "Принятый пост", 1, accepted())
        main.commit_accepted_autopost_state(
            user_id=7,
            topic="понятная ошибка",
            task="post",
            platform="telegram",
            source_ref="test:accepted",
            theme=theme,
            result=result,
        )
        self.assertEqual(memory.get_recent_semantic_theme_keys(7), ["care"])
        with self.assertRaises(ValueError):
            main.commit_accepted_autopost_state(
                user_id=7,
                topic="дубль",
                task="post",
                platform="vk",
                source_ref="test:rejected",
                theme=semantic.THEMES_BY_KEY["work"],
                result=semantic.GenerationResult(False, "", 2, rejected()),
            )
        self.assertEqual(memory.get_recent_semantic_theme_keys(7), ["care"])

    def test_telegram_and_vk_prompts_have_separate_platform_context(self):
        telegram = prompts.build_messages(
            user_text="тема",
            task="post",
            platform="telegram",
        )[0]["content"]
        vk = prompts.build_messages(
            user_text="тема",
            task="post",
            platform="vk",
        )[0]["content"]
        self.assertIn("output is for Telegram only", telegram)
        self.assertNotIn("Сделай самостоятельный VK-пост", telegram)
        self.assertIn("output is for VK only", vk)
        self.assertIn("Сделай самостоятельный VK-пост", vk)
        self.assertNotIn("Сделай Telegram-пост", vk)
        with patch.object(main, "call_gpt", new=AsyncMock(return_value="image prompt")) as image_model:
            asyncio.run(
                main.build_image_prompt(
                    1,
                    "topic",
                    "post",
                    platform="vk",
                )
            )
        image_system = image_model.await_args.args[0][0]["content"]
        self.assertIn("VK posts only", image_system)
        self.assertNotIn("Telegram channel posts", image_system)

    def test_character_is_voice_not_mandatory_moral(self):
        context = character_state.prompt_context(
            character_state.CharacterState(),
            character_state.plan_content(
                character_state.CharacterState(),
                [],
                topic="городской маршрут",
                platform="telegram",
            ),
        )
        instruction = semantic.theme_instruction(
            semantic.THEMES_BY_KEY["city"],
            platform="telegram",
            rubric_name="Naz после смены",
        )
        self.assertIn("взгляд и интонацию", context)
        self.assertIn("обязательной темой", instruction)
        self.assertNotIn("Но итог всегда один", prompts.NAZ_CORE_PHILOSOPHY)


if __name__ == "__main__":
    unittest.main()
