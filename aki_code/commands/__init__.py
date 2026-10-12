from aki_code.commands.registry import CommandRegistry
from aki_code.commands.builtin import (
    ExitCommand,
    HelpCommand,
    HooksCommand,
    UsageCommand,
)

# Future commands register here: /compact (Lesson 08), /skill (Lesson 07).


def create_command_registry() -> CommandRegistry:
    reg = CommandRegistry()
    reg.register(HelpCommand())
    reg.register(UsageCommand())
    reg.register(HooksCommand())
    reg.register(ExitCommand())
    return reg
