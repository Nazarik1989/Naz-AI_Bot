import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class AgentContentProjectDirectoryTests(unittest.TestCase):
    def write_material(
        self, inbox: Path, project: str, date_text: str, name: str, body: str,
    ) -> Path:
        target = inbox / project / date_text / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        return target

    def test_explicit_date_finds_project_first_material(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            self.write_material(
                inbox,
                "Naz_AI_Bot_clean",
                "2026-07-25",
                "2026-07-25--new-topic--t-example.md",
                "project-first unique material",
            )

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox):
                context, risks, date_text = main.collect_agent_materials("2026-07-25")

            self.assertEqual("2026-07-25", date_text)
            self.assertEqual([], risks)
            self.assertIn("Naz_AI_Bot_clean/2026-07-25", context)
            self.assertIn("project-first unique material", context)

    def test_same_date_in_unrelated_projects_is_not_collected_for_naz(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            self.write_material(inbox, "Agent", "2026-07-25", "agent.md", "agent material")
            self.write_material(inbox, "Varvara_Landing", "2026-07-25", "varvara.md", "varvara material")

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox):
                context, _, _ = main.collect_agent_materials("2026-07-25")
                day_dirs = main.agent_content_dirs_for_date("2026-07-25")

            self.assertEqual(2, len(day_dirs))
            self.assertEqual("", context)

    def test_naz_project_is_preferred_over_unrelated_same_date_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            self.write_material(
                inbox, "Naz_AI_Bot_clean", "2026-07-25", "naz.md", "naz material"
            )
            self.write_material(
                inbox, "Void-entity", "2026-07-25", "void.md", "void material"
            )

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox):
                context, _, _ = main.collect_agent_materials("2026-07-25")
                selected = main.agent_content_source_dirs_for_date("2026-07-25")

            self.assertEqual(["Naz_AI_Bot_clean"], [path.parent.name for path in selected])
            self.assertIn("naz material", context)
            self.assertNotIn("void material", context)

    def test_random_sync_date_catalog_contains_only_naz_project_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            self.write_material(
                inbox, "Naz_AI_Bot_clean", "2026-07-11", "naz.md", "naz material"
            )
            self.write_material(
                inbox, "Void-entity", "2026-07-12", "void.md", "void material"
            )

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox):
                self.assertEqual(["2026-07-11"], main.list_agent_content_dates())

    def test_legacy_all_project_seen_hash_does_not_requeue_unchanged_naz_date(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            self.write_material(
                inbox, "Naz_AI_Bot_clean", "2026-07-11", "naz.md", "before"
            )
            sibling = self.write_material(
                inbox, "Void-entity", "2026-07-11", "void.md", "void material"
            )
            state_file = Path(directory) / "seen.json"

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox), patch.object(
                main, "AGENT_CONTENT_STATE_FILE", state_file
            ), patch.object(
                main, "current_bot_date", return_value="2026-07-29"
            ), patch.object(
                main, "AGENT_CONTENT_REUSE_SEEN", False
            ):
                legacy_seen = main.legacy_agent_content_hash_for_date("2026-07-11")
                main.write_json_file(state_file, {"2026-07-11": legacy_seen})

                self.assertEqual(
                    main.choose_agent_content_date_for_sync(), "2026-07-29"
                )
                migrated = main.read_json_file(state_file, {})["2026-07-11"]
                self.assertEqual(
                    migrated, main.agent_content_hash_for_date("2026-07-11")
                )

                sibling.write_text("unrelated change", encoding="utf-8")
                self.assertTrue(
                    main.agent_content_seen_matches("2026-07-11", migrated)
                )
                self.assertEqual(
                    main.choose_agent_content_date_for_sync(), "2026-07-29"
                )

    def test_legacy_date_directory_remains_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            legacy = inbox / "2026-07-24"
            legacy.mkdir(parents=True)
            (legacy / "2026-07-24.md").write_text(
                "legacy material", encoding="utf-8",
            )

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox), patch.object(
                main, "AGENT_CONTENT_PROJECT", ""
            ):
                context, _, date_text = main.collect_agent_materials("2026-07-24")

            self.assertEqual("2026-07-24", date_text)
            self.assertIn("legacy material", context)

    def test_project_names_are_not_returned_as_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            self.write_material(inbox, "Naz_AI_Bot_clean", "2026-07-25", "topic.md", "body")
            (inbox / "Agent").mkdir(exist_ok=True)

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox):
                self.assertEqual(["2026-07-25"], main.list_agent_content_dates())
                self.assertEqual([], main.agent_content_dirs_for_date("Naz_AI_Bot_clean"))

    def test_date_hash_changes_when_nested_material_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            document = self.write_material(
                inbox, "Naz_AI_Bot_clean", "2026-07-25", "topic.md", "before",
            )

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox):
                before = main.agent_content_hash_for_date("2026-07-25")
                document.write_text("after", encoding="utf-8")
                after = main.agent_content_hash_for_date("2026-07-25")

            self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
