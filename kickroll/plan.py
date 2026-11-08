"""Plan schema (v1 §3 with v2 changes), validation, L1 command templates and path globs."""
import copy
import re
from collections.abc import Iterable
from functools import lru_cache

from . import TIERS

SCHEMA_VERSION = 1
KINDS = ("code", "test", "analysis", "review")
WRITE_KINDS = ("code", "test")
RISKS = ("low", "medium", "high")
CRITERION_TYPES = ("file_exists", "file_contains", "command", "json_schema", "judgement")

DEFAULT_FORBIDDEN = [
    "**/package.json", "**/conftest.py", "**/pytest.ini", "**/setup.cfg", "**/pyproject.toml", "**/tox.ini",
    ".github/**", ".claude/**", "**/.mcp.json",
    "**/.env*", "**/*secret*", "**/*.pem", ".git/**",
]

# L1 templates: module -> (allowed switches without value, switches with value). No -c/-e, no shell.
TEMPLATES = {
    "unittest": ({"-q", "-v", "-f", "-b"}, {"-s", "-t", "-p", "-k"}),
    "pytest": ({"-q", "-v", "-x", "-s", "--no-header", "-rA"}, {"-k"}),
    "py_compile": (set(), set()),
}
_SAFE_ARG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./\\:\-]*$")
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_.*/\\\- ]{1,80}$")
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,15}$")


# ---------- Paths ----------

