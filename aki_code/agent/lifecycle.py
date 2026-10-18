"""Generic decisions at the end of a model turn."""
from dataclasses import dataclass


@dataclass(frozen=True)
class StopDecision:
    action: str = "allow"
    reason: str = ""


class TurnLimitReached(Exception):
    """A host-wide turn limit stopped an automatic continuation chain."""
