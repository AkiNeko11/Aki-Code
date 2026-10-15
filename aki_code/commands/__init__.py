from aki_code.commands.registry import CommandRegistry
from aki_code.commands.builtin import (
    ExitCommand,
    HelpCommand,
    HooksCommand,
    ModeCommand,
    SkillCommand,
    TodoCommand,
    UsageCommand,
)

# Future commands register here: /compact (Lesson 08).


def create_command_registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register(HelpCommand())
    reg.register(UsageCommand())
    reg.register(HooksCommand())
    reg.register(ModeCommand())
    reg.register(TodoCommand())
    reg.register(SkillCommand())
    reg.register(ExitCommand())
    return reg
