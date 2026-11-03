"""Public status files for the desktop pane (data dir = $KICKROLL_HOME or ~/.kickroll).

Small fixed interface: only IDs, counters, enums, short titles. No prompts, diffs, error output, secrets.
One entry per run or skill bypass: <data dir>/status/<key>.json (key = run_id, or bypass-<session>), written
atomically on every status change, so parallel runs and sessions never overwrite each other; state.json stays
separate. `pid` names the writing process; while it lives it beats <data dir>/status/pid-<pid>.beat.json, so the
pane can tell a dead run from a live one (a RUNNING entry without a fresh beat of its pid is stale).
Transition: <data dir>/status.json and heartbeat.json still mirror the last written entry (schema version 1)
for panes before 0.2.0.
"""
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from .proc import pid_alive
from .store import atomic_write_json, now_iso, read_json

RUN_STATUS = ("RUNNING", "PAUSED", "SUCCESS", "PARTIAL", "FAILED", "BYPASSED")
PHASES = ("PREFLIGHT", "BYPASS", "PLAN", "APPROVAL", "EXECUTE", "DONE")
TASK_STATUS = ("PENDING", "READY", "RUNNING", "ACCEPTED", "MERGED", "BLOCKED", "FAILED", "SKIPPED", "UNCLEAR")
KINDS = ("code", "analysis", "review", "test")
STEPS = ("ROUTE", "WORK", "L0", "L1", "L3", "COMMIT", "NONE")
MODELS = ("haiku", "sonnet", "opus", "")

_TOP_KEYS_V1 = {"version", "run_id", "pid", "status", "phase", "note", "updated", "started", "budgets", "usage",
                "jev", "tasks"}
_TOP_KEYS = _TOP_KEYS_V1 | {"repo", "session"}
_TASK_KEYS = {"id", "title", "kind", "status", "step", "attempt", "max_attempts", "model", "depends_on",
              "failed_criteria"}
_BUDGET_KEYS = {"calls", "opus", "planner", "minutes"}
_USAGE_MODELS = ("haiku", "sonnet", "opus")
_USAGE_KEYS = {"calls", "tokens", "seconds"}
_JEV_KEYS = {"calls", "errors", "seconds"}

# internal run states -> pane
_RUN_MAP = {"PLANNING": "RUNNING", "EXECUTING": "RUNNING", "RUNNING": "RUNNING", "PAUSED": "PAUSED",
            "SUCCESS": "SUCCESS", "PARTIAL": "PARTIAL", "ABORTED": "FAILED", "FAILED": "FAILED",
            "BYPASSED": "BYPASSED"}
_TASK_MAP = {"VERIFYING": "RUNNING", "RETRY": "RUNNING"}
_STEP_MAP = {"L2": "L1"}  # L2 (Jev shadow) is shown as L1 in the pane (quick check)

BEAT_MAX_AGE_S = 30.0  # heartbeats are rewritten every 5 s
KEEP_ENDED_S = 24 * 3600  # finished entries stay this long
KEEP_DEAD_S = 3600  # a RUNNING entry whose process is gone shows as stale this long
KEEP_ENDED_MAX = 20  # and at most this many finished entries
_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _cut(value, n: int) -> str:
    return str(value or "")[:n]


def _int(value) -> int:
    try:
        return max(0, int(round(float(value or 0))))
    except (TypeError, ValueError):
        return 0


def session_id() -> str:
    """The Claude session that started this process (desktop session first), or ''."""
    return _cut(os.environ.get("CLAUDE_CODE_HOST_SESSION_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID"), 64)


