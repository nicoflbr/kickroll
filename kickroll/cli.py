"""Orchestrator v1 - CLI (works with or without Jev).

    kickroll plan "request" [--test-cmd "python -m unittest discover -s tests"] [--yes] [--fake]
    kickroll run --plan plan.json [--yes] [--dry] [--fake]
    kickroll resume <run_id> [--fake]
    kickroll status <run_id>
    kickroll clean <run_id> [--delete-branch]
    kickroll bypassed "reason"
    kickroll bench --goal-file request.txt [--modes sonnet,orch] [--fake]

ONLY FOR YOUR OWN, TRUSTED REPOS: worker code and tests run unsandboxed under your account.
"""
import argparse
import json
import shlex
import sys
import time
from pathlib import Path

from kickroll.budget import Accountant
from kickroll.config import ConfigError, load_config
from kickroll.engine import Orchestrator, run8
from kickroll.gitops import Git, GitError
from kickroll import messages as msg
from kickroll.config import home_dir
from kickroll.jev import FakeJev, JevService, TypeSafeJev
from kickroll.plan import command_error, normalize_plan, tracked_files, validate_plan
from kickroll.planner import PlanError, create_plan, new_run_id, planner_tier
from kickroll.routing import HaikuDecider, resolve_backend
from kickroll.runner import ClaudeCall, ClaudeNotFound, ClaudeRunner, ErrorClass, FakeRunner
from kickroll.status_file import (Heartbeat, beat_path, build_status, entry_path, live_run, mark_ended, publish,
                                  remove_entry, session_id)
from kickroll.store import DailyUsage, atomic_write_json, now_iso, read_json
from kickroll.verify import PreflightError, resolve_python, run_checks

WARNING = msg.TRUST_WARNING
EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, EXIT_PAUSED, EXIT_INTERRUPT = 0, 1, 2, 3, 130


def say(msg: str) -> None:
    print(f"[kickroll] {msg}", file=sys.stderr)


# ---------- Building blocks ----------

def make_runner(cfg, fake: bool):
    return FakeRunner() if fake else ClaudeRunner(cfg.setting_sources, cfg.extra_claude_args)


_HINTS_SHOWN: set = set()


def make_jev(cfg, repo: Path, fake: bool, runner=None) -> JevService:
    """Router facade per [routing] backend; without Jev: no SDK import, one hint line, no warning flood."""
    if fake:
        backend, hint, client = "jev", "", FakeJev()
    else:
        backend, hint = resolve_backend(cfg.routing_backend, cfg.jev_enabled)
        client = TypeSafeJev(cfg.jev_model, cfg.jev_http_timeout_s, cfg.jev_deadline_s) if backend == "jev" else None
    if hint and hint not in _HINTS_SHOWN:
        _HINTS_SHOWN.add(hint)
        say(hint)
    decider = None
    if cfg.routing_backend == "haiku" and runner is not None and not fake:
        usage = DailyUsage(Path(cfg.usage_path), cfg.daily_max_calls, cfg.daily_max_weighted)
        decider = HaikuDecider(runner, home_dir() / "haiku_cache.json", str(repo), cfg.haiku_timeout_s,
                               on_call=lambda: usage.record(cfg.weight("haiku")))
    return JevService(client, roots=[str(repo)], send_diffs=cfg.jev_send_diffs, diff_chars=cfg.jev_diff_chars,
                      backend=backend, bypass_decider=decider)


def pane(cfg, run_id: str, status: str, phase: str, note: str = "", plan: dict | None = None,
         budget: dict | None = None, started: str | None = None, repo: Path | None = None,
         key: str | None = None) -> None:
    """Status entry for phases before the actual run (key: run_id unless given)."""
    acct = Accountant(cfg, dict(budget or {}))
    try:
        publish(Path(cfg.status_path), build_status({
            "run_id": run_id, "status": status, "phase": phase, "note": note, "started": started or now_iso(),
            "repo": str(repo or ""), "budget": acct.c, "limits": acct.limits(), "plan": plan or {}, "tasks": {}}),
            key=key)
    except OSError:
        pass


