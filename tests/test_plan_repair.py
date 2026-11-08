"""The planner often names an acceptance test it does not provide. kickroll repairs that instead of
rejecting the plan (which used to end the run with "Plan invalid (also after re-plan)")."""
import unittest

from helpers import CALC_TEST, GOOD_CALC, TempDirCase, code_task, make_cfg, make_plan, make_repo

from kickroll.budget import Accountant
from kickroll.config import Config
from kickroll.engine import Orchestrator
from kickroll.jev import FakeJev, JevService
from kickroll.plan import ADDED_BY, repair_test_sources, validate_plan
from kickroll.planner import create_plan
from kickroll.runner import FakeRunner, FakeStep


def _test_task(tid="T0", writes=("tests/test_calc.py",)):
    return {"id": tid, "title": "Tests", "description": "Write the tests", "kind": "test", "risk": "low",
            "writes": list(writes),
            "acceptance": [{"id": f"{tid}-A1", "type": "command", "cmd": ["python", "-m", "py_compile", *writes]}]}


class RepairTest(unittest.TestCase):
    def test_missing_test_file_gets_a_test_task(self):
        plan = make_plan([code_task(test_module="tests.test_cli")])
        notes = repair_test_sources(plan)
        self.assertEqual(len(notes), 1, notes)
        self.assertEqual(validate_plan(plan), [])
        added = [t for t in plan["tasks"] if t.get("added_by") == ADDED_BY]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["kind"], "test")
        self.assertEqual(added[0]["writes"], ["tests/test_cli.py"])
        self.assertIn(added[0]["id"], plan["tasks"][-1]["depends_on"])
        self.assertLess(plan["tasks"].index(added[0]), len(plan["tasks"]) - 1)  # listed before the code task

    def test_class_and_method_suffix_map_to_the_module_file(self):
        plan = make_plan([code_task(test_module="tests.test_cli.CliTest.test_help")])
        repair_test_sources(plan)
        self.assertEqual(validate_plan(plan), [])
        self.assertIn("tests/test_cli.py", plan["tasks"][0]["writes"])

    def test_pytest_path_target(self):
        t = code_task()
        t["acceptance"] = [{"id": "A1", "type": "command", "cmd": ["python", "-m", "pytest", "tests/test_x.py::t"]}]
        plan = make_plan([t])
        repair_test_sources(plan)
        self.assertEqual(validate_plan(plan), [])
        self.assertEqual(plan["tasks"][0]["writes"], ["tests/test_x.py"])

    def test_missing_dependency_on_existing_test_task_is_added(self):
        plan = make_plan([_test_task(), code_task()], acceptance_files=[])
        notes = repair_test_sources(plan)
        self.assertEqual(validate_plan(plan), [])
        self.assertEqual(len(plan["tasks"]), 2)  # nothing inserted
        self.assertEqual(plan["tasks"][1]["depends_on"], ["T0"])
        self.assertIn("T0", notes[0])

    def test_two_code_tasks_share_one_added_test_file(self):
        plan = make_plan([code_task("T1", test_module="tests.test_cli"),
                          code_task("T2", writes=["pkg/other.py"], test_module="tests.test_cli")])
        repair_test_sources(plan)
        self.assertEqual(validate_plan(plan), [])
        added = [t for t in plan["tasks"] if t.get("added_by") == ADDED_BY]
        self.assertEqual(len(added), 1)

    def test_valid_plan_is_untouched(self):
        plan = make_plan([code_task()])
        before = [dict(t) for t in plan["tasks"]]
        self.assertEqual(repair_test_sources(plan), [])
        self.assertEqual(plan["tasks"], before)

    def test_committed_test_needs_no_repair(self):
        plan = make_plan([code_task(test_module="tests.test_base")], acceptance_files=[])
        self.assertEqual(repair_test_sources(plan, ["tests/test_base.py"]), [])

    def test_added_tasks_do_not_count_against_max_tasks(self):
        plan = make_plan([code_task("T1", test_module="tests.test_a"),
                          code_task("T2", writes=["pkg/b.py"], test_module="tests.test_b")])
        repair_test_sources(plan)
        self.assertEqual(len(plan["tasks"]), 4)
        self.assertEqual(validate_plan(plan, max_tasks=2), [])

    def test_test_in_own_writes_is_still_rejected(self):
        plan = make_plan([code_task(writes=["pkg/calc.py", "tests/test_cli.py"], test_module="tests.test_cli")],
                         acceptance_files=[])
        self.assertEqual(repair_test_sources(plan), [])
        self.assertTrue(any("its own writes" in e for e in validate_plan(plan)))

    def test_cycle_is_left_to_validation(self):
        plan = make_plan([code_task("T1", deps=["T2"]), code_task("T2", deps=["T1"], writes=["pkg/b.py"])])
        self.assertEqual(repair_test_sources(plan), [])
        self.assertTrue(validate_plan(plan))


class PlannerRepairTest(TempDirCase):
    def test_planner_slip_no_longer_costs_a_replan(self):
        raw = {"goal_summary": "x", "acceptance_files": [{"path": "tests/test_cli_pangram.py", "content": "x = 1\n"}],
               "tasks": [code_task(test_module="tests.test_cli", complexity=1, suggested_tier="sonnet")]}
        runner = FakeRunner({"planner": FakeStep(structured=raw)})
        cfg = Config(usage_path=str(self.tmp / "u.json"))
        seen = []
        plan = create_plan("goal", "r1", cfg, runner, Accountant(cfg, {}), str(self.tmp), "opus", [],
                           on_repair=seen.extend)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(len(seen), 1)
        self.assertTrue(any(t.get("added_by") == ADDED_BY for t in plan["tasks"]))


class RepairedPlanRunsTest(TempDirCase):
    def test_added_test_task_runs_before_the_code_task(self):
        repo = make_repo(self.tmp)
        plan = make_plan([code_task()], acceptance_files=[])
        repair_test_sources(plan)
        added = next(t["id"] for t in plan["tasks"] if t.get("added_by") == ADDED_BY)
        runner = FakeRunner({f"{added}:1": FakeStep(files={"tests/test_calc.py": CALC_TEST}),
                             "T1:1": FakeStep(files={"pkg/calc.py": GOOD_CALC})})
        o = Orchestrator(make_cfg(self.tmp), repo, runner, JevService(FakeJev(), roots=[str(repo)]), plan=plan,
                         status_writer=lambda s: None)
        try:
            self.assertEqual(o.start(), "SUCCESS")
            self.assertIn("tests/test_calc.py", o.state["frozen"])
        finally:
            from helpers import git
            git(repo, "worktree", "prune")


if __name__ == "__main__":
    unittest.main()
