from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

    from aki_code.commands.registry import CommandRegistry
    from aki_code.compact.compactor import Compactor
    from aki_code.llm.usage import TokenTracker
    from aki_code.mcp.manager import MCPManager
    from aki_code.memory.store import MemoryStore
    from aki_code.security.permission import PermissionManager
    from aki_code.session import SessionSettings
    from aki_code.skills.loader import SkillLoader
    from aki_code.tasks.store import TaskStore
    from aki_code.todo.manager import TodoManager
    from aki_code.workflow.runtime import WorkflowRuntime


class CommandAction(Enum):
    CONTINUE = "continue"
    EXIT = "exit"


@dataclass
class CommandResult:
    action: CommandAction = CommandAction.CONTINUE


@dataclass
class CommandContext:
    console: "Console"
    messages: list[dict]
    token_tracker: "TokenTracker"
    commands: "CommandRegistry"
    settings: "SessionSettings"
    permission_manager: "PermissionManager"
    todo_manager: "TodoManager"
    skill_loader: "SkillLoader"
    compactor: "Compactor"
    memory_store: "MemoryStore"
    task_store: "TaskStore"
    mcp_manager: "MCPManager"
    workflow_runtime: "WorkflowRuntime | None" = None


class BaseCommand(ABC):
    name: str = ""
    aliases: tuple[str, ...] = ()
    description: str = ""

    @abstractmethod
    def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        ...
