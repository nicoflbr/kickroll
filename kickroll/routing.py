"""Routing without Jev: planner annotations, pure heuristics, optional Haiku decider (bypass only, opt-in).

Backend chain (`[routing] backend`): auto = jev (key + SDK present) -> planner -> heuristic.
The escalation ladder is the actual safety net regardless of backend.
Haiku decisions are uncalibrated and lean towards "cheapest is enough" -> never default, never per task.
"""
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

from . import TIERS
from .safety import wrap_data
from .store import atomic_write_json, read_json

BACKENDS = ("auto", "jev", "planner", "heuristic", "haiku")


def jev_available(enabled: bool = True) -> bool:
    """Jev usable? Checks only that the key is present (value never read) and the SDK is installed."""
    return bool(enabled) and "TYPESAFE_API_KEY" in os.environ and importlib.util.find_spec("typesafe_sdk") is not None


def resolve_backend(requested: str, jev_enabled: bool = True) -> tuple[str, str]:
    """(backend for task routing, one-line hint). 'haiku' routes tasks like 'planner' (Haiku is bypass-only)."""
    ok = jev_available(jev_enabled)
    if requested == "auto":
        return ("jev", "") if ok else ("planner", "Routing without Jev: planner annotations, else heuristics.")
    if requested == "jev" and not ok:
        return "planner", "Jev unavailable (key/SDK missing or disabled) -> planner annotations/heuristics."
    if requested == "haiku":
        return "planner", "Bypass via Haiku (opt-in, uncalibrated); tasks via planner annotations."
    return requested, ""


def _words(text: str) -> int:
    return len((text or "").split())


def heuristic_route(task: dict) -> tuple[str, dict, str]:
    """Pure rules: kind, number of writes, description length, risk from the plan."""
    risk = task.get("risk", "medium")
    words, writes = _words(task.get("description", "")), len(task.get("writes", []))
    m = {"risk": risk, "words": words, "writes": writes}
    if risk == "high" or (writes >= 5 and words > 200):
        return "opus", m, "heuristic: high risk or large"
    if risk == "low" and writes <= 1 and words < 60:
        return "haiku", m, "heuristic: small, low risk"
    return "sonnet", m, "heuristic: default"


def planner_route(task: dict) -> tuple[str, dict, str]:
    """Table from CONCEPT v2 §3 on planner fields (risk, complexity 0..2, suggested_tier); missing -> heuristic."""
    complexity, suggested = task.get("complexity"), task.get("suggested_tier")
    if not isinstance(complexity, (int, float)) or suggested not in TIERS:
        return heuristic_route(task)
    risk = task.get("risk", "medium")
    m = {"risk": risk, "complexity": round(float(complexity), 3), "suggested": suggested}
    if complexity > 1.5 or risk == "high":
        return "opus", m, "planner: complexity>1.5 or high risk"
    if risk == "low" and complexity < 0.8 and suggested == "haiku":
        return "haiku", m, "planner: simple"
    return "sonnet", m, "planner: default"


def heuristic_bypass(goal: str) -> dict:
    """Rough est_subtasks from goal length and enumerations."""
    words = _words(goal)
    bullets = len(re.findall(r"^\s*(?:[-*]|\d+[.)])\s+", goal or "", re.M))
    est = 0.5 if words < 15 else 1.0
    if words >= 40 or bullets >= 2:
        est = 2.0
    if words >= 150 or bullets >= 4:
        est = 3.0
    return {"est_subtasks": est, "words": words, "bullets": bullets, "risk": "medium"}


class HaikuDecider:
    """Opt-in bypass via `claude -p --model haiku --json-schema`; result cached by goal hash (hash only)."""

    SCHEMA = {"type": "object", "required": ["est_subtasks", "risk", "complexity"],
              "properties": {"est_subtasks": {"type": "number", "minimum": 0, "maximum": 3},
                             "risk": {"enum": ["low", "medium", "high"]},
                             "complexity": {"type": "number", "minimum": 0, "maximum": 2}}}

    def __init__(self, runner, cache_path: Path, cwd: str, timeout_s: float = 30.0, on_call=None):
        self.runner = runner
        self.cache_path = Path(cache_path)
        self.cwd = cwd
        self.timeout_s = timeout_s
        self.on_call = on_call

    def decide(self, goal: str) -> dict | None:
        """Metrics dict or None (caller falls back to heuristics)."""
        from .runner import ClaudeCall
        key = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:32]
        cache = read_json(self.cache_path, {}) or {}
        if key in cache:
            return cache[key]
        prompt = ("Estimate for the following request: est_subtasks (0 = a single subtask .. 3 = five or more), "
                  "risk (low|medium|high), complexity (0..2). Reply with JSON only.\n" + wrap_data("REQUEST", goal))
        if self.on_call:
            self.on_call()
        res = self.runner.run(ClaudeCall(role="router", key="router:bypass", prompt=prompt, model="haiku",
                                         cwd=self.cwd, timeout_s=self.timeout_s, tools="",
                                         json_schema=json.dumps(self.SCHEMA)))
        data = res.structured
        if res.error or not isinstance(data, dict):
            return None
        try:
            m = {"est_subtasks": float(data["est_subtasks"]), "risk": str(data["risk"]),
                 "complexity": float(data["complexity"])}
        except (KeyError, TypeError, ValueError):
            return None
        if m["risk"] not in ("low", "medium", "high"):
            return None
        cache[key] = m
        atomic_write_json(self.cache_path, dict(list(cache.items())[-500:]))
        return m
