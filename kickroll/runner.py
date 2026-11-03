"""ClaudeRunner (real `claude -p` calls) and FakeRunner (scriptable, for tests).

The output format of `claude -p --output-format json` is encapsulated in `parse_cli_output`.
Confirmed live: total_cost_usd, usage (input/output/cache_*), duration_api_ms, stop_reason, session_id.
[P] still to be confirmed: is_error, subtype, result, num_turns, structured_output (--json-schema)
as well as the error texts in _AUTH/_RATE.
Fixed overhead per call is about 27-40k cache input tokens -> budget for it per worker call.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .proc import IS_WINDOWS, JobHandle, kill_tree
from .safety import child_env


class ErrorClass:
    AUTH = "AUTH"
    RATE_LIMIT = "RATE_LIMIT"
    TIMEOUT = "TIMEOUT"
    TOOL_ERROR = "TOOL_ERROR"
    OTHER = "OTHER"
    CANCELLED = "CANCELLED"  # internal: run is paused/cancelled


@dataclass
class ClaudeCall:
    role: str  # worker | reviewer | planner | direct | ping
    key: str  # e.g. "T1:2" -> fake script and logs
    prompt: str
    model: str
    cwd: str
    timeout_s: float
    tools: str = ""
    json_schema: str | None = None


@dataclass
class ClaudeResult:
    error: str | None = None
    text: str = ""
    structured: dict | None = None
    duration_s: float = 0.0
    cost_usd: float | None = None
    num_turns: int | None = None
    usage: dict = field(default_factory=dict)
    pid: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class ClaudeNotFound(RuntimeError):
    pass


# ---------- Discovery ----------

def _version_key(path: str) -> tuple:
    for part in Path(path).parts:
        if re.fullmatch(r"\d+(?:\.\d+)+", part):
            return tuple(int(x) for x in part.split("."))
    return ()


def _desktop_candidates(environ) -> list[str]:
    appdata = environ.get("APPDATA", "")
    found = []
    if appdata:
        base = os.path.join(appdata, "Claude", "claude-code")
        found = glob.glob(os.path.join(base, "*", "claude.exe")) + glob.glob(os.path.join(base, "*", "*", "claude.exe"))
        found.sort(key=_version_key, reverse=True)
    home = environ.get("USERPROFILE") or environ.get("HOME", "")
    if home:
        exe = "claude.exe" if IS_WINDOWS else "claude"
        found += [os.path.join(home, ".local", "bin", exe), os.path.join(home, ".claude", "local", exe)]
    return found


def resolve_shim(path: str) -> str | None:
    """Resolve a `.cmd`/`.bat`/`.ps1` shim to the real claude.exe (never start shims directly)."""
    p = Path(path)
    if not IS_WINDOWS or p.suffix.lower() not in (".cmd", ".bat", ".ps1", ""):
        return str(p)
    for cand in (p.with_suffix(".exe"), p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"):
        if cand.is_file():
            return str(cand)
    return None


def find_claude(environ=None, which=shutil.which) -> str:
    """CLAUDE_BIN, PATH, then desktop installation; the fallback is always checked when a file is missing."""
    environ = os.environ if environ is None else environ
    for cand in [environ.get("CLAUDE_BIN"), which("claude")]:
        if cand and Path(cand).is_file():
            resolved = resolve_shim(cand)
            if resolved:
                return resolved
    for cand in _desktop_candidates(environ):
        if Path(cand).is_file():
            return cand
    raise ClaudeNotFound("Claude Code (`claude`) not found. Install it or set CLAUDE_BIN to the claude executable.")


# ---------- Evaluate output ([P]) ----------

_AUTH = ("not logged in", "please run /login", "/login", "invalid api key", "authentication_error",
         "oauth token has expired", "unauthorized", "401")
_RATE = ("session limit", "weekly limit", "opus limit", "usage limit", "rate limit", "rate_limit",
         "429", "too many requests", "limit reached")


def _load_json(stdout: str):
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        data = None
        for line in reversed(text.splitlines()):
            try:
                data = json.loads(line)
                break
            except ValueError:
                continue
    if isinstance(data, list):  # message list: last result object
        data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
    return data if isinstance(data, dict) else None


def classify_error(stderr: str, data: dict | None, returncode: int | None) -> str | None:
    """Error class ONLY from stderr and structured fields; payload content (result on success) is left out."""
    structured_error = data is not None and (bool(data.get("is_error")) or str(data.get("subtype", "success")) != "success")
    if data is not None and not structured_error and returncode == 0:
        return None
    parts = [stderr or ""]
    if structured_error:
        parts += [str(data.get("result", "")), str(data.get("subtype", "")), str(data.get("api_error_status", ""))]
    blob = " ".join(parts).lower()
    if any(p in blob for p in _AUTH):
        return ErrorClass.AUTH
    if any(p in blob for p in _RATE):
        return ErrorClass.RATE_LIMIT
    if data is not None and str(data.get("subtype", "")) == "error_during_execution":
        return ErrorClass.TOOL_ERROR
    return ErrorClass.OTHER


def parse_cli_output(stdout: str, stderr: str, returncode: int | None, duration_s: float = 0.0,
                     pid: int | None = None) -> ClaudeResult:
    data = _load_json(stdout)
    res = ClaudeResult(error=classify_error(stderr, data, returncode), duration_s=duration_s, pid=pid)
    if data is not None:
        res.text = str(data.get("result") or "")
        so = data.get("structured_output")
        res.structured = so if isinstance(so, dict) else None
        cost = data.get("total_cost_usd")
        res.cost_usd = float(cost) if isinstance(cost, (int, float)) else None
        turns = data.get("num_turns")
        res.num_turns = turns if isinstance(turns, int) else None
        usage = data.get("usage")
        res.usage = {k: v for k, v in usage.items() if isinstance(v, (int, float))} if isinstance(usage, dict) else {}
    return res


# ---------- Real runner ----------

class ClaudeRunner:
    """`claude -p` with prompt via stdin, strict tool allowlist, sanitized env and process-tree kill."""

    def __init__(self, setting_sources: str = "project", extra_args=None, binary: str | None = None,
                 popen=subprocess.Popen, attach_job=JobHandle.attach, killer=kill_tree):
        self._binary = binary
        self.setting_sources = setting_sources
        self.extra_args = list(extra_args or [])
        self._popen = popen
        self._attach_job = attach_job
        self._killer = killer
        self._active: dict = {}
        self._lock = threading.Lock()
        self._cancelled = threading.Event()

    @property
    def binary(self) -> str:
        if self._binary is None:
            self._binary = find_claude()
        return self._binary

    def build_argv(self, call: ClaudeCall) -> list[str]:
        argv = [self.binary, "-p", "--output-format", "json", "--model", call.model,
                "--no-session-persistence", "--strict-mcp-config", "--tools", call.tools]
        # Always set (verified live): without the flag, global user hooks (router_hook.py) also run in the worker.
        argv += ["--setting-sources", self.setting_sources or "project"]
        if call.json_schema:
            argv += ["--json-schema", call.json_schema]
        return argv + self.extra_args  # no --max-turns (not listed in --help)

    def run(self, call: ClaudeCall, on_spawn=None, on_exit=None) -> ClaudeResult:
        if self._cancelled.is_set():
            return ClaudeResult(error=ErrorClass.CANCELLED)
        # Own process group/session so the whole tree can be killed (Job Object/taskkill or killpg).
        group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {"start_new_session": True}
        start = time.monotonic()
        proc = self._popen(self.build_argv(call), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, cwd=call.cwd, env=child_env(), **group)
        job = self._attach_job(proc)
        with self._lock:
            self._active[proc.pid] = (proc, job)
        if on_spawn:
            on_spawn(proc.pid)
        timed_out = False
        try:
            try:
                out, err = proc.communicate(call.prompt.encode("utf-8"), timeout=call.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                self._kill(proc, job)
                try:
                    out, err = proc.communicate(timeout=15)
                except Exception:
                    out, err = b"", b""
        finally:
            with self._lock:
                self._active.pop(proc.pid, None)
            if job:
                job.close()
            if on_exit:
                on_exit(proc.pid)
        duration = time.monotonic() - start
        if timed_out:
            return ClaudeResult(error=ErrorClass.TIMEOUT, duration_s=duration, pid=proc.pid)
        if self._cancelled.is_set():
            return ClaudeResult(error=ErrorClass.CANCELLED, duration_s=duration, pid=proc.pid)
        dec = (lambda b: b.decode("utf-8", errors="replace") if isinstance(b, bytes) else (b or ""))
        return parse_cli_output(dec(out), dec(err), proc.returncode, duration, proc.pid)

    def _kill(self, proc, job) -> None:
        if job:
            job.terminate()
        else:
            self._killer(proc.pid)

    def kill_all(self) -> None:
        """End all running workers (pause, Ctrl+C)."""
        self._cancelled.set()
        with self._lock:
            active = list(self._active.values())
        for proc, job in active:
            self._kill(proc, job)

    def reset(self) -> None:
        self._cancelled.clear()


# ---------- Fake ----------

@dataclass
class FakeStep:
    """A scriptable call: write files (None = delete), deliver report/text or raise an error."""
    files: dict = field(default_factory=dict)
    report: dict | None = None
    text: str | None = None
    structured: dict | None = None
    error: str | None = None
    sleep_s: float = 0.0
    action: object = None  # optional callable(call) for special cases


def report_text(status: str = "done", summary: str = "done", files_changed=None) -> str:
    block = json.dumps({"status": status, "summary": summary, "files_changed": files_changed or []})
    return f"Fertig.\n```json\n{block}\n```"


class FakeRunner:
    """Script: dict key -> FakeStep | [FakeStep, ...] | callable(call). Keys are tried in order:
    exact call.key, "<task>:*", call.role. Without a match: default per role."""

    def __init__(self, script: dict | None = None):
        self.script = dict(script or {})
        self.calls: list[ClaudeCall] = []
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self.active = 0
        self.max_active = 0
        self.max_opus_active = 0
        self._opus_active = 0

    def _lookup(self, call: ClaudeCall):
        keys = [call.key, call.key.split(":")[0] + ":*", call.role]
        with self._lock:
            for k in keys:
                if k in self.script:
                    val = self.script[k]
                    if isinstance(val, list):
                        if not val:
                            continue
                        return val.pop(0) if len(val) > 1 else val[0]
                    return val(call) if callable(val) and not isinstance(val, FakeStep) else val
        return None

    def run(self, call: ClaudeCall, on_spawn=None, on_exit=None) -> ClaudeResult:
        if self._cancelled.is_set():
            return ClaudeResult(error=ErrorClass.CANCELLED)
        with self._lock:
            self.calls.append(call)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if call.model == "opus":
                self._opus_active += 1
                self.max_opus_active = max(self.max_opus_active, self._opus_active)
        fake_pid = 40000 + len(self.calls)
        if on_spawn:
            on_spawn(fake_pid)
        try:
            step = self._lookup(call) or self._default(call)
            if step.sleep_s:
                time.sleep(step.sleep_s)
            if step.action:
                step.action(call)
            for rel, content in step.files.items():
                target = Path(call.cwd) / rel
                if content is None:
                    target.unlink(missing_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
            if step.error:
                return ClaudeResult(error=step.error, pid=fake_pid)
            if self._cancelled.is_set():
                return ClaudeResult(error=ErrorClass.CANCELLED, pid=fake_pid)
            text = step.text if step.text is not None else (
                report_text(**step.report) if step.report is not None else report_text())
            return ClaudeResult(text=text, structured=step.structured, duration_s=0.01, pid=fake_pid,
                                num_turns=1, cost_usd=0.0)
        finally:
            with self._lock:
                self.active -= 1
                if call.model == "opus":
                    self._opus_active -= 1
            if on_exit:
                on_exit(fake_pid)

    def _default(self, call: ClaudeCall) -> FakeStep:
        if call.role == "reviewer":
            ids = re.findall(r"^- \[([A-Za-z0-9_-]+)\]", call.prompt, re.M)
            return FakeStep(text=json.dumps({**{i: "pass" for i in ids}, "reason": "fake"}))
        if call.role == "planner":
            return FakeStep(structured=default_fake_plan())
        return FakeStep(report={"status": "done", "summary": "fake"})

    def kill_all(self) -> None:
        self._cancelled.set()

    def reset(self) -> None:
        self._cancelled.clear()


def default_fake_plan() -> dict:
    return {
        "goal_summary": "Fake plan (plumbing test only)",
        "tasks": [{"id": "T1", "title": "Repo overview", "description": "Summarize the structure of the repo.",
                   "kind": "analysis", "risk": "low", "depends_on": [],
                   "acceptance": [{"id": "A1", "type": "judgement", "text": "Summary names the main modules"}]}],
    }
