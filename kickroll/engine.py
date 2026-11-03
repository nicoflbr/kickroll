"""Loop engine: DAG scheduling, attempts/escalation, acceptance cascade, commit, pause/resume, termination."""
import hashlib
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

from . import TIERS
from . import messages as msg
from .budget import Accountant
from .decision import (ACCEPT, AUTH, BLOCK, ENV, FAIL, FAILED, MARK_UNCLEAR, PASS, PAUSE, RATE_LIMIT, RETRY,
                       STAGNATION, TIMEOUT, UNCLEAR, Action, decide, is_stagnation, up)
from .gitops import Git, GitError, file_sha256, write_excludes
from .plan import RISKS, WRITE_KINDS, glob_match, max_attempts, norm_path
from .proc import kill_orphans
from .prompts import REVIEW_GENERAL, reviewer_prompt, worker_prompt
from .runner import ClaudeCall, ErrorClass
from .safety import truncate
from .status_file import build_status, write_status
from .store import DailyUsage, EventLog, FileLock, atomic_write_json, now_iso, read_json, sha16
from .verify import Outcome, PreflightError, level0, level1, parse_review, resolve_python, run_checks

TERMINAL = ("MERGED", "FAILED", "BLOCKED", "UNCLEAR", "SKIPPED")
DEAD = ("FAILED", "BLOCKED", "UNCLEAR", "SKIPPED")
_NOTES = {ErrorClass.RATE_LIMIT: msg.NOTE_RATE_LIMIT, ErrorClass.AUTH: msg.NOTE_AUTH,
          "INTERRUPTED": msg.NOTE_INTERRUPTED}


def run8(run_id: str) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()[:8]


def _max_risk(a: str | None, b: str | None) -> str:
    vals = [r for r in (a, b) if r in RISKS]
    return max(vals, key=RISKS.index) if vals else "medium"