def norm_path(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def path_error(path: str) -> str | None:
    """Relative path in the repo? Otherwise error text."""
    if not isinstance(path, str) or not path.strip():
        return "empty path"
    p = path.replace("\\", "/")
    if p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        return f"absolute path: {p}"
    if ".." in p.split("/"):
        return f"'..' in path: {p}"
    if p.split("/")[0].lower() == ".git":
        return f".git is not allowed: {p}"
    return None


@lru_cache(maxsize=512)
def _glob_regex(pattern: str) -> re.Pattern:
    pat = pattern.replace("\\", "/")
    if pat.endswith("/"):
        pat += "**"
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def glob_match(path: str, pattern: str) -> bool:
    return bool(_glob_regex(pattern).match(norm_path(path)))


def matches_any(path: str, patterns) -> bool:
    return any(glob_match(path, p) for p in patterns)


# ---------- Commands ----------

def command_error(cmd) -> str | None:
    """Only templates `python -m unittest|pytest|py_compile ...` as argv list."""
    if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
        return "command must be an argv list of strings"
    if len(cmd) < 3 or cmd[0] != "python" or cmd[1] != "-m" or cmd[2] not in TEMPLATES:
        return "only templates allowed: python -m unittest|pytest|py_compile ..."
    flags, value_opts = TEMPLATES[cmd[2]]
    rest, i = cmd[3:], 0
    while i < len(rest):
        arg = rest[i]
        if arg in ("-c", "-e") or arg.startswith(("-c", "-e")) and arg not in flags:
            return f"switch not allowed: {arg}"
        if arg.startswith("-"):
            if arg in flags:
                i += 1
                continue
            if arg in value_opts and i + 1 < len(rest) and _SAFE_VALUE.match(rest[i + 1]):
                if arg in ("-s", "-t") and path_error(rest[i + 1]):
                    return f"invalid path: {rest[i + 1]}"
                i += 2
                continue
            return f"switch not in template: {arg}"
        if cmd[2] == "unittest" and arg == "discover" and i == 0:
            i += 1
            continue
        if not _SAFE_ARG.match(arg) or path_error(arg.split("::")[0]):
            return f"argument not allowed: {arg}"
        i += 1
    return None


def test_targets(cmd: list) -> tuple[str, list[str]]:
    """(kind, targets): kind 'module' (unittest, dotted), 'path' (pytest), 'compile', 'discover'."""
    mod, args = cmd[2], cmd[3:]
    if mod == "unittest" and "discover" in args:
        return "discover", []
    positional, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-"):
            skip = a in TEMPLATES[mod][1]
            continue
        positional.append(a)
    kind = {"unittest": "module", "pytest": "path", "py_compile": "compile"}[mod]
    if kind == "path":
        positional = [norm_path(p.split("::")[0]) for p in positional]
    return kind, positional


def target_covers(kind: str, target: str, path: str) -> bool:
    """Does the file `path` belong to the test target?"""
    p = norm_path(path)
    if kind == "module":
        if not p.endswith(".py"):
            return False
        dotted = p[:-3].replace("/", ".")
        return target == dotted or target.startswith(dotted + ".")
    return p.lower() == norm_path(target).lower() or p.lower().startswith(norm_path(target).rstrip("/").lower() + "/")


# ---------- Plan ----------

def normalize_plan(plan: dict) -> dict:
    """Insert defaults (forbidden defaults always included); the original stays unchanged."""
    p = copy.deepcopy(plan)
    p.setdefault("schema_version", SCHEMA_VERSION)
    p.setdefault("acceptance_files", [])
    p.setdefault("global_checks", [])
    for t in p.get("tasks", []) if isinstance(p.get("tasks"), list) else []:
        if not isinstance(t, dict):
            continue
        t.setdefault("depends_on", [])
        t.setdefault("risk", "medium")
        t.setdefault("kind", "code" if t.get("writes") else "analysis")
        t.setdefault("writes", [])
        t.setdefault("inputs", {"files": [], "artifacts": []})
        forb = list(t.get("forbidden") or [])
        t["forbidden"] = forb + [f for f in DEFAULT_FORBIDDEN if f not in forb]
        t.setdefault("budget", {})
        _assign_criterion_ids(t)
    return p


def _assign_criterion_ids(task: dict) -> None:
    """Planners often omit criterion IDs. Give every criterion without a usable ID one (<task>-A<n>), in order."""
    crits = task.get("acceptance")
    if not isinstance(crits, list):
        return
    taken = set()
    for c in crits:
        if isinstance(c, dict) and isinstance(c.get("id"), str) and _ID.match(c["id"]) and c["id"] not in taken:
            taken.add(c["id"])
    n = 0
    seen = set()
    for c in crits:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        if isinstance(cid, str) and _ID.match(cid) and cid not in seen:
            seen.add(cid)
            continue
        while True:
            n += 1
            cid = f"{task.get('id', 'T')}-A{n}"
            if cid not in taken and _ID.match(cid):
                break
        c["id"] = cid
        taken.add(cid)
        seen.add(cid)


def max_attempts(task: dict, risk: str | None = None) -> int:
    risk = risk or task.get("risk", "medium")
    default = 4 if risk == "high" else 3
    budget = task.get("budget")
    value = budget.get("max_attempts", default) if isinstance(budget, dict) else default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        value = default  # planner output: "abc", None, ...
    return max(1, min(4, int(value)))


def ancestors(tasks: dict, tid: str) -> set[str]:
    seen, stack = set(), list(tasks[tid].get("depends_on", []))
    while stack:
        d = stack.pop()
        if d in seen or d not in tasks:
            continue
        seen.add(d)
        stack.extend(tasks[d].get("depends_on", []))
    return seen


def validate_plan(plan: dict, max_tasks: int = 8, existing: Iterable[str] = ()) -> list[str]:
    """List of errors (empty = valid). Expects a normalized plan.

    `existing` are the files already tracked in the repository: tests that are already committed and not
    written by the accepted task are trusted as acceptance tests (regression checks).
    """
    errors: list[str] = []
    if plan.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    tasks_list = plan.get("tasks")
    if not isinstance(tasks_list, list) or not tasks_list:
        return errors + ["tasks missing or empty"]
    if len([t for t in tasks_list if not (isinstance(t, dict) and t.get("added_by") == ADDED_BY)]) > max_tasks:
        errors.append(f"at most {max_tasks} tasks allowed")
    tasks: dict = {}
    for t in tasks_list:
        tid = t.get("id") if isinstance(t, dict) else None
        if not isinstance(tid, str) or not _ID.match(tid):
            errors.append(f"invalid task ID: {tid!r}")
            continue
        if tid in tasks:
            errors.append(f"duplicate task ID: {tid}")
        tasks[tid] = t
    for key in ("global_checks", "acceptance_files"):
        if not isinstance(plan.get(key, []), list):
            errors.append(f"{key} must be a list")
            plan[key] = []
    for i, cmd in enumerate(plan.get("global_checks", [])):
        if err := command_error(cmd):
            errors.append(f"global_checks[{i}]: {err}")

    acc_files = []
    for f in plan.get("acceptance_files", []):
        path = f.get("path") if isinstance(f, dict) else None
        if not isinstance(path, str) or (err := path_error(path)) or not isinstance(f.get("content"), str):
            errors.append(f"acceptance_files: invalid entry {path!r}")
            continue
        if matches_any(path, DEFAULT_FORBIDDEN):
            errors.append(f"acceptance_files: forbidden path {path}")
        acc_files.append(norm_path(path))

    dag_errors = _check_dag(tasks)
    errors += dag_errors
    for tid, t in tasks.items():
        errors += [f"{tid}: {e}" for e in _check_task(t)]
    if not dag_errors:
        errors += _check_test_sources(tasks, acc_files, [norm_path(e) for e in existing])
    return errors


def _check_dag(tasks: dict) -> list[str]:
    errors = []
    for tid, t in tasks.items():
        deps = t.get("depends_on", [])
        if not _str_list(deps):
            errors.append(f"{tid}: depends_on must be a list of task IDs")
            t["depends_on"] = []
            continue
        for d in deps:
            if d not in tasks:
                errors.append(f"{tid}: unknown dependency {d}")
    state: dict = {}

    def visit(n: str) -> bool:
        if state.get(n) == 1:
            return False
        if state.get(n) == 2:
            return True
        state[n] = 1
        ok = all(visit(d) for d in tasks[n].get("depends_on", []) if d in tasks)
        state[n] = 2
        return ok

    if not all(visit(n) for n in tasks):
        errors.append("Dependencies contain a cycle")
    return errors


def _check_task(t: dict) -> list[str]:
    errors = []
    for key in ("title", "description"):
        if not isinstance(t.get(key), str) or not t[key].strip():
            errors.append(f"{key} missing")
    if isinstance(t.get("description"), str) and len(t["description"]) > 1500:
        errors.append("description > 1500 characters")
    kind = t.get("kind")
    if kind not in KINDS:
        errors.append(f"kind must be one of {KINDS}")
    if t.get("risk") not in RISKS:
        errors.append(f"risk must be one of {RISKS}")
    if t.get("model_hint") not in (None, *TIERS):
        errors.append("model_hint invalid")
    if t.get("suggested_tier") not in (None, *TIERS):
        errors.append("suggested_tier must be haiku|sonnet|opus")
    cx = t.get("complexity")
    if cx is not None and (not isinstance(cx, (int, float)) or isinstance(cx, bool) or not 0 <= cx <= 2):
        errors.append("complexity must be a number 0..2")
    writes = t.get("writes", [])
    if not isinstance(writes, list) or not all(isinstance(w, str) for w in writes):
        errors.append("writes must be a list of strings")
        writes = []
    if not isinstance(t.get("forbidden", []), list):
        errors.append("forbidden must be a list of strings")
        t["forbidden"] = []
    if kind in WRITE_KINDS and not writes:
        errors.append("code/test task needs writes")
    if kind not in WRITE_KINDS and writes:
        errors.append("analysis/review task must not write anything")
    for w in writes:
        if err := path_error(w):
            errors.append(f"writes: {err}")
        elif w.strip("/*") == "" or w in ("**", "*", "**/*"):
            errors.append(f"writes too broad: {w}")
        elif matches_any(w, t.get("forbidden", [])):
            errors.append(f"writes overlap forbidden: {w}")
    crits = t.get("acceptance")
    if not isinstance(crits, list) or not crits:
        return errors + ["at least one acceptance criterion required"]
    ids = set()
    has_test = False
    for c in crits:
        if not isinstance(c, dict):
            errors.append(f"criterion must be an object: {str(c)[:40]!r}")
            continue
        cid, ctype = c.get("id"), c.get("type")
        if not isinstance(cid, str) or not _ID.match(cid) or cid in ids:
            errors.append(f"criterion ID invalid/duplicate: {cid!r}")
        ids.add(cid)
        if ctype not in CRITERION_TYPES:
            errors.append(f"{cid}: unknown type {ctype!r}")
            continue
        if ctype in ("file_exists", "file_contains", "json_schema") and (err := path_error(c.get("path", ""))):
            errors.append(f"{cid}: {err}")
        if ctype == "file_contains":
            try:
                re.compile(c.get("pattern", ""))
            except re.error:
                errors.append(f"{cid}: invalid regex")
        if ctype == "judgement" and not str(c.get("text", "")).strip():
            errors.append(f"{cid}: judgement without text")
        if ctype == "command":
            if err := command_error(c.get("cmd")):
                errors.append(f"{cid}: {err}")
                continue
            tkind, targets = test_targets(c["cmd"])
            if tkind in ("module", "path"):
                if not targets:
                    errors.append(f"{cid}: test command needs explicit targets")
                    continue
                has_test = True
                if kind == "code" and any(target_covers(tkind, tg, w) for tg in targets for w in writes):
                    errors.append(f"{cid}: acceptance test comes from its own writes")
    if kind == "code" and not has_test:
        errors.append("code task needs at least one acceptance test (unittest/pytest with a target)")
    return errors


def _str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _check_test_sources(tasks: dict, acc_files: list[str], existing: list[str] = ()) -> list[str]:
    """Acceptance tests of code tasks must come from acceptance_files, an upstream test task or a test
    that is already committed and not written by that task."""
    errors = []
    frozen_by_test = {tid: [norm_path(w) for w in t.get("writes", [])]
                      for tid, t in tasks.items() if t.get("kind") == "test" and _str_list(t.get("writes"))}
    all_frozen = set(acc_files) | {p for ps in frozen_by_test.values() for p in ps}
    for tid, t in tasks.items():
        if t.get("kind") != "code" or not _str_list(t.get("writes")) or not isinstance(t.get("acceptance"), list):
            continue  # reported by _check_task
        for w in t.get("writes", []):
            if any(glob_match(f, w) for f in all_frozen):
                errors.append(f"{tid}: writes overwrite a frozen test file ({w})")
        anc = ancestors(tasks, tid)
        own_writes = [norm_path(w) for w in t.get("writes", [])]
        trusted = [e for e in existing if not any(glob_match(w, e) for w in own_writes)]
        sources = list(acc_files) + [p for a in anc for p in frozen_by_test.get(a, [])] + trusted
        for c in t.get("acceptance", []):
            if not isinstance(c, dict) or c.get("type") != "command" or command_error(c.get("cmd")):
                continue
            tkind, targets = test_targets(c["cmd"])
            if tkind not in ("module", "path"):
                continue
            for tg in targets:
                if not any(target_covers(tkind, tg, s) for s in sources):
                    have = ", ".join(sorted(set(sources) - set(trusted))[:6]) or "none"
                    errors.append(f"{tid}/{c.get('id')}: test target {tg} not from acceptance_files/upstream test task "
                                  f"(frozen test files in this plan: {have})")
    return errors


ADDED_BY = "kickroll"  # task["added_by"] of test tasks that repair_test_sources inserted
_TEST_PART = re.compile(r"^(test_\w*|\w+_test|test)$")


def _test_file_for(kind: str, target: str, tid: str) -> str | None:
    """File a missing test target would live in (`tests.test_cli.CliTest` -> `tests/test_cli.py`)."""
    if kind == "module":
        parts = target.split(".")
        cut = next((i for i, p in enumerate(parts) if _TEST_PART.match(p)), len(parts) - 1)
        path = "/".join(parts[:cut + 1]) + ".py"
    elif target.endswith(".py"):
        path = norm_path(target)
    else:
        path = f"{norm_path(target).rstrip('/')}/test_{tid.lower()}.py"
    if path_error(path) or matches_any(path, DEFAULT_FORBIDDEN):
        return None
    return path


def repair_test_sources(plan: dict, existing: Iterable[str] = ()) -> list[str]:
    """Fix the planner's most common slip in place instead of rejecting the plan: a code task's acceptance
    test that nobody provides. If a test task already writes it, the missing dependency is added; otherwise a
    test task is inserted that writes the file before the implementation (it is frozen once accepted).
    Returns one note per repair. Plans with structural errors are left alone (validate_plan reports them)."""
    tasks_list = plan.get("tasks")
    if not isinstance(tasks_list, list) or not all(isinstance(t, dict) and isinstance(t.get("id"), str)
                                                   for t in tasks_list):
        return []
    tasks = {t["id"]: t for t in tasks_list}
    if len(tasks) != len(tasks_list) or not isinstance(plan.get("acceptance_files", []), list) or _check_dag(tasks):
        return []
    acc_files = [norm_path(f["path"]) for f in plan.get("acceptance_files", [])
                 if isinstance(f, dict) and isinstance(f.get("path"), str)]
    existing = [norm_path(e) for e in existing]
    notes = []
    for tid in [t["id"] for t in tasks_list]:
        t = tasks[tid]
        if t.get("kind") != "code" or not _str_list(t.get("writes")) or not isinstance(t.get("acceptance"), list):
            continue
        own_writes = [norm_path(w) for w in t["writes"]]
        test_writers = {k: [norm_path(w) for w in x["writes"]] for k, x in tasks.items()
                        if x.get("kind") == "test" and _str_list(x.get("writes"))}
        new_files = []
        for c in t["acceptance"]:
            if not isinstance(c, dict) or c.get("type") != "command" or command_error(c.get("cmd")):
                continue
            tkind, targets = test_targets(c["cmd"])
            if tkind not in ("module", "path"):
                continue
            for tg in targets:
                anc = ancestors(tasks, tid)
                trusted = [e for e in existing if not any(glob_match(w, e) for w in own_writes)]
                sources = acc_files + [p for a in anc for p in test_writers.get(a, [])] + trusted + new_files
                if any(target_covers(tkind, tg, s) for s in sources):
                    continue
                writer = next((k for k, ps in test_writers.items() if k != tid and tid not in ancestors(tasks, k)
                               and any(target_covers(tkind, tg, p) for p in ps)), None)
                if writer:
                    t.setdefault("depends_on", []).append(writer)
                    notes.append(f"{tid}: added dependency on test task {writer} (provides {tg})")
                    continue
                path = _test_file_for(tkind, tg, tid)
                if path and not any(glob_match(path, w) for w in own_writes) and path not in new_files:
                    new_files.append(path)
        if new_files:
            new_id = next(f"T{n}" for n in range(1, 1000) if f"T{n}" not in tasks)
            tasks[new_id] = _auto_test_task(new_id, t, new_files)
            tasks_list.insert(tasks_list.index(t), tasks[new_id])
            t.setdefault("depends_on", []).append(new_id)
            notes.append(f"{tid}: added test task {new_id} that writes {', '.join(new_files)} first")
    return notes


def _auto_test_task(new_id: str, code: dict, files: list[str]) -> dict:
    cmds = "; ".join(" ".join(c["cmd"]) for c in code.get("acceptance", [])
                     if isinstance(c, dict) and c.get("type") == "command" and _str_list(c.get("cmd")))
    desc = (f"Write the acceptance tests for task {code['id']} before it is implemented, in exactly these files: "
            f"{', '.join(files)}. Test the behaviour task {code['id']} asks for, concretely (real inputs and expected "
            f"outputs, no placeholder asserts). The tests are expected to fail until {code['id']} is done; they will "
            f"later run as: {cmds}.\nTask {code['id']} ({code.get('title', '')}): {code.get('description', '')}")
    hints = [norm_path(w) for w in code.get("writes", []) if "*" not in w]
    crits = [{"id": f"{new_id}-A{i + 1}", "type": "file_exists", "path": p} for i, p in enumerate(files)]
    crits.append({"id": f"{new_id}-A{len(files) + 1}", "type": "command", "cmd": ["python", "-m", "py_compile", *files]})
    return {"id": new_id, "title": f"Tests for {code['id']}: {code.get('title', '')}"[:60], "description": desc[:1500],
            "kind": "test", "risk": "low", "complexity": 0.8, "suggested_tier": "sonnet",
            "depends_on": list(code.get("depends_on", [])), "writes": list(files),
            "forbidden": list(DEFAULT_FORBIDDEN), "inputs": {"files": hints, "artifacts": []}, "budget": {},
            "acceptance": crits, "added_by": ADDED_BY}


def frozen_paths_for(plan: dict, tid: str | None = None) -> list[str]:
    """Frozen paths: acceptance_files + writes of all test tasks (or only those of one task)."""
    if tid is not None:
        t = next(t for t in plan["tasks"] if t["id"] == tid)
        return [norm_path(w) for w in t.get("writes", [])] if t.get("kind") == "test" else []
    return [norm_path(f["path"]) for f in plan.get("acceptance_files", [])]


# JSON schema for the planner call (--json-schema); deliberately kept flat.
PLANNER_SCHEMA = {
    "type": "object",
    "required": ["goal_summary", "tasks"],
    "properties": {
        "goal_summary": {"type": "string", "maxLength": 300},
        "acceptance_files": {"type": "array", "items": {
            "type": "object", "required": ["path", "content"],
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}}}},
        "tasks": {"type": "array", "maxItems": 8, "items": {
            "type": "object",
            "required": ["id", "title", "description", "kind", "risk", "complexity", "suggested_tier", "acceptance"],
            "properties": {
                "id": {"type": "string"}, "title": {"type": "string", "maxLength": 60},
                "description": {"type": "string", "maxLength": 1500},
                "kind": {"enum": list(KINDS)}, "risk": {"enum": list(RISKS)},
                "model_hint": {"enum": list(TIERS)},
                "complexity": {"type": "number", "minimum": 0, "maximum": 2},
                "suggested_tier": {"enum": list(TIERS)},
                "depends_on": {"type": "array", "items": {"type": "string"}},
                "inputs": {"type": "object"},
                "writes": {"type": "array", "items": {"type": "string"}},
                "forbidden": {"type": "array", "items": {"type": "string"}},
                "acceptance": {"type": "array", "items": {"type": "object"}},
                "budget": {"type": "object"},
            }}},
    },
}


def tracked_files(repo: str) -> list[str]:
    """Files tracked by git in `repo` (empty list if git fails); read-only."""
    import subprocess
    try:
        out = subprocess.run(["git", "-C", str(repo), "ls-files", "-z"], capture_output=True, check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]
