import unittest

import helpers  # noqa: F401 - sets sys.path

from kickroll.decision import (ACCEPT, AUTH, BLOCK, ENV, FAIL, FAILED, MARK_UNCLEAR, PASS, PAUSE, RATE_LIMIT,
                               RETRY, STAGNATION, TIMEOUT, UNCLEAR, decide, is_stagnation)


class DecisionTableTest(unittest.TestCase):
    def test_table(self):
        # (attempt, signal, tier, risk, max_attempts, timeouts) -> (action, next tier, counts)
        cases = [
            ((1, PASS, "haiku", "low", 3, 0), (ACCEPT, "haiku", True)),
            ((1, FAIL, "haiku", "low", 3, 0), (RETRY, "haiku", True)),  # attempt 2: same tier + feedback
            ((2, FAIL, "haiku", "low", 3, 0), (RETRY, "sonnet", True)),  # attempt 3: one tier higher
            ((3, FAIL, "sonnet", "low", 3, 0), (FAILED, "sonnet", True)),
            ((3, FAIL, "sonnet", "high", 4, 0), (RETRY, "opus", True)),  # attempt 4 with risk=high: opus
            ((3, FAIL, "haiku", "high", 4, 0), (RETRY, "opus", True)),
            ((2, STAGNATION, "sonnet", "medium", 3, 0), (RETRY, "opus", True)),
            ((2, STAGNATION, "opus", "high", 4, 0), (FAILED, "opus", True)),
            ((1, RATE_LIMIT, "opus", "high", 4, 0), (PAUSE, "opus", False)),
            ((2, AUTH, "sonnet", "low", 3, 0), (PAUSE, "sonnet", False)),
            ((1, TIMEOUT, "sonnet", "low", 3, 1), (RETRY, "sonnet", False)),
            ((1, TIMEOUT, "sonnet", "low", 3, 3), (BLOCK, "sonnet", False)),
            ((1, ENV, "haiku", "low", 3, 0), (BLOCK, "haiku", True)),
            ((1, UNCLEAR, "haiku", "low", 3, 0), (MARK_UNCLEAR, "haiku", True)),
            ((3, FAIL, "opus", "high", 4, 0), (RETRY, "opus", True)),
        ]
        for args, (kind, tier, counts) in cases:
            a = decide(*args, max_timeouts=2)
            self.assertEqual((a.kind, a.next_tier, a.counts), (kind, tier, counts), args)

    def test_rate_limit_and_timeout_never_escalate(self):
        for sig in (RATE_LIMIT, AUTH, TIMEOUT):
            for attempt in (1, 2, 3):
                a = decide(attempt, sig, "haiku", "low", 3, 1)
                self.assertIn(a.next_tier, ("haiku",))
                self.assertFalse(a.counts)

    def test_stagnation(self):
        self.assertFalse(is_stagnation(None, ["A1"]))
        self.assertTrue(is_stagnation(["A1", "A2"], ["A2", "A1"]))
        self.assertFalse(is_stagnation(["A1"], ["A2"]))
        self.assertTrue(is_stagnation(["A1"], ["A2"], "abc", "abc"))
        self.assertFalse(is_stagnation([], []))


if __name__ == "__main__":
    unittest.main()
