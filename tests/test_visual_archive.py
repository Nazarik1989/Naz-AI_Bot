import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import visual_archive


class VisualArchiveTests(unittest.TestCase):
    def test_requires_approval_and_tracks_used_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "selected_originals" / "naz_core" / "one.jpg"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image")
            manifest = root / "publication_candidates.json"
            manifest.write_text(
                json.dumps(
                    [
                        {
                            "id": "one",
                            "rubric": "naz_core",
                            "curated_original_copy": "selected_originals/naz_core/one.jpg",
                            "review_status": "approved",
                            "publication_status": "not_published",
                            "edited_versions": [],
                            "ocr_text": "AI content system",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            state = root / "seen.json"
            with patch("random.choice", side_effect=lambda items: items[0]):
                candidate = visual_archive.choose_candidate(manifest, state, root, require_approved=True)
            self.assertEqual(candidate["id"], "one")
            visual_archive.mark_used(state, "one")
            self.assertIsNone(visual_archive.choose_candidate(manifest, state, root, require_approved=True))

    def test_prefers_edited_version_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / "selected_originals" / "one.jpg"
            edited = root / "edited" / "one-v1.png"
            original.parent.mkdir(parents=True)
            edited.parent.mkdir(parents=True)
            original.write_bytes(b"original")
            edited.write_bytes(b"edited")
            candidate = {
                "curated_original_copy": "selected_originals/one.jpg",
                "edited_versions": [{"path": "edited/one-v1.png"}],
            }
            self.assertEqual(visual_archive.preferred_image_path(root, candidate), edited)


if __name__ == "__main__":
    unittest.main()
