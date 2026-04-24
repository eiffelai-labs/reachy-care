from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable


class LLMAdapter(ABC):
    # Callbacks — set by ConversationEngine before connect()
    on_audio_delta: Callable[[bytes], Awaitable[None]] | None = None
    on_tool_call: Callable[[str, str, dict], Awaitable[None]] | None = None
    on_speech_started: Callable[[], Awaitable[None]] | None = None
    on_speech_stopped: Callable[[], Awaitable[None]] | None = None
    on_response_done: Callable[[], Awaitable[None]] | None = None

    @abstractmethod
    async def connect(self, system_prompt: str, tools: list[dict], voice: str = "cedar") -> None: ...

    @abstractmethod
    async def send_audio(self, pcm_chunk: bytes) -> None: ...

    @abstractmethod
    async def send_text_event(self, text: str, instructions: str = "") -> None: ...

    @abstractmethod
    async def update_instructions(self, new_instructions: str) -> None: ...

    @abstractmethod
    async def cancel_response(self) -> None: ...

    @abstractmethod
    async def send_tool_result(self, call_id: str, result: dict) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...
