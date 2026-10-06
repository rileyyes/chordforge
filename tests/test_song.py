import unittest

from chordforge.errors import ValidationError
from chordforge.song import SectionSpec, SongStructureSpec, validate_structure


class TestValidateStructure(unittest.TestCase):
    def test_valid_minimal(self):
        r = validate_structure({"sections": [{"name": "Intro", "clips": []}]}, set())
        self.assertEqual(len(r["sections"]), 1)
        self.assertTrue(r["sections"][0]["id"])

    def test_assigns_stable_ids_and_dedupes_clips(self):
        r = validate_structure({"sections": [
            {"name": "Verse", "clips": [{"clip_id": "a", "loop": True}, {"clip_id": "a", "loop": False}]}
        ]}, {"a"})
        self.assertEqual(len(r["sections"][0]["clips"]), 1)  # deduped

    def test_rejects_unknown_clip(self):
        with self.assertRaises(ValidationError):
            validate_structure({"sections": [{"name": "V", "clips": [{"clip_id": "ghost"}]}]}, {"a"})

    def test_rejects_bad_shapes(self):
        for bad in (None, [], "x", {"sections": None}, {"sections": [1, 2]}, {"extra": 1, "sections": []},
                    {"sections": [{"cycles": 1, "name": "x", "clips": []}]}):
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(ValidationError):
                validate_structure(bad, set())

    def test_rejects_missing_name(self):
        with self.assertRaises(ValidationError):
            validate_structure({"sections": [{"clips": []}]}, set())

    def test_section_count_bounds(self):
        with self.assertRaises(ValidationError):
            validate_structure({"sections": []}, set())
        too_many = {"sections": [{"name": "X", "clips": []} for _ in range(30)]}
        with self.assertRaises(ValidationError):
            validate_structure(too_many, set())

    def test_ids_preserved_when_valid_and_unique(self):
        r = validate_structure({"sections": [{"id": "keep-me", "name": "Intro", "clips": []}]}, set())
        self.assertEqual(r["sections"][0]["id"], "keep-me")

    def test_duplicate_ids_get_replaced(self):
        r = validate_structure({"sections": [{"id": "dup", "name": "A", "clips": []},
                                             {"id": "dup", "name": "B", "clips": []}]}, set())
        ids = [s["id"] for s in r["sections"]]
        self.assertEqual(len(set(ids)), 2)


class TestSongStructureSpec(unittest.TestCase):
    def test_construction(self):
        spec = SongStructureSpec([SectionSpec("Intro"), SectionSpec("Verse")])
        self.assertEqual([s.name for s in spec.sections], ["Intro", "Verse"])
        self.assertEqual(spec.sections[0].placements, [])

    def test_empty(self):
        self.assertEqual(SongStructureSpec([]).sections, [])


if __name__ == "__main__":
    unittest.main()
