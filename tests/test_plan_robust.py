import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kickroll.plan import max_attempts, normalize_plan, validate_plan  # noqa: E402


def _plan(task):
    return normalize_plan({"schema_version": 1, "goal_summary": "x", "acceptance_files": [], "tasks": [task]})


class PlanRobustnessTest(unittest.TestCase):
    def test_non_dict_criterion_is_error_not_crash(self):
        t = {"id": "T1", "title": "a", "description": "b", "kind": "analysis", "risk": "low",
             "acceptance": ["oops"]}
        self.assertTrue(validate_plan(_plan(t)))

    def test_non_dict_criterion_in_code_task(self):
        t = {"id": "T1", "title": "a", "description": "b", "kind": "code", "risk": "low", "writes": ["pkg/a.py"],
             "acceptance": ["oops", {"id": "A1", "type": "command", "cmd": ["python", "-m", "unittest", "tests.test_a"]}]}
        self.assertTrue(validate_plan(_plan(t)))

    def test_writes_string_is_error(self):
        t = {"id": "T1", "title": "a", "description": "b", "kind": "code", "risk": "low", "writes": "pkg/a.py",
             "acceptance": [{"id": "A1", "type": "command", "cmd": ["python", "-m", "unittest", "tests.test_a"]}]}
        errors = validate_plan(_plan(t))
        self.assertIn("writes must be a list of strings", " ".join(errors))

    def test_non_string_description(self):
        t = {"id": "T1", "title": "a", "description": 5, "kind": "analysis", "risk": "low",
             "acceptance": [{"id": "J1", "type": "judgement", "text": "ok"}]}
        errors = validate_plan(_plan(t))
        self.assertTrue(any("description" in e for e in errors), errors)

    def test_valid_plan_still_valid(self):
        t = {"id": "T1", "title": "a", "description": "b", "kind": "analysis", "risk": "low",
             "acceptance": [{"id": "J1", "type": "judgement", "text": "ok"}]}
        self.assertEqual(validate_plan(_plan(t)), [])

    def test_max_attempts_bad_values(self):
        self.assertEqual(max_attempts({"risk": "low", "budget": {"max_attempts": "abc"}}), 3)
        self.assertEqual(max_attempts({"risk": "high", "budget": None}), 4)
        self.assertEqual(max_attempts({"risk": "low", "budget": "x"}), 3)
        self.assertEqual(max_attempts({"risk": "low", "budget": {"max_attempts": None}}), 3)
        self.assertEqual(max_attempts({"budget": {"max_attempts": 0}}), 1)
        self.assertEqual(max_attempts({"budget": {"max_attempts": 2}}), 2)


if __name__ == "__main__":
    unittest.main()
