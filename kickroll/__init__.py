"""kickroll v1: deterministic loop around `claude -p` workers; Jev is an optional routing backend."""

APP_NAME = "kickroll"  # single source for the project name (data dir, env var, worktree root)
HOME_ENV = "KICKROLL_HOME"
TIERS = ("haiku", "sonnet", "opus")
