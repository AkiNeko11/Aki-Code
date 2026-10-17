from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.scheduler.scheduler import CronScheduler


class CancelCronTool(BaseTool):
    """Cancel a scheduled job by id (Lesson 12)."""

    name = "cancel_cron"
    description = "Cancel a scheduled cron job by its id."
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "The job id, e.g. sched_0001."},
        },
        "required": ["id"],
    }

    def __init__(self, scheduler: CronScheduler):
        self._scheduler = scheduler

    def execute(self, *, id: str) -> str:
        if self._scheduler.cancel(id):
            return f"Cancelled {id}"
        return f"No such job: {id}"
