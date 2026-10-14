from __future__ import annotations

import os
import platform
import shutil

from aki_code.shell import get_execution_shell


def _detect_environment() -> str:
    system = platform.system()
    release = platform.release()
    machine = platform.machine()
    shell = get_execution_shell()
    cwd = os.getcwd()
    path_sep = os.sep

    lines = [
        f"- OS: {system} {release}",
        f"- Architecture: {machine}",
        f"- Shell: {shell}",
        f"- Working directory: {cwd}",
        f"- Path separator: {path_sep}",
    ]

    if system == "Windows":
        lines.append("- Use Windows commands (del, dir, type, move, copy) instead of Unix commands")
        lines.append("- Use backslash or forward slash for paths")
    else:
        lines.append("- Use Unix commands (rm, ls, cat, mv, cp)")

    if shutil.which("git"):
        lines.append("- git is available")
    if shutil.which("python"):
        lines.append("- python is available")
    elif shutil.which("python3"):
        lines.append("- python3 is available")

    return "\n".join(lines)


_BASE_PROMPT = """You are Aki-Code, a helpful coding assistant that runs in the terminal.

You help users with programming tasks by reading files, writing code, and executing commands.

Guidelines:
- Be concise and direct in your responses.
- When making code changes, explain what you're doing briefly.
- Before running destructive commands (rm, git reset --hard, etc.), confirm with the user.
- Prefer editing existing files over creating new ones.
- When you encounter an error, try to diagnose and fix it rather than giving up.
- Use the available tools to interact with the file system and run commands.
- Adapt your commands to the user's operating system and shell.

You have access to tools for:
- Reading files from the file system
- Writing/creating files
- Executing shell commands

Always use tools when you need to interact with the codebase. Don't guess file contents - read them."""


def build_system_prompt() -> str:
    env = _detect_environment()
    return f"{_BASE_PROMPT}\n\n# Environment\n{env}"


def build_subagent_prompt() -> str:
    """System prompt for the nested subagent loop (Lesson 06).

    Deliberately terse: the subagent gets a self-contained task, does the work,
    and returns a concise final answer that becomes the parent's tool result.
    """
    env = _detect_environment()
    return (
        f"You are a coding agent working in {os.getcwd()}.\n\n"
        f"Complete the given task using the available tools, then return a "
        f"concise final answer. Only your final text is shown to the caller — "
        f"tool calls and intermediate steps are not.\n\n"
        f"# Environment\n{env}"
    )


SYSTEM_PROMPT = build_system_prompt()
SUBAGENT_SYSTEM_PROMPT = build_subagent_prompt()
