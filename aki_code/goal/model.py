"""Session-scoped goal state; terminal goals remain inspectable."""
from dataclasses import dataclass, field
import time
from uuid import uuid4


def empty_usage():
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


@dataclass
class GoalState:
    condition: str
    goal_id: str = field(default_factory=lambda: uuid4().hex)
    active: bool = True
    automatic: bool = True
    status: str = "active"
    checks: int = 0
    blocks: int = 0
    chain_turns: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    reason: str = ""
    agent_usage: dict = field(default_factory=empty_usage)
    evaluator_usage: dict = field(default_factory=empty_usage)
    task_ids: set[str] = field(default_factory=set)
    history: list = field(default_factory=list)
