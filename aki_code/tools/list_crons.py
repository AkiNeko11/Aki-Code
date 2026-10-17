from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.scheduler.scheduler import CronScheduler


class ListCronsTool(BaseTool):
    """List every scheduled job (Lesson 12)."""

    name = "list_crons"
    description = (
        "List all scheduled cron jobs: id, expression, prompt, and whether each "
        "is recurring / durable / pending delivery."
    )
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def __init__(self, scheduler: CronScheduler):
        self._scheduler = scheduler

    def execute(self, **_) -> str:
        jobs = self._scheduler.list()
        if not jobs:
            return "(no scheduled jobs)"
        return "\n".join(job.one_line() for job in jobs)
