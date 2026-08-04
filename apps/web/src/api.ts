import { authorizationHeader } from "./telegram";
import type { Chat, Message, Page, StreamCallbacks, User } from "./types";

const API_ROOT = "/api/v1";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public code = "api_error"
  ) {
    super(message);
  }
}

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { code?: string; message?: string };
    return new ApiError(body.message ?? `HTTP ${response.status}`, response.status, body.code);
  } catch {
    return new ApiError(`HTTP ${response.status}`, response.status);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Authorization", authorizationHeader());
  if (init.body) headers.set("Content-Type", "application/json");
  const response = await fetch(`${API_ROOT}${path}`, { ...init, headers });
  if (!response.ok) throw await parseError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  me: () => request<User>("/me"),
  chats: () => request<Page<Chat>>("/chats"),
  createChat: (title = "Новый чат") =>
    request<Chat>("/chats", { method: "POST", body: JSON.stringify({ title }) }),
  renameChat: (chatId: string, title: string) =>
    request<Chat>(`/chats/${chatId}`, {
      method: "PATCH",
      body: JSON.stringify({ title })
    }),
  deleteChat: (chatId: string) => request<void>(`/chats/${chatId}`, { method: "DELETE" }),
  messages: (chatId: string) => request<Page<Message>>(`/chats/${chatId}/messages`),
  cancelGeneration: (requestId: string) =>
    request<{ status: string }>(`/generations/${requestId}/cancel`, { method: "POST" })
};

function dispatchEvent(block: string, callbacks: StreamCallbacks): void {
  if (!block.trim() || block.startsWith(":")) return;
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trim());
  }
  if (!data.length) return;
  const payload = JSON.parse(data.join("\n")) as Record<string, unknown>;
  if (event === "message_start") {
    callbacks.onStart(payload as unknown as Parameters<StreamCallbacks["onStart"]>[0]);
  } else if (event === "reasoning_delta") {
    callbacks.onReasoning(String(payload.delta ?? ""));
  } else if (event === "content_delta") {
    callbacks.onContent(String(payload.delta ?? ""));
  } else if (event === "done") {
    callbacks.onDone();
  } else if (event === "cancelled") {
    callbacks.onCancelled();
  } else if (event === "error") {
    callbacks.onError(String(payload.message ?? "Ошибка генерации"));
  }
}

export async function streamMessage(
  chatId: string,
  content: string,
  callbacks: StreamCallbacks,
  signal: AbortSignal
): Promise<void> {
  const response = await fetch(`${API_ROOT}/chats/${chatId}/messages`, {
    method: "POST",
    headers: {
      Authorization: authorizationHeader(),
      "Content-Type": "application/json",
      "Idempotency-Key": crypto.randomUUID()
    },
    body: JSON.stringify({ content }),
    signal
  });
  if (!response.ok) throw await parseError(response);
  if (!response.body) throw new ApiError("Браузер не поддерживает streaming", 500);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replaceAll("\r\n", "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      dispatchEvent(buffer.slice(0, boundary), callbacks);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  if (buffer.trim()) dispatchEvent(buffer, callbacks);
}

