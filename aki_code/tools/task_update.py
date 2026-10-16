from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.tasks.store import TaskStore


class UpdateTaskTool(BaseTool):
    """Add blockedBy dependencies to a task (Lesson 10).

    The graph is built in two phases: create every node first, then add edges
    using the ids create_task returned — sibling tool calls in one response
    cannot see each other's newly generated ids. The whole change is validated
    before anything is saved.
    """

    name = "update_task"
    description = (
        "Add dependencies to a task: it becomes blocked until every task in "
        "addBlockedBy is completed. Use the ids returned by create_task. The "
        "target must still be pending and unclaimed, dependencies must exist, "
        "and the change must not create a cycle. Re-adding an existing "
        "dependency is a safe no-op."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The task that will be blocked.",
            },
            "addBlockedBy": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ids of tasks that must complete first.",
            },
        },
        "required": ["task_id", "addBlockedBy"],
    }

    def __init__(self, task_store: TaskStore):
        self._store = task_store

    def execute(self, *, task_id: str, addBlockedBy) -> str:
        task = self._store.update_dependencies(task_id, addBlockedBy)
        return f"Updated {task.id}: blockedBy={task.blockedBy or '[]'}"
