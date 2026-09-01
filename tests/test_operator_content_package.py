from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import operator_content_package as ocp


TIMINGS = ((0, 3), (3, 7), (7, 12), (12, 17), (17, 23), (23, 32), (32, 38), (38, 43), (43, 47))


def valid_package(*, request_id: str = "operator-request-0001") -> dict:
    return {
        "schema_version": ocp.SCHEMA_VERSION,
        "package_id": "ocp-" + "a" * 24,
        "operator_request_id": request_id,
        "source_provenance": {
            "kind": "editorial-development-log-adaptation",
            "source_date": "2026-08-08",
            "source_title": "future_self_bot дневной чат проекта",
            "source_document_sha256": "b" * 64,
            "editorial_basis": "Редакционно подготовленная адаптация дневного журнала разработки.",
        },
        "editorial_disclaimer": (
            "Материал основан на дневном чате проекта. Это журнал разработки, "
            "а не независимый аудит репозитория."
        ),
        "approved_fact_map": [
            {
                "fact_id": f"F{index}",
                "statement": f"Подтверждённый редакционный факт {index}.",
                "publication_status": "required" if index == 7 else "approved",
                "restriction": f"Ограничение {index}.",
            }
            for index in range(1, 8)
        ],
        "prohibited_claims": ["Система абсолютно безопасна."],
        "title": "Сообщение, которое бот не отправил",
        "story_post_adaptation": "Технический разбор двух невидимых границ без заявления о релизе.",
        "reel": {
            "duration_seconds": 47,
            "format": "9:16/1080x1920/30fps",
            "scenes": [
                {
                    "scene_id": f"scene-{index:02d}",
                    "order": index,
                    "start_second": start,
                    "end_second": end,
                    "screen_text": f"Экранный текст {index}",
                    "visual": f"Синтетический визуальный план {index} без приватных данных.",
                    "fact_refs": [f"F{min(index, 7)}"],
                }
                for index, (start, end) in enumerate(TIMINGS, start=1)
            ],
        },
        "voice_over": "Безопасный полный voice-over редакционного пакета.",
        "caption": "Безопасный caption без запрещённых утверждений.",
        "cover_brief": {
            "text": "ГОТОВО. НО НЕ ДО КОНЦА.",
            "subtitle": "Две невидимые границы в AI-боте",
            "composition": "Одно синтетическое сообщение на графитовом фоне.",
            "alt_text": "Тёмная обложка с макетом одного сообщения.",
        },
        "music_brief": {
            "brief": "Минималистичная электроника без вокала.",
            "tempo_bpm": [92, 98],
            "mood": "Собранное техническое расследование.",
            "excluded": ["massive drop", "вокальные chops"],
            "track": None,
        },
        "rights_status": ocp.RIGHTS_UNCLEAR,
        "publication_restrictions": [
            "no_automatic_publication",
            "no_music_without_rights_approval",
            "second_admin_action_required",
            "no_claim_of_production_release",
        ],
    }


