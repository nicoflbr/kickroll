"""Budgets per run (calls, Opus, planner, reviewer, wall clock) + daily budget. Counters live in the state (resume)."""
import threading
import time

from .store import DailyUsage

COUNTER_KEYS = ("calls", "opus", "planner", "reviewer", "weighted", "elapsed_s")


class Accountant:
    """Checks BEFORE each call (usage + expected call) and counts afterwards.

    Planner calls count only towards `planner` (own counter) and the daily budget, not towards
    the workers' calls/opus.
    """

    def __init__(self, cfg, counters: dict, usage: DailyUsage | None = None, lock=None):
        self.cfg = cfg
        self.c = counters
        for k in COUNTER_KEYS:
            self.c.setdefault(k, 0)
        self.c.setdefault("by_model", {})  # tier -> {"calls", "tokens", "seconds"} for the status pane
        self.c.setdefault("jev", {"calls": 0, "errors": 0, "seconds": 0.0})  # Jev requests; no Claude budget
        self.usage = usage
        self.lock = lock or threading.RLock()
        self._tick_start = time.monotonic()

    def limits(self) -> dict:
        return {"calls": self.cfg.max_calls, "opus": self.cfg.max_opus, "planner": self.cfg.max_planner_calls,
                "reviewer": self.cfg.max_reviewer_calls, "minutes": self.cfg.max_minutes}

    def tick(self) -> None:
        """Advance the consumed wall clock (summed across resumes)."""
        with self.lock:
            now = time.monotonic()
            self.c["elapsed_s"] = round(self.c["elapsed_s"] + (now - self._tick_start), 3)
            self._tick_start = now

    def run_exhausted(self) -> str | None:
        """Reason why no new tasks are started any more (or None)."""
        self.tick()
        if self.c["calls"] >= self.cfg.max_calls:
            return "call_budget"
        if self.c["elapsed_s"] >= self.cfg.max_minutes * 60:
            return "time_budget"
        if self.usage and not self.usage.allows(self.cfg.weight("haiku")):
            return "daily_budget"
        return None

    def check(self, role: str, tier: str) -> str | None:
        with self.lock:
            self.tick()
            if role == "planner":
                if self.c["planner"] >= self.cfg.max_planner_calls:
                    return "planner_budget"
            else:
                if self.c["calls"] >= self.cfg.max_calls:
                    return "call_budget"
                if self.c["elapsed_s"] >= self.cfg.max_minutes * 60:
                    return "time_budget"
                if tier == "opus" and self.c["opus"] >= self.cfg.max_opus:
                    return "opus_budget"
                if role == "reviewer" and self.c["reviewer"] >= self.cfg.max_reviewer_calls:
                    return "reviewer_budget"
            if self.usage and not self.usage.allows(self.cfg.weight(tier)):
                return "daily_budget"
            return None

    def _model(self, tier: str) -> dict:
        return self.c["by_model"].setdefault(tier, {"calls": 0, "tokens": 0, "seconds": 0.0})

    def record_result(self, tier: str, res) -> None:
        """Add what a finished call actually used: wall time and all tokens (input incl. cache, plus output)."""
        usage = getattr(res, "usage", None) or {}
        tokens = sum(int(usage.get(k, 0) or 0) for k in
                     ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))
        with self.lock:
            m = self._model(tier)
            m["tokens"] += tokens
            m["seconds"] = round(m["seconds"] + float(getattr(res, "duration_s", 0.0) or 0.0), 2)

    def record_jev(self, ok: bool, seconds: float) -> None:
        """One Jev request (answered or failed). Counts for the status pane only, not against any budget."""
        with self.lock:
            j = self.c["jev"]
            j["calls"] += 1
            j["errors"] += 0 if ok else 1
            j["seconds"] = round(j["seconds"] + max(0.0, seconds), 3)

    def record(self, role: str, tier: str) -> None:
        with self.lock:
            self._model(tier)["calls"] += 1
            if role == "planner":
                self.c["planner"] += 1
            else:
                self.c["calls"] += 1
                if tier == "opus":
                    self.c["opus"] += 1
                if role == "reviewer":
                    self.c["reviewer"] += 1
            self.c["weighted"] = round(self.c["weighted"] + self.cfg.weight(tier), 3)
        if self.usage:
            self.usage.record(self.cfg.weight(tier))
