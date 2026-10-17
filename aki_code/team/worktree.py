from __future__ import annotations

import re
import subprocess
from pathlib import Path

from aki_code import config
from aki_code.tasks.model import PENDING

# Worktree names become a directory name and a git branch suffix, so restrict
# them to a safe, lowercase slug.
_NAME_OK = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


class WorktreeError(RuntimeError):
    """A worktree operation was refused (bad name/state/binding)."""


class PartialWorktreeError(WorktreeError):
    """git failed mid-create but may have left a branch/checkout behind.

    The task is left unbound and the artifacts are preserved for manual
    recovery rather than being silently cleaned up.
    """

    def __init__(self, message: str, artifacts: list[str]):
        super().__init__(message)
        self.artifacts = artifacts


def validate_worktree_name(name: str) -> str:
    normalized = str(name).strip().lower()
    if not _NAME_OK.match(normalized):
        raise ValueError(
            f"invalid worktree name '{name}' (use lowercase letters, digits, '-' or '_')"
        )
    return normalized


def worktrees_root(base_repo: str | Path, worktrees_dir: str = config.WORKTREES_DIR) -> Path:
    root = Path(worktrees_dir)
    return root if root.is_absolute() else Path(base_repo).resolve() / root


def _git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True
    )


def create_worktree(
    name: str,
    task_id: str,
    *,
    task_store,
    base_repo: str | Path,
    worktrees_dir: str = config.WORKTREES_DIR,
) -> Path:
    """Create a git worktree on branch wt/<name> and bind it to the task.

    Requires the task to be pending, unclaimed and unbound. On a git failure that
    still left a branch or checkout behind, raises PartialWorktreeError and keeps
    the task unbound so nothing is silently half-created.
    """
    name = validate_worktree_name(name)
    task = task_store.load(task_id)  # raises KeyError if unknown
    if task.status != PENDING:
        raise WorktreeError(
            f"task {task_id} is {task.status}; a worktree requires a pending task"
        )
    if task.owner is not None:
        raise WorktreeError(f"task {task_id} is already claimed by {task.owner}")
    if task.worktree:
        raise WorktreeError(f"task {task_id} is already bound to {task.worktree}")

    path = worktrees_root(base_repo, worktrees_dir) / name
    branch = f"wt/{name}"
    if path.exists():
        raise WorktreeError(f"worktree path already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)

    proc = _git(["worktree", "add", str(path), "-b", branch], base_repo)
    if proc.returncode != 0:
        artifacts = _partial_artifacts(base_repo, path, branch)
        raise PartialWorktreeError(
            (proc.stderr or proc.stdout).strip() or "git worktree add failed",
            artifacts,
        )

    task.worktree = str(path)
    task_store.save(task)
    return path


def _partial_artifacts(base_repo: str | Path, path: Path, branch: str) -> list[str]:
    """Describe what a failed create may have left, for manual recovery."""
    artifacts: list[str] = []
    if path.exists():
        artifacts.append(f"directory {path}")
    listed = _git(["branch", "--list", branch], base_repo)
    if listed.returncode == 0 and branch in (listed.stdout or ""):
        artifacts.append(f"branch {branch}")
    registered = _git(["worktree", "list"], base_repo)
    if registered.returncode == 0 and str(path) in (registered.stdout or ""):
        artifacts.append(f"registered worktree {path}")
    return artifacts


def resolve_task_cwd(task, base_repo: str | Path) -> str:
    """The directory a claimed task's tools should run in.

    An unbound task runs in the repository working directory. A bound task must
    resolve to a real worktree; a missing or invalid binding fails loudly rather
    than silently falling back to the repo directory.
    """
    if not task.worktree:
        return str(Path(base_repo).resolve())
    path = Path(task.worktree)
    if not path.is_dir() or not (path / ".git").exists():
        raise WorktreeError(
            f"task {task.id} is bound to a missing or invalid worktree: {path}"
        )
    return str(path)
