from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.tasks.store import TaskStore


class ClaimTaskTool(BaseTool):
    """Claim a task: pending -> in_progress (Lesson 10).

    Refuses a task that is not pending or whose dependencies are not all
    completed, so an agent can never start blocked work.
    """

    name = "claim_task"
    description = (
        "Claim a pending task to start working on it: sets the owner and moves "
        "it to in_progress. Fails if the task is not pending or is still blocked "
        "by incomplete dependencies."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The task to claim."},
            "owner": {
                "type": "string",
                "description": "Who is claiming it (defaults to 'agent').",
            },
        },
        "required": ["task_id"],
    }

    def __init__(self, task_store: TaskStore):
        self._store = task_store

    def execute(self, *, task_id: str, owner: str = "agent") -> str:
        return self._store.claim(task_id, owner)
