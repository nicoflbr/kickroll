import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kickroll.safety import redact  # noqa: E402


class RedactHomeTest(unittest.TestCase):
    def test_home_all_slash_variants(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\someone"}):
            out = redact("see C:/Users/someone/proj/a.py and C:\\Users\\someone\\b.txt and c:/users/SOMEONE/c")
        self.assertNotIn("someone", out.lower())
        self.assertIn("~/proj/a.py", out)
        self.assertIn("~\\b.txt", out)

    def test_plain_text_unchanged(self):
        with mock.patch.dict(os.environ, {"USERPROFILE": "C:\\Users\\someone"}):
            self.assertEqual(redact("hello world"), "hello world")


if __name__ == "__main__":
    unittest.main()
