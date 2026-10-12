from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionSettings:
    """Per-session mutable state shared between hooks and commands."""

    show_hooks: bool = True
