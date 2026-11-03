# kickroll-panel

A Claude Desktop mod that shows a running [kickroll](../../README.md) loop as a pane:
phase pipeline, the current loop step (routing, worker, checks, tests, review, commit) with the
attempt and retry arrow, task cards, budgets, the actual usage per model (calls, tokens, time)
and the router's recent choices. The status line shows the active task and step.

It only reads two files that kickroll writes, so it works with any kickroll run:

| File | Content |
| --- | --- |
| `<data dir>/status.json` | run, phase, tasks, budgets, usage (no prompts, no diffs) |
| `<data dir>/router_log.jsonl` | router decisions (prompt hashes only) |

The data directory is `$KICKROLL_HOME` or `~/.kickroll`.

## Use

Open the pane with `/kickroll`. Set `KICKROLL_LANG=de` for German texts (default: English).

The pane is drawn as an image on the Desktop surface (sharper than the interactive frame in our
tests) and as text on other surfaces. It polls every 1.5 s.

## Install

The mod format is described in the Claude Code plugin docs. From a checkout:

```bash
claude --plugin-dir mod/kickroll-panel
```

or install it from the marketplace of this repository:

```
/plugin install kickroll-panel --marketplace <owner>/<repo>
```

Validate with `claude plugin validate mod/kickroll-panel`.
