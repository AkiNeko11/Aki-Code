from __future__ import annotations

from aki_code.tools.base import BaseTool


class LoadSkillTool(BaseTool):
    """Return a skill's full instructions by name (Lesson 07).

    The catalog (names + descriptions) lives in the system prompt; this tool
    pulls the full SKILL.md into context only when the model decides a skill
    applies, so unrelated skills never consume tokens.
    """

    name = "load_skill"
    description = (
        "Load the full instructions of a skill by its name. Use this when a "
        "skill listed in the 'Skills available' catalog applies to the current "
        "task. Returns the complete SKILL.md content."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The skill name exactly as listed in the catalog.",
            },
        },
        "required": ["name"],
    }

    def __init__(self, loader):
        self._loader = loader

    def execute(self, *, name: str) -> str:
        return self._loader.load(name)
