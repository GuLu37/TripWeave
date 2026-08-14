import { FormEvent, useEffect, useRef, useState } from "react";

type Role = "user" | "assistant";
type DeliveryState = "pending" | "failed";
type BudgetScope = "per_person" | "total";

type Message = {
  id: number;
  role: Role;
  content: string;
  delivery?: DeliveryState;
};

type TripRequirements = {
  origin: string | null;
  destination: string | null;
  departure_date: string | null;
  return_date: string | null;
  trip_duration: {
    raw_text: string;
    amount: number;
    unit: "hour" | "day" | "week" | "month";
    is_approximate: boolean;
  } | null;
  traveler_count: number | null;
  budget: string | null;
  transport_preferences: string[];
  accommodation_preferences: string[];
  dining_preferences: string[];
  attraction_preferences: string[];
  general_preferences: string[];
  fixed_schedule: string[];
};

type ConversationAnalysis = {
  intent: "chat" | "trip_planning";
  reply: string;
  requirements: TripRequirements | null;
  plan_action: "plan" | "modify" | "confirm" | null;
  pending_plan: TripPlanSnapshot | null;
  missing_fields: string[];
  is_complete: boolean | null;
};

type ReviewResult = {
  status: "ready_for_confirmation" | "needs_replanning" | "needs_user_decision";
  summary: string;
  risks: string[];
  pending_items: string[];
};

type TripPlanSnapshot = {
  requirements: TripRequirements;
  proposal: string;
  review_result: ReviewResult;
};

type ChatApiResponse = {
  analysis: ConversationAnalysis;
};

type ChatApiError = {
  error?: {
    message?: string;
  };
};

type QuickForm = {
  origin: string;
  destination: string;
  departureDate: string;
  returnDate: string;
  tripDuration: string;
  travelerCount: string;
  budget: string;
  budgetScope: BudgetScope;
  transport: string;
  interests: string[];
};

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000";
const CONTEXT_MESSAGE_LIMIT = 8;
const TRANSPORT_OPTIONS = ["高铁", "飞机", "自驾"];
const INTEREST_OPTIONS = ["人文历史", "亲子", "美食", "自然风光"];

function createEmptyQuickForm(): QuickForm {
  return {
    origin: "",
    destination: "",
    departureDate: "",
    returnDate: "",
    tripDuration: "",
    travelerCount: "",
    budget: "",
    budgetScope: "per_person",
    transport: "",
    interests: [],
  };
}

function formatDateForMessage(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  return match ? `${match[1]}年${match[2]}月${match[3]}日` : value;
}

function buildQuickRequirementMessage(form: QuickForm): string {
  const parts: string[] = [];
  if (form.origin) parts.push(`从${form.origin}出发`);
  if (form.destination) parts.push(`前往${form.destination}`);
  if (form.departureDate && form.returnDate) {
    parts.push(
      `出行时间为${formatDateForMessage(form.departureDate)}至${formatDateForMessage(form.returnDate)}`,
    );
  } else if (form.departureDate) {
    parts.push(`出发时间为${formatDateForMessage(form.departureDate)}`);
  }
  if (form.tripDuration) parts.push(`旅行时长为${form.tripDuration}`);
  if (form.travelerCount) parts.push(`${form.travelerCount}人出行`);
  if (form.budget) {
    parts.push(`${form.budgetScope === "per_person" ? "每人" : "总"}预算${form.budget}元`);
  }
  if (form.transport) parts.push(`交通优先${form.transport}`);
  if (form.interests.length) parts.push(`偏好${form.interests.join("、")}`);
  return parts.length ? `补充行程需求：${parts.join("，")}。` : "";
}

