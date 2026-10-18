from aki_code.commands.registry import CommandRegistry
from aki_code.commands.workflow import WorkflowsCommand
from aki_code.commands.builtin import (
    CompactCommand,
    ExitCommand,
    HelpCommand,
    HooksCommand,
    McpCommand,
    MemoryCommand,
    ModeCommand,
    SkillCommand,
    TasksCommand,
    TodoCommand,
    UsageCommand,
)

# Future commands register here.


def create_command_registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register(HelpCommand())
    reg.register(UsageCommand())
    reg.register(HooksCommand())
    reg.register(ModeCommand())
    reg.register(TodoCommand())
    reg.register(TasksCommand())
    reg.register(SkillCommand())
    reg.register(McpCommand())
    reg.register(WorkflowsCommand())
    reg.register(MemoryCommand())
    reg.register(CompactCommand())
    reg.register(ExitCommand())
    return reg
