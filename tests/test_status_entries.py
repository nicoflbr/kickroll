import contextlib
import io
import json
import os
import time
import unittest
from unittest import mock

from helpers import TempDirCase, make_repo

from kickroll import cli
from kickroll.status_file import (Heartbeat, beat_path, build_status, entries, entry_path, legacy, live_run,
                                  mark_ended, prune, publish, validate_status)


def _status(run_id="r1", **over) -> dict:
    d = build_status({"run_id": run_id, "status": "EXECUTING", "phase": "EXECUTE", "repo": over.pop("repo", "")})
    d.update(over)
    return d


def _beat(status_path, pid=None, age_s=0.0):
    p = beat_path(status_path, pid)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"pid": pid or os.getpid(), "beat": 1}), encoding="utf-8")
    if age_s:
        t = time.time() - age_s
        os.utime(p, (t, t))
    return p


def _age(path, seconds):
    d = json.loads(path.read_text(encoding="utf-8"))
    d["updated"] = "2000-01-01T00:00:00+00:00" if seconds is None else \
        time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - seconds))
    path.write_text(json.dumps(d), encoding="utf-8")


class EntryTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.sp = self.tmp / "status.json"

    def test_two_writers_keep_their_own_entries(self):
        publish(self.sp, _status("run-a", repo="C:/a"))
        publish(self.sp, _status("run-b", repo="C:/b"))
        publish(self.sp, _status("run-a", repo="C:/a", note="again"))
        found = {d["run_id"]: d for _, d in entries(self.sp)}
        self.assertEqual(set(found), {"run-a", "run-b"})
        self.assertEqual(found["run-a"]["note"], "again")
        self.assertEqual(found["run-b"]["repo"], "C:/b")

    def test_mirror_is_version_1_of_the_last_entry(self):
        publish(self.sp, _status("run-a", repo="C:/a"))
        mirror = json.loads(self.sp.read_text(encoding="utf-8"))
        self.assertEqual((mirror["version"], mirror["run_id"]), (1, "run-a"))
        self.assertNotIn("repo", mirror)
        validate_status(mirror)

    def test_schema_v2_has_repo_and_session(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_HOST_SESSION_ID": "local_abc"}):
            s = _status(repo="C:/x")
        self.assertEqual((s["version"], s["repo"], s["session"]), (2, "C:/x", "local_abc"))
        bad = dict(s)
        bad.pop("session")
        with self.assertRaises(ValueError):
            validate_status(bad)
        validate_status(legacy(s))

    def test_key_is_made_file_safe(self):
        path = publish(self.sp, _status("../evil run"))
        self.assertEqual(path.parent, self.tmp / "status")
        self.assertEqual(path.name, ".._evil_run.json")

    def test_mark_ended_only_touches_own_entries(self):
        publish(self.sp, _status("mine"))
        publish(self.sp, _status("other", pid=os.getpid() + 1))
        publish(self.sp, _status("done", status="SUCCESS"))
        self.assertTrue(mark_ended(self.sp, "Aborted: X"))
        found = {d["run_id"]: d for _, d in entries(self.sp)}
        self.assertEqual((found["mine"]["status"], found["mine"]["note"]), ("FAILED", "Aborted: X"))
        self.assertEqual(found["other"]["status"], "RUNNING")
        self.assertEqual(found["done"]["status"], "SUCCESS")

    def test_live_run_per_repo(self):
        repo_a, repo_b = self.tmp / "a", self.tmp / "b"
        repo_a.mkdir()
        repo_b.mkdir()
        publish(self.sp, _status("run-a", repo=str(repo_a)))
        self.assertIsNone(live_run(self.sp))  # no beat yet
        _beat(self.sp)
        self.assertEqual(live_run(self.sp), "run-a")
        self.assertEqual(live_run(self.sp, repo=repo_a), "run-a")
        self.assertIsNone(live_run(self.sp, repo=repo_b))

    def test_live_run_ignores_stale_beat(self):
        publish(self.sp, _status("run-a"))
        _beat(self.sp, age_s=60)
        self.assertIsNone(live_run(self.sp))

    def test_prune_old_finished_and_dead(self):
        dead_pid = 2 ** 22 + 12345
        for rid, st in (("old-done", "SUCCESS"), ("new-done", "PARTIAL"), ("dead", "RUNNING"),
                        ("dead-fresh", "RUNNING")):
            publish(self.sp, _status(rid, status=st, pid=dead_pid if st == "RUNNING" else os.getpid()))
        _age(entry_path(self.sp, "old-done"), 2 * 24 * 3600)
        _age(entry_path(self.sp, "dead"), 2 * 3600)
        dead_beat = _beat(self.sp, dead_pid, age_s=120)
        removed = prune(self.sp, alive=lambda pid: pid == os.getpid())
        self.assertEqual(set(removed), {"old-done.json", "dead.json", dead_beat.name})
        self.assertEqual({d["run_id"] for _, d in entries(self.sp)}, {"new-done", "dead-fresh"})

    def test_prune_never_removes_a_live_run(self):
        publish(self.sp, _status("live"))
        _age(entry_path(self.sp, "live"), 5 * 3600)
        _beat(self.sp)
        self.assertEqual(prune(self.sp), [])

    def test_prune_caps_finished_entries(self):
        for i in range(25):
            publish(self.sp, _status(f"r{i:02d}", status="SUCCESS"))
            _age(entry_path(self.sp, f"r{i:02d}"), 1000 - i)
        prune(self.sp)
        left = sorted(d["run_id"] for _, d in entries(self.sp))
        self.assertEqual(len(left), 20)
        self.assertEqual(left[0], "r05")

    def test_heartbeat_writes_extra_beat_and_removes_it(self):
        extra = beat_path(self.sp)
        with Heartbeat(self.tmp / "heartbeat.json", every_s=0.05, also=[extra]):
            deadline = time.monotonic() + 5
            while not extra.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(extra.exists())
        self.assertFalse(extra.exists())
        self.assertTrue((self.tmp / "heartbeat.json").exists())


class CliEntryTest(TempDirCase):
    def _cfg(self):
        p = self.tmp / "kickroll.toml"
        base = self.tmp.as_posix()
        p.write_text("[paths]\n"
                     f'runs_dir = "{base}/runs"\nworktree_root = "{base}/wt"\n'
                     f'usage_path = "{base}/usage.json"\nstatus_path = "{base}/status.json"\n',
                     encoding="utf-8")
        return p

    def _main(self, *argv, repo=None):
        if repo is None:
            repo = self.__dict__.get("_repo") or make_repo(self.tmp)
            self._repo = repo
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            code = cli.main(["--repo", str(repo), "--config", str(self._cfg()), *argv])
        return code, out.getvalue()

    def test_bypass_is_its_own_entry_per_session(self):
        sp = self.tmp / "status.json"
        publish(sp, _status("other-repo-run", repo=str(self.tmp / "elsewhere")))
        _beat(sp)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_HOST_SESSION_ID": "local_s1"}):
            self.assertEqual(self._main("bypassed", "fix one")[0], cli.EXIT_OK)
            self.assertEqual(self._main("bypassed", "fix two")[0], cli.EXIT_OK)
        bypass = json.loads(entry_path(sp, "bypass-local_s1").read_text(encoding="utf-8"))
        self.assertEqual((bypass["status"], bypass["session"]), ("BYPASSED", "local_s1"))
        self.assertIn("fix two", bypass["note"])
        found = {d["run_id"]: d["status"] for _, d in entries(sp)}
        self.assertEqual(found["other-repo-run"], "RUNNING")
        self.assertEqual(len(found), 2)

    def test_bypass_blocked_by_live_run_in_same_repo(self):
        repo = make_repo(self.tmp)
        sp = self.tmp / "status.json"
        publish(sp, _status("same-repo-run", repo=str(repo)))
        _beat(sp)
        self.assertEqual(self._main("bypassed", "x", repo=repo)[0], cli.EXIT_USAGE)
        self.assertEqual(len(entries(sp)), 1)

    def test_status_shows_the_runs_pid(self):
        sp = self.tmp / "status.json"
        state = {"run_id": "r9", "status": "EXECUTING", "phase": "EXECUTE", "plan": {"tasks": []}, "tasks": {}}
        (self.tmp / "runs" / "r9").mkdir(parents=True)
        (self.tmp / "runs" / "r9" / "state.json").write_text(json.dumps(state), encoding="utf-8")
        code, out = self._main("status", "r9")
        self.assertEqual((code, json.loads(out)["pid"]), (cli.EXIT_OK, 0))
        publish(sp, _status("r9", pid=4242))
        code, out = self._main("status", "r9")
        self.assertEqual(json.loads(out)["pid"], 4242)


if __name__ == "__main__":
    unittest.main()