/** 渲染并管理浏览器内存中的多轮对话。 */
function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState("");
  const [analysis, setAnalysis] = useState<ConversationAnalysis | null>(null);
  const [isQuickFormOpen, setIsQuickFormOpen] = useState(false);
  const [quickForm, setQuickForm] = useState<QuickForm>(createEmptyQuickForm);
  const nextMessageId = useRef(0);
  const failedMessage = messages.find((message) => message.delivery === "failed");
  const canUseQuickForm = analysis?.intent === "trip_planning";
  const missingFields = new Set(analysis?.missing_fields ?? []);

  useEffect(() => {
    if (analysis?.intent !== "trip_planning" || !analysis.requirements) return;

    const { requirements } = analysis;
    // 第一步：仅以服务端已确认的需求回填快捷面板，保留用户正在编辑但尚未提交的字段。
    setQuickForm((current) => ({
      origin: requirements.origin ?? current.origin,
      destination: requirements.destination ?? current.destination,
      departureDate: current.departureDate,
      returnDate: current.returnDate,
      tripDuration: requirements.trip_duration?.raw_text ?? current.tripDuration,
      travelerCount: requirements.traveler_count?.toString() ?? current.travelerCount,
      budget: requirements.budget?.replace(/[^\d.]/g, "") || current.budget,
      budgetScope: requirements.budget?.includes("总") ? "total" : current.budgetScope,
      transport: requirements.transport_preferences[0] ?? current.transport,
      interests: requirements.attraction_preferences.length
        ? requirements.attraction_preferences
        : current.interests,
    }));
    // 第二步：旅差需求未完整时自动展开，减少用户在连续追问中寻找入口的成本。
    if (!analysis.is_complete) setIsQuickFormOpen(true);
  }, [analysis]);

  /** 将指定的本地消息历史提交后端，并同步处理成功或失败状态。 */
  async function requestAssistantReply(
    nextMessages: Message[],
    pendingMessageId: number,
  ) {
    setError("");
    setIsSending(true);
    try {
      const response = await fetch(`${API_BASE_URL}/api/v1/chat/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          // 第一步：只传输近期对话，避免会话变长后每次请求重复携带全部文本。
          messages: nextMessages
            .slice(-CONTEXT_MESSAGE_LIMIT)
            .map(({ role, content }) => ({ role, content })),
          // 第二步：旅差场景补充上轮确认的结构化需求，保证截短历史后仍保留关键条件。
          known_requirements: analysis?.intent === "trip_planning"
            ? analysis.requirements
            : undefined,
          // 第三步：待确认方案随下一轮提交，后端可判断用户是在确认还是修改上一版方案。
          pending_plan: analysis?.pending_plan ?? undefined,
        }),
      });
      const data = await response.json().catch(() => null) as ChatApiResponse | ChatApiError | null;
      if (!response.ok) {
        // 第一步：优先展示后端统一错误响应中的业务提示，网络异常仍使用通用文案。
        const errorMessage = data && "error" in data ? data.error?.message : undefined;
        throw new Error(errorMessage || "暂时无法获取回复。");
      }
      if (!data || !("analysis" in data) || !data.analysis) {
        throw new Error("对话服务返回的数据不完整。");
      }

      setMessages((current) => [
        ...current.map((message) =>
          message.id === pendingMessageId
            ? { ...message, delivery: undefined }
            : message,
        ),
        {
          id: nextMessageId.current++,
          role: "assistant",
          content: data.analysis.reply,
        },
      ]);
      // 第一步：保存后端已校验的分析结果，作为快捷补充面板的唯一状态来源。
      setAnalysis(data.analysis);
    } catch (requestError) {
      // 第一步：保留失败的用户消息供重试展示，但后续新消息不会带入请求上下文。
      setMessages((current) =>
        current.map((message) =>
          message.id === pendingMessageId
            ? { ...message, delivery: "failed" }
            : message,
        ),
      );
      setError(
        requestError instanceof Error
          ? requestError.message
          : "暂时无法获取回复。",
      );
    } finally {
      setIsSending(false);
    }
  }

  /** 创建一条待发送用户消息，并仅在没有失败回合时发起新的会话请求。 */
  async function sendUserMessage(content: string) {
    if (!content || isSending || failedMessage) return;

    // 第一步：用户消息先以待发送状态展示，成功后才进入后续上下文。
    const userMessage: Message = {
      id: nextMessageId.current++,
      role: "user",
      content,
      delivery: "pending",
    };
    const nextMessages = [...messages, userMessage];
    setMessages(nextMessages);
    setDraft("");
    await requestAssistantReply(nextMessages, userMessage.id);
  }

  /** 提交自由文本输入。 */
  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await sendUserMessage(draft.trim());
  }

  /** 将快捷面板字段合成为一条自然语言补充消息后提交。 */
  async function handleQuickFormSubmit() {
    // 第一步：将快捷字段转换为统一对话 Agent 可消费的一条自然语言消息。
    const content = buildQuickRequirementMessage(quickForm);
    if (!content) return;
    // 第二步：复用聊天发送逻辑，确保失败重试和对话上下文规则完全一致。
    await sendUserMessage(content);
  }

  /** 确认当前待确认方案。 */
  async function handleConfirmPlan() {
    // 第一步：确认消息仍走统一入口，后端根据 pending_plan 决定结束确认分支。
    await sendUserMessage("确认当前方案");
  }

  /** 切换一个行程偏好选项。 */
  function toggleInterest(interest: string) {
    // 第一步：已选项移除，未选项加入，保持多选状态与按钮高亮一致。
    setQuickForm((current) => ({
      ...current,
      interests: current.interests.includes(interest)
        ? current.interests.filter((item) => item !== interest)
        : [...current.interests, interest],
    }));
  }

  /** 重试最后一条失败的用户消息，避免重复插入或跳过该轮对话。 */
  async function handleRetry() {
    if (!failedMessage || isSending) return;

    // 第一步：将失败消息恢复为待发送状态，并以原有已确认历史重新发起请求。
    const retryMessages = messages.map((message) =>
      message.id === failedMessage.id
        ? { ...message, delivery: "pending" as const }
        : message,
    );
    setMessages(retryMessages);
    await requestAssistantReply(retryMessages, failedMessage.id);
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
          {messages.map((message) => (
            <article
              className={`message ${message.role} ${message.delivery ?? ""}`}
              key={message.id}
            >
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
          {error && (
            <div className="error-row" role="alert">
              <p className="error">{error}</p>
              <button
                type="button"
                className="retry-button"
                onClick={handleRetry}
                disabled={!failedMessage || isSending}
              >
                重试
              </button>
            </div>
          )}
          {canUseQuickForm && (
            <section className="quick-entry" aria-label="快速补充行程">
              <div className="quick-entry-header">
                <button
                  type="button"
                  className="quick-toggle"
                  onClick={() => setIsQuickFormOpen((current) => !current)}
                  aria-expanded={isQuickFormOpen}
                >
                  快速补充
                </button>
                {analysis?.pending_plan?.review_result.status === "ready_for_confirmation" ? (
                  <button
                    type="button"
                    className="quick-submit"
                    onClick={handleConfirmPlan}
                    disabled={isSending || Boolean(failedMessage)}
                  >
                    确认方案
                  </button>
                ) : (
                  analysis?.is_complete && <span className="quick-status">需求已齐</span>
                )}
              </div>
              {isQuickFormOpen && (
                <div className="quick-form">
                  <div className="quick-grid">
                    <label className={missingFields.has("destination") ? "is-required" : ""}>
                      目的地
                      <input
                        value={quickForm.destination}
                        onChange={(event) =>
                          setQuickForm((current) => ({
                            ...current,
                            destination: event.target.value,
                          }))
                        }
                        placeholder="北京"
                      />
                    </label>
                    <label>
                      出发地
                      <input
                        value={quickForm.origin}
                        onChange={(event) =>
                          setQuickForm((current) => ({
                            ...current,
                            origin: event.target.value,
                          }))
                        }
                        placeholder="广州"
                      />
                    </label>
                    <label className={missingFields.has("departure_date") ? "is-required" : ""}>
                      出发日期
                      <input
                        type="date"
                        value={quickForm.departureDate}
                        onChange={(event) =>
                          setQuickForm((current) => ({
                            ...current,
                            departureDate: event.target.value,
                          }))
                        }
                      />
                    </label>
                    <label className={missingFields.has("trip_schedule") ? "is-required" : ""}>
                      返程日期
                      <input
                        type="date"
                        value={quickForm.returnDate}
                        min={quickForm.departureDate || undefined}
                        onChange={(event) =>
                          setQuickForm((current) => ({
                            ...current,
                            returnDate: event.target.value,
                          }))
                        }
                      />
                    </label>
                    <label className={missingFields.has("trip_schedule") ? "is-required" : ""}>
                      旅行时长
                      <input
                        value={quickForm.tripDuration}
                        onChange={(event) =>
                          setQuickForm((current) => ({
                            ...current,
                            tripDuration: event.target.value,
                          }))
                        }
                        placeholder="一周"
                      />
                    </label>
                    <label className={missingFields.has("traveler_count") ? "is-required" : ""}>
                      人数
                      <input
                        type="number"
                        min="1"
                        max="100"
                        value={quickForm.travelerCount}
                        onChange={(event) =>
                          setQuickForm((current) => ({
                            ...current,
                            travelerCount: event.target.value,
                          }))
                        }
                        placeholder="5"
                      />
                    </label>
                    <label>
                      预算
                      <input
                        type="number"
                        min="0"
                        value={quickForm.budget}
                        onChange={(event) =>
                          setQuickForm((current) => ({
                            ...current,
                            budget: event.target.value,
                          }))
                        }
                        placeholder="3000"
                      />
                    </label>
                  </div>
                  <div className="quick-control-row">
                    <div className="segmented-control" aria-label="预算口径">
                      {[
                        ["per_person", "每人"],
                        ["total", "总额"],
                      ].map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          className={quickForm.budgetScope === value ? "is-active" : ""}
                          onClick={() =>
                            setQuickForm((current) => ({
                              ...current,
                              budgetScope: value as BudgetScope,
                            }))
                          }
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    <div className="choice-group" aria-label="交通偏好">
                      {TRANSPORT_OPTIONS.map((option) => (
                        <button
                          key={option}
                          type="button"
                          className={quickForm.transport === option ? "is-active" : ""}
                          onClick={() =>
                            setQuickForm((current) => ({
                              ...current,
                              transport: current.transport === option ? "" : option,
                            }))
                          }
                        >
                          {option}
                        </button>
                      ))}
                    </div>
                    <div className="choice-group" aria-label="游玩偏好">
                      {INTEREST_OPTIONS.map((interest) => (
                        <button
                          key={interest}
                          type="button"
                          className={quickForm.interests.includes(interest) ? "is-active" : ""}
                          onClick={() => toggleInterest(interest)}
                        >
                          {interest}
                        </button>
                      ))}
                    </div>
                    <button
                      className="quick-submit"
                      type="button"
                      onClick={handleQuickFormSubmit}
                      disabled={isSending || Boolean(failedMessage)}
                    >
                      发送补充
                    </button>
                  </div>
                </div>
              )}
            </section>
          )}
          <textarea
            aria-label="输入消息"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="例如：下个月去上海出差三天，想住得离客户近一些"
            rows={3}
            disabled={isSending || Boolean(failedMessage)}
          />
          <div className="composer-footer">
            <span>对话内容仅保留在当前浏览器页面</span>
            <button
              type="submit"
              disabled={!draft.trim() || isSending || Boolean(failedMessage)}
              aria-label="发送消息"
            >
              <span aria-hidden="true">↑</span>
            </button>
          </div>
        </form>
      </section>
    </main>
  );
}

export default App;
