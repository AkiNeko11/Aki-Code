from __future__ import annotations

import os
from abc import ABC, abstractmethod


class BaseTool(ABC):
    name: str = ""
    description: str = ""
    input_schema: dict = {}

    @abstractmethod
    def execute(self, **kwargs) -> str:
        ...

    def setup(self) -> None:
        pass

    def teardown(self) -> None:
        pass

    def to_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class WorkspaceTool(BaseTool):
    """A tool whose relative paths resolve against an optional working directory.

    cwd_provider is a zero-arg callable returning the directory this instance
    should operate in, or None to use the process working directory (the default,
    so the lead's tools are unchanged). Teammates get instances bound to their
    current task's directory (a git worktree, or the repo when unbound), which
    lets parallel teammates edit different checkouts of the same repository.
    """

    def __init__(self, cwd_provider=None):
        self._cwd_provider = cwd_provider

    def _cwd(self):
        return self._cwd_provider() if self._cwd_provider else None

    def _resolve(self, path: str) -> str:
        base = self._cwd()
        if base and not os.path.isabs(path):
            return os.path.join(base, path)
        return path
