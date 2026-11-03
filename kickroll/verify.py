"""Acceptance cascade: L0 (structure/paths/frozen tests), L1 (template commands), helpers for L3.

L2 (Jev) lives in jev.py and is only a fail filter / shadow log.
"""
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .gitops import file_sha256
from .plan import WRITE_KINDS, command_error, glob_match, matches_any, norm_path
from .safety import child_env, tail_lines


class PreflightError(RuntimeError):
    pass


@dataclass
class Outcome:
    passed: bool
    failed: list = field(default_factory=list)  # criterion IDs
    feedback: str = ""  # local only for the next attempt, never logged
    stage: str = ""
    env_problem: bool = False
    hard: bool = False
    unclear: bool = False
    terminal: bool = False  # FAILED immediately (e.g. reviewer budget exhausted)
    call_error: str | None = None  # error class of a check call (pause)


# ---------- Worker report ----------

def extract_report(text: str) -> dict | None:
    """Find the last JSON block with status/summary/files_changed (the report is data)."""
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    candidates += re.findall(r"(\{[^{}]*\"status\"[^{}]*\})", text or "", re.S)
    for raw in reversed(candidates):
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and {"status", "summary", "files_changed"} <= set(data):
            if isinstance(data["status"], str) and isinstance(data["summary"], str):
                return data
    return None


# ---------- L0 ----------

def level0(task: dict, result_text: str, changed: list[str], frozen: dict, wt: Path) -> tuple[Outcome, dict | None]:
    report = extract_report(result_text)
    if report is None:
        return Outcome(False, ["L0:report"], "Output format violated: JSON block {status, summary, files_changed} missing.",
                       "L0"), None
    status = report["status"].strip().lower()
    if status == "unclear":
        return Outcome(False, ["L0:unclear"], "", "L0", unclear=True), report
    failed, notes, hard = [], [], False
    writes, forbidden = task.get("writes", []), task.get("forbidden", [])
    frozen_paths = {norm_path(p).lower() for p in frozen}
    if task.get("kind") in WRITE_KINDS and not changed:
        failed.append("L0:empty_diff")
        notes.append("No change in the working tree.")
    if task.get("kind") not in WRITE_KINDS and changed:
        failed.append("L0:read_only")
        notes.append("Read-only task changed files.")
        hard = True
    for p in changed:
        if matches_any(p, forbidden):
            failed.append("L0:forbidden")
            notes.append(f"Forbidden file changed: {p}")
            hard = True
        elif norm_path(p).lower() in frozen_paths:
            failed.append("L0:frozen")
            notes.append(f"Frozen test file changed: {p}")
            hard = True
        elif task.get("kind") in WRITE_KINDS and not any(glob_match(p, w) for w in writes):
            failed.append("L0:outside_writes")
            notes.append(f"File outside writes: {p}")
            hard = True
    for p, sha in frozen.items():
        if file_sha256(Path(wt) / p) != sha and "L0:frozen" not in failed:
            failed.append("L0:frozen")
            notes.append(f"Frozen test file changed/deleted: {p}")
            hard = True
    if status != "done" and not failed:
        failed.append("L0:status")
        notes.append(f"Report says status={status!r} instead of 'done'.")
    failed = sorted(set(failed))
    return Outcome(not failed, failed, "\n".join(notes), "L0", hard=hard), report


# ---------- L1 ----------

def resolve_python(setting: str, repo: Path) -> str:
    """'auto': the repo's venv (.venv/venv), otherwise this interpreter. An explicit path must exist."""
    if setting and setting != "auto":
        p = Path(setting)
        p = p if p.is_absolute() else Path(repo) / p
        if not p.is_file():
            raise PreflightError(f"Python/venv missing: {p} (check checks.python in kickroll.toml or create the venv).")
        return str(p)
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe", ".venv/bin/python", "venv/bin/python"):
        if (Path(repo) / rel).is_file():
            return str(Path(repo) / rel)
    return sys.executable


_ENV_PATTERNS = re.compile(r"No module named '?(pytest|unittest|py_compile)'?|is not recognized as an internal", re.I)


def run_template(cmd: list, wt: Path, python: str, timeout_s: float) -> tuple[int | None, str, bool]:
    """Template command as argv (no shell). -> (exitcode|None on timeout, output, env_problem)."""
    if err := command_error(cmd):
        return 2, f"Command rejected: {err}", False
    argv = [python] + list(cmd[1:])
    try:
        proc = subprocess.run(argv, cwd=str(wt), capture_output=True, timeout=timeout_s, env=child_env(),
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return 127, f"Interpreter not found: {python}", True
    except subprocess.TimeoutExpired:
        return None, f"Timeout after {timeout_s}s", False
    out = (proc.stdout + b"\n" + proc.stderr).decode("utf-8", errors="replace")
    return proc.returncode, out, bool(proc.returncode and _ENV_PATTERNS.search(out))


_UNITTEST_FAIL = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([^)]+)\)", re.M)
_PYTEST_FAIL = re.compile(r"^(?:FAILED|ERROR) (\S+\.py\S*)", re.M)