def build_status(run_state: dict, pid: int | None = None) -> dict:
    """Pure function: internal run state -> pane schema (validated, strings truncated).

    `pid` is the process that owns the run: this one unless given (`kickroll status` passes the recorded one)."""
    budget = run_state.get("budget", {})
    limits = run_state.get("limits", {})
    by_model = budget.get("by_model", {}) if isinstance(budget.get("by_model"), dict) else {}
    jev = budget.get("jev", {}) if isinstance(budget.get("jev"), dict) else {}
    plan_tasks = {t["id"]: t for t in run_state.get("plan", {}).get("tasks", []) if isinstance(t, dict) and "id" in t}
    tasks = []
    for tid, t in plan_tasks.items():
        ts = run_state.get("tasks", {}).get(tid, {})
        status = _TASK_MAP.get(ts.get("status", "PENDING"), ts.get("status", "PENDING"))
        status = status if status in TASK_STATUS else "PENDING"
        step = _STEP_MAP.get(ts.get("step"), ts.get("step")) if status == "RUNNING" else "NONE"
        history = ts.get("history") or []
        kind = t.get("kind", "code")
        tasks.append({
            "id": _cut(tid, 16),
            "title": _cut(t.get("title"), 60),
            "kind": kind if kind in KINDS else "code",
            "status": status,
            "step": step if step in STEPS else "NONE",
            "attempt": _int(ts.get("attempt")),
            "max_attempts": _int(ts.get("max_attempts", 3)),
            "model": ts.get("tier") if ts.get("tier") in MODELS else "",
            "depends_on": [_cut(d, 16) for d in t.get("depends_on", [])][:16],
            "failed_criteria": len(history[-1].get("failed", [])) if history else 0,
        })
    status = {
        "version": 2,
        "run_id": _cut(run_state.get("run_id"), 40),
        "repo": _cut(run_state.get("repo"), 400),
        "session": session_id(),
        "pid": os.getpid() if pid is None else _int(pid),
        "status": _RUN_MAP.get(run_state.get("status", "RUNNING"), "RUNNING"),
        "phase": run_state.get("phase") if run_state.get("phase") in PHASES else "PREFLIGHT",
        "note": _cut(run_state.get("note"), 80),
        "updated": now_iso(),
        "started": _cut(run_state.get("started") or now_iso(), 40),
        "budgets": {
            "calls": {"used": _int(budget.get("calls")), "max": _int(limits.get("calls"))},
            "opus": {"used": _int(budget.get("opus")), "max": _int(limits.get("opus"))},
            "planner": {"used": _int(budget.get("planner")), "max": _int(limits.get("planner"))},
            "minutes": {"used": _int(budget.get("elapsed_s", 0) / 60), "max": _int(limits.get("minutes"))},
        },
        "usage": {m: {"calls": _int(by_model.get(m, {}).get("calls")),
                      "tokens": _int(by_model.get(m, {}).get("tokens")),
                      "seconds": _int(by_model.get(m, {}).get("seconds"))} for m in _USAGE_MODELS},
        "jev": {"calls": _int(jev.get("calls")), "errors": _int(jev.get("errors")),
                "seconds": _int(jev.get("seconds"))},
        "tasks": tasks,
    }
    validate_status(status)
    return status


