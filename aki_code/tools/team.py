from __future__ import annotations

from aki_code.tools.base import BaseTool
from aki_code.team.manager import TeamManager


class SpawnTeammateTool(BaseTool):
    """Start a persistent teammate on a task (Lesson 13, Lead-only).

    The initial task is claimed before the thread starts; if it cannot be
    claimed the teammate is not spawned. The Lead's system prompt requires it to
    propose a team and get the user's confirmation before calling this.
    """

    name = "spawn_teammate"
    description = (
        "Spawn a persistent teammate to work a task in parallel. Claims the task "
        "first (fails if it is unavailable). Only call this after the user has "
        "confirmed the proposed team. require_plan=true makes the teammate submit "
        "a plan for your approval before it can run shell commands or edit files."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Unique teammate name, e.g. 'auth'."},
            "task_id": {"type": "string", "description": "The task to assign first."},
            "require_plan": {"type": "boolean", "description": "Require plan approval before mutating work."},
        },
        "required": ["name", "task_id"],
    }

    def __init__(self, team: TeamManager):
        self._team = team

    def execute(self, *, name: str, task_id: str, require_plan: bool = False) -> str:
        return self._team.spawn(name, task_id, require_plan=require_plan)


class ListTeammatesTool(BaseTool):
    name = "list_teammates"
    description = (
        "List spawned teammates and their state. Prefer waiting for team events "
        "over polling this."
    )
    input_schema = {"type": "object", "properties": {}}

    def __init__(self, team: TeamManager):
        self._team = team

    def execute(self, **_) -> str:
        return self._team.list_teammates()


class RequestPlanTool(BaseTool):
    name = "request_plan"
    description = "Ask a running teammate to submit its plan for your approval."
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Teammate name."}},
        "required": ["name"],
    }

    def __init__(self, team: TeamManager):
        self._team = team

    def execute(self, *, name: str) -> str:
        return self._team.request_plan(name)


class ApprovePlanTool(BaseTool):
    name = "approve_plan"
    description = (
        "Approve or reject a teammate's plan using the request_id from its plan "
        "approval event. Approval unblocks the teammate's mutating tools."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "request_id": {"type": "string", "description": "The plan request id."},
            "approve": {"type": "boolean", "description": "True to approve, false to reject."},
            "feedback": {"type": "string", "description": "Optional feedback for the teammate."},
        },
        "required": ["request_id", "approve"],
    }

    def __init__(self, team: TeamManager):
        self._team = team

    def execute(self, *, request_id: str, approve: bool, feedback: str = "") -> str:
        return self._team.approve_plan(request_id, approve, feedback=feedback)


class ShutdownTeammateTool(BaseTool):
    name = "shutdown_teammate"
    description = (
        "Ask a teammate to shut down gracefully. It finishes its current step, "
        "acknowledges, and exits."
    )
    input_schema = {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Teammate name."}},
        "required": ["name"],
    }

    def __init__(self, team: TeamManager):
        self._team = team

    def execute(self, *, name: str) -> str:
        return self._team.shutdown_teammate(name)


class SubmitPlanTool(BaseTool):
    """Teammate-side plan submission (Lesson 13).

    Handled inside TeammateRuntime._dispatch (it needs the teammate's own gate
    and work version), so the registered execute only guards against the Lead
    calling it directly.
    """

    name = "submit_plan"
    description = (
        "Submit your plan for the lead's approval. Until it is approved you can "
        "read files but cannot run shell commands or write/edit files."
    )
    input_schema = {
        "type": "object",
        "properties": {"plan": {"type": "string", "description": "The plan text."}},
        "required": ["plan"],
    }

    def execute(self, *, plan: str = "") -> str:
        return "submit_plan is only available to a teammate during a team run."
