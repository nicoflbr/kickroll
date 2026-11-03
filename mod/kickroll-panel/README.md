# kickroll-panel

A Claude Desktop mod that shows a running [kickroll](../../README.md) loop as a pane:
phase pipeline, the current loop step (routing, worker, checks, tests, review, commit) with the
attempt and retry arrow, task cards, budgets, the actual usage per model (calls, tokens, time)
and the router's recent choices. The status line shows the active task and step.

It only reads files that kickroll writes, so it works with any kickroll run. Each run and each skill bypass
has its own entry; with more than one entry the pane shows a tab bar (repository and short run id, a dot in
the run's status color; live runs first, then by last update; at most five tabs plus an overflow tab that
steps through the rest). Click a tab to see that run. Several sessions and repositories can run at the
same time; only two runs in the same repository are refused by kickroll itself.

| File | Content |
| --- | --- |
| `<data dir>/status/<key>.json` | one entry per run (`<run id>`) or skill bypass (`bypass-<session>`): repository, run, phase, tasks, budgets, usage, jev request counters (no prompts, no diffs) |
| `<data dir>/status/pid-<pid>.beat.json` | pid and a counter, rewritten every 5 s while a kickroll command runs |
| `<data dir>/status.json`, `heartbeat.json` | kickroll before 0.2.0: read only when `status/` does not exist |
| `<data dir>/router_log.jsonl` | decisions of the prompt router hook in `examples/router` (prompt hashes only); they cover all sessions, not the run |

The data directory is `$KICKROLL_HOME` or `~/.kickroll`.

A run shows as `STALE` (grey) when its entry says RUNNING but the beat file of its pid is missing or
older than 30 s: the kickroll process is gone. The pane then shows one line (run id, the phase it
stopped in, last update) instead of the whole loop. `BYPASSED` (blue) means the chat handled the task
itself without a run (`kickroll bypassed "<reason>"`, called by the kickroll skill).

## Use

The pane opens when a session starts (start a new session after installing). If you closed it,
reopen it with `/kickroll-panel` (`/kickroll` is the kickroll skill). Set `KICKROLL_LANG=de` for German texts (default: English).

The pane is drawn as an image on the Desktop surface (sharper than the interactive frame in our
tests) and as text on other surfaces. It polls every 1.5 s.

## Install

The mod format is described in the Claude Code plugin docs. From a checkout:

```bash
claude --plugin-dir mod/kickroll-panel
```

or install it from the marketplace of this repository:

```
/plugin install kickroll-panel --marketplace nicoflbr/kickroll
```

Validate with `claude plugin validate mod/kickroll-panel`.
