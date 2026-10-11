from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PermissionLevel(Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass
class Rule:
    name: str
    tool: str
    patterns: list[str] = field(default_factory=list)
    level: PermissionLevel = PermissionLevel.CONFIRM

    def matches(self, tool_name: str, tool_input: dict) -> bool:
        if self.tool != "*" and self.tool != tool_name:
            return False
        if not self.patterns:
            return True
        text = " ".join(str(v) for v in tool_input.values()).lower()
        return any(p.lower() in text for p in self.patterns)


DEFAULT_RULES = [
    Rule(
        name="destructive_file_ops",
        tool="bash",
        patterns=[
            "rm -rf", "rm -r ", "sudo rm",
            "> /dev/", "mkfs", "dd if=",
        ],
    ),
    Rule(
        name="destructive_git_ops",
        tool="bash",
        patterns=[
            "git reset --hard", "git push --force",
            "git clean -f", "git checkout --",
        ],
    ),
    Rule(
        name="system_commands",
        tool="bash",
        patterns=[
            "shutdown", "reboot", "kill -9",
            "chmod 777", "chown",
        ],
    ),
]


class PermissionChecker:
    def __init__(self, rules: list[Rule] | None = None):
        self.rules = rules if rules is not None else list(DEFAULT_RULES)

    def check(self, tool_name: str, tool_input: dict) -> tuple[PermissionLevel, Rule | None]:
        for rule in self.rules:
            if rule.matches(tool_name, tool_input):
                return rule.level, rule
        return PermissionLevel.ALLOW, None

    def add_rule(self, rule: Rule) -> None:
        self.rules.append(rule)
