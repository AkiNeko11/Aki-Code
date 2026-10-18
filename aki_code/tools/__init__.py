from __future__ import annotations

from typing import TYPE_CHECKING

from aki_code.tools.registry import ToolRegistry
from aki_code.tools.bash import BashTool
from aki_code.tools.file_read import ReadFileTool
from aki_code.tools.file_write import WriteFileTool
from aki_code.tools.file_edit import EditFileTool
from aki_code.tools.glob import GlobTool
from aki_code.tools.todo_write import TodoWriteTool
from aki_code.tools.load_skill import LoadSkillTool
from aki_code.tools.compact import CompactTool
from aki_code.tools.task_create import CreateTaskTool
from aki_code.tools.task_get import GetTaskTool
from aki_code.tools.task_list import ListTasksTool
from aki_code.tools.task_update import UpdateTaskTool
from aki_code.tools.task_claim import ClaimTaskTool
from aki_code.tools.task_complete import CompleteTaskTool
from aki_code.tools.schedule_cron import ScheduleCronTool
from aki_code.tools.list_crons import ListCronsTool
from aki_code.tools.cancel_cron import CancelCronTool
from aki_code.tools.team import (
    ListTeammatesTool,
    RequestPlanTool,
    RequestShutdownTool,
    ReviewPlanTool,
    SendMessageTool,
    SpawnTeammateTool,
    SubmitPlanTool,
)
from aki_code.tools.create_worktree import CreateWorktreeTool
from aki_code.tools.task import TaskTool
from aki_code.todo.manager import TodoManager
from aki_code.skills.loader import SkillLoader
from aki_code.tasks.store import TaskStore
from aki_code.scheduler.scheduler import CronScheduler
from aki_code.team.manager import TeamManager

if TYPE_CHECKING:
    from aki_code.mcp.manager import MCPManager


def create_registry(
    *,
    client,
    permission_manager,
    hook_manager,
    todo_manager: TodoManager,
    skill_loader: SkillLoader,
    task_store: TaskStore,
    scheduler: CronScheduler,
    team_manager: TeamManager,
    mcp_manager: MCPManager,
    ask_permission,
    console,
    workflow_runtime=None,
) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(BashTool())
    reg.register(ReadFileTool())
    reg.register(WriteFileTool())
    reg.register(EditFileTool())
    reg.register(GlobTool())
    reg.register(TodoWriteTool(todo_manager))
    reg.register(LoadSkillTool(skill_loader))
    reg.register(CompactTool())
    reg.register(CreateTaskTool(task_store))
    reg.register(GetTaskTool(task_store))
    reg.register(ListTasksTool(task_store))
    reg.register(UpdateTaskTool(task_store))
    reg.register(ClaimTaskTool(task_store))
    reg.register(CompleteTaskTool(task_store))
    reg.register(ScheduleCronTool(scheduler))
    reg.register(ListCronsTool(scheduler))
    reg.register(CancelCronTool(scheduler))
    reg.register(SpawnTeammateTool(team_manager))
    reg.register(ListTeammatesTool(team_manager))
    reg.register(SendMessageTool(team_manager))
    reg.register(RequestPlanTool(team_manager))
    reg.register(ReviewPlanTool(team_manager))
    reg.register(RequestShutdownTool(team_manager))
    reg.register(CreateWorktreeTool(task_store))
    # Imported lazily: mcp.tools subclasses tools.base.BaseTool, and a top-level
    # import here would run tools/__init__ -> mcp.tools -> tools.base -> tools/
    # __init__ in a partially-initialized cycle (same reason team.teammate defers
    # its registry import).
    from aki_code.mcp.tools import ConnectMcpTool

    reg.register(ConnectMcpTool(registry=reg, manager=mcp_manager))
    if workflow_runtime is not None:
        from aki_code.tools.workflow import WorkflowTool

        reg.register(WorkflowTool(workflow_runtime, workflow_runtime.registry))
    reg.register(TaskTool(
        client=client,
        registry=reg,
        permission_manager=permission_manager,
        hook_manager=hook_manager,
        ask_permission=ask_permission,
        console=console,
    ))
    return reg


def create_teammate_registry(*, cwd_provider, task_store, skill_loader) -> ToolRegistry:
    """A teammate's own tool set (Lesson 13).

    Workspace tools are bound to the teammate's current task directory via
    cwd_provider, so parallel teammates can edit separate worktrees. Only the
    tools a teammate may use are registered: file/shell (cwd-bound), skill
    loading, reading and claiming/completing tasks, and submit_plan (intercepted
    by the runtime). Lead-only tools — graph editing, team control, worktree
    creation, cron, subagent recursion, compaction — are simply absent.
    """
    reg = ToolRegistry()
    reg.register(BashTool(cwd_provider))
    reg.register(ReadFileTool(cwd_provider))
    reg.register(WriteFileTool(cwd_provider))
    reg.register(EditFileTool(cwd_provider))
    reg.register(GlobTool(cwd_provider))
    reg.register(LoadSkillTool(skill_loader))
    reg.register(ListTasksTool(task_store))
    reg.register(GetTaskTool(task_store))
    reg.register(ClaimTaskTool(task_store))
    reg.register(CompleteTaskTool(task_store))
    reg.register(SubmitPlanTool())
    return reg
