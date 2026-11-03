"""User-facing short texts in one place (status.json notes, CLI banner). English only."""

TRUST_WARNING = ("WARNING: Only for your own, trusted repositories. Worker code and tests run unsandboxed under "
                 "your user account (isolation only covers tool calls).")

# status.json `note` (max. 80 chars, never output fragments)
NOTE_RATE_LIMIT = "Rate limit: paused"
NOTE_AUTH = "Not logged in: paused"
NOTE_INTERRUPTED = "Interrupted (Ctrl+C): paused"
NOTE_PAUSED = "paused"
NOTE_BUDGET = "Budget exhausted ({reason})"
NOTE_MERGED = "{merged} of {total} tasks merged"
NOTE_PLAN_WAITING = "Plan waiting for approval"
NOTE_DIRECT_SUGGESTED = "Direct call suggested"
NOTE_DIRECT_RUNNING = "Direct call {tier}"
NOTE_DIRECT_DONE = "Direct call finished"
NOTE_DIRECT_FAILED = "Direct call: {error}"
NOTE_PLANNER = "Planner: {tier}"
NOTE_PLAN_INVALID = "Plan invalid"
NOTE_ABORTED = "Aborted: {error}"
NOTE_ENDED = "Ended without a final status"
