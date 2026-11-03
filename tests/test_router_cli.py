import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from helpers import ROOT, TempDirCase, make_repo

from kickroll import cli as orch
from kickroll.runner import FakeStep

ROUTER_PATH = ROOT / "examples" / "router" / "router.py"


@unittest.skipUnless(ROUTER_PATH.is_file() and importlib.util.find_spec("typesafe_sdk"), "examples/router/router.py or typesafe_sdk not available")
class RouterTest(TempDirCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("router_under_test", ROUTER_PATH)
        cls.router = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.router)

    def _answers(self, choice="code", conf=0.8, complexity=1.0, deep=0.2):
        return {"task": SimpleNamespace(choice=choice, confidence=conf),
                "complexity": SimpleNamespace(score=complexity), "needs_deep_reasoning": SimpleNamespace(noul=deep)}

    def test_route_from_answers(self):
        r = self.router.route_from_answers
        self.assertEqual(r(self._answers()).tier, "medium")
        self.assertEqual(r(self._answers(choice="smalltalk")).tier, "small")
        self.assertEqual(r(self._answers(deep=0.9)).tier, "large")
        self.assertEqual(r(self._answers(conf=0.1, deep=0.9)).tier, "medium")

    def test_route_uses_route_from_answers(self):
        client = mock.Mock()
        client.system_one.return_value = SimpleNamespace(answers=self._answers(complexity=1.8))
        self.assertEqual(self.router.route("x", client).tier, "large")

    def test_find_claude_fallback_and_tuple_sort(self):
        for v in ("2.1.99", "2.1.100", "2.1.9"):
            p = self.tmp / "Claude" / "claude-code" / v / "x" / "claude.exe"
            p.parent.mkdir(parents=True)
            p.write_bytes(b"")
        env = {"CLAUDE_BIN": str(self.tmp / "missing.exe"), "APPDATA": str(self.tmp), "PATH": ""}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(self.router.shutil, "which",
                                                                             return_value=None):
            self.assertIn("2.1.100", self.router.find_claude())


