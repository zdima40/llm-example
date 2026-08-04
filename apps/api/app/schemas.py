import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    telegram_user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    language_code: str | None


class ChatCreate(BaseModel):
    title: str = Field(default="Новый чат", min_length=1, max_length=120)


class ChatUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ChatRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    chat_id: uuid.UUID
    ordinal: int
    role: str
    content: str
    status: str
    request_id: uuid.UUID | None
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None


class ChatPage(BaseModel):
    items: list[ChatRead]
    next_cursor: str | None = None


class MessagePage(BaseModel):
    items: list[MessageRead]
    next_cursor: str | None = None


class ErrorResponse(BaseModel):
    code: str
    message: str