class OperatorPackageContractTests(unittest.TestCase):
    def test_exact_happy_path(self):
        package = valid_package()
        self.assertIs(ocp.validate_package(package), package)
        self.assertEqual(len(package["reel"]["scenes"]), 9)
        self.assertEqual(package["reel"]["duration_seconds"], 47)

    def test_missing_disclaimer_rejected(self):
        package = valid_package()
        package["editorial_disclaimer"] = "Редакционная адаптация."
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_editorial_disclaimer_missing"):
            ocp.validate_package(package)

    def test_extra_field_rejected(self):
        package = valid_package()
        package["extra"] = True
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_package_top_level_invalid"):
            ocp.validate_package(package)

    def test_nested_extra_field_rejected(self):
        package = valid_package()
        package["reel"]["extra"] = True
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_reel_invalid"):
            ocp.validate_package(package)

    def test_wrong_scalar_type_rejected(self):
        package = valid_package()
        package["reel"]["duration_seconds"] = True
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_reel_duration_invalid"):
            ocp.validate_package(package)

    def test_dict_subclass_rejected(self):
        class ForgedDict(dict):
            pass
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_package_top_level_invalid"):
            ocp.validate_package(ForgedDict(valid_package()))

    def test_duplicate_fact_id_rejected(self):
        package = valid_package()
        package["approved_fact_map"][1]["fact_id"] = "F1"
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_fact_id_duplicate"):
            ocp.validate_package(package)

    def test_duplicate_scene_id_rejected(self):
        package = valid_package()
        package["reel"]["scenes"][1]["scene_id"] = "scene-01"
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_scene_id_duplicate"):
            ocp.validate_package(package)

    def test_non_contiguous_timing_rejected(self):
        package = valid_package()
        package["reel"]["scenes"][3]["start_second"] = 16
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_scene_timing_invalid"):
            ocp.validate_package(package)

    def test_duration_mismatch_rejected(self):
        package = valid_package()
        package["reel"]["duration_seconds"] = 48
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_reel_duration_mismatch"):
            ocp.validate_package(package)

    def test_unknown_fact_reference_rejected(self):
        package = valid_package()
        package["reel"]["scenes"][0]["fact_refs"] = ["F8"]
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_scene_fact_refs_invalid"):
            ocp.validate_package(package)

    def test_prohibited_claim_in_public_text_rejected(self):
        package = valid_package()
        package["caption"] += " Система абсолютно безопасна."
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_prohibited_claim_present"):
            ocp.validate_package(package)

    def test_rights_status_absent_rejected(self):
        package = valid_package()
        del package["rights_status"]
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_package_top_level_invalid"):
            ocp.validate_package(package)

    def test_rights_status_must_stay_unclear(self):
        package = valid_package()
        package["rights_status"] = "CLEARED"
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_rights_status_invalid"):
            ocp.validate_package(package)

    def test_unclear_rights_forbid_track(self):
        package = valid_package()
        package["music_brief"]["track"] = "unverified.mp3"
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_music_track_forbidden"):
            ocp.validate_package(package)

    def test_json_multiple_values_rejected(self):
        raw = ocp.canonical_package_bytes(valid_package()) + b"{}"
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_package_json_invalid"):
            ocp.parse_json_package(raw)

    def test_preview_has_exact_product_values(self):
        text = ocp.preview_text(valid_package())
        self.assertIn("Сообщение, которое бот не отправил", text)
        self.assertIn("47 секунд", text)
        self.assertIn("Сцен: 9", text)
        self.assertIn("UNCLEAR_DO_NOT_USE", text)
        self.assertIn("Музыка не выбрана", text)

    def test_script_contains_scene_voiceover_caption_and_cover(self):
        text = ocp.script_text(valid_package())
        self.assertIn("СЦЕН-ПЛАН", text)
        self.assertIn("VOICE-OVER", text)
        self.assertIn("CAPTION", text)
        self.assertIn("ГОТОВО. НО НЕ ДО КОНЦА.", text)

    def test_existing_media_pipeline_rejects_nine_scene_47_second_package(self):
        self.assertFalse(ocp.media_pipeline_compatible(valid_package()))


class OperatorPackagePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "operator-store"

    def tearDown(self):
        self.temp.cleanup()

    def test_non_admin_import_rejected_before_write(self):
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_package_admin_required"):
            ocp.import_package(self.root, valid_package(), operator_id=41, expected_operator_id=42)
        self.assertFalse(self.root.exists())

    def test_exact_duplicate_is_byte_idempotent(self):
        package = valid_package()
        first = ocp.import_package(self.root, package, operator_id=42, expected_operator_id=42)
        before = first.package_path.read_bytes()
        second = ocp.import_package(self.root, package, operator_id=42, expected_operator_id=42)
        self.assertTrue(first.created)
        self.assertFalse(second.created)
        self.assertEqual(first.package_path, second.package_path)
        self.assertEqual(before, second.package_path.read_bytes())

    def test_divergent_duplicate_request_conflicts_without_clobber(self):
        package = valid_package()
        first = ocp.import_package(self.root, package, operator_id=42, expected_operator_id=42)
        before = first.package_path.read_bytes()
        divergent = valid_package()
        divergent["package_id"] = "ocp-" + "c" * 24
        divergent["title"] = "Другой пакет"
        with self.assertRaisesRegex(ocp.OperatorPackageConflict, "operator_request_conflict"):
            ocp.import_package(self.root, divergent, operator_id=42, expected_operator_id=42)
        self.assertEqual(before, first.package_path.read_bytes())

    def test_callback_is_bound_to_admin_package_digest_and_request(self):
        imported = ocp.import_package(self.root, valid_package(), operator_id=42, expected_operator_id=42)
        data = ocp.callback_data(imported, "script")
        binding = ocp.resolve_callback(self.root, data, operator_id=42, expected_operator_id=42)
        self.assertEqual(binding.package_digest, imported.package_digest)
        self.assertEqual(binding.operator_request_id, imported.operator_request_id)
        self.assertEqual(binding.action, "script")

    def test_non_admin_callback_rejected(self):
        imported = ocp.import_package(self.root, valid_package(), operator_id=42, expected_operator_id=42)
        with self.assertRaisesRegex(ocp.OperatorPackageError, "operator_package_admin_required"):
            ocp.resolve_callback(self.root, ocp.callback_data(imported, "build"), operator_id=41, expected_operator_id=42)

    def test_tampered_package_rejected_by_callback(self):
        imported = ocp.import_package(self.root, valid_package(), operator_id=42, expected_operator_id=42)
        imported.package_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ocp.OperatorPackageError):
            ocp.resolve_callback(self.root, ocp.callback_data(imported, "script"), operator_id=42, expected_operator_id=42)

    def test_private_artifacts_and_no_unrelated_roots(self):
        imported = ocp.import_package(self.root, valid_package(), operator_id=42, expected_operator_id=42)
        self.assertTrue(imported.package_path.is_file())
        self.assertEqual({item.name for item in self.root.iterdir()}, {"packages", "requests", "callbacks"})


class TelegramBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_keyboard_has_exact_actions(self):
        import main
        with tempfile.TemporaryDirectory() as raw:
            imported = ocp.import_package(Path(raw), valid_package(), operator_id=42, expected_operator_id=42)
            keyboard = main.operator_package_preview_keyboard(imported)
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(labels, ["Собрать Reel", "Показать сценарий", "Пропустить"])

    async def test_ready_reel_keyboard_has_second_action_controls(self):
        import main
        with tempfile.TemporaryDirectory() as raw:
            imported = ocp.import_package(Path(raw), valid_package(), operator_id=42, expected_operator_id=42)
            keyboard = main.operator_package_ready_reel_keyboard(imported)
        labels = [button.text for row in keyboard.inline_keyboard for button in row]
        self.assertEqual(labels, ["Опубликовать", "Переделать", "Отменить"])

    async def test_non_admin_document_rejected_before_download(self):
        import main
        message = SimpleNamespace(
            document=SimpleNamespace(file_size=100, file_name="package.md", file_id="id", file_unique_id="u"),
            reply_text=AsyncMock(),
            message_id=1,
        )
        update = SimpleNamespace(effective_user=SimpleNamespace(id=41), message=message)
        context = SimpleNamespace(bot=SimpleNamespace(get_file=AsyncMock()))
        with patch.object(main, "ADMIN_ID", 42):
            await main.operator_content_package_document(update, context)
        context.bot.get_file.assert_not_awaited()
        message.reply_text.assert_awaited_once()

    async def test_script_callback_uses_no_provider_or_publication(self):
        import main
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            imported = ocp.import_package(root, valid_package(), operator_id=42, expected_operator_id=42)
            query = SimpleNamespace(data=ocp.callback_data(imported, "script"), answer=AsyncMock())
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42))
            bot = SimpleNamespace(send_message=AsyncMock())
            context = SimpleNamespace(bot=bot)
            with patch.object(main, "ADMIN_ID", 42), patch.object(main, "NAZ_OPERATOR_CONTENT_PACKAGE_ROOT", root):
                await main.operator_content_package_callback(update, context)
            query.answer.assert_awaited_once()
            self.assertGreaterEqual(bot.send_message.await_count, 1)

    async def test_build_callback_fails_closed_before_media_provider(self):
        import main
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            imported = ocp.import_package(root, valid_package(), operator_id=42, expected_operator_id=42)
            query = SimpleNamespace(data=ocp.callback_data(imported, "build"), answer=AsyncMock())
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42))
            bot = SimpleNamespace(send_message=AsyncMock())
            with patch.object(main, "ADMIN_ID", 42), patch.object(main, "NAZ_OPERATOR_CONTENT_PACKAGE_ROOT", root):
                await main.operator_content_package_callback(update, SimpleNamespace(bot=bot))
            self.assertIn("не запущена", bot.send_message.await_args.kwargs["text"])

    async def test_publish_callback_never_calls_publication(self):
        import main
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            imported = ocp.import_package(root, valid_package(), operator_id=42, expected_operator_id=42)
            query = SimpleNamespace(data=ocp.callback_data(imported, "publish"), answer=AsyncMock())
            update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=42))
            bot = SimpleNamespace(send_message=AsyncMock())
            with patch.object(main, "ADMIN_ID", 42), patch.object(main, "NAZ_OPERATOR_CONTENT_PACKAGE_ROOT", root):
                await main.operator_content_package_callback(update, SimpleNamespace(bot=bot))
            self.assertIn("Автоматическая публикация", bot.send_message.await_args.kwargs["text"])


class IsolationContractTests(unittest.TestCase):
    def test_module_has_no_normalizer_provider_or_publication_import(self):
        source = Path(ocp.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import narrative_normalizer", source)
        self.assertNotIn("import narrative_normalizer_provider", source)
        self.assertNotIn("publish_to_channel", source)
        self.assertNotIn("OpenAI", source)

    def test_canonical_digest_changes_with_request(self):
        first = valid_package(request_id="operator-request-0001")
        second = valid_package(request_id="operator-request-0002")
        self.assertNotEqual(ocp.package_digest(first), ocp.package_digest(second))

    def test_roundtrip_is_exact_plain_json(self):
        package = valid_package()
        parsed = ocp.parse_json_package(ocp.canonical_package_bytes(package))
        self.assertEqual(parsed, package)
        self.assertEqual(ocp.canonical_package_bytes(parsed), ocp.canonical_package_bytes(package))


if __name__ == "__main__":
    unittest.main()
