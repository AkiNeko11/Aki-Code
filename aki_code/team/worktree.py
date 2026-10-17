from __future__ import annotations

import re
import subprocess
from pathlib import Path

from aki_code import config
from aki_code.tasks.model import IN_PROGRESS, PENDING

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


def remove_worktree(
    name: str,
    *,
    task_store,
    base_repo: str | Path,
    assignments=None,
    worktrees_dir: str = config.WORKTREES_DIR,
    discard_changes: bool = False,
) -> str:
    """Remove a task-bound worktree while preserving its wt/<name> branch.

    Host-only and destructive: the model can create a worktree but never remove
    one. Refuses while the bound task is still pending or in_progress, or while a
    teammate holds a lease on the checkout this turn. Without discard_changes, any
    tracked, untracked or ignored file blocks removal so nothing is lost by
    accident. On success the task binding is cleared. Neither path deletes the
    branch, so clean local commits with no upstream survive for later merge.
    """
    name = validate_worktree_name(name)
    path = worktrees_root(base_repo, worktrees_dir) / name
    branch = f"wt/{name}"

    task = _bound_task(task_store, path)
    if task is not None and task.status in (PENDING, IN_PROGRESS):
        raise WorktreeError(
            f"task {task.id} is {task.status}; complete or cancel it before "
            f"removing its worktree"
        )
    if assignments is not None:
        holders = assignments.owners_on(path)
        if holders:
            raise WorktreeError(
                f"worktree '{name}' is still in use by {', '.join(holders)}; "
                f"wait for them to go idle before removing it"
            )
    if not _is_registered_worktree(base_repo, path):
        raise WorktreeError(f"no worktree is registered at {path}")

    if not discard_changes:
        changes = _worktree_changes(path)
        if changes:
            raise WorktreeError(
                f"worktree '{name}' has {len(changes)} uncommitted, untracked or "
                f"ignored file(s); pass discard_changes=True to remove anyway"
            )

    _git_remove_worktree(base_repo, path, force=discard_changes)

    if task is not None:
        task.worktree = None
        task_store.save(task)
    return f"Removed worktree {path} (branch {branch} preserved)"


def _bound_task(task_store, path: Path):
    """The task bound to this worktree path, or None if no binding references it."""
    target = str(Path(path).resolve())
    for task in task_store.list():
        if task.worktree and str(Path(task.worktree).resolve()) == target:
            return task
    return None


def _is_registered_worktree(base_repo: str | Path, path: Path) -> bool:
    """True if git still tracks `path` as a linked worktree of the repo."""
    listed = _git(["worktree", "list", "--porcelain"], base_repo)
    if listed.returncode != 0:
        return False
    target = str(Path(path).resolve())
    for line in (listed.stdout or "").splitlines():
        if line.startswith("worktree "):
            recorded = line[len("worktree "):].strip()
            if recorded and str(Path(recorded).resolve()) == target:
                return True
    return False


def _worktree_changes(path: Path) -> list[str]:
    """Tracked, untracked and ignored entries that removal would destroy.

    A failed status check is treated as dirty so an unverifiable worktree is
    never silently deleted.
    """
    proc = _git(["status", "--porcelain", "--ignored"], path)
    if proc.returncode != 0:
        return ["<git status failed>"]
    return [line for line in (proc.stdout or "").splitlines() if line.strip()]


def _git_remove_worktree(base_repo: str | Path, path: Path, *, force: bool) -> None:
    # git worktree remove never deletes the branch, so wt/<name> always survives.
    args = ["worktree", "remove", str(path)]
    if force:
        args.append("--force")
    proc = _git(args, base_repo)
    if proc.returncode != 0 and force:
        # Ignored files or a stale lock can require a second --force.
        proc = _git([*args, "--force"], base_repo)
    if proc.returncode != 0:
        raise WorktreeError(
            (proc.stderr or proc.stdout).strip() or "git worktree remove failed"
        )
