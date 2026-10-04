import unittest

from image_review.status import MARK_MODES, VERDICTS, parse_choice


class ParseChoiceTest(unittest.TestCase):
    def test_returns_the_member(self):
        self.assertEqual(parse_choice("CLEAN", VERDICTS), "CLEAN")
        self.assertEqual(parse_choice("grid", MARK_MODES), "grid")

    def test_non_members_are_none(self):
        for value in ("clean", "", "single", None, 1, b"CLEAN"):
            with self.subTest(value=value):
                self.assertIsNone(parse_choice(value, VERDICTS))


if __name__ == "__main__":
    unittest.main()