def failing_ids(rc: int | None, output: str) -> list[str]:
    """Failed test IDs (unittest/pytest); without recognizable IDs: the command as a whole."""
    if rc == 0:
        return []
    ids = {m.group(2) if "." in m.group(2) else m.group(1) for m in _UNITTEST_FAIL.finditer(output)}
    ids |= {m.group(1) for m in _PYTEST_FAIL.finditer(output)}
    return sorted(ids) or ["__command__"]


def run_checks(checks: list, wt: Path, python: str, timeout_s: float) -> dict:
    """{index: {"ids": [...], "env": bool, "out": tail}} for global_checks."""
    result = {}
    for i, cmd in enumerate(checks):
        rc, out, env = run_template(cmd, wt, python, timeout_s)
        result[str(i)] = {"ids": failing_ids(rc, out), "env": env, "out": tail_lines(out)}
    return result


def _json_schema_ok(value, schema: dict) -> bool:
    """Minimal structure check (type/required/properties/items/enum)."""
    types = {"object": dict, "array": list, "string": str, "number": (int, float), "integer": int,
             "boolean": bool, "null": type(None)}
    t = schema.get("type")
    if t and not isinstance(value, types.get(t, object)):
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, dict):
        if any(k not in value for k in schema.get("required", [])):
            return False
        return all(_json_schema_ok(value[k], s) for k, s in schema.get("properties", {}).items() if k in value)
    if isinstance(value, list) and "items" in schema:
        return all(_json_schema_ok(v, schema["items"]) for v in value)
    return True


def level1(task: dict, wt: Path, python: str, timeout_s: float, changed: list[str],
           checks: list, baseline: dict) -> Outcome:
    failed, notes, env = [], [], False
    for c in task.get("acceptance", []):
        cid, ctype = c.get("id"), c.get("type")
        path = Path(wt) / c.get("path", "") if c.get("path") else None
        if ctype == "file_exists":
            if not (path and path.is_file()):
                failed.append(cid)
                notes.append(f"[{cid}] File missing: {c.get('path')}")
        elif ctype == "file_contains":
            text = path.read_text(encoding="utf-8", errors="replace") if path and path.is_file() else ""
            if not re.search(c.get("pattern", ""), text, re.M):
                failed.append(cid)
                notes.append(f"[{cid}] Pattern not found in {c.get('path')}")
        elif ctype == "json_schema":
            try:
                ok = _json_schema_ok(json.loads(path.read_text(encoding="utf-8")), c.get("schema", {}))
            except (OSError, ValueError, AttributeError):
                ok = False
            if not ok:
                failed.append(cid)
                notes.append(f"[{cid}] JSON structure does not match: {c.get('path')}")
        elif ctype == "command":
            rc, out, is_env = run_template(c["cmd"], wt, python, timeout_s)
            if rc != c.get("expect_exit", 0):
                failed.append(cid)
                env = env or is_env
                notes.append(f"[{cid}] Exit code {rc}:\n{tail_lines(out)}")
    py_files = [p for p in changed if p.endswith(".py") and (Path(wt) / p).is_file()]
    if py_files:
        rc, out, is_env = run_template(["python", "-m", "py_compile", *py_files], wt, python, timeout_s)
        if rc != 0:
            failed.append("L1:py_compile")
            env = env or is_env
            notes.append(f"[py_compile]\n{tail_lines(out)}")
    if task.get("kind") in WRITE_KINDS and checks:
        # test task: failures of its own new tests are expected (implementation follows), they do not count.
        own = [p[:-3].replace("/", ".") for p in changed if p.endswith(".py")] if task.get("kind") == "test" else []
        for idx, res in run_checks(checks, wt, python, timeout_s).items():
            new = sorted(i for i in set(res["ids"]) - set(baseline.get(idx, []))
                         if not any(m in i or m.rsplit(".", 1)[-1] in i.split(".") for m in own)
                         and not (own and i == "__command__"))
            if new:
                failed.append(f"G{idx}")
                env = env or res["env"]
                notes.append(f"[G{idx}] new failures: {', '.join(new[:10])}\n{res['out']}")
    return Outcome(not failed, failed, "\n".join(notes), "L1", env_problem=env and bool(failed))


# ---------- L3 ----------

def parse_review(text: str, structured: dict | None, ids: list[str]) -> tuple[bool, list[str]]:
    """Reviewer reply strictly: {"<id>": "pass|fail", ..., "reason": "..."}; anything else counts as fail."""
    data = structured
    if data is None:
        for raw in reversed(re.findall(r"\{.*?\}", text or "", re.S)):
            try:
                data = json.loads(raw)
                break
            except ValueError:
                continue
    if not isinstance(data, dict):
        return False, list(ids)
    failed = [i for i in ids if str(data.get(i, "")).strip().lower() != "pass"]
    return not failed, failed
