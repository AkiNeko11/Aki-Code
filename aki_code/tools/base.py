from __future__ import annotations

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
