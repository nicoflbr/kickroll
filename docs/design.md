# kickroll v2 – binding decisions

v2 replaces the disputed parts of `CONCEPT_v1.md` after two independent Opus reviews (A: feasibility, B: economics/safety). What is not stated here applies as in v1 (plan schema §3, state machine §6, logging §10). In case of conflict, v2 wins.

## 1. Honest expectation
- Costs: compared with "everything Sonnet directly", the orchestrator rarely saves subscription quota (overhead from the planner, worker restarts, verification). The benefit lies in **quality** (test-driven retries, independent acceptance) and **context hygiene** for large tasks; for clearly separable, read-only subtasks also in **time**. This is an assumption, not a measurement -> the benchmark mode is part of v1.
- Therefore: **bypass before the planner** (see 2) and measurement instead of belief.

## 2. Flow (v1)
0. **Preflight** (deterministic): Git repo? clean working tree? `claude` found and logged in? Test command present in `orch.toml`/CLI? Run the baseline of the `global_checks` once; save the result as the baseline (only NEW failures count).
1. **Bypass decision BEFORE the planner** via Jev (one request, questions on the raw request): `est_subtasks` (Score 0..3), `needs_repo_changes` (Noul), `risk` (Choice low/medium/high). Orchestrator only if est_subtasks >= 2 (Score >= 1.5) AND a test command is present; otherwise a direct call: `claude -p` with the tier from the routing table.
2. **Planner** (one `claude -p --json-schema` call): model defaults to **Opus** (overridable via config `planner_model = "sonnet"`; then Opus only if complexity > 1.5 or risk=high). Planner calls do **not** count towards the worker Opus budget, but towards their own counter `planner_calls` (default max. 2 per run = 1 plan + 1 re-plan). Plan = task list (JSON, validated). Afterwards **plan approval by the user** (`--dry` shows/saves the plan, `run --plan plan.json` executes; `--yes` for autostart).
3. **Acceptance tests first**: every task with `kind=code` needs `acceptance` tests that do NOT come from the worker of the same task (from the planner or from a dedicated test task with another model). Commit before implementation; freeze file hashes. If a worker changes a frozen file -> hard fail (L0).
4. **Execution on the integration branch** `kickroll/<run_id>` in ONE Git worktree per run (short path `%LOCALAPPDATA%\kickroll\wt\<run_id8>`, `core.longpaths=true`), never on the user's branch. Tasks with `writes` run **sequentially**; tasks without `writes` (analysis/review/research) may run in parallel threads (`max_parallel` default 2, never two Opus at once). All Git operations behind a lock. One commit per accepted task with trailer `Kickroll-Task: <run_id>/<task_id>:<attempt>`. At the end the orchestrator suggests a merge/fast-forward; the user executes it.
5. **Acceptance cascade**: L0 (paths/structure/frozen tests; diff via `git add -A` + `git diff --cached --binary`, `PYTHONDONTWRITEBYTECODE=1`, fixed artifact ignore list) -> L1 (tests/checks, template commands only, see 4) -> L2 Jev (v1: only **fail filter and shadow log**, never the sole pass) -> L3 review by another/higher model for `judgement` criteria and when uncertain; the reviewer sees criteria + diff, not the worker's reasoning. If the diff is truncated, any Jev result counts at most as "unsure".
6. **Loop**: decision table (attempt x signal -> action) with unit tests: attempt 1 routing tier; attempt 2 same tier + feedback (error output, failed criteria); attempt 3 one tier higher; with risk=high attempt 4 Opus. Stagnation = **same failed criteria in two attempts** -> escalate immediately. A retry starts from a fresh reset of the task's worktree state (back to the base commit). Timeouts, rate limits and auth errors NEVER count as solution attempts and NEVER escalate.
7. **Termination**: no READY/RUNNING task left -> status SUCCESS or PARTIAL (with a list of BLOCKED/FAILED/UNCLEAR). Dependents of BLOCKED/FAILED become SKIPPED. A task becomes READY only when all predecessors are **MERGED** (committed on the integration branch). An exhausted reviewer budget on "unsure" -> fail -> PARTIAL.

## 3. Routing of the subtasks (own table, not `router.route`)
One Jev request per task: `task` (Choice), `complexity` (Score 0..2), `simplicity` (Score 0..2, R2), `risk` (Choice). Table (starting values, to be checked against escalation data in shadow mode):
- haiku: risk=low AND complexity < 0.8 AND simplicity >= 1.2 AND choice confidence >= 0.6
- opus: complexity > 1.5 OR risk=high
- otherwise sonnet. Jev error/uncertainty (confidence < 0.3) -> sonnet.
`router.py` gets `route_from_answers(answers)` extracted; the existing code stays compatible.

