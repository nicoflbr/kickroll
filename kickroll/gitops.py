"""Git layer: preflight, integration branch, one worktree per run, diff/commit/reset. Everything behind a lock."""
import hashlib
import subprocess
import threading
from pathlib import Path

from . import APP_NAME
from .safety import child_env

ARTIFACT_IGNORE = [
    "__pycache__/", "*.pyc", "*.pyo", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/", "*.egg-info/",
    ".coverage", "htmlcov/", "node_modules/", ".venv/", "venv/", ".tox/", "Thumbs.db", ".DS_Store", "desktop.ini",
]
TRAILER = "Kickroll-Task"


class GitError(RuntimeError):
    pass


class Git:
    """All git calls as argv, without shell, with core.longpaths=true and a shared lock."""

    def __init__(self, repo: Path, lock=None, excludes_file: Path | None = None):
        self.repo = Path(repo)
        self.lock = lock or threading.RLock()
        self.excludes_file = excludes_file

    def run(self, *args: str, cwd: Path | None = None, check: bool = True, timeout: int = 120) -> str:
        argv = ["git", "-c", "core.longpaths=true", "-c", "core.quotepath=off", *args]
        with self.lock:
            proc = subprocess.run(argv, cwd=str(cwd or self.repo), capture_output=True, timeout=timeout,
                                  env=child_env(), stdin=subprocess.DEVNULL)
        out = proc.stdout.decode("utf-8", errors="replace")
        if check and proc.returncode != 0:
            # Only command + code; stderr may contain paths/contents and is not logged.
            raise GitError(f"git {args[0]} failed (code {proc.returncode})")
        return out

    # ---------- Preflight ----------

    def preflight(self) -> list[str]:
        """List of problems: repo? clean working tree? HEAD present?"""
        try:
            top = self.run("rev-parse", "--show-toplevel").strip()
        except (GitError, FileNotFoundError):
            return [f"{self.repo} is not a Git repository (or git is missing)."]
        problems = []
        if Path(top).resolve() != self.repo.resolve():
            problems.append(f"Please specify the repo root directory: {top}")
        if self.run("rev-parse", "--verify", "-q", "HEAD", check=False).strip() == "":
            problems.append("Repository has no commit yet.")
        if self.run("status", "--porcelain", "--untracked-files=all").strip():
            problems.append("Working tree is not clean (commit or stash first).")
        return problems

    def head(self, cwd: Path | None = None) -> str:
        return self.run("rev-parse", "HEAD", cwd=cwd).strip()

    # ---------- Worktree ----------

    def create_worktree(self, branch: str, path: Path, base: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.run("worktree", "add", "-q", "-b", branch, str(path), base)

    def worktree_exists(self, path: Path) -> bool:
        listing = self.run("worktree", "list", "--porcelain", check=False)
        target = str(Path(path).resolve()).replace("\\", "/").lower()
        return any(line[9:].replace("\\", "/").lower() == target
                   for line in listing.splitlines() if line.startswith("worktree "))

    def remove_worktree(self, path: Path, branch: str | None = None) -> None:
        self.run("worktree", "remove", "--force", str(path), check=False)
        self.run("worktree", "prune", check=False)
        if branch:
            self.run("branch", "-D", branch, check=False)

    # ---------- Changes ----------

    def _add_all(self, wt: Path) -> None:
        extra = ["-c", f"core.excludesFile={self.excludes_file}"] if self.excludes_file else []
        self.run(*extra, "add", "-A", cwd=wt)

    def stage_changes(self, wt: Path) -> tuple[list[str], str]:
        """`git add -A` (with artifact ignores) -> (changed paths, binary diff)."""
        with self.lock:
            self._add_all(wt)
            names = self.run("diff", "--cached", "--name-only", "-z", "--no-renames", "HEAD", cwd=wt)
            diff = self.run("diff", "--cached", "--binary", "HEAD", cwd=wt)
        return [n for n in names.split("\0") if n], diff

    def diff_stat(self, wt: Path, rev_from: str, rev_to: str = "HEAD") -> str:
        return self.run("diff", "--stat", rev_from, rev_to, cwd=wt)

    def commit(self, wt: Path, title: str, run_id: str, task_id: str, attempt: int) -> str:
        msg = f"{title}\n\n{TRAILER}: {run_id}/{task_id}:{attempt}\n"
        with self.lock:
            self._add_all(wt)
            self.run("-c", f"user.name={APP_NAME}", "-c", f"user.email={APP_NAME}@localhost",
                     "commit", "-q", "-m", msg, cwd=wt)
            return self.head(wt)

    def reset_to(self, wt: Path, rev: str) -> None:
        """Retry/resume: hard reset to base; remove untracked files, including ignored artifacts."""
        with self.lock:
            self.run("reset", "-q", "--hard", rev, cwd=wt)
            self.run("clean", "-q", "-fdx", cwd=wt)

    def find_task_commits(self, wt: Path, run_id: str, task_id: str) -> list[tuple[str, int]]:
        """Match via git log instead of state: [(commit, attempt)] for the run/task trailer."""
        needle = f"{TRAILER}: {run_id}/{task_id}:"
        out = self.run("log", "--format=%H%x1f%B%x1e", "--fixed-strings", f"--grep={needle}", "HEAD", cwd=wt)
        found = []
        for rec in out.split("\x1e"):
            if "\x1f" not in rec:
                continue
            sha, body = rec.strip().split("\x1f", 1)
            for line in body.splitlines():
                if line.startswith(needle) and line[len(needle):].strip().isdigit():
                    found.append((sha.strip(), int(line[len(needle):].strip())))
        return found


def file_sha256(path: Path) -> str | None:
    """Content hash with normalized line endings (core.autocrlf must not 'change' frozen tests)."""
    try:
        return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    except FileNotFoundError:
        return None


def write_excludes(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(ARTIFACT_IGNORE) + "\n", encoding="utf-8")
    return path
