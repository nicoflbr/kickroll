"""Per-model usage: counters in the accountant end up in the public status file."""
import unittest

import helpers  # noqa: F401  (puts the project root on sys.path)
from kickroll.budget import Accountant
from kickroll.config import Config
from kickroll.runner import ClaudeResult
from kickroll.status_file import build_status, validate_status


def result(duration_s: float, **usage) -> ClaudeResult:
    return ClaudeResult(duration_s=duration_s, usage=usage)


class UsageStatusTest(unittest.TestCase):
    def setUp(self):
        self.counters: dict = {}
        self.acct = Accountant(Config(), self.counters)

    def test_calls_tokens_and_seconds_are_counted_per_model(self):
        self.acct.record("worker", "haiku")
        self.acct.record_result("haiku", result(2.5, input_tokens=2, cache_creation_input_tokens=100,
                                                cache_read_input_tokens=50, output_tokens=8))
        self.acct.record("worker", "haiku")
        self.acct.record_result("haiku", result(1.0, input_tokens=1, output_tokens=4))
        self.acct.record("reviewer", "opus")
        self.acct.record_result("opus", result(10.0, output_tokens=30))

        by_model = self.counters["by_model"]
        self.assertEqual(by_model["haiku"], {"calls": 2, "tokens": 165, "seconds": 3.5})
        self.assertEqual(by_model["opus"], {"calls": 1, "tokens": 30, "seconds": 10.0})
        self.assertNotIn("sonnet", by_model)

    def test_result_without_usage_does_not_break_counting(self):
        self.acct.record("worker", "sonnet")
        self.acct.record_result("sonnet", ClaudeResult(duration_s=0.0))
        self.assertEqual(self.counters["by_model"]["sonnet"], {"calls": 1, "tokens": 0, "seconds": 0.0})

    def test_status_file_reports_usage_for_all_three_models(self):
        self.acct.record("worker", "sonnet")
        self.acct.record_result("sonnet", result(4.4, input_tokens=10, output_tokens=5))
        status = build_status({"run_id": "r1", "status": "RUNNING", "phase": "EXECUTE",
                               "budget": self.counters, "limits": {}, "plan": {"tasks": []}, "tasks": {}})
        self.assertEqual(status["usage"]["sonnet"], {"calls": 1, "tokens": 15, "seconds": 4})
        self.assertEqual(status["usage"]["haiku"], {"calls": 0, "tokens": 0, "seconds": 0})
        self.assertEqual(set(status["usage"]), {"haiku", "sonnet", "opus"})
        validate_status(status)

    def test_status_without_any_calls_still_validates(self):
        status = build_status({"run_id": "r2", "status": "RUNNING", "phase": "PREFLIGHT"})
        self.assertEqual(status["usage"]["opus"]["calls"], 0)

    def test_invalid_usage_is_rejected(self):
        status = build_status({"run_id": "r3", "status": "RUNNING", "phase": "PREFLIGHT"})
        status["usage"]["haiku"]["tokens"] = "many"
        with self.assertRaises(ValueError):
            validate_status(status)


if __name__ == "__main__":
    unittest.main()
