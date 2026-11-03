import json
import os
import subprocess
import unittest
from unittest import mock

from helpers import TempDirCase

from kickroll.runner import (ClaudeCall, ClaudeNotFound, ClaudeRunner, ErrorClass, FakeRunner, FakeStep,
                             find_claude, parse_cli_output, resolve_shim)


def cli_json(**fields) -> str:
    base = {"type": "result", "subtype": "success", "is_error": False, "result": "ok", "num_turns": 2,
            "total_cost_usd": 0.01, "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 30000},
            "duration_api_ms": 1200, "stop_reason": "end_turn", "session_id": "s1"}
    base.update(fields)
    return json.dumps(base)


class ParseOutputTest(unittest.TestCase):
    def test_success(self):
        r = parse_cli_output(cli_json(), "", 0)
        self.assertTrue(r.ok)
        self.assertEqual(r.text, "ok")
        self.assertEqual(r.cost_usd, 0.01)
        self.assertEqual(r.usage["cache_read_input_tokens"], 30000)

    def test_rate_limit_text_in_worker_output_is_not_an_error(self):
        r = parse_cli_output(cli_json(result="Note in code: 'You've hit your session limit' / 429"), "", 0)
        self.assertIsNone(r.error)

    def test_login_in_worker_code_is_not_auth(self):
        r = parse_cli_output(cli_json(result='app.route("/login") # not logged in -> redirect'), "", 0)
        self.assertIsNone(r.error)

    def test_structured_errors(self):
        self.assertEqual(parse_cli_output(cli_json(is_error=True, result="Session limit reached"), "", 1).error,
                         ErrorClass.RATE_LIMIT)
        self.assertEqual(parse_cli_output(cli_json(is_error=True, result="Opus limit reached ∙ resets 5pm"), "", 1).error,
                         ErrorClass.RATE_LIMIT)
        self.assertEqual(parse_cli_output("", "Invalid API key · Please run /login", 1).error, ErrorClass.AUTH)
        self.assertEqual(parse_cli_output(cli_json(subtype="error_during_execution", is_error=True, result="x"), "", 1).error,
                         ErrorClass.TOOL_ERROR)
        self.assertEqual(parse_cli_output("no json", "", 0).error, ErrorClass.OTHER)
        self.assertEqual(parse_cli_output("", "boom", 2).error, ErrorClass.OTHER)

    def test_structured_output_and_multiline(self):
        out = "warn line\n" + cli_json(structured_output={"tasks": []})
        r = parse_cli_output(out, "", 0)
        self.assertEqual(r.structured, {"tasks": []})


class FindClaudeTest(TempDirCase):
    def _exe(self, rel: str):
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"")
        return p

    def test_env_bin_missing_falls_back_and_sorts_versions_as_tuples(self):
        self._exe("Claude/claude-code/2.1.99/x/claude.exe")
        newest = self._exe("Claude/claude-code/2.1.100/x/claude.exe")
        env = {"CLAUDE_BIN": str(self.tmp / "missing.exe"), "APPDATA": str(self.tmp), "USERPROFILE": str(self.tmp)}
        self.assertEqual(find_claude(env, which=lambda _: None), str(newest))

    def test_env_bin_used(self):
        exe = self._exe("bin/claude.exe")
        self.assertEqual(find_claude({"CLAUDE_BIN": str(exe)}, which=lambda _: None), str(exe))

    def test_cmd_shim_resolved(self):
        shim = self._exe("npm/claude.cmd")
        real = self._exe("npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe")
        self.assertEqual(resolve_shim(str(shim)), str(real))
        self.assertEqual(find_claude({}, which=lambda _: str(shim)), str(real))

    def test_not_found(self):
        with self.assertRaises(ClaudeNotFound):
            find_claude({"APPDATA": str(self.tmp)}, which=lambda _: None)


class _FakeProc:
    def __init__(self, stdout=b"", stderr=b"", rc=0, hang=False):
        self.pid = 4242
        self.returncode = rc
        self._out, self._err, self._hang = stdout, stderr, hang
        self.stdin_data = None
        self.calls = 0

    def communicate(self, data=None, timeout=None):
        self.calls += 1
        if data is not None:
            self.stdin_data = data
        if self._hang and self.calls == 1:
            raise subprocess.TimeoutExpired("claude", timeout)
        return self._out, self._err