def check_login(runner, cfg, repo: Path) -> str | None:
    """Short haiku call ('OK'); user-facing error text or None."""
    try:
        res = runner.run(ClaudeCall(role="ping", key="ping", prompt="Reply only: OK", model="haiku",
                                    cwd=str(repo), timeout_s=120, tools=""))
    except ClaudeNotFound as exc:
        return str(exc)
    except OSError:
        return "claude could not be started (check CLAUDE_BIN)."
    if res.error == ErrorClass.AUTH:
        return "Claude Code is not logged in. Run `claude` once and execute /login."
    if res.error == ErrorClass.RATE_LIMIT:
        return "Claude quota exhausted (rate/usage limit). Try again later."
    if res.error:
        return f"claude test call failed ({res.error})."
    return None


def checks_from(cfg, test_cmd: str | None) -> list:
    if test_cmd:
        cfg.test_command = shlex.split(test_cmd)
    checks = cfg.checks()
    for cmd in checks:
        if err := command_error(cmd):
            raise PreflightError(f"Invalid test command ({err}): {cmd}")
    return checks


def preflight(cfg, repo: Path, runner, fake: bool) -> None:
    problems = Git(repo).preflight()
    if problems:
        raise PreflightError(" ".join(problems))
    resolve_python(cfg.python, repo)
    if not fake:
        if err := check_login(runner, cfg, repo):
            raise PreflightError(err)


def print_plan(plan: dict, routes: dict | None = None) -> None:
    print(f"Plan {plan['run_id']}: {plan.get('goal_summary', '')}")
    for f in plan.get("acceptance_files", []):
        print(f"  acceptance test (frozen): {f['path']}")
    for t in plan["tasks"]:
        route = f"  -> {routes[t['id']]}" if routes and t["id"] in routes else ""
        deps = ",".join(t.get("depends_on", [])) or "-"
        print(f"  {t['id']} [{t['kind']}/{t['risk']}] {t['title']} (after: {deps}){route}")
        for w in t.get("writes", []):
            print(f"      writes: {w}")
        for c in t.get("acceptance", []):
            detail = c.get("text") or c.get("path") or " ".join(c.get("cmd", []))
            print(f"      {c['id']} {c['type']}: {detail}")
    print(f"  global checks: {plan.get('global_checks')}")


def confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------- Subcommands ----------

