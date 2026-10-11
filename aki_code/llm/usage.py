from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )


class TokenTracker:
    """Accumulates token usage across LLM calls in a session.

    Registered as a POST_LLM_CALL hook; queried by the /usage command.
    """

    def __init__(self) -> None:
        self._usage = TokenUsage()

    def add_from_response(self, response) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self._usage.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self._usage.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self._usage.cache_read_input_tokens += (
            getattr(usage, "cache_read_input_tokens", 0) or 0
        )
        self._usage.cache_creation_input_tokens += (
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        self._usage.calls += 1

    def on_post_llm_call(self, ctx) -> None:
        response = ctx.data.get("response")
        if response is not None:
            self.add_from_response(response)

    @property
    def usage(self) -> TokenUsage:
        return self._usage
