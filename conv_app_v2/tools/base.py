"""Base classes for the conv_app_v2 tool system."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolDependencies:
    engine: Any = None   # ConversationEngine
    robot: Any = None    # RobotLayer
    llm: Any = None      # LLMAdapter


class Tool(ABC):
    name: str = ""
    description: str = ""
    parameters_schema: dict = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.parameters_schema is None:
            cls.parameters_schema = {"type": "object", "properties": {}}

    @abstractmethod
    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> dict[str, Any]:
        ...