def cmd_plan(args, cfg, repo: Path) -> int:
    run_id = new_run_id()
    started = now_iso()
    runner = make_runner(cfg, args.fake)
    pane(cfg, run_id, "RUNNING", "PREFLIGHT", started=started, repo=repo)
    checks = checks_from(cfg, args.test_cmd)
    preflight(cfg, repo, runner, args.fake)

    pane(cfg, run_id, "RUNNING", "BYPASS", started=started, repo=repo)
    jev = make_jev(cfg, repo, args.fake, runner)
    decision = jev.bypass(args.goal, bool(checks), cfg.bypass_min_subtasks)
    if decision.source == "jev" and not decision.jev_ok:
        say(f"Jev unreachable ({decision.reason}) -> fallback sonnet, planner runs anyway.")
    if args.bypass or (not decision.use_orchestrator and not args.force_orch):
        say(f"Bypass: direct call with {decision.tier} ({'--bypass' if args.bypass else decision.reason}).")
        if not args.yes:
            pane(cfg, run_id, "PAUSED", "APPROVAL", msg.NOTE_DIRECT_SUGGESTED, started=started, repo=repo)
            say("Run with --yes (changes the current branch directly) or --force-orchestrate for the orchestrator.")
            return EXIT_OK
        return run_direct(args.goal, decision.tier, cfg, runner, repo, run_id, started)

    usage = DailyUsage(Path(cfg.usage_path), cfg.daily_max_calls, cfg.daily_max_weighted)
    counters: dict = {}
    acct = Accountant(cfg, counters, usage)
    tier = planner_tier(cfg, decision.metrics)
    pane(cfg, run_id, "RUNNING", "PLAN", msg.NOTE_PLANNER.format(tier=tier), budget=counters, started=started, repo=repo)
    try:
        plan = create_plan(args.goal, run_id, cfg, runner, acct, str(repo), tier, checks,
                           on_invalid=lambda n, errs: say(f"Planner attempt {n} rejected: " + "; ".join(errs)[:600]))
    except PlanError as exc:
        pane(cfg, run_id, "FAILED", "DONE", msg.NOTE_PLAN_INVALID, budget=counters, started=started, repo=repo)
        say(f"{exc}")
        for e in exc.errors[:20]:
            say(f"  - {e}")
        return EXIT_PARTIAL
    plan["_meta"] = {"planner_calls": counters.get("planner", 0), "planner_model": tier, "started": started}
    run_dir = Path(cfg.runs_dir) / run_id
    atomic_write_json(run_dir / "plan.json", plan)
    out = Path(args.out)
    atomic_write_json(out, plan)
    print_plan(plan)
    say(f"Plan saved: {out} (and {run_dir / 'plan.json'})")
    pane(cfg, run_id, "PAUSED", "APPROVAL", msg.NOTE_PLAN_WAITING, plan=plan, budget=counters, started=started, repo=repo)
    if args.yes and not args.dry:
        return execute(plan, cfg, repo, args.fake)
    say(f"Approve with: kickroll run --plan {out}")
    return EXIT_OK


def run_direct(goal: str, tier: str, cfg, runner, repo: Path, run_id: str, started: str) -> int:
    say(f"Direct call: claude -p --model {tier} in the current working tree.")
    pane(cfg, run_id, "RUNNING", "EXECUTE", msg.NOTE_DIRECT_RUNNING.format(tier=tier), started=started, repo=repo)
    res = runner.run(ClaudeCall(role="direct", key="direct", prompt=goal, model=tier, cwd=str(repo),
                                timeout_s=cfg.timeouts[tier], tools=cfg.worker_tools))
    DailyUsage(Path(cfg.usage_path), cfg.daily_max_calls, cfg.daily_max_weighted).record(cfg.weight(tier))
    if res.error:
        pane(cfg, run_id, "FAILED", "DONE", msg.NOTE_DIRECT_FAILED.format(error=res.error), started=started, repo=repo)
        say(f"Direct call failed ({res.error}).")
        return EXIT_PAUSED if res.error in (ErrorClass.RATE_LIMIT, ErrorClass.AUTH) else EXIT_PARTIAL
    pane(cfg, run_id, "SUCCESS", "DONE", msg.NOTE_DIRECT_DONE, started=started, repo=repo)
    print(res.text)
    return EXIT_OK


def load_plan(path: Path, cfg, repo) -> dict:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise PreflightError(f"Plan not readable: {path}")
    meta = raw.pop("_meta", {})
    plan = normalize_plan(raw)
    errors = validate_plan(plan, cfg.max_tasks, tracked_files(str(repo)))
    if not plan.get("global_checks"):
        errors.append("global_checks empty: test command missing (checks.test_command or --test-cmd)")
    if errors:
        raise PreflightError("Invalid plan: " + "; ".join(errors[:10]))
    plan["_meta"] = meta
    return plan


