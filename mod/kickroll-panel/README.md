# kickroll-panel

A Claude Desktop mod that shows a running [kickroll](../../README.md) loop as a pane:
phase pipeline, the current loop step (routing, worker, checks, tests, review, commit) with the
attempt and retry arrow, task cards, budgets, the actual usage per model (calls, tokens, time)
and the router's recent choices. The status line shows the active task and step.

It only reads two files that kickroll writes, so it works with any kickroll run:

| File | Content |
| --- | --- |
| `<data dir>/status.json` | run, phase, tasks, budgets, usage, jev request counters (no prompts, no diffs) |
| `<data dir>/heartbeat.json` | pid and a counter, rewritten every 5 s while a kickroll command runs |
| `<data dir>/router_log.jsonl` | decisions of the prompt router hook in `examples/router` (prompt hashes only); they cover all sessions, not the run |

The data directory is `$KICKROLL_HOME` or `~/.kickroll`.

A run shows as `STALE` (grey) when `status.json` says RUNNING but no heartbeat from the same pid has
arrived for 30 s: the kickroll process is gone and the pane shows its last state, not a live one.

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