def validate_status(d: dict) -> None:
    """Exactly this schema (version 2, or version 1 for the status.json mirror), otherwise ValueError."""
    def need(cond, msg):
        if not cond:
            raise ValueError(f"status.json invalid: {msg}")

    need(isinstance(d, dict) and d.get("version") in (1, 2), "version")
    need(set(d) == (_TOP_KEYS_V1 if d["version"] == 1 else _TOP_KEYS), "top-level fields")
    if d["version"] == 2:
        need(isinstance(d["repo"], str) and len(d["repo"]) <= 400, "repo")
        need(isinstance(d["session"], str) and len(d["session"]) <= 64, "session")
    need(isinstance(d["pid"], int) and d["pid"] >= 0, "pid")
    need(d["status"] in RUN_STATUS, "status")
    need(d["phase"] in PHASES, "phase")
    need(isinstance(d["note"], str) and len(d["note"]) <= 80, "note")
    need(isinstance(d["run_id"], str) and isinstance(d["updated"], str) and isinstance(d["started"], str), "Strings")
    need(isinstance(d["budgets"], dict) and set(d["budgets"]) == _BUDGET_KEYS, "budgets")
    for b in d["budgets"].values():
        need(isinstance(b, dict) and set(b) == {"used", "max"} and all(isinstance(v, int) for v in b.values()),
             "budget entry")
    need(isinstance(d["usage"], dict) and set(d["usage"]) == set(_USAGE_MODELS), "usage")
    for u in d["usage"].values():
        need(isinstance(u, dict) and set(u) == _USAGE_KEYS and all(isinstance(v, int) for v in u.values()),
             "usage entry")
    need(isinstance(d["jev"], dict) and set(d["jev"]) == _JEV_KEYS and all(isinstance(v, int) for v in d["jev"].values()),
         "jev")
    need(isinstance(d["tasks"], list), "tasks")
    for t in d["tasks"]:
        need(isinstance(t, dict) and set(t) == _TASK_KEYS, "task fields")
        need(t["status"] in TASK_STATUS and t["step"] in STEPS and t["kind"] in KINDS and t["model"] in MODELS,
             "task enums")
        need(len(t["title"]) <= 60 and isinstance(t["depends_on"], list), "task title/depends_on")
        need(all(isinstance(t[k], int) for k in ("attempt", "max_attempts", "failed_criteria")), "task numbers")


def write_status(path: Path, status: dict) -> None:
    validate_status(status)
    atomic_write_json(Path(path), status)


def status_dir(status_path: Path) -> Path:
    """<dir of status.json>/status: one file per entry."""
    return Path(status_path).with_name("status")


def entry_key(key: str) -> str:
    return _KEY_UNSAFE.sub("_", str(key or ""))[:80] or "unknown"


def entry_path(status_path: Path, key: str) -> Path:
    return status_dir(status_path) / f"{entry_key(key)}.json"


def beat_path(status_path: Path, pid: int | None = None) -> Path:
    return status_dir(status_path) / f"pid-{os.getpid() if pid is None else int(pid)}.beat.json"


def legacy(status: dict) -> dict:
    """Schema version 1 for status.json, which panes before 0.2.0 read."""
    return {**{k: v for k, v in status.items() if k in _TOP_KEYS_V1}, "version": 1}


def publish(status_path: Path, status: dict, key: str | None = None) -> Path:
    """Writes the entry status/<key>.json (key: run_id by default), mirrors it into status.json, prunes. -> path"""
    validate_status(status)
    path = entry_path(status_path, key or status["run_id"])
    atomic_write_json(path, status)
    try:
        write_status(Path(status_path), legacy(status))
    except (OSError, ValueError):
        pass  # the mirror is a courtesy for old panes
    prune(status_path, keep=path.name)
    return path


def entries(status_path: Path) -> list[tuple[Path, dict]]:
    """All readable entries in status/ (beat and temp files skipped)."""
    try:
        names = sorted(p for p in status_dir(status_path).iterdir()
                       if p.suffix == ".json" and not p.name.endswith(".beat.json"))
    except OSError:
        return []
    out = []
    for p in names:
        d = read_json(p)
        if isinstance(d, dict):
            out.append((p, d))
    return out


