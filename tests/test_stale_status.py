import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from helpers import TempDirCase, make_repo

from kickroll import cli
from kickroll.status_file import Heartbeat, build_status, mark_ended, write_status


def _status(**over) -> dict:
    d = build_status({"run_id": "r1", "status": "PLANNING", "phase": "PLAN", "note": "Planner: opus"})
    d.update(over)
    return d


class MarkEndedTest(TempDirCase):
    def test_own_running_status_becomes_failed(self):
        path = self.tmp / "status.json"
        write_status(path, _status())
        self.assertTrue(mark_ended(path, "Aborted: TimeoutError"))
        d = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((d["status"], d["phase"], d["note"]), ("FAILED", "PLAN", "Aborted: TimeoutError"))

    def test_foreign_or_finished_status_is_left_alone(self):
        path = self.tmp / "status.json"
        write_status(path, _status(pid=os.getpid() + 1))
        self.assertFalse(mark_ended(path, "x"))
        write_status(path, _status(status="PAUSED"))
        self.assertFalse(mark_ended(path, "x"))
        self.assertFalse(mark_ended(self.tmp / "missing.json", "x"))


class HeartbeatTest(TempDirCase):
    def test_beats_while_running_and_stops(self):
        path = self.tmp / "heartbeat.json"
        with Heartbeat(path, every_s=0.05) as hb:
            deadline = time.monotonic() + 5
            while hb.beats < 3 and time.monotonic() < deadline:
                time.sleep(0.02)
        d = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(d["pid"], os.getpid())
        self.assertGreaterEqual(d["beat"], 3)
        beats = hb.beats
        time.sleep(0.15)
        self.assertEqual(hb.beats, beats)


class CliAbortTest(TempDirCase):
    def _cfg(self):
        p = self.tmp / "kickroll.toml"
        base = self.tmp.as_posix()
        p.write_text("[paths]\n"
                     f'runs_dir = "{base}/runs"\nworktree_root = "{base}/wt"\n'
                     f'usage_path = "{base}/usage.json"\nstatus_path = "{base}/status.json"\n'
                     "[checks]\n"
                     'test_command = ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."]\n',
                     encoding="utf-8")
        return p

    def test_crash_in_planner_leaves_failed_not_running(self):
        repo = make_repo(self.tmp)
        err = io.StringIO()
        with mock.patch.object(cli, "create_plan", side_effect=TimeoutError("planner hung")), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(TimeoutError):
                cli.main(["--repo", str(repo), "--config", str(self._cfg()), "plan", "Do x", "--fake",
                          "--force-orchestrate", "--out", str(self.tmp / "plan.json")])
        status = json.loads((self.tmp / "status.json").read_text(encoding="utf-8"))
        self.assertEqual((status["status"], status["phase"]), ("FAILED", "PLAN"))
        self.assertEqual(status["note"], "Aborted: TimeoutError")
        self.assertTrue((self.tmp / "heartbeat.json").is_file())


if __name__ == "__main__":
    unittest.main()
