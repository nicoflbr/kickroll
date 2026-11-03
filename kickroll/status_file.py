"""Public status file for the desktop pane (<data dir>/status.json; data dir = $KICKROLL_HOME or ~/.kickroll).

Small fixed interface: only IDs, counters, enums, short titles. No prompts, diffs, error output, secrets.
Written atomically on every status change and never deleted; state.json stays separate.
`pid` names the writing process; while it lives it also beats <data dir>/heartbeat.json, so the pane can tell a
dead run from a live one (a RUNNING status.json without a matching, moving heartbeat is stale).
"""
import os
import threading
from pathlib import Path

from .store import atomic_write_json, now_iso, read_json

RUN_STATUS = ("RUNNING", "PAUSED", "SUCCESS", "PARTIAL", "FAILED")
PHASES = ("PREFLIGHT", "BYPASS", "PLAN", "APPROVAL", "EXECUTE", "DONE")
TASK_STATUS = ("PENDING", "READY", "RUNNING", "ACCEPTED", "MERGED", "BLOCKED", "FAILED", "SKIPPED", "UNCLEAR")
KINDS = ("code", "analysis", "review", "test")
STEPS = ("ROUTE", "WORK", "L0", "L1", "L3", "COMMIT", "NONE")
MODELS = ("haiku", "sonnet", "opus", "")

_TOP_KEYS = {"version", "run_id", "pid", "status", "phase", "note", "updated", "started", "budgets", "usage",
             "jev", "tasks"}
_TASK_KEYS = {"id", "title", "kind", "status", "step", "attempt", "max_attempts", "model", "depends_on",
              "failed_criteria"}
_BUDGET_KEYS = {"calls", "opus", "planner", "minutes"}
_USAGE_MODELS = ("haiku", "sonnet", "opus")
_USAGE_KEYS = {"calls", "tokens", "seconds"}
_JEV_KEYS = {"calls", "errors", "seconds"}

# internal run states -> pane
_RUN_MAP = {"PLANNING": "RUNNING", "EXECUTING": "RUNNING", "RUNNING": "RUNNING", "PAUSED": "PAUSED",
            "SUCCESS": "SUCCESS", "PARTIAL": "PARTIAL", "ABORTED": "FAILED", "FAILED": "FAILED"}
_TASK_MAP = {"VERIFYING": "RUNNING", "RETRY": "RUNNING"}
_STEP_MAP = {"L2": "L1"}  # L2 (Jev shadow) is shown as L1 in the pane (quick check)


def _cut(value, n: int) -> str:
    return str(value or "")[:n]


def _int(value) -> int:
    try:
        return max(0, int(round(float(value or 0))))
    except (TypeError, ValueError):
        return 0


def build_status(run_state: dict) -> dict:
    """Pure function: internal run state -> pane schema (validated, strings truncated)."""
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
        "version": 1,
        "run_id": _cut(run_state.get("run_id"), 40),
        "pid": os.getpid(),
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
    """Exactly this schema, otherwise ValueError."""
    def need(cond, msg):
        if not cond:
            raise ValueError(f"status.json invalid: {msg}")

    need(isinstance(d, dict) and set(d) == _TOP_KEYS, "top-level fields")
    need(d["version"] == 1, "version")
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


def mark_ended(path: Path, note: str) -> bool:
    """Process exit: a status.json this process left at RUNNING becomes FAILED with `note`. -> changed?"""
    d = read_json(Path(path))
    if not isinstance(d, dict) or d.get("status") != "RUNNING" or d.get("pid") != os.getpid():
        return False
    d.update(status="FAILED", note=note[:80], updated=now_iso())
    try:
        write_status(Path(path), d)
    except (OSError, ValueError):
        return False
    return True


class Heartbeat:
    """Daemon thread: rewrites heartbeat.json ({pid, beat, ts}) every `every_s` seconds while the process runs."""

    def __init__(self, path: Path, every_s: float = 5.0):
        self.path = Path(path)
        self.every_s = every_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="kickroll-heartbeat", daemon=True)
        self.beats = 0

    def _beat(self) -> None:
        self.beats += 1
        try:
            atomic_write_json(self.path, {"pid": os.getpid(), "beat": self.beats, "ts": now_iso()})
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
