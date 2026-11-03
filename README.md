# kickroll

**A deterministic orchestrator for Claude Code workers, with test-driven retries and a model ladder.**

You describe a task. kickroll plans it, freezes the acceptance tests, hands each sub-task to a
`claude -p` worker on the cheapest model that is likely to do the job, checks the result with real
tests, and retries with feedback on a stronger model until the checks pass or a budget runs out.
Escalates like a kickroll: faster, harder, until it drops.

It runs on your Claude subscription through Claude Code's print mode. **No Anthropic API key is
needed or used.** A [Jev](https://docs.typesafe.ai) (TypeSafe AI) key is optional.

> **Status: v0.1, experimental.** The loop works end to end (see [Live test](#live-test)), but the
> claim "cheaper, faster or better than one direct call" is **not measured yet**. A `bench` command
> exists to measure it; please treat savings as a hypothesis until you have run it on your own tasks.

> **Only run it on repositories you trust.** Workers and test commands run unsandboxed under your
> user account. The tool whitelist for workers only restricts tool calls, not the code that tests
> execute.

## How it works

```
task ──► preflight ──► bypass? ──► planner ──► you approve ──► loop per task ──► integration branch
         (git, login,   (small      (Opus,                      route → worker →
          baseline)      task? run     tasks + frozen            L0 paths/hashes → L1 tests →
                         directly)     acceptance tests)         L3 review (when unsure) → commit
                                                                 fail: retry with feedback,
                                                                 then a higher tier
```

- **Plan first.** One planner call (Opus by default) splits the task into a small DAG, writes the
  acceptance tests, and annotates each task with risk, complexity and a suggested tier. You approve
  the plan before anything runs.
- **Frozen acceptance tests.** The tests exist before the implementation and are hashed. A worker
  that changes them fails immediately. A task's own worker never writes the tests that accept it.
- **Check cascade.** L0 structure (changed paths, forbidden files, frozen hashes), L1 your test
  command (only *new* failures count against a task), L3 an independent review by another tier when a
  judgement criterion needs it. Jev, if enabled, is only a shadow filter and never accepts on its own.
- **Loop engineering.** Attempt 1 on the routed tier, attempt 2 with feedback, attempt 3 one tier
  higher, a fourth on Opus for high-risk tasks. Identical failed criteria in two attempts escalate
  immediately. Timeouts, rate limits and login problems **pause** the run and never count as an attempt.
- **Budgets.** Per run (calls, Opus calls, planner calls, reviewer calls, minutes) and per day.
  `resume` keeps the counters.
- **Safe landing.** Work happens on a `kickroll/<run id>` branch in its own worktree. Your branch is
  never touched; you merge with one command at the end.

## Requirements

- Python 3.11+ (standard library only)
- git
- [Claude Code](https://code.claude.com) installed and logged in with your subscription
  (`claude`, then `/login`). If `claude` is not on your `PATH`, set `CLAUDE_BIN`.
- Optional: `pip install "kickroll[jev]"` and `TYPESAFE_API_KEY` for Jev routing.

## Quick start

```bash
pip install .                      # or: python -m kickroll ...
cd your-repo                       # a clean git working tree
cp /path/to/kickroll/kickroll.example.toml kickroll.toml   # optional

kickroll plan "Add a CLI flag --json to report.py and cover it with tests" \
  --test-cmd "python -m unittest discover -s tests" --dry   # create and show the plan only

kickroll run --plan plan.json      # approve and execute
kickroll status <run id>
git merge --ff-only kickroll/<run id>      # take over the result
kickroll clean <run id> --delete-branch
```

`kickroll plan` decides first whether the task is worth orchestrating at all: small tasks are
answered directly with one call. Use `--force-orchestrate` to skip that decision, `--bypass` to
force the direct call, and `--fake` to try the whole flow without calling Claude.

Every `claude -p` call carries a fixed overhead of roughly 27k–40k cached input tokens (measured),
so very small tasks are cheaper to run directly.

## Start it from the chat

The repository is also a Claude Code plugin. Its skill lets you describe the task in the chat; Claude
checks the preconditions (clean git tree, a test command, logged-in `claude`), creates the plan,
shows it and waits for your approval, runs it in the background, verifies the branch in a clean
checkout itself, and offers the merge. It never merges, pushes or cleans up without your explicit yes,
and it does not orchestrate small tasks.

At the prompt of a terminal session:

```
/plugin install kickroll --marketplace nicoflbr/kickroll
```

From a shell: `claude plugin install kickroll --marketplace nicoflbr/kickroll`. In the desktop app,
where `/plugin` does not run: click **+** next to the prompt box, then Plugins, Add marketplace with
`nicoflbr/kickroll`, then Add plugin. Optionally also install `kickroll-panel` (see below) to watch a run live.

The skill finds the Python package relative to itself, so `pip install` is not required for this route.
Then simply say, in a git repository of your own: *"Use kickroll to add X, with tests"*.

## Routing: with or without Jev

`[routing] backend` in `kickroll.toml`:

| Backend | What decides the tier | Cost |
| --- | --- | --- |
| `auto` (default) | Jev if a key and the SDK are present, else the planner's annotations, else the heuristic | – |
| `jev` | Jev (`system_one` with small Choice/Score/Noul questions, about 0.3 s) | one Jev request per task |
| `planner` | the risk/complexity/tier the planner wrote into the plan | free |
| `heuristic` | plain rules (task kind, number of files, description length, risk) | free |
| `haiku` | opt-in: a Haiku call for the bypass decision only | 9–17 s and 35–74k cached tokens per call (measured), uncalibrated, tends to pick itself |

The escalation ladder protects every backend against mis-routing.

## What leaves your machine

- Claude calls go through your own Claude Code login, like any other `claude -p`.
- With Jev enabled, task titles, descriptions and acceptance criteria are sent to TypeSafe AI.
  Diffs are **not** sent unless you set `[jev] send_diffs = true` per repository. Everything sent is
  passed through a secret redactor first. Without Jev nothing is sent anywhere else.
- Logs and status files contain hashes, counters and short titles. They never contain prompts,
  diffs, test output or keys. The Jev key is read from `TYPESAFE_API_KEY` and stripped from the
  environment of every worker.
- Worker sessions run with `--no-session-persistence` and `--setting-sources project`, so your
  global hooks and settings are not loaded into workers.

## Files

| Path | Purpose |
| --- | --- |
| `~/.kickroll` or `$KICKROLL_HOME` | `status.json` (for the desktop pane; includes per-model `usage` and the `jev` request counters), `heartbeat.json` (rewritten every 5 s while a kickroll command runs, so the pane can tell a dead run from a live one), `usage.json` (daily budget), `router_log.jsonl` (written by the optional prompt router in `examples/router`, not by runs), `runs/<id>/` (state, events, plan, report) |
| `kickroll.toml` in your repo | configuration, see `kickroll.example.toml` (unknown keys are an error) |
| `docs/design.md` | design decisions and the reasoning behind them |
| `mod/kickroll-panel` | Claude Desktop pane that shows a run live ([README](mod/kickroll-panel/README.md)) |
| `examples/router` | the original stand-alone router and a Claude Code hook (see its caveats below) |

## Live test

A first end-to-end run on a throw-away repository (add `count_words` and `top_n` plus a CLI to a
tiny package): Opus planner (2 calls), three tasks accepted on the first attempt by Sonnet, review by
Opus; 14 tests green in a clean checkout, about 2 minutes. An earlier run of the same plan failed for
a real reason the unit tests could not show (an uncalibrated Jev shadow value rejected three attempts
whose tests were green), which is why Jev may only filter and thresholds sit at 0.7.
That task is small enough that a direct call would have been cheaper, which is the point of the
bypass and the benchmark.

## Examples: the stand-alone router

`examples/router` contains the first version of this idea: classify one prompt (with Jev) and either
forward it with `claude -p --model <tier>` or use it as a Claude Code hook. Be aware of the limits:
**no hook can change the model of a running session.** The hook can only advise Claude to delegate to
a subagent with the chosen model, and delegation is not guaranteed. The orchestrator avoids this by
starting each worker with `--model` itself.

## Limitations

- Tested on Windows only; Linux and macOS code paths exist (process groups, XDG paths) but are untested.
- Not sandboxed (see the warning above). Run it in a disposable checkout if in doubt.
- Jev's shadow verification (L2) does not decide yet; thresholds are not calibrated.
- Workers run sequentially for writing tasks; only read-only tasks run in parallel.
- `bench` is a scaffold: it records calls, time and test results for "direct" versus "kickroll" but
  has no false-accept sampling yet.
- Windows job objects and rate-limit message texts were verified against Claude Code 2.1.x only.

## Development

```bash
python -m unittest discover -s tests     # about 75 s, no network, no claude calls
```

Tests use fake runners and a fake Jev client; the end-to-end behaviour above was checked live once.

## License

MIT, see [LICENSE](LICENSE). kickroll is an independent project. It is not affiliated with
Anthropic or TypeSafe AI; "Claude" and "Jev" are their respective products.
