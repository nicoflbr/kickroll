"""Jev as a model router: decides per request which Claude model it gets.

Setup (only a Jev key needed; Claude runs via your subscription through Claude Code):
    pip install typesafe-sdk
    Windows (PowerShell, permanent):  setx TYPESAFE_API_KEY "your-jev-key"   # then open a new terminal
    Claude Code must be installed and logged in with your subscription (`claude`, then /login).

Usage:
    python router.py "Your prompt"           # route and forward to Claude
    python router.py --dry "Your prompt"     # only show the decision, no Claude call

Every decision is written as a JSON line to ~/.kickroll/router_log.jsonl (prompt hash only,
no plain text). The directory can be changed with the KICKROLL_HOME environment variable.
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

# Model tiers. Adjust the names here.
MODELS = {  # aliases for `claude --model`
    "small": "haiku",
    "medium": "sonnet",
    "large": "opus",
}
DEFAULT_TIER = "medium"  # fallback when Jev is unsure or fails

JEV_TIMEOUT_S = 5.0  # per HTTP attempt; the SDK itself retries on 429/5xx
CLAUDE_TIMEOUT_S = 300
DATA_DIR = Path(os.environ.get("KICKROLL_HOME") or Path.home() / ".kickroll")
LOG_PATH = DATA_DIR / "router_log.jsonl"

# One question = one clearly bounded judgement. Combined in code, not in the prompt.
QUESTIONS = {
    "task": Choice(
        instructions="What kind of task does the request pose?",
        criteria={
            "smalltalk": "Greeting, thanks, short follow-up, trivial factual question",
            "simple_transform": "Translating, summarizing, rephrasing, formatting",
            "code": "Writing, explaining, debugging or reviewing code",
            "analysis": "Multi-step analysis, weighing options, planning, architecture",
            "creative": "Longer creative or editorial texts",
        },
    ),
    "complexity": Score(
        instructions="How demanding is the request for an AI model?",
        criteria=[
            "Simple: a direct answer is enough",
            "Medium: some thinking or several parts",
            "Hard: deep reasoning, many dependencies, high error risk",
        ],
    ),
    "needs_deep_reasoning": Noul(
        instructions="The request requires long, careful reasoning or mathematics",
    ),
}


@dataclass
class Decision:
    tier: str
    model: str
    reason: str
    metrics: dict = field(default_factory=dict)


def route(prompt: str, client: TypeSafeClient) -> Decision:
    """One Jev request (~100-500 ms) -> model choice."""
    return route_from_answers(client.system_one(state=prompt, questions=QUESTIONS).answers)


def route_from_answers(answers) -> Decision:
    """Pure routing logic on finished Jev answers (no network, testable)."""
    task = answers["task"]
    complexity = answers["complexity"].score  # 0.0 .. 2.0 (expected value)
    deep = answers["needs_deep_reasoning"].noul  # 0 .. 1 (no confidence for Noul)
    metrics = {
        "task": task.choice,
        "complexity": round(complexity, 3),
        "deep": round(deep, 3),
        "confidence": round(task.confidence, 3),
    }

    # Jev unsure about the task kind -> prefer the medium tier over guessing.
    if task.confidence < 0.3:
        return _decision(DEFAULT_TIER, f"Jev unsure (confidence {task.confidence:.2f})", metrics)

    if deep > 0.7 or complexity > 1.5:
        return _decision("large", f"deep={deep:.2f}, complexity={complexity:.2f}", metrics)

    if task.choice == "smalltalk" or (task.choice == "simple_transform" and complexity < 0.7):
        return _decision("small", f"{task.choice}, complexity={complexity:.2f}", metrics)

    return _decision("medium", f"{task.choice}, complexity={complexity:.2f}", metrics)


def _decision(tier: str, reason: str, metrics: dict | None = None) -> Decision:
    return Decision(tier=tier, model=MODELS[tier], reason=reason, metrics=metrics or {})


def decide(prompt: str) -> tuple[Decision, int]:
    """Ask Jev; fall back to DEFAULT_TIER on any error. Returns (decision, latency in ms)."""
    start = time.perf_counter()
    try:
        # The key comes from TYPESAFE_API_KEY and is never logged or printed.
        with TypeSafeClient(model="jev-latest", timeout=JEV_TIMEOUT_S) as jev:
            decision = route(prompt, jev)
    except Exception as exc:  # key missing, network, timeout, rate limit, ...
        # Only the type, not the message: error texts could contain request details.
        decision = _decision(DEFAULT_TIER, f"Jev error: {type(exc).__name__}")
    return decision, round((time.perf_counter() - start) * 1000)


def log_decision(prompt: str, decision: Decision, latency_ms: int) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
        "prompt_chars": len(prompt),
        "tier": decision.tier,
        "model": decision.model,
        "reason": decision.reason,
        "latency_ms": latency_ms,
        **decision.metrics,
    }
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _version_key(path: str) -> tuple:
    """Version folder as a number tuple (2.1.100 > 2.1.99); without a version -> ()."""
    for part in Path(path).parts:
        if all(p.isdigit() for p in part.split(".")) and "." in part:
            return tuple(int(p) for p in part.split("."))
    return ()


def find_claude() -> str:
    """Find the claude binary: CLAUDE_BIN, PATH, then the Claude desktop installation (always check the fallback)."""
    candidates = [os.environ.get("CLAUDE_BIN"), shutil.which("claude")]
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        found = glob.glob(os.path.join(appdata, "Claude", "claude-code", "*", "*", "claude.exe"))
        candidates += sorted(found, key=_version_key, reverse=True)
    for path in candidates:
        if path and Path(path).is_file():
            return path
    raise RuntimeError(
        "Claude Code (`claude`) not found. Install it or set CLAUDE_BIN to the claude.exe."
    )


def run_claude(prompt: str, model: str) -> str:
    """Claude Code in print mode: uses your subscription, no API key needed."""
    binary = find_claude()
    try:
        result = subprocess.run(
            [binary, "-p", prompt, "--model", model],
            capture_output=True, text=True, encoding="utf-8", timeout=CLAUDE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude did not respond after {CLAUDE_TIMEOUT_S}s.") from None
    output = (result.stdout + result.stderr).strip()
    if "not logged in" in output.lower() or "/login" in output:
        raise RuntimeError("Claude Code is not logged in. Start `claude` once and run /login.")
    if result.returncode != 0:
        raise RuntimeError(f"claude exited with code {result.returncode}: {output[:500]}")
    return result.stdout.strip()


def ask(prompt: str, dry: bool = False) -> str:
    decision, latency_ms = decide(prompt)
    log_decision(prompt, decision, latency_ms)
    print(f"[router] {decision.model} <- {decision.reason} ({latency_ms} ms)", file=sys.stderr)
    return "" if dry else run_claude(prompt, decision.model)


if __name__ == "__main__":
    args = sys.argv[1:]
    dry = "--dry" in args
    text = " ".join(a for a in args if a != "--dry") or "Thanks, that's all!"
    try:
        answer = ask(text, dry=dry)
        if not dry:
            print(answer)
    except RuntimeError as err:
        sys.exit(f"[router] Error: {err}")
