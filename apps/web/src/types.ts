export interface User {
  telegram_user_id: number;
  username: string | null;
  first_name: string | null;
  last_name: string | null;
  language_code: string | null;
}

export interface Chat {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export type MessageStatus = "completed" | "streaming" | "cancelled" | "failed";

export interface Message {
  id: string;
  chat_id: string;
  ordinal: number;
  role: "user" | "assistant";
  content: string;
  status: MessageStatus;
  request_id: string | null;
  finish_reason: string | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

export interface StreamCallbacks {
  onStart(payload: {
    request_id: string;
    user_message_id: string;
    assistant_message_id: string;
  }): void;
  onReasoning(delta: string): void;
  onContent(delta: string): void;
  onDone(): void;
  onCancelled(): void;
  onError(message: string): void;
}

