import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(slots=True)
class GenerationHandle:
    request_id: str
    chat_id: str
    user_id: int
    cancel_event: asyncio.Event
    task: asyncio.Task[None] | None = None


class GenerationRegistry:
    def __init__(self) -> None:
        self._by_request: dict[str, GenerationHandle] = {}
        self._by_chat: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, request_id: str, chat_id: str, user_id: int) -> GenerationHandle:
        async with self._lock:
            if chat_id in self._by_chat:
                raise RuntimeError("generation_already_active")
            handle = GenerationHandle(
                request_id=request_id,
                chat_id=chat_id,
                user_id=user_id,
                cancel_event=asyncio.Event(),
            )
            self._by_request[request_id] = handle
            self._by_chat[chat_id] = request_id
            return handle

    async def release(self, request_id: str) -> None:
        async with self._lock:
            handle = self._by_request.pop(request_id, None)
            if handle:
                self._by_chat.pop(handle.chat_id, None)

    async def get(self, request_id: str) -> GenerationHandle | None:
        async with self._lock:
            return self._by_request.get(request_id)

    async def get_for_chat(self, chat_id: str) -> GenerationHandle | None:
        async with self._lock:
            request_id = self._by_chat.get(chat_id)
            return self._by_request.get(request_id) if request_id else None

    async def count_for_user(self, user_id: int) -> int:
        async with self._lock:
            return sum(1 for handle in self._by_request.values() if handle.user_id == user_id)

    async def cancel_all(self) -> None:
        async with self._lock:
            handles = list(self._by_request.values())
        for handle in handles:
            handle.cancel_event.set()
            if handle.task and not handle.task.done():
                handle.task.cancel()


class SlidingWindowRateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        self.limit = requests_per_minute
        self._requests: dict[int, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def allow(self, user_id: int) -> bool:
        if self.limit <= 0:
            return True
        cutoff = time.monotonic() - 60
        async with self._lock:
            requests = self._requests[user_id]
            while requests and requests[0] <= cutoff:
                requests.popleft()
            if len(requests) >= self.limit:
                return False
            requests.append(time.monotonic())
            return True
