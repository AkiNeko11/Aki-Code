from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    content: str


class SkillLoader:
    """Discover skills on disk and serve them by name (Lesson 07).

    Progressive disclosure: scan() reads only each SKILL.md's frontmatter
    (name + description) into a catalog that is embedded in the system prompt.
    The full body is returned on demand via load(), as a tool result, so skills
    irrelevant to the current task never consume context.
    """

    def __init__(self, skills_dir: str | Path = "skills"):
        self.skills_dir = Path(skills_dir)
        self.skills: dict[str, Skill] = {}

    def scan(self) -> None:
        """(Re)build the registry from skills_dir/*/SKILL.md."""
        self.skills.clear()
        if not self.skills_dir.is_dir():
            return
        root = self.skills_dir.resolve()
        for manifest in sorted(self.skills_dir.glob("*/SKILL.md")):
            try:
                resolved = manifest.resolve()
            except OSError:
                continue
            # Skip anything that is not a real file inside the skills root
            # (e.g. a symlink pointing outside it).
            if not manifest.is_file() or not resolved.is_relative_to(root):
                continue
            content = manifest.read_text(encoding="utf-8")
            metadata, body = self._parse_frontmatter(content)
            name = (metadata.get("name") or "").strip() or manifest.parent.name
            description = (metadata.get("description") or "").strip()
            if not description:
                description = body.split("\n", 1)[0]
            description = " ".join(description.lstrip("# ").split())
            self.skills[name] = Skill(
                name=name, description=description, content=content
            )

    def catalog(self) -> str:
        """Name + description for every skill; '' when none are loaded."""
        return "\n".join(
            f"- {s.name}: {s.description}" for s in self.skills.values()
        )

    def load(self, name: str) -> str:
        """Full SKILL.md for `name`.

        `name` indexes the registry built by scan(); it is never treated as a
        file path, so a crafted name cannot escape the skills directory.
        """
        skill = self.skills.get(name)
        if skill is not None:
            return skill.content
        available = ", ".join(self.skills) or "none"
        return f"Error: Unknown skill '{name}'. Available: {available}"

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
        """Split an optional '---' frontmatter block from the body.

        Minimal flat 'key: value' parser (values may be quoted). Skills are
        author-controlled, so this avoids pulling in a YAML dependency.
        """
        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            return {}, content
        end = next(
            (i for i in range(1, len(lines)) if lines[i].strip() == "---"), None
        )
        if end is None:
            return {}, content  # unterminated fence: treat everything as body
        metadata: dict[str, str] = {}
        for line in lines[1:end]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, sep, value = line.partition(":")
            if not sep:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            key = key.strip()
            if key:
                metadata[key] = value
        body = "\n".join(lines[end + 1:]).lstrip("\n")
        return metadata, body
