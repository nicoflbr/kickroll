"""State on disk: atomic JSON, run lock, JSONL event log (hashes/numbers/IDs only), daily budget."""
import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .proc import pid_alive


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha16(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()[:16]


def atomic_write_json(path: Path, data, retries: int = 8, replace=None) -> None:
    """tmp + flush + fsync + os.replace; retry with backoff against Windows PermissionError (virus scanners, readers)."""
    replace = replace or os.replace
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    delay = 0.02
    for attempt in range(retries):
        try:
            replace(tmp, path)
            return
        except PermissionError:
            if attempt == retries - 1:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.0)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return default


class LockBusy(RuntimeError):
    pass


class FileLock:
    """Exclusive lock file with PID; orphaned locks (dead process) are taken over."""

    def __init__(self, path: Path, wait_s: float = 0.0):
        self.path = Path(path)
        self.wait_s = wait_s

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait_s
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    owner = int(self.path.read_text().strip() or 0)
                except (ValueError, OSError):
                    owner = 0
                if owner and owner != os.getpid() and not pid_alive(owner):
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() >= deadline:
                    raise LockBusy(f"Locked by process {owner}")
                time.sleep(0.05)

    def release(self) -> None:
        self.path.unlink(missing_ok=True)

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()


_SAFE_STR = re.compile(r"^[A-Za-z0-9_.:/\-]{0,64}$")


def _sanitize(value):
    """Only numbers, bools, None and short ID-like strings; everything else becomes a hash."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if _SAFE_STR.match(value) else "sha:" + sha16(value)
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value][:50]
    if isinstance(value, dict):
        return {str(k)[:40]: _sanitize(v) for k, v in list(value.items())[:50]}
    return "sha:" + sha16(repr(value))


class EventLog:
    """runs/<id>/events.jsonl: one line per event with run_id, task_id, attempt, ts."""

    def __init__(self, path: Path, run_id: str):
        self.path = Path(path)
        self.run_id = run_id
        self._lock = threading.Lock()

    def emit(self, event: str, task_id: str | None = None, attempt: int | None = None, **fields) -> None:
        entry = {"ts": now_iso(), "run_id": self.run_id, "task_id": task_id, "attempt": attempt,
                 "event": event, **{k: _sanitize(v) for k, v in fields.items()}}
        line = json.dumps(_sanitize(entry), ensure_ascii=False)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")


class DailyUsage:
    """Daily budget in usage.json: {date: {calls, weighted}} across all runs."""

    def __init__(self, path: Path, max_calls: int, max_weighted: float):
        self.path = Path(path)
        self.max_calls = max_calls
        self.max_weighted = max_weighted
        self._lock = threading.Lock()

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def allows(self, weight: float) -> bool:
        day = (read_json(self.path, {}) or {}).get(self._today(), {})
        return day.get("calls", 0) + 1 <= self.max_calls and day.get("weighted", 0.0) + weight <= self.max_weighted + 1e-9

    def record(self, weight: float) -> None:
        with self._lock, FileLock(self.path.with_suffix(".lock"), wait_s=5.0):
            data = read_json(self.path, {}) or {}
            day = data.setdefault(self._today(), {"calls": 0, "weighted": 0.0})
            day["calls"] += 1
            day["weighted"] = round(day["weighted"] + weight, 3)
            for old in sorted(data)[:-30]:  # keep only 30 days
                data.pop(old, None)
            atomic_write_json(self.path, data)
