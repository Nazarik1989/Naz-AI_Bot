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

    def test_same_date_is_collected_from_multiple_projects(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            self.write_material(inbox, "Agent", "2026-07-25", "agent.md", "agent material")
            self.write_material(inbox, "Varvara_Landing", "2026-07-25", "varvara.md", "varvara material")

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox):
                context, _, _ = main.collect_agent_materials("2026-07-25")
                day_dirs = main.agent_content_dirs_for_date("2026-07-25")

            self.assertEqual(2, len(day_dirs))
            self.assertIn("agent material", context)
            self.assertIn("varvara material", context)

    def test_legacy_date_directory_remains_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            inbox = Path(directory) / "agent_content"
            legacy = inbox / "2026-07-24"
            legacy.mkdir(parents=True)
            (legacy / "2026-07-24.md").write_text(
                "legacy material", encoding="utf-8",
            )

            with patch.object(main, "AGENT_CONTENT_INBOX", inbox):
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
