"""Thread-safe, shared limits for every request made by one workflow run."""
from __future__ import annotations

import threading

from aki_code.workflow.model import (
    RunLimits, WorkflowBudgetExceeded, WorkflowCancelled,
)


TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def response_usage(response) -> dict:
    usage = getattr(response, "usage", None)
    result = {}
    for key in TOKEN_FIELDS:
        value = getattr(usage, key, 0) if usage is not None else 0
        result[key] = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
    return result


class RunBudget:
    """Calls are reserved before starting; tokens are a post-response soft limit.

    Requests already in flight may exceed the token limit. Their actual usage is
    still recorded, and the next request is refused.
    """

    def __init__(self, limits: RunLimits, cancel_event: threading.Event):
        self.limits = limits
        self.cancel_event = cancel_event
        self._lock = threading.Lock()
        self._calls = 0
        self._tokens = dict.fromkeys(TOKEN_FIELDS, 0)

    def before_call(self) -> None:
        with self._lock:
            if self.cancel_event.is_set():
                raise WorkflowCancelled("Workflow was cancelled")
            if self._calls >= self.limits.max_calls:
                raise WorkflowBudgetExceeded("Workflow model call limit reached")
            if sum(self._tokens.values()) >= self.limits.max_tokens:
                raise WorkflowBudgetExceeded("Workflow token limit reached")
            self._calls += 1

    def record_response(self, response) -> None:
        usage = response_usage(response)
        with self._lock:
            for key, value in usage.items():
                self._tokens[key] += value

    def snapshot(self) -> dict:
        with self._lock:
            return {"calls": self._calls, **self._tokens,
                    "total_tokens": sum(self._tokens.values())}