class ClaudeRunnerTest(unittest.TestCase):
    def _runner(self, proc, killer=None):
        self.popen_kwargs = {}

        def popen(argv, **kw):
            self.argv = argv
            self.popen_kwargs = kw
            return proc

        return ClaudeRunner(setting_sources="", binary="C:/x/claude.exe", popen=popen,
                            attach_job=lambda p: None, killer=killer or (lambda pid: None))

    def _call(self, **kw):
        base = dict(role="worker", key="T1:1", prompt="SECRET PROMPT äöü", model="sonnet", cwd=".",
                    timeout_s=5, tools="Read,Grep,Glob,Edit,Write")
        base.update(kw)
        return ClaudeCall(**base)

    def test_argv_stdin_env(self):
        proc = _FakeProc(stdout=cli_json().encode())
        with mock.patch.dict(os.environ, {"TYPESAFE_API_KEY": "x", "ANTHROPIC_API_KEY": "y", "GH_TOKEN": "z"}):
            res = self._runner(proc).run(self._call())
        self.assertTrue(res.ok)
        a = self.argv
        self.assertEqual(a[:4], ["C:/x/claude.exe", "-p", "--output-format", "json"])
        for flag in ("--no-session-persistence", "--strict-mcp-config"):
            self.assertIn(flag, a)
        self.assertEqual(a[a.index("--tools") + 1], "Read,Grep,Glob,Edit,Write")
        # verified live: without --setting-sources project the global UserPromptSubmit hook also runs
        self.assertEqual(a[a.index("--setting-sources") + 1], "project")
        self.assertNotIn("--max-turns", a)
        self.assertNotIn("SECRET PROMPT äöü", " ".join(a))  # prompt only via stdin
        self.assertEqual(proc.stdin_data, "SECRET PROMPT äöü".encode("utf-8"))
        env = self.popen_kwargs["env"]
        for k in ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN"):
            self.assertNotIn(k, env)
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("shell", self.popen_kwargs)

    def test_timeout_kills_tree(self):
        killed = []
        proc = _FakeProc(hang=True)
        res = self._runner(proc, killer=killed.append).run(self._call())
        self.assertEqual(res.error, ErrorClass.TIMEOUT)
        self.assertEqual(killed, [4242])

    def test_job_object_used_when_available(self):
        proc = _FakeProc(hang=True)
        job = mock.Mock()
        r = ClaudeRunner(binary="c.exe", popen=lambda argv, **kw: proc, attach_job=lambda p: job,
                         killer=lambda pid: self.fail("taskkill despite job"))
        self.assertEqual(r.run(self._call()).error, ErrorClass.TIMEOUT)
        job.terminate.assert_called_once()
        job.close.assert_called_once()

    def test_json_schema_and_pids(self):
        proc = _FakeProc(stdout=cli_json().encode())
        seen = []
        self._runner(proc).run(self._call(json_schema='{"type":"object"}'), on_spawn=seen.append, on_exit=seen.append)
        self.assertIn("--json-schema", self.argv)
        self.assertEqual(seen, [4242, 4242])

    def test_real_taskkill_argv(self):
        from kickroll.proc import IS_WINDOWS, kill_tree
        calls = []
        kill_tree(99, run=lambda argv, **kw: calls.append(argv))
        if IS_WINDOWS:
            self.assertEqual(calls[0], ["taskkill", "/T", "/F", "/PID", "99"])


class FakeRunnerTest(TempDirCase):
    def test_script_sequence(self):
        r = FakeRunner({"T1:1": [FakeStep(error=ErrorClass.TIMEOUT), FakeStep(files={"a.txt": "x"})]})
        call = ClaudeCall("worker", "T1:1", "p", "haiku", str(self.tmp), 1)
        self.assertEqual(r.run(call).error, ErrorClass.TIMEOUT)
        self.assertTrue(r.run(call).ok)
        self.assertTrue((self.tmp / "a.txt").exists())


if __name__ == "__main__":
    unittest.main()
