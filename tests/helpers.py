"""Test helpers: temporary Git repos, config with temp paths, plan building blocks. No network, no claude."""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kickroll.config import Config  # noqa: E402

TEST_CMD = ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."]
CALC_TEST = (
    "import unittest\n"
    "from pkg.calc import add\n\n\n"
    "class CalcTest(unittest.TestCase):\n"
    "    def test_add(self):\n"
    "        self.assertEqual(add(2, 3), 5)\n"
)
GOOD_CALC = "def add(a, b):\n    return a + b\n"
BAD_CALC = "def add(a, b):\n    return a - b\n"


def git(cwd: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)
    return out.stdout


def _rm_readonly(func, path, _exc):
    os.chmod(path, stat.S_IWRITE)
    func(path)


class TempDirCase(unittest.TestCase):
    """Creates a temp directory and removes it again (including read-only git objects)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jevorch_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, onexc=_rm_readonly)


def make_repo(base: Path, extra_files: dict | None = None) -> Path:
    repo = base / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    files = {
        "pkg/__init__.py": "",
        "pkg/calc.py": "def add(a, b):\n    raise NotImplementedError\n",
        "tests/__init__.py": "",
        "tests/test_base.py": "import unittest\n\n\nclass Base(unittest.TestCase):\n"
                              "    def test_ok(self):\n        self.assertTrue(True)\n",
        ".gitignore": "",
    }
    files.update(extra_files or {})
    for rel, content in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(content, encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def make_cfg(base: Path, **over) -> Config:
    cfg = Config(runs_dir=str(base / "runs"), worktree_root=str(base / "wt"), usage_path=str(base / "usage.json"),
                 status_path=str(base / "status.json"), test_command=list(TEST_CMD))
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def code_task(tid="T1", writes=None, risk="medium", deps=None, test_module="tests.test_calc", **extra) -> dict:
    t = {"id": tid, "title": f"Implement {tid}", "description": "implement add() correctly",
         "kind": "code", "risk": risk, "depends_on": deps or [], "writes": writes or ["pkg/calc.py"],
         "acceptance": [{"id": "A1", "type": "command", "cmd": ["python", "-m", "unittest", test_module]}]}
    t.update(extra)
    return t


def read_task(tid, deps=None, risk="low") -> dict:
    return {"id": tid, "title": f"Analysis {tid}", "description": "Read and summarize", "kind": "analysis",
            "risk": risk, "depends_on": deps or [],
            "acceptance": [{"id": "J1", "type": "judgement", "text": "The summary is complete"}]}


def make_plan(tasks, acceptance_files=None, run_id="run-test-0001") -> dict:
    from kickroll.plan import normalize_plan
    return normalize_plan({
        "schema_version": 1, "run_id": run_id, "goal_sha256": "0" * 16, "goal_summary": "Test",
        "global_checks": [list(TEST_CMD)],
        "acceptance_files": acceptance_files if acceptance_files is not None else
        [{"path": "tests/test_calc.py", "content": CALC_TEST}],
        "tasks": tasks,
    })


def route_answers(complexity=1.0, simplicity=1.0, risk="medium", conf=0.8) -> dict:
    return {"task": {"type": "choice", "choice": "code", "confidence": conf},
            "complexity": {"type": "score", "score": complexity, "confidence": 0.8},
            "simplicity": {"type": "score", "score": simplicity, "confidence": 0.8},
            "risk": {"type": "choice", "choice": risk, "confidence": 0.8}}
