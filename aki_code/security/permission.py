from __future__ import annotations

import os
import re
from enum import Enum
from pathlib import Path


class PermissionMode(Enum):
    """How aggressively the agent may act without asking.

    DEFAULT: file writes/edits require confirmation.
    ACCEPT_EDITS: file writes/edits inside allowed_dirs are auto-accepted;
                  bash approval rules and the deny-list still apply.
    """

    DEFAULT = "default"
    ACCEPT_EDITS = "accept-edits"


# ── Level 1: Deny List ───────────────────────────────────────
# Commands matching these patterns are ALWAYS rejected, no user prompt.
DENY_LIST = [
    r"rm\s+-rf\s+/",
    r"mkfs\.",
    r"dd\s+if=.+of=/dev/",
    r">\s*/dev/sd",
    r"chmod\s+-R\s+777\s+/",
    r":\(\)\s*\{",
    r"shutdown",
    r"reboot",
    r"halt",
    r"poweroff",
    r"init\s+0",
    r"del\s+/[sq]\s+[a-z]:\\",  # Windows: del /s /q C:\
    r"format\s+[a-z]:",           # Windows: format C:
    r"rd\s+/s\s+/q\s+[a-z]:\\",  # Windows: rd /s /q C:\
]
DENY_PATTERNS = [re.compile(p, re.IGNORECASE) for p in DENY_LIST]

# ── Level 2: Ask Rules ───────────────────────────────────────
# Commands matching these patterns require user approval.
ASK_RULES_RAW = [
    r"sudo\s+",
    r"pip\s+install",
    r"apt(-get)?\s+(install|remove|purge)",
    r"npm\s+(install|uninstall)",
    r"curl\s+.+\|\s*(bash|sh)",
    r"wget\s+.+\|\s*(bash|sh)",
    r"git\s+push",
    r"git\s+reset\s+--hard",
    r"git\s+clean\s+-f",
    r"docker\s+(rm|stop|kill|rmi)",
    r"chmod\s+777",
    r"kill\s+-9",
    r"\brm\s+",
    r"\bmkdir\s+",
    r"\btouch\s+",
    r"\btee\s+",
    r"\btruncate\s+",
    r"\bdel\s+",
    r"\brd\s+/s",
    r"\bmove\s+",
]
ASK_RULES = [re.compile(p, re.IGNORECASE) for p in ASK_RULES_RAW]

# Tools that are read-only / side-effect-free and always auto-allowed.
SAFE_TOOLS_AUTO = {
    "read_file",
    "glob",
    "todo_write",
    "load_skill",
    "compact",
    "create_task",
    "get_task",
    "list_tasks",
    "update_task",
    "claim_task",
    "complete_task",
}


class PermissionManager:
    def __init__(
        self,
        allowed_dirs: list[str] | None = None,
        base_dir: str | Path | None = None,
        mode: PermissionMode = PermissionMode.DEFAULT,
    ):
        if allowed_dirs:
            self.allowed_dirs = [Path(d).expanduser().resolve() for d in allowed_dirs]
        else:
            self.allowed_dirs = [Path(os.getcwd()).resolve()]

        if base_dir is not None:
            self.base_dir = Path(base_dir).expanduser().resolve()
        else:
            self.base_dir = Path(os.getcwd()).resolve()

        self.mode = mode

    # ── Path helpers ──────────────────────────────────────────

    def _is_subpath(self, path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    def _resolve_path(self, path_str: str) -> Path:
        p = Path(path_str).expanduser()
        if not p.is_absolute():
            p = self.base_dir / p
        return p.resolve()

    def _is_path_safe(self, path_str: str) -> bool:
        try:
            p = self._resolve_path(path_str)
        except (OSError, ValueError):
            return False
        return any(self._is_subpath(p, d) for d in self.allowed_dirs)

    # ── Path extraction ───────────────────────────────────────

    def _extract_paths_from_args(self, tool_name: str, args: dict) -> list[str]:
        path_keys = {
            "read_file": ["path"],
            "write_file": ["path"],
            "edit_file": ["path"],
            "bash": [],
            "glob": ["path"],
        }
        keys = path_keys.get(tool_name, [])
        return [args[k] for k in keys if k in args and args[k]]

    def _extract_bash_paths(self, command: str) -> list[str]:
        paths = []
        for match in re.finditer(
            r"(?:cat|less|more|head|tail|vim|nano|vi|open|mkdir|type)\s+(?:-\S+\s+)*(\S+)",
            command,
        ):
            paths.append(match.group(1))

        _SYSTEM_SINKS = {"/dev/null", "/dev/stdout", "/dev/stderr", "nul"}
        for match in re.finditer(r"(?:\d*>>|\d*>)\s*(\S+)", command):
            if match.group(1) not in _SYSTEM_SINKS:
                paths.append(match.group(1))
        return paths

    # ── Main check ────────────────────────────────────────────

    def check(self, tool_name: str, args: dict) -> tuple[str, str]:
        """
        Evaluate whether a tool invocation is allowed.

        Returns:
            (decision, reason) where decision is one of:
              - "allow": safe to execute without asking
              - "deny":  forbidden, do NOT execute
              - "ask":   requires user approval before executing
        """
        # Layer 1: Safe-path check
        paths = self._extract_paths_from_args(tool_name, args)
        if tool_name == "bash":
            paths += self._extract_bash_paths(args.get("command", ""))

        for p in paths:
            if not self._is_path_safe(p):
                return "ask", f"Path outside allowed directories: {p}"

        # Read-only tools are auto-allowed (after boundary checks).
        if tool_name in SAFE_TOOLS_AUTO:
            return "allow", "Read-only tool, auto-allowed"

        # Layer 2, Level 1: Deny list (bash only).
        if tool_name == "bash":
            command = args.get("command", "")
            for pattern in DENY_PATTERNS:
                if pattern.search(command):
                    return "deny", f"Command matches deny-list: {pattern.pattern}"

            # Layer 2, Level 2: Ask rules.
            for pattern in ASK_RULES:
                if pattern.search(command):
                    return "ask", f"Command matches approval rule: {pattern.pattern}"

        # Layer 2, Level 3: File-write tools.
        if tool_name in ("write_file", "edit_file"):
            if self.mode == PermissionMode.ACCEPT_EDITS:
                return "allow", "Auto-accepted edit (accept-edits mode)"
            return "ask", f"File modification: {args.get('path', 'unknown')}"

        return "allow", "Passed all checks"
