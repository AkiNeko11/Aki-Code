from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from aki_code import config
from aki_code.agent import extract_text
from aki_code.llm.client import call_with_retry
from aki_code.llm.prompts import MEMORY_SELECT_PROMPT

# Four durable-knowledge categories (Lesson 09). A candidate memory must carry
# one of these; anything else is rejected at write time.
VALID_MEMORY_TYPES = ("user", "feedback", "project", "reference")

_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")
# English words (len >= 2) and individual CJK chars, for keyword recall.
_TOKEN = re.compile(r"[a-z0-9]{2,}|[\u4e00-\u9fff]")
_CODE_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def memory_slug(name: str) -> str:
    """Stable, filesystem-safe filename stem for a memory name."""
    slug = _SLUG_UNSAFE.sub("-", str(name).lower()).strip("-")
    return slug or "memory"


def memory_document(name: str, mem_type: str, description: str, body: str) -> str:
    """Render a memory as a Markdown file with YAML-ish frontmatter."""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"type: {mem_type}\n"
        "---\n\n"
        f"{body.strip()}\n"
    )


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN.findall(str(text).lower()))


def _parse_json_array(text: str):
    """Extract the first JSON array from model output; None if not parseable.

    Tolerates code fences and surrounding prose so a slightly chatty model
    still yields usable indices/records.
    """
    if not text:
        return None
    stripped = _CODE_FENCE.sub("", text.strip())
    start = stripped.find("[")
    end = stripped.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def _coerce_index(value) -> int | None:
    """Best-effort int from a model-returned index (number or numeric string)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


@dataclass(frozen=True)
class Memory:
    name: str
    description: str
    type: str
    body: str
    filename: str


class MemoryStore:
    """Durable, cross-session knowledge (Lesson 09).

    Storage is one Markdown file per memory under memory_dir, with MEMORY.md as
    a rebuilt index. Recall is selective: only memories relevant to the current
    request are loaded into the prompt, so the token cost stays flat as the
    store grows. This mirrors SkillLoader's progressive disclosure — but skills
    are author-written and read-only, while memories are extracted from
    conversation and rewritten over time.

    Recall prefers a lightweight model call that picks relevant catalog indices
    and falls back to keyword overlap whenever that call (or its JSON) fails.
    """

    def __init__(
        self,
        client=None,
        *,
        memory_dir: str | Path = config.MEMORY_DIR,
        index_name: str = config.MEMORY_INDEX_NAME,
        max_recall: int = config.MEMORY_MAX_RECALL,
        recall_char_cap: int = config.MEMORY_RECALL_CHAR_CAP,
        select_max_tokens: int = config.MEMORY_SELECT_MAX_TOKENS,
    ):
        self.client = client
        self.memory_dir = Path(memory_dir)
        self.index_name = index_name
        self.max_recall = max_recall
        self.recall_char_cap = recall_char_cap
        self.select_max_tokens = select_max_tokens
        self.memories: list[Memory] = []

    # ── Storage ─────────────────────────────────────────────────

    def scan(self) -> None:
        """(Re)build the in-memory catalog from *.md files (index excluded).

        Sorted by filename so catalog indices are stable across scans. Only
        real files directly inside memory_dir are read; the slug-based writer
        means names never become paths, so nothing can escape the directory.
        """
        self.memories = []
        if not self.memory_dir.is_dir():
            return
        for path in sorted(self.memory_dir.glob("*.md")):
            if path.name == self.index_name or not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            meta, body = self._parse_frontmatter(content)
            self.memories.append(
                Memory(
                    name=(meta.get("name") or path.stem).strip(),
                    description=(meta.get("description") or "").strip(),
                    type=(meta.get("type") or "project").strip(),
                    body=body,
                    filename=path.name,
                )
            )

    def write_memory_file(
        self, name: str, mem_type: str, description: str, body: str
    ) -> Path:
        """Persist one memory and rebuild the index; return its path."""
        if mem_type not in VALID_MEMORY_TYPES:
            raise ValueError(
                f"invalid memory type '{mem_type}'; expected one of {VALID_MEMORY_TYPES}"
            )
        slug = memory_slug(name)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        path = self.memory_dir / f"{slug}.md"
        path.write_text(
            memory_document(name, mem_type, description, body), encoding="utf-8"
        )
        self.rebuild_memory_index()
        return path

    def rebuild_memory_index(self) -> Path:
        """Regenerate MEMORY.md (one line per memory file) from disk."""
        self.scan()
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        lines = ["# Memory Index", ""]
        for m in self.memories:
            lines.append(f"- [{m.type}] {m.name}: {m.description} ({m.filename})")
        index_path = self.memory_dir / self.index_name
        index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return index_path

    # ── Recall ──────────────────────────────────────────────────

    def catalog_lines(self) -> str:
        """Indexed catalog (index. [type] name: description) for the selector."""
        return "\n".join(
            f"{i}. [{m.type}] {m.name}: {m.description}"
            for i, m in enumerate(self.memories)
        )

    def select_relevant(self, query: str, limit: int | None = None) -> list[int]:
        """Catalog indices of memories relevant to `query`, most relevant first.

        Tries a lightweight model selection; falls back to keyword overlap when
        there is no client, the call fails, or the output is not usable JSON.
        """
        self.scan()
        try:
            selected = self._model_select(query, limit)
        except Exception:
            selected = None
        if selected is None:
            selected = self._keyword_select(query, limit)
        return selected

    def _model_select(self, query: str, limit: int | None = None) -> list[int] | None:
        """Ask the model for relevant indices; None means "fall back to keyword"."""
        limit = self.max_recall if limit is None else limit
        if self.client is None or not self.memories or limit <= 0:
            return None
        user_content = (
            f"Current user request:\n{query}\n\n"
            f"Memory catalog:\n{self.catalog_lines()}"
        )
        response = call_with_retry(
            lambda: self.client.messages.create(
                model=config.MODEL,
                max_tokens=self.select_max_tokens,
                system=MEMORY_SELECT_PROMPT,
                messages=[{"role": "user", "content": user_content}],
            )
        )
        parsed = _parse_json_array(extract_text(response.content))
        if parsed is None:
            return None
        selected: list[int] = []
        seen: set[int] = set()
        for value in parsed:
            index = _coerce_index(value)
            if index is None or index in seen or not (0 <= index < len(self.memories)):
                continue
            seen.add(index)
            selected.append(index)
            if len(selected) >= limit:
                break
        return selected

    def _keyword_select(self, query: str, limit: int | None = None) -> list[int]:
        limit = self.max_recall if limit is None else limit
        tokens = _tokenize(query)
        if not tokens or not self.memories or limit <= 0:
            return []
        scored: list[tuple[int, int]] = []
        for i, m in enumerate(self.memories):
            haystack = _tokenize(f"{m.name} {m.description} {m.type} {m.body}")
            score = len(tokens & haystack)
            if score:
                scored.append((score, i))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        return [i for _, i in scored[:limit]]

    def load_memories(self, indices: list[int]) -> list[Memory]:
        """Read the selected memories, capping the total body length."""
        loaded: list[Memory] = []
        total = 0
        for i in indices:
            if i < 0 or i >= len(self.memories):
                continue
            m = self.memories[i]
            if loaded and total + len(m.body) > self.recall_char_cap:
                break
            loaded.append(m)
            total += len(m.body)
        return loaded

    def render(self, memories: list[Memory]) -> str:
        """Format recalled memories for injection into the system prompt."""
        if not memories:
            return ""
        return "\n\n".join(
            f"## {m.name} ({m.type})\n{m.body.strip()}" for m in memories
        )

    # ── Frontmatter (flat key: value; mirrors SkillLoader, no YAML dep) ──

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            return {}, content.strip()
        end = next(
            (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
        )
        if end is None:
            return {}, content.strip()
        meta: dict[str, str] = {}
        for line in lines[1:end]:
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip().strip('"').strip("'")
        body = "\n".join(lines[end + 1 :]).strip()
        return meta, body
