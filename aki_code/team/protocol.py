from __future__ import annotations

import secrets
from dataclasses import dataclass, field

# Protocol request types. Free-form collaboration uses plain bus messages; the
# control flows (shutdown, plan approval) use these typed requests so a reply is
# matched to its request by id, a mismatched type cannot mutate state, and a
# status guards against applying the same reply twice.
TYPE_SHUTDOWN = "shutdown"
TYPE_PLAN_APPROVAL = "plan_approval"

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


def new_request_id(prefix: str = "req") -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


@dataclass
class ProtocolState:
    """One in-flight control request tracked by the Lead (Lesson 13)."""

    request_id: str
    type: str
    sender: str
    target: str
    status: str = STATUS_PENDING
    payload: str = ""
    work_version: int | None = None
    task_id: str | None = None
    metadata: dict = field(default_factory=dict)
