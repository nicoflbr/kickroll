"""Planner: one `claude -p --json-schema` call (default Opus), validation, at most one re-plan."""
import hashlib
import json
import re
import secrets
from datetime import datetime

from .plan import normalize_plan, repair_test_sources, tracked_files, validate_plan
from .prompts import planner_prompt, planner_schema_json
from .runner import ClaudeCall


class PlanError(RuntimeError):
    def __init__(self, message: str, errors: list[str] | None = None, error_class: str | None = None):
        super().__init__(message)
        self.errors = errors or []
        self.error_class = error_class


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def planner_tier(cfg, bypass_metrics: dict | None) -> str:
    """Default from config (opus). With planner.model=sonnet: opus only for complexity>1.5 or risk=high."""
    if cfg.planner_model == "opus":
        return "opus"
    m = bypass_metrics or {}
    return "opus" if m.get("complexity", 0) > 1.5 or m.get("risk") == "high" else "sonnet"


def _extract_json(text: str) -> dict | None:
    for raw in [text] + re.findall(r"```(?:json)?\s*(\{.*\})\s*```", text or "", re.S):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except (ValueError, TypeError):
            continue
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except ValueError:
            return None
    return None


def assemble_plan(raw: dict, run_id: str, goal: str, checks: list) -> dict:
    plan = {
        "schema_version": 1,
        "run_id": run_id,
        "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest()[:16],
        "goal_summary": str(raw.get("goal_summary", ""))[:300],
        "global_checks": checks,
        "acceptance_files": raw.get("acceptance_files", []),
        "tasks": raw.get("tasks", []),
    }
    return normalize_plan(plan)


def create_plan(goal: str, run_id: str, cfg, runner, acct, repo: str, tier: str, checks: list,
                on_call=None, on_invalid=None, on_repair=None) -> dict:
    errors: list[str] | None = None
    existing = tracked_files(repo)
    for round_no in (1, 2):
        reason = acct.check("planner", tier)
        if reason:
            raise PlanError(f"Planner budget exhausted ({reason})", errors)
        call = ClaudeCall(role="planner", key=f"planner:{round_no}", model=tier, cwd=str(repo),
                          prompt=planner_prompt(goal, checks, errors, cfg.max_tasks),
                          timeout_s=cfg.planner_timeout_s, tools=cfg.read_tools, json_schema=planner_schema_json())
        acct.record("planner", tier)
        if on_call:
            on_call(round_no)
        res = runner.run(call)
        acct.record_result(tier, res)
        if res.error:
            raise PlanError(f"Planner call failed ({res.error})", error_class=res.error)
        raw = res.structured or _extract_json(res.text)
        if raw is None:
            errors = ["Response was not a JSON object matching the schema"]
            if on_invalid:
                on_invalid(round_no, errors)
            continue
        plan = assemble_plan(raw, run_id, goal, checks)
        notes = repair_test_sources(plan, existing)
        if notes and on_repair:
            on_repair(notes)
        errors = validate_plan(plan, cfg.max_tasks, existing)
        if not errors:
            return plan
        if on_invalid:
            on_invalid(round_no, errors)
    raise PlanError("Plan invalid (also after re-plan)", errors)
