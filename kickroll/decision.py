"""Decision table (attempt x signal -> action) as a pure function, plus stagnation and the tier ladder.

| Signal             | Action                                                                    | counts? |
|--------------------|---------------------------------------------------------------------------|---------|
| PASS               | ACCEPT                                                                    | yes     |
| RATE_LIMIT / AUTH  | PAUSE (run paused, no escalation)                                         | no      |
| TIMEOUT            | RETRY same tier; BLOCK after max_timeouts                                 | no      |
| ENV                | BLOCK (escalation does not help)                                          | yes     |
| UNCLEAR            | UNCLEAR                                                                   | yes     |
| FAIL               | attempt >= max -> FAIL; next attempt 2: same tier + feedback;             | yes     |
|                    | 3: one tier higher; 4 (risk=high): opus                                   |         |
| STAGNATION         | immediately one tier higher; on opus -> FAIL                              | yes     |
"""
from dataclasses import dataclass

from . import TIERS

PASS, FAIL, STAGNATION, ENV, TIMEOUT, RATE_LIMIT, AUTH, UNCLEAR = (
    "PASS", "FAIL", "STAGNATION", "ENV", "TIMEOUT", "RATE_LIMIT", "AUTH", "UNCLEAR")
ACCEPT, RETRY, FAILED, BLOCK, PAUSE, MARK_UNCLEAR = "ACCEPT", "RETRY", "FAILED", "BLOCK", "PAUSE", "UNCLEAR"


@dataclass(frozen=True)
class Action:
    kind: str
    next_tier: str | None = None
    counts: bool = True  # does the call that just ended count as a solution attempt?


def up(tier: str) -> str:
    return TIERS[min(TIERS.index(tier) + 1, len(TIERS) - 1)]


def is_stagnation(prev_failed, cur_failed, prev_diff_sha: str | None = None, cur_diff_sha: str | None = None) -> bool:
    """Same failed criteria in two attempts (or identical diff) -> stagnation."""
    if prev_failed is None:
        return False
    if prev_diff_sha and cur_diff_sha and prev_diff_sha == cur_diff_sha:
        return True
    return bool(cur_failed) and sorted(prev_failed) == sorted(cur_failed)


def decide(attempt: int, signal: str, tier: str, risk: str, max_attempts: int,
           timeouts: int = 0, max_timeouts: int = 2) -> Action:
    """attempt = number of the attempt that just ended (1-based); timeouts = timeouts so far, including this one."""
    if signal == PASS:
        return Action(ACCEPT, tier)
    if signal in (RATE_LIMIT, AUTH):
        return Action(PAUSE, tier, counts=False)
    if signal == TIMEOUT:
        return Action(BLOCK if timeouts > max_timeouts else RETRY, tier, counts=False)
    if signal == ENV:
        return Action(BLOCK, tier)
    if signal == UNCLEAR:
        return Action(MARK_UNCLEAR, tier)
    if attempt >= max_attempts:
        return Action(FAILED, tier)
    if signal == STAGNATION:
        return Action(FAILED, tier) if tier == "opus" else Action(RETRY, up(tier))
    nxt = attempt + 1
    if nxt == 2:
        return Action(RETRY, tier)
    if nxt == 4 and risk == "high":
        return Action(RETRY, "opus")
    return Action(RETRY, up(tier))