def execute(plan: dict, cfg, repo: Path, fake: bool, status_writer=None) -> int:
    meta = plan.pop("_meta", {}) or {}
    if (Path(cfg.runs_dir) / plan["run_id"] / "state.json").exists():
        say(f"Run {plan['run_id']} already exists -> kickroll resume {plan['run_id']}")
        return EXIT_USAGE
    runner = make_runner(cfg, fake)
    orch = Orchestrator(cfg, repo, runner, make_jev(cfg, repo, fake, runner), plan=plan, status_writer=status_writer)
    orch.state["budget"]["planner"] = int(meta.get("planner_calls", 0))
    if meta.get("started"):
        orch.state["started"] = meta["started"]
    return finish_message(orch, orch.start)


def finish_message(orch: Orchestrator, action) -> int:
    try:
        status = action()
    except KeyboardInterrupt:
        say(f"Interrupted. State saved. Continue: kickroll resume {orch.run_id}")
        return EXIT_INTERRUPT
    st = orch.state
    say(f"Run {orch.run_id}: {status}  (calls {st['budget']['calls']}, Opus {st['budget']['opus']}, "
        f"planner {st['budget']['planner']}, {st['budget']['elapsed_s'] / 60:.1f} min)")
    for tid, ts in st["tasks"].items():
        say(f"  {tid}: {ts['status']} (attempt {ts['attempt']}, {ts['tier'] or '-'}) {ts.get('reason', '')}")
    if status == "PAUSED":
        say(f"Paused: {st.get('note')}. Continue: kickroll resume {orch.run_id}")
        return EXIT_PAUSED
    say(f"Report: {orch.run_dir / 'report.md'}")
    say(f"Take over: git merge --ff-only {st['branch']}   Clean up: kickroll clean {orch.run_id}")
    return EXIT_OK if status == "SUCCESS" else EXIT_PARTIAL


def cmd_run(args, cfg, repo: Path) -> int:
    plan = load_plan(Path(args.plan), cfg, repo)
    if args.dry:
        jev = make_jev(cfg, repo, args.fake)
        routes = {t["id"]: jev.route_task(t)[0] for t in plan["tasks"]}
        print_plan(plan, routes)
        return EXIT_OK
    print_plan(plan)
    if not args.yes and not confirm("Execute plan?"):
        say("Not approved.")
        return EXIT_OK
    return execute(plan, cfg, repo, args.fake)


def cmd_resume(args, cfg, repo: Path) -> int:
    runner = make_runner(cfg, args.fake)
    state = read_json(Path(cfg.runs_dir) / args.run_id / "state.json")
    if not state:
        say(f"No run {args.run_id} in {cfg.runs_dir}.")
        return EXIT_USAGE
    orch = Orchestrator(cfg, Path(state["repo"]), runner, make_jev(cfg, Path(state["repo"]), args.fake, runner),
                        run_id=args.run_id)
    return finish_message(orch, orch.resume)


def cmd_status(args, cfg, repo: Path) -> int:
    state = read_json(Path(cfg.runs_dir) / args.run_id / "state.json")
    if not state:
        say(f"No run {args.run_id}.")
        return EXIT_USAGE
    entry = read_json(entry_path(Path(cfg.status_path), args.run_id)) or {}
    # the pid of the process that ran it (0 when unknown), not this status command's own
    pid = entry.get("pid") if isinstance(entry.get("pid"), int) else 0
    print(json.dumps(build_status(state, pid=pid), ensure_ascii=False, indent=1))
    return EXIT_OK


def cmd_clean(args, cfg, repo: Path) -> int:
    state = read_json(Path(cfg.runs_dir) / args.run_id / "state.json") or {}
    repo = Path(state.get("repo") or repo)
    wt = Path(state.get("worktree") or Path(cfg.worktree_root) / run8(args.run_id))
    branch = state.get("branch") or f"kickroll/{args.run_id}"
    Git(repo).remove_worktree(wt, branch if args.delete_branch else None)
    remove_entry(Path(cfg.status_path), args.run_id)
    say(f"Worktree removed: {wt}" + (f", branch {branch} deleted" if args.delete_branch else
                                     f" (branch {branch} kept; --delete-branch to delete)"))
    return EXIT_OK


