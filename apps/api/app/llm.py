import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from .config import Settings


@dataclass(slots=True)
class LLMChunk:
    kind: Literal["reasoning", "content", "done"]
    delta: str = ""
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class LLMError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class LLMAdapter:
    async def stream_chat(
        self, messages: list[dict[str, str]], cancel_event: asyncio.Event
    ) -> AsyncIterator[LLMChunk]:
        raise NotImplementedError

    async def ready(self) -> bool:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class MockLLMAdapter(LLMAdapter):
    def __init__(self, settings: Settings):
        self.delay = settings.mock_chunk_delay_seconds

    async def stream_chat(
        self, messages: list[dict[str, str]], cancel_event: asyncio.Event
    ) -> AsyncIterator[LLMChunk]:
        question = next(
            (message["content"] for message in reversed(messages) if message["role"] == "user"),
            "",
        )
        answer = (
            "Это локальный mock-ответ, который проверяет потоковую выдачу без GPU.\n\n"
            f"Вы написали: **{question}**\n\n"
            "Для ответа настоящей модели запустите Compose с профилем `gpu`."
        )
        parts = answer.split(" ")
        for index, part in enumerate(parts):
            if cancel_event.is_set():
                raise asyncio.CancelledError
            if self.delay:
                await asyncio.sleep(self.delay)
            suffix = " " if index < len(parts) - 1 else ""
            yield LLMChunk(kind="content", delta=part + suffix)

        yield LLMChunk(
            kind="done",
            finish_reason="stop",
            prompt_tokens=max(1, sum(len(item["content"]) for item in messages) // 4),
            completion_tokens=max(1, len(answer) // 4),
        )

    async def ready(self) -> bool:
        return True


class VLLMAdapter(LLMAdapter):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.health_url = settings.vllm_base_url.rstrip("/").removesuffix("/v1") + "/health"
        timeout = httpx.Timeout(
            connect=settings.vllm_connect_timeout_seconds,
            read=settings.vllm_read_timeout_seconds,
            write=30.0,
            pool=10.0,
        )
        self.client = httpx.AsyncClient(
            base_url=settings.vllm_base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {settings.vllm_api_key}"},
        )

    async def stream_chat(
        self, messages: list[dict[str, str]], cancel_event: asyncio.Event
    ) -> AsyncIterator[LLMChunk]:
        payload: dict[str, Any] = {
            "model": self.settings.vllm_model,
            "messages": messages,
            "temperature": self.settings.llm_temperature,
            "max_tokens": self.settings.llm_max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        try:
            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode(errors="replace")[:500]
                    raise LLMError(
                        "vllm_http_error",
                        f"vLLM returned HTTP {response.status_code}: {body}",
                    )

                finish_reason: str | None = None
                usage: dict[str, int] = {}
                async for line in response.aiter_lines():
                    if cancel_event.is_set():
                        raise asyncio.CancelledError
                    if not line.startswith("data:"):
                        continue
                    raw_data = line.removeprefix("data:").strip()
                    if not raw_data or raw_data == "[DONE]":
                        continue
                    try:
                        data = json.loads(raw_data)
                    except json.JSONDecodeError as exc:
                        raise LLMError("vllm_invalid_stream", "vLLM returned invalid JSON") from exc

                    if data.get("usage"):
                        usage = data["usage"]
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0]
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                    content = delta.get("content")
                    if reasoning:
                        yield LLMChunk(kind="reasoning", delta=reasoning)
                    if content:
                        yield LLMChunk(kind="content", delta=content)

                yield LLMChunk(
                    kind="done",
                    finish_reason=finish_reason or "stop",
                    prompt_tokens=usage.get("prompt_tokens"),
                    completion_tokens=usage.get("completion_tokens"),
                )
        except asyncio.CancelledError:
            raise
        except LLMError:
            raise
        except httpx.TimeoutException as exc:
            raise LLMError("vllm_timeout", "vLLM response timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMError("vllm_unavailable", "vLLM is unavailable") from exc

    async def ready(self) -> bool:
        try:
            response = await self.client.get(self.health_url)
            return response.is_success
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        await self.client.aclose()


def create_llm_adapter(settings: Settings) -> LLMAdapter:
    if settings.llm_mode == "vllm":
        return VLLMAdapter(settings)
    return MockLLMAdapter(settings)