class CliTest(TempDirCase):
    def _cfg(self) -> Path:
        p = self.tmp / "kickroll.toml"
        base = self.tmp.as_posix()
        p.write_text(
            "[paths]\n"
            f'runs_dir = "{base}/runs"\nworktree_root = "{base}/wt"\n'
            f'usage_path = "{base}/usage.json"\nstatus_path = "{base}/status.json"\n'
            "[checks]\n"
            'test_command = ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."]\n', encoding="utf-8")
        return p

    def run_cli(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = orch.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_help(self):
        proc = subprocess.run([sys.executable, "-m", "kickroll", "--help"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("resume", proc.stdout)

    def test_plan_and_run_fake_end_to_end(self):
        repo = make_repo(self.tmp)
        cfg = self._cfg()
        plan_path = self.tmp / "plan.json"
        code, out, err = self.run_cli("--repo", str(repo), "--config", str(cfg), "plan", "Analyze the repo",
                                      "--fake", "--out", str(plan_path))
        self.assertEqual(code, 0, err)
        self.assertIn("trusted repositories", err)
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["_meta"]["planner_calls"], 1)
        self.assertEqual(plan["_meta"]["planner_model"], "opus")
        status = json.loads((self.tmp / "status.json").read_text(encoding="utf-8"))
        self.assertEqual((status["phase"], status["status"]), ("APPROVAL", "PAUSED"))
        code, out, err = self.run_cli("--repo", str(repo), "--config", str(cfg), "run", "--plan", str(plan_path),
                                      "--dry", "--fake")
        self.assertEqual(code, 0, err)
        self.assertIn("-> sonnet", out)
        code, out, err = self.run_cli("--repo", str(repo), "--config", str(cfg), "run", "--plan", str(plan_path),
                                      "--yes", "--fake")
        self.assertEqual(code, 0, err)
        status = json.loads((self.tmp / "status.json").read_text(encoding="utf-8"))
        self.assertEqual((status["status"], status["phase"]), ("SUCCESS", "DONE"))
        self.assertEqual(status["budgets"]["planner"], {"used": 1, "max": 2})
        code, _, err = self.run_cli("--repo", str(repo), "--config", str(cfg), "clean", plan["run_id"],
                                    "--delete-branch")
        self.assertEqual(code, 0, err)
        self.assertNotIn("kickroll/", subprocess.run(["git", "branch"], cwd=repo, capture_output=True, text=True).stdout)

    def test_dirty_repo_message(self):
        repo = make_repo(self.tmp)
        (repo / "x.txt").write_text("x")
        code, _, err = self.run_cli("--repo", str(repo), "--config", str(self._cfg()), "plan", "x", "--fake")
        self.assertEqual(code, 2)
        self.assertIn("is not clean", err)

    def test_claude_missing_message(self):
        repo = make_repo(self.tmp)
        with mock.patch("kickroll.runner.find_claude", side_effect=orch.ClaudeNotFound("Claude Code missing")):
            code, _, err = self.run_cli("--repo", str(repo), "--config", str(self._cfg()), "plan", "x")
        self.assertEqual(code, 2)
        self.assertIn("Claude Code missing", err)

    def test_not_logged_in_message(self):
        repo = make_repo(self.tmp)
        fake = orch.FakeRunner({"ping": FakeStep(error="AUTH")})
        with mock.patch.object(orch, "make_runner", return_value=fake):
            code, _, err = self.run_cli("--repo", str(repo), "--config", str(self._cfg()), "plan", "x")
        self.assertEqual(code, 2)
        self.assertIn("/login", err)

    def test_invalid_plan_replan_then_abort(self):
        from kickroll.budget import Accountant
        from kickroll.config import Config
        from kickroll.planner import PlanError, create_plan
        from kickroll.runner import FakeRunner, FakeStep
        bad = {"goal_summary": "x", "tasks": [{"id": "T1", "title": "t", "description": "d", "kind": "code",
                                                "risk": "low", "writes": ["pkg/a.py"],
                                                "acceptance": [{"id": "A1", "type": "command",
                                                                "cmd": ["python", "-c", "1"]}]}]}
        runner = FakeRunner({"planner": FakeStep(structured=bad)})
        counters = {}
        cfg = Config(usage_path=str(self.tmp / "u.json"))
        with self.assertRaises(PlanError) as ctx:
            create_plan("goal", "r1", cfg, runner, Accountant(cfg, counters), str(self.tmp), "opus", [])
        self.assertEqual(len(runner.calls), 2)
        self.assertEqual(counters["planner"], 2)
        self.assertEqual(counters["opus"], 0)  # planner does not count towards the worker Opus budget
        self.assertTrue(ctx.exception.errors)
        self.assertIn("Fix these errors", runner.calls[1].prompt)

    def test_replan_succeeds(self):
        from kickroll.budget import Accountant
        from kickroll.config import Config
        from kickroll.planner import create_plan
        from kickroll.runner import FakeRunner, FakeStep, default_fake_plan
        runner = FakeRunner({"planner": [FakeStep(text="no json"), FakeStep(structured=default_fake_plan())]})
        cfg = Config(usage_path=str(self.tmp / "u.json"))
        plan = create_plan("goal", "r1", cfg, runner, Accountant(cfg, {}), str(self.tmp), "opus", [])
        self.assertEqual(plan["tasks"][0]["id"], "T1")
        self.assertEqual(runner.calls[0].model, "opus")

    def test_rejected_planner_attempts_are_reported(self):
        from kickroll.budget import Accountant
        from kickroll.config import Config
        from kickroll.planner import create_plan
        from kickroll.runner import FakeRunner, FakeStep, default_fake_plan
        runner = FakeRunner({"planner": [FakeStep(text="no json"), FakeStep(structured=default_fake_plan())]})
        cfg = Config(usage_path=str(self.tmp / "u.json"))
        seen = []
        create_plan("goal", "r1", cfg, runner, Accountant(cfg, {}), str(self.tmp), "opus", [],
                    on_invalid=lambda n, errs: seen.append((n, list(errs))))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], 1)
        self.assertIn("JSON", seen[0][1][0])


if __name__ == "__main__":
    unittest.main()
