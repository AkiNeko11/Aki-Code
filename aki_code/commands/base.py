from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

    from aki_code.commands.registry import CommandRegistry
    from aki_code.llm.usage import TokenTracker
    from aki_code.security.permission import PermissionManager
    from aki_code.session import SessionSettings
    from aki_code.todo.manager import TodoManager


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


class BaseCommand(ABC):
    name: str = ""
    aliases: tuple[str, ...] = ()
    description: str = ""

    @abstractmethod
    def execute(self, ctx: CommandContext, args: str) -> CommandResult:
        ...
