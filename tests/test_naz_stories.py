import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class NazStorySourcesTests(unittest.TestCase):
    def test_primary_and_extra_story_files_are_combined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary.md"
            extra = root / "extra.md"
            primary.write_text("Первая история", encoding="utf-8")
            extra.write_text("Вторая история", encoding="utf-8")
            with patch.object(main, "NAZ_STORIES_FILE", primary), patch.object(
                main, "NAZ_STORIES_EXTRA_FILES", (extra,)
            ):
                text = main.read_naz_stories()
            self.assertIn("Первая история", text)
            self.assertIn("Вторая история", text)

    def test_missing_source_does_not_hide_available_story(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            available = root / "extra.md"
            available.write_text("Доступная история", encoding="utf-8")
            with patch.object(main, "NAZ_STORIES_FILE", root / "missing.md"), patch.object(
                main, "NAZ_STORIES_EXTRA_FILES", (available,)
            ):
                self.assertEqual(main.read_naz_stories(), "Доступная история")

    def test_duplicate_paths_are_read_once(self):
        with tempfile.TemporaryDirectory() as directory:
            story = Path(directory) / "story.md"
            story.write_text("Одна история", encoding="utf-8")
            with patch.object(main, "NAZ_STORIES_FILE", story), patch.object(
                main, "NAZ_STORIES_EXTRA_FILES", (story, story)
            ):
                self.assertEqual(main.naz_story_files(), (story,))
                self.assertEqual(main.read_naz_stories(), "Одна история")

    def test_repository_default_includes_second_story_file(self):
        self.assertIn(Path("naz_stories_2.md"), main.NAZ_STORIES_EXTRA_FILES)
        self.assertTrue(Path("naz_stories_2.md").is_file())


if __name__ == "__main__":
    unittest.main()