class Orchestrator:
    def __init__(self, cfg, repo, runner, jev, plan: dict | None = None, run_id: str | None = None,
                 status_writer=None, fault_hook=None):
        self.cfg = cfg
        self.repo = Path(repo).resolve()
        self.runner = runner
        self.jev = jev
        run_id = run_id or plan["run_id"]
        self.run_dir = Path(cfg.runs_dir) / run_id
        self.state_path = self.run_dir / "state.json"
        self.lock = threading.RLock()
        self.git = Git(self.repo, excludes_file=write_excludes(self.run_dir / "artifact-ignore"))
        self.log = EventLog(self.run_dir / "events.jsonl", run_id)
        self.status_writer = status_writer or (lambda d: write_status(Path(cfg.status_path), d))
        self.fault_hook = fault_hook or (lambda point, tid: None)
        self.pause_reason: str | None = None
        self.budget_stop: str | None = None
        self.opus_slot = threading.Semaphore(1)  # never two Opus at once
        self._feedback: dict = {}
        if plan is not None:
            self.state = self._initial_state(run_id, plan)
        else:
            self.state = read_json(self.state_path)
            if not self.state:
                raise FileNotFoundError(f"No state for run {run_id}")
        self.acct = Accountant(cfg, self.state["budget"], DailyUsage(Path(cfg.usage_path), cfg.daily_max_calls,
                                                                      cfg.daily_max_weighted), self.lock)
        self.jev.on_call = self.acct.record_jev
        self.state["limits"] = {k: v for k, v in self.acct.limits().items()}
        self._run_lock = FileLock(self.run_dir / "run.lock")

    # ---------- State ----------

    def _initial_state(self, run_id: str, plan: dict) -> dict:
        return {
            "version": 1, "run_id": run_id, "status": "EXECUTING", "phase": "EXECUTE", "note": "",
            "started": now_iso(), "updated": now_iso(), "repo": str(self.repo), "worktree": "", "branch": "",
            "base_commit": "", "head": "", "python": "", "pids": [], "baseline": {}, "frozen": {},
            "budget": {}, "limits": {}, "plan": plan,
            "tasks": {t["id"]: {"status": "PENDING", "step": "NONE", "attempt": 0, "max_attempts": max_attempts(t),
                                "tier": "", "base_tier": "", "risk": t.get("risk", "medium"), "history": [],
                                "timeouts": 0, "commit": None, "reason": ""} for t in plan["tasks"]},
        }

    @property
    def run_id(self) -> str:
        return self.state["run_id"]

    @property
    def tasks(self) -> dict:
        return {t["id"]: t for t in self.state["plan"]["tasks"]}

    def _save(self) -> None:
        with self.lock:
            self.acct.tick()
            self.state["updated"] = now_iso()
            atomic_write_json(self.state_path, self.state)
            try:
                self.status_writer(build_status(self.state))
            except (OSError, ValueError) as exc:
                self.log.emit("status_write_failed", error=type(exc).__name__)

    def _set(self, tid: str, **kw) -> None:
        with self.lock:
            self.state["tasks"][tid].update(kw)
            self._save()

    def _run(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)
            self._save()

    # ---------- Start / Resume ----------

    def start(self) -> str:
        self._run_lock.acquire()
        try:
            self._setup()
            self._loop()
            return self._finish()
        finally:
            self._run_lock.release()

    def _setup(self) -> None:
        problems = self.git.preflight()
        if problems:
            raise PreflightError(" ".join(problems))
        python = resolve_python(self.cfg.python, self.repo)
        base = self.git.head()
        wt = Path(self.cfg.worktree_root) / run8(self.run_id)
        if wt.exists():
            raise PreflightError(f"Worktree path already exists: {wt}")
        branch = f"kickroll/{self.run_id}"
        self.git.create_worktree(branch, wt, base)
        self.state.update(worktree=str(wt), branch=branch, base_commit=base, head=base, python=python)
        self._save()
        self.log.emit("run_start", tasks=len(self.tasks), checks=len(self.state["plan"].get("global_checks", [])))
        try:
            files = self.state["plan"].get("acceptance_files", [])
            if files:  # commit acceptance tests first, then freeze them
                for f in files:
                    target = wt / norm_path(f["path"])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(f["content"], encoding="utf-8")
                self.state["head"] = self.git.commit(wt, "Acceptance tests", self.run_id, "_acceptance", 1)
                for f in files:
                    p = norm_path(f["path"])
                    self.state["frozen"][p] = file_sha256(wt / p)
            baseline = run_checks(self.state["plan"].get("global_checks", []), wt, python, self.cfg.l1_timeout_s)
            if any(r["env"] for r in baseline.values()):
                raise PreflightError("Test environment broken (test module/interpreter missing). Check the venv: checks.python.")
            self.state["baseline"] = {k: r["ids"] for k, r in baseline.items()}
            self.log.emit("baseline", failing={k: len(v) for k, v in self.state["baseline"].items()})
            self._save()
        except BaseException:
            self.git.remove_worktree(wt, branch)
            raise

    def resume(self) -> str:
        self._run_lock.acquire()
        try:
            killed = kill_orphans(self.state.get("pids", []))
            self.state["pids"] = []
            wt = Path(self.state["worktree"])
            if not wt.is_dir():
                raise PreflightError(f"Worktree missing: {wt}. Remove the run with 'clean' and start again.")
            for tid, ts in self.state["tasks"].items():
                if ts["status"] in TERMINAL:
                    continue
                commits = self.git.find_task_commits(wt, self.run_id, tid)
                if commits and self.tasks[tid].get("kind") in WRITE_KINDS:
                    sha, attempt = commits[0]
                    ts.update(status="MERGED", step="NONE", commit=sha, attempt=attempt)
                    if self.tasks[tid].get("kind") == "test":
                        self._freeze_test_task(tid, self._commit_files(wt, sha))
                    self.log.emit("resume_reconciled", tid, attempt, commit=sha[:12])
                else:
                    ts.update(status="PENDING", step="NONE")
            head = self.git.head(wt)
            self.git.reset_to(wt, head)
            self.pause_reason = None
            self.runner.reset()
            self._run(head=head, status="EXECUTING", phase="EXECUTE", note="")
            self.log.emit("run_resume", killed=len(killed))
            self._loop()
            return self._finish()
        finally:
            self._run_lock.release()

    def _commit_files(self, wt: Path, sha: str) -> list[str]:
        out = self.git.run("show", "--name-only", "--format=", sha, cwd=wt)
        return [line.strip() for line in out.splitlines() if line.strip()]

    # ---------- Scheduler ----------

    def _skip_dependents(self) -> None:
        changed = True
        while changed:
            changed = False
            for tid, t in self.tasks.items():
                ts = self.state["tasks"][tid]
                if ts["status"] in ("PENDING", "READY") and any(
                        self.state["tasks"][d]["status"] in DEAD for d in t.get("depends_on", [])):
                    ts.update(status="SKIPPED", step="NONE", reason="dependency")
                    self.log.emit("state_change", tid, status="SKIPPED")
                    changed = True
        self._save()

    def _ready(self, running: set) -> list[str]:
        out = []
        for tid, t in self.tasks.items():
            ts = self.state["tasks"][tid]
            if tid in running or ts["status"] not in ("PENDING", "READY"):
                continue
            if all(self.state["tasks"][d]["status"] == "MERGED" for d in t.get("depends_on", [])):
                if ts["status"] == "PENDING":
                    self._set(tid, status="READY")
                out.append(tid)
        return out

    def _loop(self) -> None:
        running: dict = {}
        budget_logged = False
        with ThreadPoolExecutor(max_workers=max(1, int(self.cfg.max_parallel))) as pool:
            try:
                while True:
                    self._skip_dependents()
                    exhausted = self.acct.run_exhausted() or self.budget_stop
                    if exhausted and not budget_logged:
                        self.log.emit("budget_exhausted", reason=exhausted)
                        self._run(note=msg.NOTE_BUDGET.format(reason=exhausted))
                        budget_logged = True
                    if not self.pause_reason and not exhausted:
                        ready = self._ready(set(running.values()))
                        writers = [t for t in ready if self.tasks[t].get("kind") in WRITE_KINDS]
                        readers = [t for t in ready if t not in writers]
                        if writers and not running:
                            self._guarded(writers[0])  # write tasks run exclusively and sequentially
                            continue
                        if not writers:
                            for tid in readers:
                                if len(running) >= max(1, int(self.cfg.max_parallel)):
                                    break
                                running[pool.submit(self._guarded, tid)] = tid
                    if not running:
                        break
                    done, _ = wait(list(running), return_when=FIRST_COMPLETED)
                    for fut in done:
                        running.pop(fut)
                        fut.result()
            except KeyboardInterrupt:
                self._pause("INTERRUPTED", None)
                wait(list(running))
                self._save()
                raise

    def _guarded(self, tid: str) -> None:
        try:
            self._run_task(tid)
        except Exception as exc:  # internal error: block the task, continue the run
            self.log.emit("task_error", tid, error=type(exc).__name__)
            self._reset_worktree_if_writer(tid)
            self._set(tid, status="BLOCKED", step="NONE", reason="internal_error")

    def _reset_worktree_if_writer(self, tid: str, force: bool = False) -> None:
        if (force or self.tasks[tid].get("kind") in WRITE_KINDS) and self.state.get("worktree"):
            try:
                self.git.reset_to(Path(self.state["worktree"]), self.state["head"])
            except GitError:
                pass

    # ---------- One task ----------

    def _route(self, tid: str) -> None:
        task, ts = self.tasks[tid], self.state["tasks"][tid]
        self._set(tid, status="RUNNING", step="ROUTE", attempt=max(1, ts["attempt"]))
        tier, metrics, why = self.jev.route_task(task)
        hint = task.get("model_hint")
        if hint in TIERS and TIERS.index(hint) - TIERS.index(tier) >= 2:
            tier = up(tier)
        risk = _max_risk(task.get("risk"), metrics.get("risk"))
        ts.update(tier=tier, base_tier=tier, risk=risk, max_attempts=max_attempts(task, risk))
        self.log.emit("task_routed", tid, ts["attempt"], tier=tier, risk=risk,
                      **{k: v for k, v in metrics.items() if k != "risk"})
        self._save()

    def _run_task(self, tid: str) -> None:
        task, ts = self.tasks[tid], self.state["tasks"][tid]
        writer = task.get("kind") in WRITE_KINDS
        wt = Path(self.state["worktree"])
        if not ts["tier"]:
            self._route(tid)
        while True:
            if self.pause_reason:
                self._set(tid, status="READY", step="NONE")
                return
            attempt, tier = max(1, ts["attempt"]), ts["tier"]
            reason = self.acct.check("worker", tier)
            if reason:
                # Opus budget: the task fails; otherwise it stays READY and the run starts nothing new.
                if reason != "opus_budget":
                    self.budget_stop = reason
                self.log.emit("budget_stop", tid, attempt, reason=reason)
                self._set(tid, status="FAILED" if reason == "opus_budget" else "READY", step="NONE", reason=reason)
                return
            if writer:
                self.git.reset_to(wt, self.state["head"])
            self._set(tid, status="RUNNING", step="WORK", attempt=attempt)
            prompt = worker_prompt(task, sorted(self.state["frozen"]), self._predecessors(task),
                                   self._feedback.get(tid, ""))
            call = ClaudeCall(role="worker", key=f"{tid}:{attempt}", prompt=prompt, model=tier, cwd=str(wt),
                              timeout_s=self.cfg.timeouts[tier],
                              tools=self.cfg.worker_tools if writer else self.cfg.read_tools)
            self.acct.record("worker", tier)
            res = self._call(call, tid, attempt)
            outcome, report, diff_sha, changed = None, None, "", []
            if res.error == ErrorClass.CANCELLED:
                self._reset_worktree_if_writer(tid)
                self._set(tid, status="READY", step="NONE")
                return
            if res.error:
                signal = {ErrorClass.RATE_LIMIT: RATE_LIMIT, ErrorClass.AUTH: AUTH,
                          ErrorClass.TIMEOUT: TIMEOUT}.get(res.error, FAIL)
                failed = [] if signal != FAIL else [f"L0:worker_{res.error.lower()}"]
                feedback = "The last call ended with an error." if signal == FAIL else ""
            else:
                outcome, report, diff_sha, changed = self._verify(tid, task, ts, res, wt, attempt)
                if outcome.call_error == ErrorClass.CANCELLED:
                    self._reset_worktree_if_writer(tid, force=bool(changed))
                    self._set(tid, status="READY", step="NONE")
                    return
                if outcome.passed:
                    signal = PASS
                elif outcome.call_error:
                    signal = {ErrorClass.RATE_LIMIT: RATE_LIMIT, ErrorClass.AUTH: AUTH}.get(outcome.call_error, FAIL)
                elif outcome.unclear:
                    signal = UNCLEAR
                elif outcome.env_problem:
                    signal = ENV
                else:
                    signal = FAIL
                failed, feedback = outcome.failed, outcome.feedback
            if signal == TIMEOUT:
                ts["timeouts"] += 1
            prev = ts["history"][-1] if ts["history"] else None
            if signal == FAIL and prev and is_stagnation(prev["failed"], failed, prev.get("diff_sha"), diff_sha):
                signal = STAGNATION
            if outcome is not None and outcome.terminal:
                action = Action(FAILED, tier)
            else:
                action = decide(attempt, signal, tier, ts["risk"], ts["max_attempts"], ts["timeouts"],
                                self.cfg.max_timeouts_per_task)
            if action.counts:
                ts["history"].append({"attempt": attempt, "tier": tier, "signal": signal,
                                      "failed": sorted(failed), "diff_sha": diff_sha})
            self.log.emit("decision", tid, attempt, signal=signal, action=action.kind, tier=tier,
                          next_tier=action.next_tier, failed=sorted(failed), counts=action.counts)
            if action.kind == ACCEPT:
                self._accept(tid, task, ts, attempt, report, changed)
                return
            self._reset_worktree_if_writer(tid, force=bool(changed))
            if action.kind == PAUSE:
                self._pause(res.error or outcome.call_error, tid)
                return
            if action.kind == RETRY:
                self._feedback[tid] = feedback
                self._set(tid, status="RUNNING", step="WORK", tier=action.next_tier,
                          attempt=attempt + 1 if action.counts else attempt)
                continue
            final = {FAILED: "FAILED", BLOCK: "BLOCKED", MARK_UNCLEAR: "UNCLEAR"}[action.kind]
            self.log.emit("state_change", tid, attempt, status=final)
            self._set(tid, status=final, step="NONE", reason=signal.lower())
            return

    def _call(self, call: ClaudeCall, tid: str, attempt: int):
        def spawned(pid):
            with self.lock:
                self.state["pids"].append(pid)
                self._save()

        def exited(pid):
            with self.lock:
                if pid in self.state["pids"]:
                    self.state["pids"].remove(pid)

        slot = self.opus_slot if call.model == "opus" else None
        if slot:
            slot.acquire()
        try:
            res = self.runner.run(call, on_spawn=spawned, on_exit=exited)
        finally:
            if slot:
                slot.release()
        self.acct.record_result(call.model, res)
        self.log.emit("worker_done", tid, attempt, role=call.role, model=call.model,
                      duration_s=round(res.duration_s, 2), num_turns=res.num_turns, cost_usd=res.cost_usd,
                      usage=res.usage, prompt_sha256=sha16(call.prompt), prompt_chars=len(call.prompt),
                      error=res.error)
        return res

    def _predecessors(self, task: dict) -> list:
        out = []
        for d in task.get("depends_on", []):
            summary_path = self.run_dir / "artifacts" / d / "summary.md"
            summary = summary_path.read_text(encoding="utf-8") if summary_path.is_file() else ""
            commit = self.state["tasks"][d].get("commit")
            stat = ""
            if commit:
                try:
                    stat = self.git.diff_stat(Path(self.state["worktree"]), f"{commit}^", commit)
                except GitError:
                    stat = ""
            out.append((d, summary, stat))
        return out

    # ---------- Acceptance ----------

    def _verify(self, tid, task, ts, res, wt: Path, attempt: int):
        self._set(tid, status="VERIFYING", step="L0")
        changed, diff = self.git.stage_changes(wt)
        diff_sha = sha16(diff) if diff else ""
        out0, report = level0(task, res.text, changed, self.state["frozen"], wt)
        self.log.emit("verify_stage", tid, attempt, stage="L0", passed=out0.passed, failed=out0.failed,
                      changed=len(changed), diff_sha=diff_sha)
        if not out0.passed:
            return out0, report, diff_sha, changed
        self._set(tid, step="L1")
        out1 = level1(task, wt, self.state["python"], self.cfg.l1_timeout_s, changed,
                      self.state["plan"].get("global_checks", []), self.state["baseline"])
        self.log.emit("verify_stage", tid, attempt, stage="L1", passed=out1.passed, failed=out1.failed,
                      env=out1.env_problem)
        if not out1.passed:
            return out1, report, diff_sha, changed
        judgement = [c for c in task.get("acceptance", []) if c.get("type") == "judgement"]
        need_l3 = bool(judgement)
        if judgement or ts["risk"] in ("medium", "high"):
            self._set(tid, step="L2")
            verdict, metrics = self.jev.verify(task, (report or {}).get("summary", ""), diff, ts["risk"])
            self.log.emit("verify_stage", tid, attempt, stage="L2", verdict=verdict, **metrics)
            if verdict == "fail":
                return Outcome(False, ["L2:jev"], "The check reports omissions or placeholders in the report.",
                                "L2"), report, diff_sha, changed
            need_l3 = need_l3 or verdict == "suspect" or (ts["risk"] == "high" and verdict != "pass")
        if need_l3:
            out3 = self._review(tid, task, ts, diff, changed, judgement, attempt)
            if not out3.passed:
                return out3, report, diff_sha, changed
        return Outcome(True, [], "", "L3" if need_l3 else "L1"), report, diff_sha, changed

    def _reviewer_tier(self, worker_tier: str, risk: str) -> str:
        if worker_tier == "opus":
            return "sonnet"  # different model
        rt = up(worker_tier)
        if rt == "opus" and not (risk == "high" or worker_tier == "sonnet"):
            rt = "sonnet"
        return rt

    def _review(self, tid, task, ts, diff: str, changed: list, judgement: list, attempt: int) -> Outcome:
        self._set(tid, step="L3")
        criteria = judgement + [REVIEW_GENERAL]
        ids = [c["id"] for c in criteria]
        tier = self._reviewer_tier(ts["tier"], ts["risk"])
        reason = self.acct.check("reviewer", tier)
        if reason:
            self.log.emit("verify_stage", tid, attempt, stage="L3", skipped=reason)
            return Outcome(False, ["L3:budget"], "", "L3", terminal=True)
        short, _ = truncate(diff, 20000)
        call = ClaudeCall(role="reviewer", key=f"{tid}:{attempt}:review", model=tier, cwd=self.state["worktree"],
                          prompt=reviewer_prompt(task, criteria, short, "\n".join(changed)),
                          timeout_s=self.cfg.timeouts[tier], tools=self.cfg.read_tools)
        self.acct.record("reviewer", tier)
        res = self._call(call, tid, attempt)
        if res.error in (ErrorClass.RATE_LIMIT, ErrorClass.AUTH, ErrorClass.CANCELLED):
            return Outcome(False, ["L3:paused"], "", "L3", call_error=res.error)
        passed, failed = parse_review(res.text, res.structured, ids) if not res.error else (False, ids)
        self.log.emit("verify_stage", tid, attempt, stage="L3", tier=tier, passed=passed, failed=failed,
                      error=res.error)
        feedback = "Independent review: not met: " + ", ".join(failed) if failed else ""
        return Outcome(passed, [f"L3:{f}" for f in failed], feedback, "L3")

    # ---------- Finishing a task ----------

    def _accept(self, tid, task, ts, attempt, report, changed) -> None:
        wt = Path(self.state["worktree"])
        if task.get("kind") in WRITE_KINDS:
            self._set(tid, status="RUNNING", step="COMMIT")
            commit = self.git.commit(wt, f"{tid}: {task['title']}", self.run_id, tid, attempt)
            self.fault_hook("after_commit", tid)
            ts["commit"] = commit
            self.state["head"] = commit
        self._set(tid, status="ACCEPTED", step="NONE")
        if task.get("kind") in WRITE_KINDS:
            if task.get("kind") == "test":
                self._freeze_test_task(tid, changed)
                fresh = run_checks(self.state["plan"].get("global_checks", []), wt, self.state["python"],
                                   self.cfg.l1_timeout_s)
                self.state["baseline"] = {k: sorted(set(self.state["baseline"].get(k, [])) | set(r["ids"]))
                                          for k, r in fresh.items()}
        art = self.run_dir / "artifacts" / tid
        art.mkdir(parents=True, exist_ok=True)
        words = str((report or {}).get("summary", "")).split()
        (art / "summary.md").write_text(" ".join(words[:300]), encoding="utf-8")
        self._feedback.pop(tid, None)
        self.log.emit("state_change", tid, attempt, status="MERGED", tier=ts["tier"])
        self._set(tid, status="MERGED", step="NONE", reason="")

    def _freeze_test_task(self, tid: str, changed: list) -> None:
        wt = Path(self.state["worktree"])
        for p in changed:
            if any(glob_match(p, w) for w in self.tasks[tid].get("writes", [])):
                self.state["frozen"][norm_path(p)] = file_sha256(wt / p)

    def _pause(self, error: str, tid: str | None) -> None:
        with self.lock:
            first = self.pause_reason is None
            self.pause_reason = self.pause_reason or error
            if tid:
                self.state["tasks"][tid].update(status="READY", step="NONE")
            if first:
                self.runner.kill_all()
                self.log.emit("run_paused", tid, reason=error)
            self._run(status="PAUSED", note=_NOTES.get(self.pause_reason, msg.NOTE_PAUSED))

    def _finish(self) -> str:
        if self.pause_reason:
            self._run(status="PAUSED", phase="EXECUTE")
            return "PAUSED"
        tasks = self.state["tasks"]
        merged = sum(1 for t in tasks.values() if t["status"] == "MERGED")
        status = "SUCCESS" if merged == len(tasks) else "PARTIAL"
        note = self.state.get("note") if status == "PARTIAL" and self.state.get("note") else ""
        self._run(status=status, phase="DONE", note=note or msg.NOTE_MERGED.format(merged=merged, total=len(tasks)))
        self._write_report(status)
        self.log.emit("run_done", status=status, merged=merged, total=len(tasks), **{
            k: self.state["budget"].get(k) for k in ("calls", "opus", "planner", "reviewer", "weighted", "elapsed_s")})
        return status

    def _write_report(self, status: str) -> None:
        lines = [f"# Run {self.run_id}: {status}", "",
                 f"Integration branch: `{self.state['branch']}` (worktree `{self.state['worktree']}`)", "",
                 "| Task | Title | Status | Attempts | Model | Reason | Verdict |", "|---|---|---|---|---|---|---|"]
        for tid, t in self.tasks.items():
            ts = self.state["tasks"][tid]
            lines.append(f"| {tid} | {t['title'][:60]} | {ts['status']} | {ts['attempt']} | {ts['tier']} "
                         f"| {ts.get('reason') or '-'} | [ok]/[wrong] |")
        budget = self.state["budget"]
        lines += ["", "Verdict: fill in by hand for the false-accept sample.", "", "## Usage", "",
                  "| Model | Calls | Tokens | Seconds |", "|---|---|---|---|"]
        for m, u in budget.get("by_model", {}).items():
            lines.append(f"| {m} | {u.get('calls', 0)} | {u.get('tokens', 0)} | {u.get('seconds', 0)} |")
        jev = budget.get("jev", {})
        if jev.get("calls"):
            lines.append(f"| jev | {jev['calls']} ({jev.get('errors', 0)} errors) | - | {jev.get('seconds', 0)} |")
        lines += ["", "Take over (from its own branch):", "",
                  f"    git merge --ff-only {self.state['branch']}", "",
                  f"Clean up: kickroll clean {self.run_id}"]
        (self.run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

