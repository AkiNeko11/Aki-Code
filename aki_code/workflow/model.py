"""Small, shared contracts for saved workflows (Lesson 16)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable


class WorkflowError(Exception):
    pass


class WorkflowInputError(WorkflowError):
    pass


class WorkflowInputChanged(WorkflowInputError):
    """A live source no longer matches the input captured for this run."""


class WorkflowCancelled(WorkflowError):
    pass


class WorkflowBudgetExceeded(WorkflowError):
    pass


class WorkflowBusy(WorkflowError):
    pass


@dataclass(frozen=True)
class RunLimits:
    max_concurrency: int = 3
    max_calls: int = 60
    max_turns: int = 12
    max_tokens: int = 100_000
    request_timeout: float = 120.0

    def __post_init__(self):
        for key in ("max_concurrency", "max_calls", "max_turns", "max_tokens"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise WorkflowInputError(f"{key} must be a positive integer")
        if (isinstance(self.request_timeout, bool)
                or not isinstance(self.request_timeout, (int, float))
                or not 0 < self.request_timeout <= 3600):
            raise WorkflowInputError("request_timeout must be between 0 and 3600 seconds")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgentRunResult:
    status: str
    text: str = ""
    value: Any = None
    error: str = ""
    usage: dict = field(default_factory=dict)
    input_changed: bool = False


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    description: str
    script: Callable
    version: str = "1"
    phases: tuple[str, ...] = ()
    args_schema: dict | None = None
    read_only: bool = True

    def metadata(self) -> dict:
        return {"name": self.name, "description": self.description,
                "version": self.version, "phases": list(self.phases),
                "read_only": self.read_only}