## 4. Security (honest commitment)
- Isolation applies to tool calls, **not** to executed code. v1 therefore: **only for your own, trusted repos**, with a clear CLI warning at start; sandbox / separate Windows account = later stage.
- Worker call: `claude -p --output-format json --no-session-persistence --strict-mcp-config --setting-sources project?` [P: verify effect live] `--tools "Read,Grep,Glob,Edit,Write"` (strict whitelist: **no Bash, no PowerShell, no Agent, no WebFetch/WebSearch**), prompt via **stdin**. Tests are run by the **orchestrator** (L1), not by the worker.
- L1 commands only from templates (e.g. `python -m pytest <path>`, `python -m unittest <module>`, `python -m py_compile <file>`), argv list, no `shell=True`, no `-c`/`-e`, no `npm` script. In `forbidden` by default: `package.json`, `conftest.py`, `pytest.ini`, `setup.cfg`, `pyproject.toml`, `tox.ini`, `.github/**`, `.claude/**`, `.mcp.json`.
- Environment of child processes: `TYPESAFE_API_KEY`, `ANTHROPIC_*` and tokens removed; `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`, `PYTHONDONTWRITEBYTECODE=1` set. Note in the docs: `setx` stores the key in `HKCU\Environment`; a malicious code run could read it there (hence the "trusted repositories" warning).
- Worker/review output is **data**: in check prompts it sits between random nonce markers; summaries likewise; additionally `git diff --stat` of the predecessors instead of pure self-reports.
- Data flow to TypeSafe: **default `jev_send_diffs=false`** (opt-in per repo). Even then title/description/criteria are sent; before every send **secret redaction** (regex for keys/tokens/PEM, long high-entropy strings) and relative shortening of paths. Test output is sent to Jev only redacted/truncated.
- Logs (JSONL): only hashes, numbers, IDs, status. No prompts, diffs, raw test output. Exceptions are never logged with output fragments.

## 5. Claude call (ClaudeRunner) – do not reuse `router.run_claude`
- Discovery: `CLAUDE_BIN`, then PATH, then the desktop installation; **always check the fallback when a file is missing**; sort versions as tuples (2.1.100 > 2.1.99). For a `.cmd` shim, resolve to the real `claude.exe`.
- Result only from `--output-format json` (`is_error`, `subtype`, `result`, if present `usage`/`total_cost_usd`; verify field names live [P]). Error classes: AUTH, RATE_LIMIT (texts such as "session limit", "weekly limit", "Opus limit", "429"), TIMEOUT, TOOL_ERROR, OTHER. Classification only from stderr and structured fields, NOT from the worker's payload (no `"/login" in output`).
- Rate limit/auth -> run `PAUSED`, save state, end processes, clear message; no retry loop, no escalation.
- Timeouts per tier (start: haiku 300 s, sonnet 600 s, opus 900 s) from **one** source (config), not additionally in the plan.
- Process trees: Windows Job Object (`KILL_ON_JOB_CLOSE`, ctypes) or `taskkill /T /F`; PIDs in `state.json`; on `resume`, orphaned processes are ended. `--max-turns` does NOT exist in the installed version according to `--help` -> do not use it.
- Parallel `claude -p` runs with one subscription login: check in a live test [P].

## 6. State, budgets, resumption
- `state.json` is atomic (tmp + `flush` + `os.fsync` + `os.replace` with retry/backoff against Windows PermissionError), write access under a lock. Stored: task status, attempts, budget counters, consumed wall clock, PIDs, base/integration commit.
- `resume`: takes over the consumed budgets; checks "task already committed?" via `git log --grep "Kickroll-Task: <run>/<task>:<attempt>"` (not via the state).
- Budgets per run (start): 25 calls (workers + reviewers), of which max. 3 Opus, 60 min; the planner separately via `planner_calls` (default max. 2, does not count towards calls/Opus). Additionally a daily budget in `%USERPROFILE%\.kickroll\usage.json`; before every call, usage + the expected call are checked. Ctrl+C ends all workers cleanly and saves.
- Status pane: `status.json` (version 1) has a top-level `jev` block `{calls, errors, seconds}`: Jev requests of the run (routing, L2 check; the bypass question happens before the run and is not included). Counted in `Accountant.record_jev` (state `budget.jev`, survives resume), not a Claude call and not against any budget. The block was added without a version bump because readers ignore unknown fields (the pane shows zeros when it is missing); `validate_status` requires it.
- Clean-up: `kickroll clean <run_id>` removes worktree/branch (branch only on request).

## 7. Measuring
`bench`: same task + same test command once "Sonnet directly", once with the orchestrator (optionally "Opus directly"); records calls per model, wall clock, number of retries, test result, false-accept sample. Cost estimate only as calls x weight (haiku 0.33, sonnet 1, opus 1.7; [A] unknown subscription weighting, configurable). Thresholds in 3 and 2 are readjusted based on this data. v1 success criterion: the orchestrator must not be worse on test results for medium/large tasks; costs/time are reported, not promised.

## 8. Scope v1 / later
v1: preflight, bypass, planner + approval, integration branch + one worktree, sequential write tasks, parallel read tasks, L0/L1/L3, Jev routing + shadow verification, loop/escalation, resume, JSONL log, dry run, bench, FakeRunner/FakeJev tests (incl. scenarios: previously red tests, missing venv, `/login` in code, rate-limit text in worker output, new untracked files, `__pycache__`, crash between commit and state).
Later: worktree per task (real parallel write tasks), Jev as pass instance after calibration (`calibrate.py`), sandbox/separate account, Claude Code subagent mode.

## 9. Open [P] points (only checkable live with a logged-in `claude`)
JSON field names of `--output-format json`; effect of `--setting-sources`/`--strict-mcp-config` on hooks (our own `router_hook.py` must not interfere in the worker); parallel operation with one login; exact rate-limit texts.
