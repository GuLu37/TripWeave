import { FormEvent, useState } from "react";

type Role = "system" | "user" | "assistant";

type Message = {
  role: Role;
  content: string;
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const WELCOME_MESSAGE: Message = {
  role: "assistant",
  content: "你好，我是 TripWeave。告诉我你的出行想法，我会先陪你把需求理清楚。",
};

/** 渲染并管理浏览器内存中的多轮对话。 */
function App() {
  const [messages, setMessages] = useState<Message[]>([WELCOME_MESSAGE]);
  const [draft, setDraft] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState("");

  /** 将用户输入和当前对话历史提交给后端，并展示助手回复。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || isSending) return;

    const nextMessages = [...messages, { role: "user" as const, content }];
    setMessages(nextMessages);
    setDraft("");
    setError("");
    setIsSending(true);

    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/chat/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: nextMessages }),
      });
      const data = await response.json();
      if (!response.ok) {
        throw new Error(data.error?.message || "暂时无法获取回复。");
      }

      setMessages((current) => [...current, data.message]);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "暂时无法获取回复。",
      );
    } finally {
      setIsSending(false);
    }
  }

  return (
    <main className="app-shell">
      <section className="chat-panel" aria-label="TripWeave 对话">
        <header className="topbar">
          <div>
            <p className="eyebrow">TRIPWEAVE</p>
            <h1>旅差智能助手</h1>
          </div>
          <span className="status"><i />在线</span>
        </header>

        <div className="conversation" aria-live="polite">
          {messages.map((message, index) => (
            <article className={`message ${message.role}`} key={`${message.role}-${index}`}>
              <span className="message-label">
                {message.role === "assistant" ? "TripWeave" : "你"}
              </span>
              <p>{message.content}</p>
            </article>
          ))}
          {isSending && (
            <article className="message assistant pending">
              <span className="message-label">TripWeave</span>
              <p>正在思考…</p>
            </article>
          )}
        </div>

        <form className="composer" onSubmit={handleSubmit}>
          {error && <p className="error" role="alert">{error}</p>}
          <textarea
            aria-label="输入消息"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="例如：下个月去上海出差三天，想住得离客户近一些"
            rows={3}
            disabled={isSending}
          />
          <div className="composer-footer">
            <span>对话内容仅保留在当前浏览器页面</span>
            <button type="submit" disabled={!draft.trim() || isSending} aria-label="发送消息">
              <span aria-hidden="true">↑</span>
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}

export default App;