def cmd_bypassed(args, cfg, repo: Path) -> int:
    """The chat handles the task itself (skill bypass): its own pane entry, one per session (a later bypass of
    the same session replaces it). Runs of other repositories do not matter; a live run in this one does."""
    if live := live_run(Path(cfg.status_path), repo=repo):
        say(f"Run {live} is still active in this repository; status not changed.")
        return EXIT_USAGE
    run_id = new_run_id()
    pane(cfg, run_id, "BYPASSED", "BYPASS", msg.NOTE_BYPASSED.format(reason=" ".join(args.reason.split())),
         repo=repo, key=f"bypass-{session_id() or run_id}")
    say("Pane: task handled directly, no kickroll run.")
    return EXIT_OK


def cmd_bench(args, cfg, repo: Path) -> int:
    """Scaffold: same request + same test command per mode; records calls per model, wall clock, retries, tests."""
    goal = Path(args.goal_file).read_text(encoding="utf-8")
    checks = checks_from(cfg, args.test_cmd)
    if not checks:
        raise PreflightError("bench needs a test command (--test-cmd or checks.test_command).")
    runner = make_runner(cfg, args.fake)
    git = Git(repo)
    if problems := git.preflight():
        raise PreflightError(" ".join(problems))
    python = resolve_python(cfg.python, repo)
    results = []
    for mode in [m.strip() for m in args.modes.split(",") if m.strip()]:
        start = time.monotonic()
        row = {"mode": mode, "calls": {}, "retries": 0, "test_ok": None, "false_accept": None}
        if mode in ("sonnet", "opus"):
            wt = Path(cfg.worktree_root) / f"bench-{run8(mode + str(start))}"
            git.create_worktree(f"kickroll/bench-{run8(mode + str(start))}", wt, git.head())
            try:
                res = runner.run(ClaudeCall(role="direct", key=f"bench:{mode}", prompt=goal, model=mode, cwd=str(wt),
                                            timeout_s=cfg.timeouts[mode], tools=cfg.worker_tools))
                row["calls"] = {mode: 1}
                row["error"] = res.error
                row["test_ok"] = all(not r["ids"] for r in run_checks(checks, wt, python, cfg.l1_timeout_s).values())
            finally:
                git.remove_worktree(wt, f"kickroll/bench-{run8(mode + str(start))}")
        elif mode == "orch":
            run_id = new_run_id()
            counters: dict = {}
            try:
                plan = create_plan(goal, run_id, cfg, runner, Accountant(cfg, counters), str(repo),
                                   planner_tier(cfg, None), checks)
            except PlanError as exc:
                row["error"] = f"plan: {exc}"
                results.append(row)
                continue
            orch = Orchestrator(cfg, repo, runner, make_jev(cfg, repo, args.fake, runner), plan=plan)
            orch.state["budget"]["planner"] = counters.get("planner", 0)
            status = orch.start()
            calls: dict = {}
            for line in (orch.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines():
                ev = json.loads(line)
                if ev.get("event") == "worker_done":
                    calls[ev["model"]] = calls.get(ev["model"], 0) + 1
            row["calls"] = calls
            row["planner_calls"] = counters.get("planner", 0)
            row["retries"] = sum(max(0, ts["attempt"] - 1) for ts in orch.state["tasks"].values())
            row["test_ok"] = status == "SUCCESS"
            row["run_id"] = run_id
        else:
            row["error"] = "unknown mode"
        row["wall_s"] = round(time.monotonic() - start, 1)
        row["weighted"] = round(sum(cfg.weight(m) * n for m, n in row["calls"].items()), 2)
        results.append(row)
        print(json.dumps(row, ensure_ascii=False))
    out = Path(cfg.runs_dir) / "bench.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        for row in results:
            fh.write(json.dumps({"ts": now_iso(), **row}, ensure_ascii=False) + "\n")
    say(f"Bench results appended to {out}")
    return EXIT_OK


# ---------- main ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kickroll", description="Orchestrator v1 (Jev optional). " + WARNING)
    p.add_argument("--repo", default=".", help="git repository root (default: current directory)")
    p.add_argument("--config", help="path to kickroll.toml (default: <repo>/kickroll.toml)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="bypass decision + create plan (saves plan.json, shows plan)")
    sp.add_argument("goal")
    sp.add_argument("--test-cmd", help='e.g. "python -m unittest discover -s tests"')
    sp.add_argument("--out", default="plan.json")
    sp.add_argument("--yes", action="store_true", help="execute right after planning without asking")
    sp.add_argument("--dry", action="store_true", help="only plan and show, never execute")
    sp.add_argument("--force-orchestrate", "--force-orch", dest="force_orch", action="store_true",
                    help="ignore the bypass decision, always plan")
    sp.add_argument("--bypass", action="store_true", help="skip the orchestrator: direct call")
    sp.add_argument("--fake", action="store_true", help="FakeRunner/FakeJev (no claude, no network)")

    sp = sub.add_parser("run", help="execute an approved plan")
    sp.add_argument("--plan", required=True)
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--dry", action="store_true", help="only show plan + routing")
    sp.add_argument("--fake", action="store_true")

    sp = sub.add_parser("resume", help="continue a paused/interrupted run")
    sp.add_argument("run_id")
    sp.add_argument("--fake", action="store_true")

    sp = sub.add_parser("status", help="show the status of a run")
    sp.add_argument("run_id")

    sp = sub.add_parser("clean", help="remove a run's worktree (optionally its branch)")
    sp.add_argument("run_id")
    sp.add_argument("--delete-branch", action="store_true")

    sp = sub.add_parser("bypassed", help="tell the pane the task is handled directly, without a run")
    sp.add_argument("reason", help="short reason, e.g. 'single-file fix'")

    sp = sub.add_parser("bench", help="measure: direct call vs. orchestrator")
    sp.add_argument("--goal-file", required=True)
    sp.add_argument("--test-cmd")
    sp.add_argument("--modes", default="sonnet,orch", help="comma list of sonnet, opus, orch")
    sp.add_argument("--fake", action="store_true")
    return p


def run_with_heartbeat(handler, args, cfg, repo: Path) -> int:
    """Beat heartbeat.json while the command runs; whatever ends it, never leave the pane at RUNNING."""
    note = msg.NOTE_ENDED
    try:
        with Heartbeat(Path(cfg.status_path).with_name("heartbeat.json"), also=[beat_path(Path(cfg.status_path))]):
            return handler(args, cfg, repo)
    except KeyboardInterrupt:
        note = msg.NOTE_ABORTED.format(error="interrupted")
        raise
    except BaseException as exc:
        note = msg.NOTE_ABORTED.format(error=type(exc).__name__)
        raise
    finally:
        mark_ended(Path(cfg.status_path), note)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    print(WARNING, file=sys.stderr)
    repo = Path(args.repo).resolve()
    try:
        cfg = load_config(Path(args.config) if args.config else repo / "kickroll.toml")
        handler = {"plan": cmd_plan, "run": cmd_run, "resume": cmd_resume, "status": cmd_status,
                   "clean": cmd_clean, "bench": cmd_bench, "bypassed": cmd_bypassed}[args.cmd]
        if args.cmd in ("status", "clean", "bypassed"):
            return handler(args, cfg, repo)
        return run_with_heartbeat(handler, args, cfg, repo)
    except (PreflightError, ConfigError, ClaudeNotFound) as exc:
        say(f"Error: {exc}")
        return EXIT_USAGE
    except GitError as exc:
        say(f"git error: {exc}")
        return EXIT_USAGE
    except KeyboardInterrupt:
        say("Interrupted.")
        return EXIT_INTERRUPT


if __name__ == "__main__":
    sys.exit(main())
