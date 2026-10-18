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

    def fire(self, event: HookEvent, **data) -> HookContext:
        ctx = HookContext(event=event, data=data)
        for callback in self._hooks[event]:
            result = callback(ctx)
            if result is not None:
                ctx = result
        return ctx
