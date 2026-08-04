import asyncio
import contextlib
import json
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .auth import get_current_user
from .config import Settings
from .db import get_session
from .generation import GenerationHandle, GenerationRegistry, SlidingWindowRateLimiter
from .llm import LLMAdapter, LLMError
from .models import Chat, Message, User, utc_now
from .schemas import (
    ChatCreate,
    ChatPage,
    ChatRead,
    ChatUpdate,
    MessageCreate,
    MessagePage,
    MessageRead,
    UserRead,
)

router = APIRouter(prefix="/api/v1")


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "chat_not_found", "message": "Чат не найден"},
    )


async def _owned_chat(session: AsyncSession, chat_id: uuid.UUID, user_id: int) -> Chat:
    chat = await session.scalar(
        select(Chat).where(Chat.id == chat_id, Chat.owner_telegram_user_id == user_id)
    )
    if chat is None:
        raise _not_found()
    return chat


@router.get("/me", response_model=UserRead)
async def read_me(user: User = Depends(get_current_user)) -> User:
    return user


@router.get("/chats", response_model=ChatPage)
async def list_chats(
    cursor: datetime | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatPage:
    statement = select(Chat).where(Chat.owner_telegram_user_id == user.telegram_user_id)
    if cursor is not None:
        statement = statement.where(Chat.updated_at < cursor)
    chats = list(
        await session.scalars(statement.order_by(Chat.updated_at.desc()).limit(limit + 1))
    )
    has_more = len(chats) > limit
    items = chats[:limit]
    return ChatPage(
        items=[ChatRead.model_validate(chat) for chat in items],
        next_cursor=items[-1].updated_at.isoformat() if has_more and items else None,
    )


@router.post("/chats", response_model=ChatRead, status_code=status.HTTP_201_CREATED)
async def create_chat(
    payload: ChatCreate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Chat:
    chat = Chat(owner_telegram_user_id=user.telegram_user_id, title=payload.title.strip())
    session.add(chat)
    await session.commit()
    await session.refresh(chat)
    return chat


@router.patch("/chats/{chat_id}", response_model=ChatRead)
async def update_chat(
    chat_id: uuid.UUID,
    payload: ChatUpdate,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Chat:
    chat = await _owned_chat(session, chat_id, user.telegram_user_id)
    chat.title = payload.title.strip()
    chat.updated_at = utc_now()
    await session.commit()
    await session.refresh(chat)
    return chat


@router.delete("/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    request: Request,
    chat_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _owned_chat(session, chat_id, user.telegram_user_id)
    registry: GenerationRegistry = request.app.state.generation_registry
    if await registry.get_for_chat(str(chat_id)):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "generation_active", "message": "Сначала остановите генерацию"},
        )
    await session.execute(delete(Chat).where(Chat.id == chat_id))
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/chats/{chat_id}/messages", response_model=MessagePage)
async def list_messages(
    chat_id: uuid.UUID,
    cursor: int | None = Query(default=None, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> MessagePage:
    await _owned_chat(session, chat_id, user.telegram_user_id)
    statement = select(Message).where(Message.chat_id == chat_id)
    if cursor is not None:
        statement = statement.where(Message.ordinal > cursor)
    messages = list(
        await session.scalars(statement.order_by(Message.ordinal).limit(limit + 1))
    )
    has_more = len(messages) > limit
    items = messages[:limit]
    return MessagePage(
        items=[MessageRead.model_validate(message) for message in items],
        next_cursor=str(items[-1].ordinal) if has_more and items else None,
    )


def _build_context(settings: Settings, messages: list[Message]) -> list[dict[str, str]]:
    history = [
        {"role": message.role, "content": message.content}
        for message in messages
        if message.status == "completed" and message.role in {"user", "assistant"}
    ]
    while (
        len(history) > 1
        and sum(len(item["content"]) for item in history) > settings.llm_context_char_budget
    ):
        history.pop(0)
    while history and history[0]["role"] == "assistant":
        history.pop(0)
    return [{"role": "system", "content": settings.llm_system_prompt}, *history]


def _sse(event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


async def _set_message_state(
    factory: async_sessionmaker[AsyncSession],
    assistant_message_id: uuid.UUID,
    *,
    content: str,
    status_value: str,
    finish_reason: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    error_code: str | None = None,
) -> None:
    async with factory() as session:
        message = await session.get(Message, assistant_message_id)
        if message is None:
            return
        message.content = content
        message.status = status_value
        message.finish_reason = finish_reason
        message.prompt_tokens = prompt_tokens
        message.completion_tokens = completion_tokens
        message.error_code = error_code
        message.completed_at = utc_now()
        chat = await session.get(Chat, message.chat_id)
        if chat:
            chat.updated_at = utc_now()
        await session.commit()


async def _produce_generation(
    app,
    handle: GenerationHandle,
    assistant_message_id: uuid.UUID,
    context: list[dict[str, str]],
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None],
) -> None:
    settings: Settings = app.state.settings
    adapter: LLMAdapter = app.state.llm_adapter
    factory: async_sessionmaker[AsyncSession] = app.state.session_factory
    content_parts: list[str] = []
    try:
        async for chunk in adapter.stream_chat(context, handle.cancel_event):
            if chunk.kind == "reasoning":
                if settings.show_reasoning:
                    await queue.put(("reasoning_delta", {"delta": chunk.delta}))
            elif chunk.kind == "content":
                content_parts.append(chunk.delta)
                await queue.put(("content_delta", {"delta": chunk.delta}))
            elif chunk.kind == "done":
                content = "".join(content_parts)
                await _set_message_state(
                    factory,
                    assistant_message_id,
                    content=content,
                    status_value="completed",
                    finish_reason=chunk.finish_reason,
                    prompt_tokens=chunk.prompt_tokens,
                    completion_tokens=chunk.completion_tokens,
                )
                await queue.put(
                    (
                        "done",
                        {
                            "finish_reason": chunk.finish_reason,
                            "usage": {
                                "prompt_tokens": chunk.prompt_tokens,
                                "completion_tokens": chunk.completion_tokens,
                            },
                        },
                    )
                )
    except asyncio.CancelledError:
        handle.cancel_event.set()
        await _set_message_state(
            factory,
            assistant_message_id,
            content="".join(content_parts),
            status_value="cancelled",
            finish_reason="cancelled",
        )
        queue.put_nowait(("cancelled", {"message": "Генерация остановлена"}))
        raise
    except LLMError as exc:
        await _set_message_state(
            factory,
            assistant_message_id,
            content="".join(content_parts),
            status_value="failed",
            finish_reason="error",
            error_code=exc.code,
        )
        await queue.put(("error", {"code": exc.code, "message": str(exc)}))
    except Exception:
        await _set_message_state(
            factory,
            assistant_message_id,
            content="".join(content_parts),
            status_value="failed",
            finish_reason="error",
            error_code="generation_failed",
        )
        await queue.put(
            ("error", {"code": "generation_failed", "message": "Не удалось получить ответ"})
        )
    finally:
        queue.put_nowait(None)


async def _generation_stream(
    request: Request,
    handle: GenerationHandle,
    user_message_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    context: list[dict[str, str]],
):
    registry: GenerationRegistry = request.app.state.generation_registry
    settings: Settings = request.app.state.settings
    queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
    yield _sse(
        "message_start",
        {
            "request_id": handle.request_id,
            "user_message_id": str(user_message_id),
            "assistant_message_id": str(assistant_message_id),
        },
    )
    handle.task = asyncio.create_task(
        _produce_generation(request.app, handle, assistant_message_id, context, queue)
    )
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=settings.sse_heartbeat_seconds)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if item is None:
                break
            event, payload = item
            yield _sse(event, payload)
    finally:
        if handle.task and not handle.task.done():
            handle.cancel_event.set()
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handle.task
        await registry.release(handle.request_id)


@router.post("/chats/{chat_id}/messages")
async def create_message(
    request: Request,
    chat_id: uuid.UUID,
    payload: MessageCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    settings: Settings = request.app.state.settings
    registry: GenerationRegistry = request.app.state.generation_registry
    rate_limiter: SlidingWindowRateLimiter = request.app.state.rate_limiter
    chat = await _owned_chat(session, chat_id, user.telegram_user_id)
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="Message cannot be empty")
    if len(content) > settings.max_input_chars:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={"code": "message_too_large", "message": "Сообщение слишком длинное"},
        )

    if not await rate_limiter.allow(user.telegram_user_id):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "rate_limit_exceeded",
                "message": "Слишком много запросов. Попробуйте через минуту",
            },
        )

    key = idempotency_key or str(uuid.uuid4())
    existing = await session.scalar(
        select(Message).where(Message.chat_id == chat_id, Message.idempotency_key == key)
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "duplicate_message", "message": "Сообщение уже принято"},
        )

    active_count = await registry.count_for_user(user.telegram_user_id)
    if active_count >= settings.max_concurrent_generations_per_user:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"code": "generation_limit", "message": "Дождитесь текущего ответа"},
        )

    request_id = uuid.uuid4()
    try:
        handle = await registry.reserve(str(request_id), str(chat_id), user.telegram_user_id)
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "generation_active", "message": "В этом чате уже идёт генерация"},
        ) from exc

    try:
        max_ordinal = await session.scalar(
            select(func.max(Message.ordinal)).where(Message.chat_id == chat_id)
        )
        next_ordinal = (max_ordinal or 0) + 1
        user_message = Message(
            chat_id=chat_id,
            ordinal=next_ordinal,
            role="user",
            content=content,
            status="completed",
            idempotency_key=key,
            completed_at=utc_now(),
        )
        assistant_message = Message(
            chat_id=chat_id,
            ordinal=next_ordinal + 1,
            role="assistant",
            content="",
            status="streaming",
            request_id=request_id,
        )
        session.add_all([user_message, assistant_message])
        if chat.title == "Новый чат":
            chat.title = content[:60] + ("…" if len(content) > 60 else "")
        chat.updated_at = utc_now()
        await session.commit()
        await session.refresh(user_message)
        await session.refresh(assistant_message)

        history = list(
            await session.scalars(
                select(Message).where(Message.chat_id == chat_id).order_by(Message.ordinal)
            )
        )
        context = _build_context(settings, history)
    except Exception:
        await registry.release(str(request_id))
        raise

    return StreamingResponse(
        _generation_stream(
            request,
            handle,
            user_message.id,
            assistant_message.id,
            context,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/generations/{request_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_generation(
    request: Request,
    request_id: uuid.UUID,
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    registry: GenerationRegistry = request.app.state.generation_registry
    handle = await registry.get(str(request_id))
    if handle is None or handle.user_id != user.telegram_user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "generation_not_found", "message": "Генерация не найдена"},
        )
    handle.cancel_event.set()
    if handle.task and not handle.task.done():
        handle.task.cancel()
    return {"status": "cancelling"}
