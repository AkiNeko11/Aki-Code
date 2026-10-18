from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from rich.markup import escape

from aki_code import config
from aki_code.agent import extract_text
from aki_code.llm.client import call_with_retry
from aki_code.llm.prompts import SUMMARY_SYSTEM_PROMPT

# Tool-use ids are API-generated, but sanitize anyway before using them as a
# filename so a crafted id can never escape the tool-outputs directory.
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9_-]")

# Matches the path inside any offload placeholder ("... saved at <path>]"),
# so an already-persisted result is not written to disk a second time.
_SAVED_AT = re.compile(r"saved at (.+?)\]")


def _json_default(obj):
    """Serialize Anthropic SDK content blocks (pydantic) and anything else.

    Aki-Code stores assistant turns as SDK objects and tool results as dicts,
    so json.dumps needs a fallback that produces clean, recoverable output.
    """
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return str(obj)


def is_prompt_too_long(error: BaseException) -> bool:
    """True when an API error means the context exceeded the model's window."""
    text = str(error).lower()
    return "prompt_too_long" in text or "too many tokens" in text


class Compactor:
    """Keep the conversation within the context budget (Lesson 08).

    prepare() runs a cost-ordered pipeline before every model call: offload the
    largest fresh tool results to disk, archive-and-trim by message count, then —
    only as a last resort — ask the model for a facts-only summary. Everything
    trimmed is written to disk first and replaced with a recoverable path, so
    compaction trades verbatim detail for space without losing information.
    """

    def __init__(
        self,
        client,
        *,
        todo_manager=None,
        console=None,
        context_char_limit: int = config.CONTEXT_CHAR_LIMIT,
        large_result_char_limit: int = config.LARGE_RESULT_CHAR_LIMIT,
        tool_result_budget_chars: int = config.TOOL_RESULT_BUDGET_CHARS,
        large_result_preview: int = config.LARGE_RESULT_PREVIEW_CHARS,
        max_messages: int = config.MAX_MESSAGES,
        snip_head: int = config.SNIP_HEAD_MESSAGES,
        keep_recent_messages: int = config.KEEP_RECENT_MESSAGES,
        compact_target_ratio: float = config.COMPACT_TARGET_RATIO,
        keep_recent_results: int = config.KEEP_RECENT_RESULTS,
        micro_compact_min_chars: int = config.MICRO_COMPACT_MIN_CHARS,
        fit_result_preview_chars: int = config.FIT_RESULT_PREVIEW_CHARS,
        max_reactive_retries: int = config.MAX_REACTIVE_RETRIES,
        summary_max_tokens: int = config.SUMMARY_MAX_TOKENS,
        transcripts_dir: str | Path = config.TRANSCRIPTS_DIR,
        tool_outputs_dir: str | Path = config.TOOL_OUTPUTS_DIR,
        on_response=None,
    ):
        self.client = client
        self.on_response = on_response
        self.todo_manager = todo_manager
        self.console = console
        self.context_char_limit = context_char_limit
        self.large_result_char_limit = large_result_char_limit
        self.tool_result_budget_chars = tool_result_budget_chars
        self.large_result_preview = large_result_preview
        self.max_messages = max_messages
        self.snip_head = snip_head
        self.keep_recent_messages = keep_recent_messages
        self.compact_target_ratio = compact_target_ratio
        self.keep_recent_results = keep_recent_results
        self.micro_compact_min_chars = micro_compact_min_chars
        self.fit_result_preview_chars = fit_result_preview_chars
        self.max_reactive_retries = max_reactive_retries
        self.summary_max_tokens = summary_max_tokens
        self.transcripts_dir = Path(transcripts_dir)
        self.tool_outputs_dir = Path(tool_outputs_dir)

    # ── Pipeline ────────────────────────────────────────────────

    def prepare(self, messages: list, active_request: str | None = None) -> list:
        """Run the compaction pipeline; return the (possibly rewritten) list.

        Steps 1-2 run every round and are free; step 3 (micro/fit) runs only
        once the context is over budget and still costs no API call; step 4
        (model summary) is the last resort. Each cheaper step is retried
        against the limit before falling through to the next.
        """
        messages = self.tool_result_budget(messages)
        messages = self.snip_compact(messages)
        if self.estimate_chars(messages) > self.context_char_limit:
            target = int(self.context_char_limit * self.compact_target_ratio)
            messages = self.micro_compact(messages, target)
            if self.estimate_chars(messages) > self.context_char_limit:
                messages = self.fit_tool_results(messages, target)
            if self.estimate_chars(messages) > self.context_char_limit:
                messages = self.compact_history(messages, active_request)
        return messages

    def estimate_chars(self, messages: list) -> int:
        return len(json.dumps(messages, default=_json_default, ensure_ascii=False))

    # ── Step 1: offload the largest results from the newest batch ──

    def tool_result_budget(self, messages: list) -> list:
        if not messages:
            return messages
        last = messages[-1]
        content = last.get("content") if isinstance(last, dict) else None
        if not isinstance(content, list):
            return messages
        blocks = [
            b for b in content
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        if not blocks:
            return messages

        def size(b) -> int:
            return len(str(b.get("content", "")))

        total = sum(size(b) for b in blocks)
        if total <= self.tool_result_budget_chars:
            return messages

        for block in sorted(blocks, key=size, reverse=True):
            if total <= self.tool_result_budget_chars:
                break
            text = str(block.get("content", ""))
            if len(text) <= self.large_result_char_limit:
                continue
            block["content"] = self.persist_large_output(
                block.get("tool_use_id", "unknown"), text
            )
            total = sum(size(b) for b in blocks)
        return messages

    def persist_large_output(self, tool_use_id: str, content: str) -> str:
        path = self.save_output(tool_use_id, content)
        preview = content[: self.large_result_preview]
        return (
            f"[Large tool result saved at {path}]\n"
            f"Preview (first {self.large_result_preview} chars):\n{preview}"
        )

    def save_output(self, tool_use_id: str, content: str) -> str:
        """Write content to the tool-outputs dir; return the path as a string."""
        path = self._tool_output_path(tool_use_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return str(path)

    def persisted_output_path(self, content: str) -> str | None:
        """If content is already an offload placeholder, return its path.

        Lets step 3 reuse the file written by step 1 (or a previous round)
        instead of persisting the same result a second time.
        """
        match = _SAVED_AT.search(content)
        return match.group(1) if match else None

    def _tool_output_path(self, tool_use_id: str) -> Path:
        safe = _UNSAFE_ID.sub("_", str(tool_use_id)) or "unknown"
        return self.tool_outputs_dir / f"{safe}.txt"

    # ── Step 2: archive full history, keep head + tail by count ──

    def snip_compact(self, messages: list) -> list:
        if len(messages) <= self.max_messages:
            return messages
        head_end = self.snip_head
        tail_start = len(messages) - (self.max_messages - head_end - 1)

        # Never split an assistant(tool_use) from the tool_result that follows.
        if head_end > 0 and self.has_tool_use(messages[head_end - 1]):
            while head_end < tail_start and self.is_tool_result(messages[head_end]):
                head_end += 1
        if (
            tail_start > 0
            and self.is_tool_result(messages[tail_start])
            and self.has_tool_use(messages[tail_start - 1])
        ):
            tail_start -= 1

        transcript = self.write_transcript(messages)
        archived = tail_start - head_end
        marker = {
            "role": "user",
            "content": f"[{archived} messages archived at {transcript}]",
        }
        return [*messages[:head_end], marker, *messages[tail_start:]]

    # ── Step 3: shrink consumed results toward the target (no API call) ──

    def micro_compact(self, messages: list, target_chars: int) -> list:
        """Shorten already-consumed tool results, oldest first, until target.

        The newest batch is left verbatim (the model has not read it yet), and
        the most recent KEEP_RECENT_RESULTS consumed results are kept too. Each
        shortened result is written to disk first, so its placeholder carries a
        recoverable path.
        """
        results = self._tool_result_entries(messages)
        unseen = self.unseen_tool_result_positions(messages)
        consumed = [e for e in results if e[:2] not in unseen]
        if self.keep_recent_results > 0:
            consumed = consumed[: -self.keep_recent_results]
        for _, _, block in consumed:
            if self.estimate_chars(messages) <= target_chars:
                break
            content = str(block.get("content", ""))
            if len(content) <= self.micro_compact_min_chars:
                continue
            saved_path = self.persisted_output_path(content)
            if not saved_path:
                saved_path = self.save_output(
                    block.get("tool_use_id", "unknown"), content
                )
            block["content"] = f"[Earlier tool result saved at {saved_path}]"
        return messages

    def fit_tool_results(self, messages: list, target_chars: int) -> list:
        """Shrink the newest (unseen) batch when it alone busts the budget.

        micro_compact deliberately spares the freshest results; if they are
        still too large, offload the biggest ones with a short preview so the
        model sees usable content instead of the whole round collapsing into a
        full-history summary.
        """
        unseen = self.unseen_tool_result_positions(messages)
        entries = [e for e in self._tool_result_entries(messages) if e[:2] in unseen]

        def size(entry) -> int:
            return len(str(entry[2].get("content", "")))

        for _, _, block in sorted(entries, key=size, reverse=True):
            if self.estimate_chars(messages) <= target_chars:
                break
            content = str(block.get("content", ""))
            if len(content) <= self.fit_result_preview_chars:
                continue
            saved_path = self.persisted_output_path(content)
            if not saved_path:
                saved_path = self.save_output(
                    block.get("tool_use_id", "unknown"), content
                )
            preview = content[: self.fit_result_preview_chars]
            block["content"] = (
                f"[Tool result saved at {saved_path}]\n"
                f"Preview (first {self.fit_result_preview_chars} chars):\n{preview}"
            )
        return messages

    def _tool_result_entries(self, messages: list) -> list:
        """(msg_index, block_index, block) for every tool_result, in order."""
        entries = []
        for mi, message in enumerate(messages):
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for bi, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    entries.append((mi, bi, block))
        return entries

    def unseen_tool_result_positions(self, messages: list) -> set:
        """Positions of the trailing tool_result batch the model has not read.

        prepare() runs before each model call, so when the last message is a
        tool_result batch those results are still unseen and should survive
        micro_compact verbatim.
        """
        if not messages:
            return set()
        last = messages[-1]
        content = last.get("content") if isinstance(last, dict) else None
        if not isinstance(content, list):
            return set()
        last_idx = len(messages) - 1
        return {
            (last_idx, bi)
            for bi, block in enumerate(content)
            if isinstance(block, dict) and block.get("type") == "tool_result"
        }

    # ── Step 4: model-generated facts-only summary (last resort) ──

    def compact_history(
        self, messages: list, active_request: str | None = None, focus: str | None = None
    ) -> list:
        transcript = self.write_transcript(messages)
        self._notice(f"[transcript saved: {transcript}]")
        summary = self.summarize_history(messages, focus=focus)
        return [self.summary_message("Compacted", active_request, summary, transcript)]

    def reactive_compact(self, messages: list, active_request: str | None = None) -> list:
        """Emergency path after the API rejects with prompt_too_long."""
        transcript = self.write_transcript(messages)
        self._notice(f"[reactive compact; transcript saved: {transcript}]")
        tail_start = max(0, len(messages) - self.keep_recent_messages)
        if (
            tail_start > 0
            and self.is_tool_result(messages[tail_start])
            and self.has_tool_use(messages[tail_start - 1])
        ):
            tail_start -= 1
        old_history = messages[:tail_start] if tail_start else messages
        summary = self.summarize_history(old_history)
        message = self.summary_message(
            "Reactive compact", active_request, summary, transcript
        )
        tail = messages[tail_start:] if tail_start else []
        return self._assemble(message, tail)

    def summarize_history(self, messages: list, focus: str | None = None) -> str:
        serialized = json.dumps(messages, default=_json_default, ensure_ascii=False)
        extras = []
        if self.todo_manager is not None:
            rendered = self.todo_manager.render()
            if rendered and rendered != "(no todos)":
                extras.append(f"Current task list (todos):\n{rendered}")
        if focus:
            extras.append(f"Extra focus for this summary: {focus}")
        extra_block = ("\n\n" + "\n\n".join(extras)) if extras else ""
        user_content = (
            "Summarize the following conversation as facts only, per your "
            "system instructions." + extra_block + "\n\nConversation (JSON):\n"
            + serialized
        )
        response = call_with_retry(
            lambda: self.client.messages.create(
                model=config.MODEL,
                max_tokens=self.summary_max_tokens,
                system=SUMMARY_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_content}],
                thinking={"type": "disabled"},
            )
        )
        if self.on_response is not None:
            self.on_response(response)
        return extract_text(response.content) or "(empty summary)"

    def summary_message(
        self, kind: str, active_request: str | None, summary: str, transcript: str
    ) -> dict:
        parts = [f"[{kind}]"]
        if active_request:
            parts.append(f"Current user request:\n{active_request}")
        parts.append(f"Conversation summary:\n{summary}")
        parts.append(f"Full transcript saved at: {transcript}")
        return {"role": "user", "content": "\n\n".join(parts)}

    # ── Transcript ──────────────────────────────────────────────

    def write_transcript(self, messages: list) -> str:
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = self.transcripts_dir / f"transcript-{stamp}.json"
        path.write_text(
            json.dumps(messages, default=_json_default, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return str(path)

    # ── Message inspection (tolerates dicts and SDK blocks) ──────

    @staticmethod
    def _block_type(block) -> str | None:
        if isinstance(block, dict):
            return block.get("type")
        return getattr(block, "type", None)

    def has_tool_use(self, message) -> bool:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            return any(self._block_type(b) == "tool_use" for b in content)
        return False

    def is_tool_result(self, message) -> bool:
        if not isinstance(message, dict) or message.get("role") != "user":
            return False
        content = message.get("content")
        if isinstance(content, list):
            return any(self._block_type(b) == "tool_result" for b in content)
        return False

    # ── Internals ───────────────────────────────────────────────

    def _assemble(self, summary_msg: dict, tail: list) -> list:
        """summary_msg + tail, guaranteeing user/assistant alternation.

        The summary is a user message; if the retained tail also starts with a
        user message, bridge them with a minimal assistant turn so the API does
        not reject two consecutive user messages.
        """
        result = [summary_msg, *tail]
        if (
            len(result) >= 2
            and result[0].get("role") == "user"
            and result[1].get("role") == "user"
        ):
            result.insert(
                1,
                {
                    "role": "assistant",
                    "content": "(continuing from compacted context)",
                },
            )
        return result

    def _notice(self, text: str) -> None:
        if self.console is not None:
            self.console.print(f"  [dim]{escape(text)}[/dim]")
