import time
import unittest
from types import SimpleNamespace

from helpers import route_answers

from kickroll.jev import (FakeJev, JevService, JevUnavailable, TypeSafeJev, call_with_deadline, l2_verdict,
                          normalize_answers, tier_for_task)


class TierTableTest(unittest.TestCase):
    def test_table(self):
        m = {"task_conf": 0.9, "complexity": 0.5, "simplicity": 1.5, "risk": "low"}
        self.assertEqual(tier_for_task(m)[0], "haiku")
        self.assertEqual(tier_for_task({**m, "task_conf": 0.5})[0], "sonnet")  # confidence < 0.6
        self.assertEqual(tier_for_task({**m, "simplicity": 1.0})[0], "sonnet")
        self.assertEqual(tier_for_task({**m, "risk": "medium"})[0], "sonnet")
        self.assertEqual(tier_for_task({**m, "complexity": 1.6})[0], "opus")
        self.assertEqual(tier_for_task({**m, "risk": "high"})[0], "opus")
        self.assertEqual(tier_for_task({**m, "task_conf": 0.2, "risk": "high"})[0], "sonnet")  # unsure
        self.assertEqual(tier_for_task(None)[0], "sonnet")


class ServiceTest(unittest.TestCase):
    def test_route_and_fallback(self):
        svc = JevService(FakeJev({"route": route_answers(0.3, 1.5, "low", 0.9)}))
        self.assertEqual(svc.route_task({"title": "x", "description": "y"})[0], "haiku")
        down = JevService(FakeJev(down=True))
        tier, metrics, reason = down.route_task({"title": "x"})
        self.assertEqual(tier, "sonnet")
        self.assertIn("JevUnavailable", reason)

    def test_on_call_reports_success_failure_and_duration(self):
        calls = []
        svc = JevService(FakeJev(), on_call=lambda ok, s: calls.append((ok, s)))
        svc.route_task({"title": "x", "description": "y"})
        down = JevService(FakeJev(down=True), on_call=lambda ok, s: calls.append((ok, s)))
        down.route_task({"title": "x"})
        self.assertEqual([ok for ok, _ in calls], [True, False])
        self.assertTrue(all(s >= 0 for _, s in calls))

    def test_no_jev_client_makes_no_count(self):
        calls = []
        svc = JevService(None, backend="heuristic", on_call=lambda ok, s: calls.append(ok))
        svc.route_task({"title": "x", "description": "y"})
        self.assertEqual(calls, [])

    def test_bypass(self):
        fake = FakeJev()
        svc = JevService(fake)
        self.assertTrue(svc.bypass("Build A, B and C", True).use_orchestrator)
        self.assertFalse(svc.bypass("Build A, B and C", False).use_orchestrator)  # no test command
        one = JevService(FakeJev({"bypass": {"est_subtasks": {"type": "score", "score": 0.4, "confidence": 0.9}}}))
        self.assertFalse(one.bypass("Fix a typo", True).use_orchestrator)
        down = JevService(FakeJev(down=True)).bypass("x", True)
        self.assertFalse(down.jev_ok)
        self.assertEqual(down.tier, "sonnet")

    def test_redaction_before_send(self):
        fake = FakeJev()
        svc = JevService(fake, roots=["C:\\work\\repo"])
        svc.route_task({"title": "Key sk-ant-abcdefghijklmnopqrstuvwx", "description":
                        "Path C:\\work\\repo\\src\\a.py, token=supersecretvalue123"})
        sent = fake.sent[0][1]
        self.assertNotIn("sk-ant-abcdefghijklmnopqrstuvwx", sent)
        self.assertNotIn("supersecretvalue123", sent)
        self.assertNotIn("C:\\work\\repo", sent)
        self.assertIn("src\\a.py", sent)

    def test_verify_never_sends_diff_by_default(self):
        fake = FakeJev()
        svc = JevService(fake, send_diffs=False)
        verdict, _ = svc.verify({"title": "t", "acceptance": [{"id": "J1", "type": "judgement", "text": "x"}]},
                                "Summary", "+SECRET_DIFF_CONTENT", "medium")
        self.assertNotIn("SECRET_DIFF_CONTENT", fake.sent[0][1])
        self.assertEqual(verdict, "uncertain")

    def test_l2_verdict(self):
        self.assertEqual(l2_verdict({"v4": 0.8}, [], "low", True, False), "suspect")
        self.assertEqual(l2_verdict({"v2": 0.8}, [], "low", False, False), "fail")
        # Live finding: an unsure yes/no value (about 0.5-0.66) is not a "yes" and must not fail the task.
        for unsure in (0.5, 0.56, 0.66, 0.69):
            self.assertEqual(l2_verdict({"v2": unsure}, [], "medium", False, False), "uncertain")
            self.assertEqual(l2_verdict({"v4": unsure}, [], "medium", False, False), "uncertain")
        self.assertEqual(l2_verdict({"v1_J1": 0.2}, ["J1"], "low", True, False), "fail")
        self.assertEqual(l2_verdict({"v1_J1": 0.2}, ["J1"], "low", True, True), "uncertain")  # truncated
        self.assertEqual(l2_verdict({"v1_J1": 0.9}, ["J1"], "high", True, False), "uncertain")
        self.assertEqual(l2_verdict({"v1_J1": 0.95}, ["J1"], "high", True, False), "pass")
        self.assertEqual(l2_verdict({"v1_J1": 0.99}, ["J1"], "low", False, False), "uncertain")


class TypeSafeAdapterTest(unittest.TestCase):
    def _client(self, answers, delay=0.0):
        class Client:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def system_one(self, state, questions):
                time.sleep(delay)
                return SimpleNamespace(answers=answers)
        return Client

    def test_normalizes_sdk_answers(self):
        answers = {"task": SimpleNamespace(choice="code", confidence=0.7),
                   "complexity": SimpleNamespace(score=1.2, confidence=0.6),
                   "deep": SimpleNamespace(noul=0.3)}
        jev = TypeSafeJev(client_factory=self._client(answers))
        out = jev.ask("s", {"task": {}})
        self.assertEqual(out["task"], {"type": "choice", "choice": "code", "confidence": 0.7})
        self.assertEqual(out["complexity"]["score"], 1.2)
        self.assertEqual(out["deep"], {"type": "noul", "noul": 0.3})

    def test_deadline(self):
        jev = TypeSafeJev(deadline_s=0.2, client_factory=self._client({}, delay=2.0))
        start = time.monotonic()
        with self.assertRaises(JevUnavailable):
            jev.ask("s", {})
        self.assertLess(time.monotonic() - start, 1.5)

    def test_deadline_passes_errors(self):
        def boom():
            raise ValueError("x")
        with self.assertRaises(ValueError):
            call_with_deadline(boom, 1.0)

    def test_normalize_skips_unknown(self):
        self.assertEqual(normalize_answers({"b": SimpleNamespace(box=1)}), {})


if __name__ == "__main__":
    unittest.main()