def _age_s(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _updated_age_s(d: dict, path: Path) -> float:
    try:
        return time.time() - datetime.fromisoformat(str(d.get("updated"))).timestamp()
    except (TypeError, ValueError):
        return _age_s(path) or 0.0


def beating(status_path: Path, pid, max_age_s: float = BEAT_MAX_AGE_S) -> bool:
    """Does process `pid` still beat its heartbeat file?"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    age = _age_s(beat_path(status_path, pid))
    return age is not None and age < max_age_s


def _same_repo(a, b) -> bool:
    try:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(Path(b).resolve()))
    except (OSError, ValueError):
        return False


def prune(status_path: Path, keep: str = "", alive=pid_alive) -> list[str]:
    """Removes finished entries older than a day (and beyond the newest 20), entries of runs whose process is gone
    for an hour, and beat files of dead processes. Never the entry `keep` or a live one. -> removed file names"""
    removed, ended = [], []
    for path, d in entries(status_path):
        if path.name == keep:
            continue
        age = _updated_age_s(d, path)
        if d.get("status") == "RUNNING":
            pid = d.get("pid")
            gone = not beating(status_path, pid) and not (isinstance(pid, int) and pid > 0 and alive(pid))
            if gone and age > KEEP_DEAD_S:
                removed.append(path)
        elif age > KEEP_ENDED_S:
            removed.append(path)
        else:
            ended.append((age, path.name, path))
    removed += [p for _, _, p in sorted(ended)[KEEP_ENDED_MAX:]]
    try:
        beats = [p for p in status_dir(status_path).iterdir() if p.name.endswith(".beat.json")]
    except OSError:
        beats = []
    for p in beats:
        try:
            pid = int(p.name[len("pid-"):-len(".beat.json")])
        except ValueError:
            continue
        age = _age_s(p)
        if pid != os.getpid() and age is not None and age > BEAT_MAX_AGE_S and not alive(pid):
            removed.append(p)
    names = []
    for p in removed:
        try:
            p.unlink()
            names.append(p.name)
        except OSError:
            pass
    return names


def remove_entry(status_path: Path, key: str) -> bool:
    try:
        entry_path(status_path, key).unlink()
        return True
    except OSError:
        return False


def _mark_one(path: Path, note: str) -> bool:
    d = read_json(Path(path))
    if not isinstance(d, dict) or d.get("status") != "RUNNING" or d.get("pid") != os.getpid():
        return False
    d.update(status="FAILED", note=note[:80], updated=now_iso())
    try:
        write_status(Path(path), d)
    except (OSError, ValueError):
        return False
    return True


def mark_ended(path: Path, note: str) -> bool:
    """Process exit: every entry (and the status.json mirror) this process left at RUNNING becomes FAILED with
    `note`. Entries of other processes are left alone. -> changed?"""
    changed = [_mark_one(p, note) for p, _ in entries(path)]
    return _mark_one(Path(path), note) or any(changed)


def live_run(path: Path, repo=None, max_age_s: float = BEAT_MAX_AGE_S) -> str | None:
    """run_id of a RUNNING entry whose process still beats (in `repo` when given), else None.

    Without any entry (status.json from kickroll before 0.2.0) the old single file and heartbeat.json count."""
    found = entries(path)
    for _, d in found:
        if d.get("status") != "RUNNING" or not beating(path, d.get("pid"), max_age_s):
            continue
        if repo is None or not d.get("repo") or _same_repo(d["repo"], repo):
            return str(d.get("run_id", ""))
    if found:
        return None
    d = read_json(Path(path))
    legacy_beat = Path(path).with_name("heartbeat.json")
    if not isinstance(d, dict) or d.get("status") != "RUNNING" or not d.get("pid"):
        return None
    beat = read_json(legacy_beat)
    age = _age_s(legacy_beat)
    fresh = age is not None and age < max_age_s
    return str(d.get("run_id", "")) if isinstance(beat, dict) and beat.get("pid") == d["pid"] and fresh else None


class Heartbeat:
    """Daemon thread: rewrites `path` ({pid, beat, ts}) every `every_s` seconds while the process runs.

    `also`: further beat files (status/pid-<pid>.beat.json), removed again when the process ends."""

    def __init__(self, path: Path, every_s: float = 5.0, also=()):
        self.path = Path(path)
        self.also = [Path(p) for p in also]
        self.every_s = every_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="kickroll-heartbeat", daemon=True)
        self.beats = 0

    def _beat(self) -> None:
        self.beats += 1
        beat = {"pid": os.getpid(), "beat": self.beats, "ts": now_iso()}
        for path in [self.path, *self.also]:
            try:
                atomic_write_json(path, beat)
            except OSError:
                pass  # the pane then reports the run as stale; never fail the run for it

    def _loop(self) -> None:
        while True:
            self._beat()
            if self._stop.wait(self.every_s):
                return

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for path in self.also:
            try:
                path.unlink()
            except OSError:
                pass
