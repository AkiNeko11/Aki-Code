from __future__ import annotations

from aki_code.tools.base import BaseTool


class CompactTool(BaseTool):
    """Model-initiated context compaction signal (Lesson 08).

    The model calls this after finishing a phase, when earlier details no
    longer need to be kept verbatim. execute() has no side effects: it returns a
    marker, and the agent loop performs the actual compaction only after every
    tool_use in the batch has a matching tool_result — so a response that both
    writes a file and requests compaction never loses the write or leaves an
    orphaned tool result.
    """

    name = "compact"
    description = (
        "Summarize earlier conversation to free context space. Call this after "
        "completing a phase of work when the earlier details are no longer "
        "needed verbatim. Takes no arguments."
    )
    input_schema = {
        "type": "object",
        "properties": {},
    }

    def execute(self, **_) -> str:
        return "Compaction requested after this tool batch."
