import unittest

from helpers import CALC_TEST, code_task, make_plan, read_task

from kickroll.plan import (DEFAULT_FORBIDDEN, command_error, glob_match, max_attempts, normalize_plan,
                           test_targets, validate_plan)


class CommandTemplateTest(unittest.TestCase):
    def test_allowed_templates(self):
        for cmd in (["python", "-m", "unittest", "tests.test_x"], ["python", "-m", "pytest", "-q", "tests/test_x.py"],
                    ["python", "-m", "py_compile", "pkg/a.py"],
                    ["python", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_*.py"]):
            self.assertIsNone(command_error(cmd), cmd)

    def test_rejected(self):
        for cmd in (["python", "-c", "print(1)"], ["python", "-m", "unittest", "-c"], "python -m unittest",
                    ["npm", "test"], ["python", "-m", "pip", "install", "x"], ["python", "-m", "pytest", "/abs/x.py"],
                    ["python", "-m", "pytest", "../x.py"], ["python", "-m", "pytest", "-e", "x"],
                    ["python", "-m", "unittest", "x;rm"], ["python", "-m", "pytest", "C:/x.py"]):
            self.assertIsNotNone(command_error(cmd), cmd)

    def test_targets(self):
        self.assertEqual(test_targets(["python", "-m", "unittest", "-v", "tests.test_a"]), ("module", ["tests.test_a"]))
        self.assertEqual(test_targets(["python", "-m", "pytest", "tests/t.py::A::b"]), ("path", ["tests/t.py"]))
        self.assertEqual(test_targets(["python", "-m", "unittest", "discover", "-s", "tests"])[0], "discover")


class GlobTest(unittest.TestCase):
    def test_glob(self):
        self.assertTrue(glob_match("a/b/conftest.py", "**/conftest.py"))
        self.assertTrue(glob_match("conftest.py", "**/conftest.py"))
        self.assertTrue(glob_match(".github/workflows/x.yml", ".github/**"))
        self.assertTrue(glob_match("src\\x.py", "src/*.py"))
        self.assertFalse(glob_match("src/sub/x.py", "src/*.py"))
        self.assertTrue(glob_match("cfg/.env.local", "**/.env*"))


class PlanValidationTest(unittest.TestCase):
    def test_valid_plan(self):
        self.assertEqual(validate_plan(make_plan([code_task()])), [])

    def test_forbidden_defaults_added(self):
        plan = make_plan([code_task()])
        for f in DEFAULT_FORBIDDEN:
            self.assertIn(f, plan["tasks"][0]["forbidden"])

    def test_writes_forbidden_overlap(self):
        errs = validate_plan(make_plan([code_task(writes=["pkg/calc.py", "pyproject.toml"])]))
        self.assertTrue(any("forbidden" in e for e in errs), errs)

    def test_acceptance_test_from_own_writes(self):
        t = code_task(writes=["pkg/calc.py", "tests/test_calc.py"])
        errs = validate_plan(make_plan([t]))
        self.assertTrue(any("its own writes" in e for e in errs), errs)

    def test_test_target_needs_source(self):
        errs = validate_plan(make_plan([code_task()], acceptance_files=[]))
        self.assertTrue(any("not from acceptance_files" in e for e in errs), errs)

    def test_test_task_as_source(self):
        tt = {"id": "T0", "title": "Tests", "description": "Write the tests", "kind": "test", "risk": "low",
              "writes": ["tests/test_calc.py"],
              "acceptance": [{"id": "A1", "type": "command", "cmd": ["python", "-m", "py_compile", "tests/test_calc.py"]}]}
        plan = make_plan([tt, code_task(deps=["T0"])], acceptance_files=[])
        self.assertEqual(validate_plan(plan), [])
        plan2 = make_plan([tt, code_task()], acceptance_files=[])  # without dependency
        self.assertTrue(validate_plan(plan2))

    def test_code_task_without_test(self):
        t = code_task()
        t["acceptance"] = [{"id": "A1", "type": "file_exists", "path": "pkg/calc.py"}]
        self.assertTrue(any("acceptance test" in e for e in validate_plan(make_plan([t]))))

    def test_command_only_templates(self):
        t = code_task()
        t["acceptance"].append({"id": "A2", "type": "command", "cmd": ["python", "-c", "import os"]})
        self.assertTrue(any("A2" in e for e in validate_plan(make_plan([t]))))

    def test_cycle_and_duplicates(self):
        errs = validate_plan(make_plan([read_task("T1", ["T2"]), read_task("T2", ["T1"])]))
        self.assertTrue(any("cycle" in e for e in errs))
        errs = validate_plan(make_plan([read_task("T1"), read_task("T1")]))
        self.assertTrue(any("duplicate" in e for e in errs))

    def test_too_many_tasks(self):
        errs = validate_plan(make_plan([read_task(f"T{i}") for i in range(9)]), max_tasks=8)
        self.assertTrue(any("at most" in e for e in errs))

    def test_read_task_must_not_write(self):
        t = read_task("T1")
        t["writes"] = ["x.py"]
        self.assertTrue(validate_plan(make_plan([t])))

    def test_code_writes_cannot_touch_frozen(self):
        t = code_task(writes=["pkg/calc.py", "tests/*.py"],
                      acceptance=[{"id": "A1", "type": "command", "cmd": ["python", "-m", "unittest", "tests.test_calc"]}])
        errs = validate_plan(make_plan([t], acceptance_files=[{"path": "tests/test_calc.py", "content": CALC_TEST}]))
        self.assertTrue(errs)

    def test_max_attempts(self):
        self.assertEqual(max_attempts({"risk": "low"}), 3)
        self.assertEqual(max_attempts({"risk": "high"}), 4)
        self.assertEqual(max_attempts({"budget": {"max_attempts": 9}}), 4)

    def test_normalize_does_not_mutate(self):
        raw = {"tasks": [{"id": "T1"}]}
        normalize_plan(raw)
        self.assertNotIn("forbidden", raw["tasks"][0])


class CriterionIdTest(unittest.TestCase):
    """Live finding: planners omit criterion IDs; that must not reject the plan."""

    def test_missing_ids_are_assigned_in_order(self):
        task = code_task()
        task["acceptance"] = [{"type": "command", "cmd": ["python", "-m", "unittest", "tests.test_calc"]},
                              {"type": "file_exists", "path": "pkg/calc.py"}]
        plan = make_plan([task])
        self.assertEqual([c["id"] for c in plan["tasks"][0]["acceptance"]], ["T1-A1", "T1-A2"])
        self.assertEqual(validate_plan(plan, 8), [])

    def test_valid_ids_are_kept_and_duplicates_renamed(self):
        task = code_task()
        task["acceptance"] = [{"id": "A1", "type": "command", "cmd": ["python", "-m", "unittest", "tests.test_calc"]},
                              {"id": "A1", "type": "file_exists", "path": "pkg/calc.py"}]
        plan = make_plan([task])
        ids = [c["id"] for c in plan["tasks"][0]["acceptance"]]
        self.assertEqual(ids[0], "A1")
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(validate_plan(plan, 8), [])

    def test_original_plan_is_not_modified(self):
        raw = {"schema_version": 1, "run_id": "r", "tasks": [dict(code_task(), acceptance=[{"type": "file_exists", "path": "a.py"}])]}
        normalize_plan(raw)
        self.assertNotIn("id", raw["tasks"][0]["acceptance"][0])

    def test_test_target_error_names_the_frozen_files(self):
        plan = make_plan([code_task(test_module="tests.test_other")])
        errors = validate_plan(plan, 8)
        self.assertTrue(any("frozen test files in this plan" in e and "tests/test_calc.py" in e for e in errors), errors)


class CommittedTestsTest(unittest.TestCase):
    """Live finding: a planner uses tests that are already committed as regression checks; that is fine."""

    def test_committed_test_is_trusted_as_acceptance_test(self):
        plan = make_plan([code_task(test_module="tests.test_existing")])
        self.assertTrue(validate_plan(plan, 8))
        self.assertEqual(validate_plan(plan, 8, existing=["tests/test_existing.py"]), [])

    def test_committed_test_is_not_trusted_if_the_task_writes_it(self):
        plan = make_plan([code_task(test_module="tests.test_existing",
                                    writes=["pkg/calc.py", "tests/test_existing.py"])])
        errors = validate_plan(plan, 8, existing=["tests/test_existing.py"])
        self.assertTrue(errors, "a task must not accept itself with a test it may rewrite")


if __name__ == "__main__":
    unittest.main()
