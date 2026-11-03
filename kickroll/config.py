"""Configuration from kickroll.toml (tomllib). Single source for timeouts, budgets, weights and checks."""
import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from . import APP_NAME, HOME_ENV


def home_dir() -> Path:
    """Data directory: $KICKROLL_HOME, else ~/.kickroll (status.json, usage.json, runs/, router_log.jsonl)."""
    env = os.environ.get(HOME_ENV)
    return Path(env) if env else Path.home() / f".{APP_NAME}"


def _wt_root() -> Path:
    """Short worktree root: %LOCALAPPDATA% on Windows, $XDG_CACHE_HOME (or ~/.cache) elsewhere."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / APP_NAME / "wt"


@dataclass
class Config:
    # Per-tier timeouts in seconds (single source, not in the plan)
    timeouts: dict = field(default_factory=lambda: {"haiku": 300, "sonnet": 600, "opus": 900})
    planner_timeout_s: int = 900
    l1_timeout_s: int = 120
    # Laufbudgets
    max_calls: int = 25
    max_opus: int = 3
    max_planner_calls: int = 2
    max_reviewer_calls: int = 6
    max_minutes: float = 60.0
    # Daily budget across all runs
    daily_max_calls: int = 150
    daily_max_weighted: float = 120.0
    weights: dict = field(default_factory=lambda: {"haiku": 0.33, "sonnet": 1.0, "opus": 1.7})
    # Planner
    planner_model: str = "opus"
    # Worker
    max_parallel: int = 2
    max_timeouts_per_task: int = 2
    worker_tools: str = "Read,Grep,Glob,Edit,Write"
    read_tools: str = "Read,Grep,Glob"
    setting_sources: str = "project"
    extra_claude_args: list = field(default_factory=lambda: ["--permission-mode", "acceptEdits"])
    # Routing: auto = jev (key + SDK) -> planner -> heuristic; haiku = opt-in, bypass only
    routing_backend: str = "auto"
    haiku_timeout_s: float = 30.0
    # Jev (optional)
    jev_enabled: bool = True
    jev_send_diffs: bool = False
    jev_model: str = "jev-latest"
    jev_http_timeout_s: float = 5.0
    jev_deadline_s: float = 8.0
    jev_diff_chars: int = 6000
    bypass_min_subtasks: float = 1.5
    # Checks
    test_command: list = field(default_factory=list)
    global_checks: list = field(default_factory=list)
    python: str = "auto"
    max_tasks: int = 8
    # Paths
    runs_dir: str = ""
    worktree_root: str = ""
    usage_path: str = ""
    status_path: str = ""

    def __post_init__(self) -> None:
        home = home_dir()
        self.runs_dir = self.runs_dir or str(home / "runs")
        self.worktree_root = self.worktree_root or str(_wt_root())
        self.usage_path = self.usage_path or str(home / "usage.json")
        self.status_path = self.status_path or str(home / "status.json")

    def checks(self) -> list[list[str]]:
        """All global checks: the test command first, then global_checks (without duplicates)."""
        out: list[list[str]] = []
        for cmd in ([self.test_command] if self.test_command else []) + list(self.global_checks):
            if cmd and list(cmd) not in out:
                out.append(list(cmd))
        return out

    def weight(self, tier: str) -> float:
        return float(self.weights.get(tier, 1.0))


# TOML section -> {toml_key: config_attr}
_SECTIONS = {
    "timeouts": {"haiku": None, "sonnet": None, "opus": None, "planner": "planner_timeout_s", "l1": "l1_timeout_s"},
    "budget": {k: k for k in ("max_calls", "max_opus", "max_planner_calls", "max_reviewer_calls", "max_minutes",
                              "daily_max_calls", "daily_max_weighted")},
    "weights": {"haiku": None, "sonnet": None, "opus": None},
    "planner": {"model": "planner_model"},
    "worker": {k: k for k in ("max_parallel", "max_timeouts_per_task", "worker_tools", "read_tools",
                              "setting_sources", "extra_claude_args")},
    "routing": {"backend": "routing_backend", "haiku_timeout_s": "haiku_timeout_s"},
    "jev": {"enabled": "jev_enabled", "send_diffs": "jev_send_diffs", "model": "jev_model", "http_timeout_s": "jev_http_timeout_s",
            "deadline_s": "jev_deadline_s", "diff_chars": "jev_diff_chars", "bypass_min_subtasks": "bypass_min_subtasks"},
    "checks": {k: k for k in ("test_command", "global_checks", "python", "max_tasks")},
    "paths": {k: k for k in ("runs_dir", "worktree_root", "usage_path", "status_path")},
}


class ConfigError(ValueError):
    pass


# Must be >= 1 (everything else numeric: >= 0); floats also accept TOML integers.
_POSITIVE = {"planner_timeout_s", "l1_timeout_s", "max_calls", "max_minutes", "max_parallel", "max_tasks",
             "haiku_timeout_s", "jev_http_timeout_s", "jev_deadline_s", "jev_diff_chars", "daily_max_calls",
             "daily_max_weighted"}


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _str_list(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _checked(name: str, attr: str, default, value):
    """Value of `name` (section.key) with the type of the default, or ConfigError."""
    def bad(what):
        return ConfigError(f"{name} must be {what}, not {value!r}"[:200])

    if attr == "global_checks":
        if not isinstance(value, list) or not all(_str_list(c) and c for c in value):
            raise bad("a list of argv lists, e.g. [[\"python\", \"-m\", \"pytest\"]]")
        return value
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise bad("true or false")
        return value
    if isinstance(default, (int, float)):
        if isinstance(default, int) and not (isinstance(value, int) and not isinstance(value, bool)):
            raise bad("an integer")
        if not _is_num(value):
            raise bad("a number")
        low = 1 if attr in _POSITIVE else 0
        if value < low:
            raise bad(f">= {low}")
        return float(value) if isinstance(default, float) else value
    if isinstance(default, str):
        if not isinstance(value, str):
            raise bad("a string")
        return value
    if isinstance(default, list):
        if not _str_list(value):
            raise bad("a list of strings, e.g. [\"python\", \"-m\", \"unittest\"]")
        return value
    return value


def load_config(path: Path | None) -> Config:
    """Read kickroll.toml; unknown keys are an error (so typos stand out)."""
    cfg = Config()
    if path is None or not Path(path).is_file():
        return cfg
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    valid = {f.name for f in fields(Config)}
    for section, values in data.items():
        mapping = _SECTIONS.get(section)
        if mapping is None or not isinstance(values, dict):
            raise ConfigError(f"Unknown section [{section}]")
        for key, value in values.items():
            if key not in mapping:
                raise ConfigError(f"Unknown key {section}.{key}")
            attr = mapping[key]
            if attr is None:  # timeouts/weights per tier: positive numbers
                if not _is_num(value) or value <= 0:
                    raise ConfigError(f"{section}.{key} must be a number > 0, not {value!r}"[:200])
                getattr(cfg, section)[key] = value
            elif attr in valid:
                setattr(cfg, attr, _checked(f"{section}.{key}", attr, getattr(cfg, attr), value))
    if cfg.planner_model not in ("sonnet", "opus"):
        raise ConfigError("planner.model must be 'sonnet' or 'opus'")
    if cfg.routing_backend not in ("auto", "jev", "planner", "heuristic", "haiku"):
        raise ConfigError("routing.backend must be auto|jev|planner|heuristic|haiku")
    cfg.__post_init__()
    return cfg
