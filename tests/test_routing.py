"""Routing backends without Jev: planner annotations, heuristics, opt-in Haiku bypass, config."""
import os
import unittest
from unittest import mock

from helpers import ROOT, TempDirCase, code_task, make_cfg, make_plan, make_repo

from kickroll.config import ConfigError, home_dir, load_config
from kickroll.engine import Orchestrator
from kickroll.jev import JevService
from kickroll.plan import validate_plan
from kickroll.routing import (HaikuDecider, heuristic_bypass, heuristic_route, planner_route, resolve_backend)
from kickroll.runner import FakeRunner, FakeStep


class BackendSelectionTest(unittest.TestCase):
    def test_auto_without_key_uses_planner(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_backend("auto")[0], "planner")
            self.assertEqual(resolve_backend("jev")[0], "planner")
        self.assertEqual(resolve_backend("heuristic")[0], "heuristic")
        self.assertEqual(resolve_backend("haiku")[0], "planner")

    def test_auto_with_key_and_sdk_uses_jev(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "dummy"}), \
                mock.patch("kickroll.routing.importlib.util.find_spec", return_value=object()):
            self.assertEqual(resolve_backend("auto"), ("jev", ""))
            self.assertEqual(resolve_backend("auto", jev_enabled=False)[0], "planner")

    def test_auto_without_sdk(self):
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "dummy"}), \
                mock.patch("kickroll.routing.importlib.util.find_spec", return_value=None):
            self.assertEqual(resolve_backend("auto")[0], "planner")


class RouteRulesTest(unittest.TestCase):
    def test_planner_route_table(self):
        t = {"risk": "low", "complexity": 0.3, "suggested_tier": "haiku"}
        self.assertEqual(planner_route(t)[0], "haiku")
        self.assertEqual(planner_route({**t, "suggested_tier": "sonnet"})[0], "sonnet")
        self.assertEqual(planner_route({**t, "complexity": 1.7})[0], "opus")
        self.assertEqual(planner_route({**t, "risk": "high"})[0], "opus")
        self.assertEqual(planner_route({"risk": "low", "description": "x"})[0], "haiku")  # no fields -> heuristic

    def test_heuristic_route(self):
        self.assertEqual(heuristic_route({"risk": "low", "writes": ["a.py"], "description": "fix typo"})[0], "haiku")
        self.assertEqual(heuristic_route({"risk": "medium", "writes": ["a.py"]})[0], "sonnet")
        self.assertEqual(heuristic_route({"risk": "high"})[0], "opus")
        big = {"risk": "medium", "writes": [f"f{i}.py" for i in range(6)], "description": "w " * 300}
        self.assertEqual(heuristic_route(big)[0], "opus")

    def test_heuristic_bypass(self):
        self.assertLess(heuristic_bypass("fix the typo")["est_subtasks"], 1.5)
        self.assertGreaterEqual(heuristic_bypass("- build A\n- build B\n- add tests")["est_subtasks"], 1.5)

    def test_service_without_jev(self):
        svc = JevService(None, backend="planner")
        task = {"risk": "low", "complexity": 0.2, "suggested_tier": "haiku", "description": "x"}
        self.assertEqual(svc.route_task(task)[0], "haiku")
        self.assertEqual(svc.verify(task, "s", "d", "low"), ("unavailable", {}))
        d = svc.bypass("fix a typo", True)
        self.assertEqual((d.source, d.use_orchestrator, d.jev_ok), ("heuristic", False, False))
        self.assertTrue(JevService(None, backend="heuristic").bypass("- a\n- b\n- c", True).use_orchestrator)

    def test_plan_annotations_validated(self):
        bad = code_task(complexity=5, suggested_tier="gpt")
        errs = validate_plan(make_plan([bad]))
        self.assertTrue(any("complexity" in e for e in errs))
        self.assertTrue(any("suggested_tier" in e for e in errs))
        self.assertEqual(validate_plan(make_plan([code_task(complexity=0.5, suggested_tier="haiku")])), [])


class HaikuDeciderTest(TempDirCase):
    def test_cache_by_hash_and_fallback(self):
        runner = FakeRunner({"router": FakeStep(structured={"est_subtasks": 2.5, "risk": "low", "complexity": 0.4})})
        d = HaikuDecider(runner, self.tmp / "cache.json", str(self.tmp))
        self.assertEqual(d.decide("Build A and B")["est_subtasks"], 2.5)
        self.assertEqual(d.decide("Build A and B")["est_subtasks"], 2.5)
        self.assertEqual(len(runner.calls), 1)  # cached
        call = runner.calls[0]
        self.assertEqual((call.model, call.timeout_s, call.tools), ("haiku", 30.0, ""))
        self.assertIsNotNone(call.json_schema)
        self.assertNotIn("Build A and B", (self.tmp / "cache.json").read_text())  # hash only
        broken = HaikuDecider(FakeRunner({"router": FakeStep(error="TIMEOUT")}), self.tmp / "c2.json", str(self.tmp))
        self.assertIsNone(broken.decide("x"))
        svc = JevService(None, backend="planner", bypass_decider=broken)
        self.assertEqual(svc.bypass("fix typo", True).source, "heuristic")


class ConfigTest(TempDirCase):
    def test_example_config_loads(self):
        cfg = load_config(ROOT / "kickroll.example.toml")
        self.assertEqual((cfg.planner_model, cfg.max_planner_calls, cfg.routing_backend), ("opus", 2, "auto"))
        self.assertFalse(cfg.jev_send_diffs)

    def test_unknown_key_and_backend(self):
        p = self.tmp / "c.toml"
        p.write_text("[budget]\nmax_call = 3\n")
        with self.assertRaises(ConfigError):
            load_config(p)
        p.write_text('[routing]\nbackend = "magic"\n')
        with self.assertRaises(ConfigError):
            load_config(p)

    def test_home_env(self):
        with mock.patch.dict(os.environ, {"KICKROLL_HOME": str(self.tmp / "h")}):
            self.assertEqual(home_dir(), self.tmp / "h")
        rest = {k: v for k, v in os.environ.items() if k != "KICKROLL_HOME"}
        with mock.patch.dict(os.environ, rest, clear=True):
            self.assertEqual(home_dir().name, ".kickroll")


class EngineWithoutJevTest(TempDirCase):
    def test_full_run_without_jev(self):
        repo = make_repo(self.tmp)
        cfg = make_cfg(self.tmp)
        t = code_task(risk="low", complexity=0.2, suggested_tier="haiku")
        runner = FakeRunner({"T1:*": FakeStep(files={"pkg/calc.py": "def add(a, b):\n    return a + b\n"})})
        o = Orchestrator(cfg, repo, runner, JevService(None, backend="planner"), plan=make_plan([t]),
                         status_writer=lambda s: None)
        self.assertEqual(o.start(), "SUCCESS")
        self.assertEqual(runner.calls[0].model, "haiku")


if __name__ == "__main__":
    unittest.main()
