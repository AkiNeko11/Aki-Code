from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class HookEvent(Enum):
    PRE_LLM_CALL = "pre_llm_call"
    POST_LLM_CALL = "post_llm_call"
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    ON_USER_INPUT = "on_user_input"
    ON_ERROR = "on_error"
    # A gate can block/defer a candidate stop before final cleanup runs.
    BEFORE_STOP = "before_stop"
    MESSAGE_APPENDED = "message_appended"
    TURN_STOPPED = "turn_stopped"
    # Fired once when an agent turn ends (the model returned no tool_use). The
    # lead hangs turn-end cleanup on it — durable memory extraction and
    # consolidation — so the harness loop stays free of feature-specific ending
    # logic.
    STOP = "stop"


@dataclass
class HookContext:
    event: HookEvent
    data: dict[str, Any] = field(default_factory=dict)


HookCallback = Callable[[HookContext], HookContext | None]


class HookManager:
    def __init__(self):
        self._hooks: dict[HookEvent, list[HookCallback]] = {
            event: [] for event in HookEvent
        }

    def register(self, event: HookEvent, callback: HookCallback) -> None:
        self._hooks[event].append(callback)

    def unregister(self, event: HookEvent, callback: HookCallback) -> None:
        self._hooks[event].remove(callback)

    def fork(self) -> "HookManager":
        """A child manager carrying a copy of this one's registered hooks.

        Lets an agent (subagent, teammate) add its own hooks — a permission or
        tool-exclusion PreToolUse hook — without leaking them onto siblings that
        share the parent manager. The copied callbacks are the same objects, so a
        shared TokenTracker still aggregates usage across every fork.
        """
        child = HookManager()
        child._hooks = {event: list(callbacks) for event, callbacks in self._hooks.items()}
        return child

    def fire(self, event: HookEvent, **data) -> HookContext:
        ctx = HookContext(event=event, data=data)
        for callback in self._hooks[event]:
            result = callback(ctx)
            if result is not None:
                ctx = result
        return ctx
