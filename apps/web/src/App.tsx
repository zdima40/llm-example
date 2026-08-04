import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { ApiError, api, streamMessage } from "./api";
import { isInsideTelegram } from "./telegram";
import type { Chat, Message, User } from "./types";

const temporaryId = () => `temporary-${Date.now()}-${Math.random()}`;

function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ children: linkChildren, ...props }) => (
          <a {...props} target="_blank" rel="noreferrer noopener">
            {linkChildren}
          </a>
        )
      }}
    >
      {children}
    </ReactMarkdown>
  );
}

function App() {
  const [user, setUser] = useState<User | null>(null);
  const [chats, setChats] = useState<Chat[]>([]);
  const [activeChatId, setActiveChatId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [streaming, setStreaming] = useState(false);
  const [reasoning, setReasoning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const activeRequestId = useRef<string | null>(null);
  const abortController = useRef<AbortController | null>(null);
  const messageEndRef = useRef<HTMLDivElement | null>(null);

  const loadChats = useCallback(async () => {
    const page = await api.chats();
    setChats(page.items);
    setActiveChatId((current) => current ?? page.items[0]?.id ?? null);
    return page.items;
  }, []);

  const loadMessages = useCallback(async (chatId: string) => {
    const page = await api.messages(chatId);
    setMessages(page.items);
  }, []);

  useEffect(() => {
    void (async () => {
      try {
        setUser(await api.me());
        await loadChats();
      } catch (reason) {
        const message =
          reason instanceof ApiError && reason.status === 401
            ? "Откройте приложение из Telegram. Для локального запуска включите DEV_AUTH."
            : reason instanceof Error
              ? reason.message
              : "Не удалось загрузить приложение";
        setError(message);
      } finally {
        setLoading(false);
      }
    })();
  }, [loadChats]);

  useEffect(() => {
    if (!activeChatId) {
      setMessages([]);
      return;
    }
    void loadMessages(activeChatId).catch((reason: Error) => setError(reason.message));
  }, [activeChatId, loadMessages]);

  useEffect(() => {
    messageEndRef.current?.scrollIntoView({ behavior: streaming ? "auto" : "smooth" });
  }, [messages, streaming]);

  const createChat = async () => {
    try {
      const chat = await api.createChat();
      setChats((current) => [chat, ...current]);
      setActiveChatId(chat.id);
      setMessages([]);
      setSidebarOpen(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось создать чат");
    }
  };

  const chooseChat = (chatId: string) => {
    if (streaming) return;
    setActiveChatId(chatId);
    setSidebarOpen(false);
    setError(null);
  };

  const renameChat = async (chat: Chat) => {
    const title = window.prompt("Название чата", chat.title)?.trim();
    if (!title || title === chat.title) return;
    try {
      const updated = await api.renameChat(chat.id, title);
      setChats((current) => current.map((item) => (item.id === chat.id ? updated : item)));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось переименовать чат");
    }
  };

  const deleteChat = async (chat: Chat) => {
    if (!window.confirm(`Удалить чат «${chat.title}»?`)) return;
    try {
      await api.deleteChat(chat.id);
      const remaining = chats.filter((item) => item.id !== chat.id);
      setChats(remaining);
      if (activeChatId === chat.id) setActiveChatId(remaining[0]?.id ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Не удалось удалить чат");
    }
  };

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const content = input.trim();
    if (!content || streaming) return;
    setInput("");
    setError(null);

    let chatId = activeChatId;
    if (!chatId) {
      try {
        const chat = await api.createChat();
        setChats((current) => [chat, ...current]);
        setActiveChatId(chat.id);
        chatId = chat.id;
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Не удалось создать чат");
        setInput(content);
        return;
      }
    }

    const now = new Date().toISOString();
    const temporaryUserId = temporaryId();
    const temporaryAssistantId = temporaryId();
    const optimisticUser: Message = {
      id: temporaryUserId,
      chat_id: chatId,
      ordinal: messages.length + 1,
      role: "user",
      content,
      status: "completed",
      request_id: null,
      finish_reason: null,
      prompt_tokens: null,
      completion_tokens: null,
      error_code: null,
      created_at: now,
      completed_at: now
    };
    const optimisticAssistant: Message = {
      ...optimisticUser,
      id: temporaryAssistantId,
      ordinal: messages.length + 2,
      role: "assistant",
      content: "",
      status: "streaming",
      completed_at: null
    };
    setMessages((current) => [...current, optimisticUser, optimisticAssistant]);
    setStreaming(true);
    setReasoning(true);
    const controller = new AbortController();
    abortController.current = controller;

    try {
      await streamMessage(
        chatId,
        content,
        {
          onStart: (payload) => {
            activeRequestId.current = payload.request_id;
            setMessages((current) =>
              current.map((message) => {
                if (message.id === temporaryUserId) return { ...message, id: payload.user_message_id };
                if (message.id === temporaryAssistantId) {
                  return {
                    ...message,
                    id: payload.assistant_message_id,
                    request_id: payload.request_id
                  };
                }
                return message;
              })
            );
          },
          onReasoning: () => setReasoning(true),
          onContent: (delta) => {
            setReasoning(false);
            setMessages((current) =>
              current.map((message) =>
                message.id === temporaryAssistantId || message.request_id === activeRequestId.current
                  ? { ...message, content: message.content + delta }
                  : message
              )
            );
          },
          onDone: () => setReasoning(false),
          onCancelled: () => setReasoning(false),
          onError: (message) => setError(message)
        },
        controller.signal
      );
    } catch (reason) {
      if (!(reason instanceof DOMException && reason.name === "AbortError")) {
        setError(reason instanceof Error ? reason.message : "Ошибка генерации");
      }
    } finally {
      setStreaming(false);
      setReasoning(false);
      activeRequestId.current = null;
      abortController.current = null;
      await Promise.allSettled([loadMessages(chatId), loadChats()]);
    }
  };

  const stop = async () => {
    const requestId = activeRequestId.current;
    if (requestId) await api.cancelGeneration(requestId).catch(() => undefined);
    abortController.current?.abort();
  };

  const activeChat = chats.find((chat) => chat.id === activeChatId);

  if (loading) {
    return <main className="center-state"><div className="loader" /><p>Загружаем чат…</p></main>;
  }

  if (!user) {
    return (
      <main className="center-state error-page">
        <div className="brand-mark">O</div>
        <h1>Octa Oracle</h1>
        <p>{error ?? "Авторизация недоступна"}</p>
        {!isInsideTelegram() && <small>Локальный адрес: http://localhost:8088</small>}
      </main>
    );
  }

  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebarOpen ? "sidebar-open" : ""}`}>
        <div className="sidebar-header">
          <div className="brand"><span className="brand-mark">O</span><strong>Octa Oracle</strong></div>
          <button className="icon-button mobile-only" onClick={() => setSidebarOpen(false)}>×</button>
        </div>
        <button className="new-chat" onClick={() => void createChat()}>＋ Новый чат</button>
        <nav className="chat-list" aria-label="История чатов">
          {chats.map((chat) => (
            <div className={`chat-row ${chat.id === activeChatId ? "active" : ""}`} key={chat.id}>
              <button className="chat-title" onClick={() => chooseChat(chat.id)}>{chat.title}</button>
              <div className="chat-actions">
                <button title="Переименовать" onClick={() => void renameChat(chat)}>✎</button>
                <button title="Удалить" onClick={() => void deleteChat(chat)}>×</button>
              </div>
            </div>
          ))}
        </nav>
        <div className="user-card">
          <span className="avatar">{(user.first_name ?? user.username ?? "U")[0].toUpperCase()}</span>
          <span>{user.first_name ?? user.username ?? `User ${user.telegram_user_id}`}</span>
        </div>
      </aside>

      {sidebarOpen && <button className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} />}

      <main className="chat-panel">
        <header className="topbar">
          <button className="icon-button mobile-only" onClick={() => setSidebarOpen(true)}>☰</button>
          <div><strong>{activeChat?.title ?? "Новый чат"}</strong><small>DeepSeek R1 8B</small></div>
          <span className={`status-dot ${streaming ? "busy" : ""}`} title={streaming ? "Генерация" : "Готов"} />
        </header>

        <section className="messages" aria-live="polite">
          {!messages.length && (
            <div className="welcome">
              <div className="brand-mark large">O</div>
              <h1>Чем могу помочь?</h1>
              <p>Задайте вопрос — ответ появится по мере генерации.</p>
            </div>
          )}
          {messages.map((message) => (
            <article className={`message ${message.role}`} key={message.id}>
              <div className="message-label">{message.role === "user" ? "Вы" : "Octa Oracle"}</div>
              <div className="message-body">
                {message.content ? <Markdown>{message.content}</Markdown> : (
                  message.status === "streaming" && <span className="thinking">Модель рассуждает<span>…</span></span>
                )}
                {message.status === "cancelled" && <small className="message-status">Ответ остановлен</small>}
                {message.status === "failed" && <small className="message-status error">Ошибка генерации</small>}
              </div>
            </article>
          ))}
          {reasoning && messages.at(-1)?.content && <div className="thinking-inline">Продолжаю ответ…</div>}
          <div ref={messageEndRef} />
        </section>

        {error && <div className="error-banner"><span>{error}</span><button onClick={() => setError(null)}>×</button></div>}

        <footer className="composer-area">
          <form className="composer" onSubmit={(event) => void send(event)}>
            <textarea
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder="Напишите сообщение…"
              rows={1}
              disabled={streaming}
              maxLength={16000}
            />
            {streaming ? (
              <button type="button" className="send-button stop" onClick={() => void stop()} aria-label="Остановить">■</button>
            ) : (
              <button type="submit" className="send-button" disabled={!input.trim()} aria-label="Отправить">↑</button>
            )}
          </form>
          <small>Модель может ошибаться. Проверяйте важную информацию.</small>
        </footer>
      </main>
    </div>
  );
}

export default App;
