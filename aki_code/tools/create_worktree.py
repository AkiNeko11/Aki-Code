from __future__ import annotations

import os

from aki_code import config
from aki_code.tools.base import BaseTool
from aki_code.tasks.store import TaskStore
from aki_code.team.worktree import (
    PartialWorktreeError,
    WorktreeError,
    create_worktree,
)


class CreateWorktreeTool(BaseTool):
    """Create a task-bound git worktree (Lesson 13, Lead-only).

    Lets a teammate edit an isolated checkout so parallel work does not collide.
    The task must be pending, unclaimed and unbound. This mutates git state, so
    it goes through the permission prompt rather than being auto-allowed.
    """

    name = "create_worktree"
    description = (
        "Create a git worktree on branch wt/<name> and bind it to a pending, "
        "unclaimed task, so a teammate can edit an isolated checkout. Fails if "
        "the task is not pending/unclaimed or is already bound."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Worktree name, e.g. 'auth-refactor'."},
            "task_id": {"type": "string", "description": "The task to bind the worktree to."},
        },
        "required": ["name", "task_id"],
    }

    def __init__(
        self,
        task_store: TaskStore,
        base_repo: str | None = None,
        worktrees_dir: str = config.WORKTREES_DIR,
    ):
        self._store = task_store
        self._base_repo = base_repo or os.getcwd()
        self._worktrees_dir = worktrees_dir

    def execute(self, *, name: str, task_id: str) -> str:
        try:
            path = create_worktree(
                name,
                task_id,
                task_store=self._store,
                base_repo=self._base_repo,
                worktrees_dir=self._worktrees_dir,
            )
        except PartialWorktreeError as exc:
            left = ", ".join(exc.artifacts) or "none"
            return (
                f"Partial operation: {exc}. Task left unbound. Artifacts preserved "
                f"for manual recovery: {left}"
            )
        except (WorktreeError, ValueError, KeyError) as exc:
            return f"Error: {exc}"
        return f"Created worktree {path} bound to {task_id} (branch wt/{name})"
