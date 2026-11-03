"""Claude Code hook for Jev routing (optional, standard library + router.py only).

Two hook events, one script:
  UserPromptSubmit  -> Jev classifies; if the model differs, Claude is told to delegate to the
                       Agent tool.
  PreToolUse (Agent)-> enforces the chosen `model` in the Agent call.

Limits: no hook can set the session model. Whether the main model delegates is not guaranteed.
Registration: see the comment at the end (not added to settings.json automatically).
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import router  # noqa: E402

SESSION_MODEL = os.environ.get("ROUTER_SESSION_MODEL", "sonnet")  # model the session runs on


def _state_file(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")[:64] or "default"
    return Path(tempfile.gettempdir()) / f"jev_router_{safe}.txt"


def on_prompt(event: dict) -> dict | None:
    prompt = event.get("prompt", "")
    decision, latency_ms = router.decide(prompt)
    router.log_decision(prompt, decision, latency_ms)
    state = _state_file(event.get("session_id", ""))
    if decision.model == SESSION_MODEL:
        state.unlink(missing_ok=True)
        return None  # answer directly, no delegation needed
    state.write_text(decision.model, encoding="utf-8")
    note = (
        f"[Jev-Router] This request is meant for the model '{decision.model}' ({decision.reason}). "
        f"Delegate it fully to the Agent tool (subagent_type general-purpose, model='{decision.model}') "
        "and return its result unchanged."
    )
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": note}}


def on_agent_call(event: dict) -> dict | None:
    state = _state_file(event.get("session_id", ""))
    if not state.exists():
        return None
    model = state.read_text(encoding="utf-8").strip()
    if model not in router.MODELS.values():
        return None
    tool_input = dict(event.get("tool_input", {}))
    tool_input["model"] = model  # updatedInput replaces the whole object -> keep the remaining fields
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": tool_input,
        }
    }


def main() -> None:
    try:
        event = json.load(sys.stdin)
        handler = {"UserPromptSubmit": on_prompt, "PreToolUse": on_agent_call}.get(event.get("hook_event_name"))
        out = handler(event) if handler else None
    except Exception:  # the hook must never block the session
        out = None
    if out:
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()

# Registration (e.g. in ~/.claude/settings.json), adjust the path:
# {"hooks": {
#   "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python /path/to/kickroll/examples/router/router_hook.py", "timeout": 20}]}],
#   "PreToolUse": [{"matcher": "Agent", "hooks": [{"type": "command", "command": "python /path/to/kickroll/examples/router/router_hook.py"}]}]
# }}
