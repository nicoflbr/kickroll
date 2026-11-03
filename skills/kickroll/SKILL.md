---
name: kickroll
description: Orchestrate a larger coding task in the user's own git repository with kickroll (plan, frozen acceptance tests, cost-aware Claude worker tiers, test-driven retries, integration branch). Use when the user describes a multi-part change that needs tests and asks to run it with kickroll, to orchestrate it, or to use cheaper model tiers with retries. Do not use for small single-file changes, questions, or repositories the user does not own.
---

# kickroll: start an orchestrated run from the chat

kickroll plans a task, freezes the acceptance tests, runs each sub-task as a `claude -p` worker on
the cheapest tier likely to succeed, checks the result with real tests, and retries on a higher
tier. The work lands on a `kickroll/<run id>` branch. This skill drives that from the conversation.

## Rules that always apply

- **Never merge, push, delete branches, or run `clean` without the user's explicit yes** in this chat.
- **Own, trusted repositories only.** Workers and test commands run unsandboxed under the user's
  account. If the repository is not the user's own, say so and do not run kickroll.
- **Ask before spending.** Planning costs about 2 Opus calls (tens of thousands of tokens each, from
  the user's subscription); a run adds several worker and review calls. State this before `plan`.
- **Do not orchestrate small tasks.** If the change is a single file, a quick fix, or fewer than
  about three separable sub-tasks, or the repository has no usable test command, do it directly
  yourself and say why. kickroll also makes its own bypass decision; do not override it with
  `--force-orchestrate` unless the user asks.
- Never print or pass secrets on a command line (`TYPESAFE_API_KEY` stays in the environment).
- Do not poll a running job. Run it in the background and wait for the notification.

## 1. Find how to run kickroll

This skill lives at `<plugin root>/skills/kickroll`. The Python package is at the plugin root, so
no installation is needed:

- Plugin root = two directories above this skill's base directory (the line "Base directory for
  this skill" above gives the path).
- Run it as `python -m kickroll ...` with `PYTHONPATH` set to the plugin root. In bash:
  `PYTHONPATH="<plugin root>" python -m kickroll --help`. In PowerShell:
  `$env:PYTHONPATH = "<plugin root>"; python -m kickroll --help`.
- If `kickroll --help` already works on `PATH` (pip-installed), use that instead.
- Python 3.11+ and git are required. If `--help` fails, report the error and stop.

Below, `KICKROLL` stands for whichever invocation works, always with `--repo <repo root>`.

## 2. Check the preconditions

Work in the user's repository (ask which one if it is unclear):

1. `git rev-parse --show-toplevel` gives the repository root; it must be a git repository.
2. `git status --porcelain` must be empty. If it is not, tell the user and ask them to commit or
   stash; kickroll refuses a dirty tree.
3. Find the **test command** kickroll can run. v1 accepts only `python -m unittest ...`,
   `python -m pytest ...` and `python -m py_compile ...` as commands. Look at the repository
   (a `tests/` folder, `pytest.ini`, `pyproject.toml`) and propose a command; confirm it with the
   user if it is not obvious. If the project needs another test tool, say that kickroll v1 cannot
   gate on it and offer to do the work directly instead.
4. Claude Code must be logged in. `plan` checks this itself and stops with a clear message
   ("not logged in"); relay it and ask the user to run `claude` and `/login`.

## 3. Create and show the plan (nothing is executed yet)

Write the plan outside the repository so the working tree stays clean:

```
KICKROLL --repo <repo> plan --dry --test-cmd "<test command>" --out <temp dir>/kickroll-plan.json "<the task, in the user's words>"
```

This takes one to a few minutes (the planner runs on Opus). Run it in the background if your tool
allows, otherwise with a long timeout. If kickroll answers that the task should be handled
directly (bypass), do that instead and tell the user.

Then present the plan briefly and ask for approval:

- each task: id, one-line goal, kind (code / test / review), risk, planned tier, dependencies;
- the frozen acceptance tests (file names) and the global check command;
- budgets (calls, Opus calls, minutes) and the expected cost class;
- that approval starts real worker calls.

Ask with an explicit yes/no question. If the user wants changes, adjust the task description and
re-plan (a re-plan costs another planner call) or edit the plan JSON together with them. Do not run
without a yes.

## 4. Execute after the yes

```
KICKROLL --repo <repo> run --plan <temp dir>/kickroll-plan.json --yes
```

Run it in the background. Tell the user:

- the run id (printed at the start and in `<data dir>/status.json`; data dir is `$KICKROLL_HOME` or
  `~/.kickroll`);
- progress is visible in the Claude Desktop pane `/kickroll` if the `kickroll-panel` mod is
  installed, otherwise with `KICKROLL --repo <repo> status <run id>`;
- the repository's own branch is not touched; work happens on `kickroll/<run id>`.

Wait for the completion notification. Do not start a second run in the same repository meanwhile.

## 5. Check the result yourself

Do not rely on kickroll's own summary alone:

1. Read the printed report path (`report.md` in the run folder) and the final status
   (SUCCESS / PARTIAL / FAILED / PAUSED).
2. For SUCCESS or PARTIAL, verify the branch in a clean checkout:
   `git archive kickroll/<run id> | tar -x -C <temp dir>/verify`, then run the test command there.
   Also show `git log --oneline` and `git diff --stat <base>..kickroll/<run id>`, and confirm that
   the user's branch is unchanged.
3. Where the task has a visible behaviour (a CLI, an API), try it once.
4. Report honestly: what passed, what did not, the number of calls per model (the status file has
   a `usage` block), and anything suspicious such as tests that look trivial.

## 6. Offer the merge (never without a yes)

If everything is verified, show the exact command and ask:
`git merge --ff-only kickroll/<run id>`. Only after an explicit yes, run it. Afterwards offer
`KICKROLL --repo <repo> clean <run id> --delete-branch` and wait for a separate yes.

## When it does not succeed

- **PAUSED** (rate limit, login, timeout): explain the note in the report. After the user fixed the
  cause, `KICKROLL --repo <repo> resume <run id>` continues; budgets carry over.
- **PARTIAL / FAILED**: read the report, tell the user which tasks failed and why (stagnation,
  forbidden file, budget). Offer: finish the remaining tasks directly, adjust the task and re-plan,
  or leave the branch for inspection. Do not re-run automatically.
- **Preflight errors** (dirty tree, missing test command, no baseline): fix the cause with the
  user and start again from step 2.

## What leaves the machine

Claude calls use the user's own Claude Code login. If Jev (TypeSafe AI) is enabled
(`TYPESAFE_API_KEY` set and `typesafe-sdk` installed), task titles, descriptions and acceptance
criteria go to TypeSafe AI after secret redaction; diffs only if `[jev] send_diffs = true` is set
in `kickroll.toml`. If the user did not enable Jev, nothing else is sent. Mention this once when
Jev is active.
