import { FormEvent, Fragment, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

type Role = "user" | "assistant";
type DeliveryState = "pending" | "failed";
type BudgetScope = "per_person" | "total";
type Intent =
  | "chat"
  | "trip_planning"
  | "accommodation_search"
  | "intercity_transport_search";

type Message = {
  id: number;
  role: Role;
  content: string;
  delivery?: DeliveryState;
  process?: ChatProgressEvent[];
  plan?: TripPlanSnapshot;
  planConfirmed?: boolean;
  confirmedPlan?: ConfirmedTripPlan;
  searchResults?: ConversationAnalysis["search_results"];
};

type ProgressStatus = "running" | "completed" | "failed" | "unavailable" | "rejected";

type ChatProgressEvent = {
  id: string;
  sequence: number;
  agent: string;
  action: string;
  tool: string | null;
  parent_id: string | null;
  status: ProgressStatus;
  reason?: string | null;
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
  details: TripPlanDetails;
};

type TripRouteOption = {
  mode: "transit" | "walking" | "driving" | "bicycling";
  mode_label: string;
  distance_text: string | null;
  duration_text: string | null;
  navigation_url: string | null;
};

type TripOverviewRoute = {
  origin: string;
  destination: string;
  origin_longitude: number | null;
  origin_latitude: number | null;
  destination_longitude: number | null;
  destination_latitude: number | null;
  distance_text: string | null;
  duration_text: string | null;
  navigation_url: string | null;
};

type TripMapPoint = {
  category: "attraction" | "food";
  name: string;
  address: string | null;
  longitude: number;
  latitude: number;
  sequence: number;
};

type TripRoute = {
  category: "attraction" | "food";
  origin: string;
  destination: string;
  options: TripRouteOption[];
  unavailable_modes: string[];
};

type TripPlanDetails = {
  overview_route: TripOverviewRoute | null;
  map_points: TripMapPoint[];
  routes: TripRoute[];
  weather: Record<string, unknown>;
  tool_status: Record<string, "available" | "unavailable" | "skipped">;
};

const TRANSPORT_MODE_ORDER: Record<TripRouteOption["mode"], number> = {
  driving: 0,
  transit: 1,
  walking: 2,
  bicycling: 3,
};

type ConfirmedTripPlan = TripPlanSnapshot & {
  confirmed_at: string;
  details: TripPlanDetails;
};

type ConversationAnalysis = {
  intent: Intent;
  reply: string;
  requirements: TripRequirements | null;
  plan_action: "plan" | "modify" | "confirm" | null;
  pending_plan: TripPlanSnapshot | null;
  confirmed_plan: ConfirmedTripPlan | null;
  search_results: Record<string, Record<string, unknown>>;
  missing_fields: string[];
  is_complete: boolean | null;
};

type ShortTermMemory = {
  summary: string | null;
  recent_messages: Array<{ role: Role; content: string }>;
};

type ChatApiResponse = {
  conversation_id: string;
  analysis: ConversationAnalysis;
  short_term_memory: ShortTermMemory;
  progress_events?: ChatProgressEvent[];
};

type ChatApiError = {
  error?: { message?: string };
};

type ChatProgressResponse = {
  found: boolean;
  is_complete: boolean;
  events: ChatProgressEvent[];
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
const TRANSPORT_OPTIONS = ["高铁", "飞机", "自驾"];
const INTEREST_OPTIONS = ["人文历史", "亲子", "美食", "自然风光"];
const STARTER_PROMPTS = [
  "下周从上海去杭州出差两天，2 人同行",
  "查一下北京国贸附近适合商务出行的酒店",
  "帮我看看广州到深圳下周五的高铁",
];
const INTERNAL_PLAN_SECTION = /^#{1,6}\s*(?:[a-z][a-z0-9_]*_tool|.*(?:工具调用|工具说明|Agent|Skill|API))\s*$/i;
const INTERNAL_PLAN_LINE = /(?:\b(?:map_route_tool|accommodation_tool|attraction_tool|food_tool|weather_tool|transport_tool|Agent|Skill|API)\b|(?:高德地图|住宿查询|景点查询|餐饮查询|天气查询|本地交通)工具|工具调用)/i;
const CONFIRMED_PENDING_SECTION = /^\s*#{1,6}\s*(?:待确认事项?|需要确认事项|需确认事项|待核验事项|待外部数据核验)\s*[：:]?\s*$/;
const CONFIRMED_PENDING_LINE = /(?:待确认|需要确认|需确认|待核验|待外部数据核验|等待确认|请确认)/;
const INTERNAL_REVIEW_DETAIL = /(?:TRIP_DAILY_|每日安排不满足强制要求|无法满足每天至少|按每一天至少.*重新生成方案|第\s*\d+\s*天.*(?:缺少.*(?:景点|美食|餐饮)|每日安排))/;

declare global {
  interface Window {
    AMap?: any;
    _AMapSecurityConfig?: { securityJsCode?: string };
  }
}

let amapLoaderPromise: Promise<any> | null = null;

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

function displayDate(value: string | null): string {
  if (!value) return "待补充";
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat("zh-CN", { month: "short", day: "numeric" }).format(date);
}

function buildQuickRequirementMessage(form: QuickForm): string {
  const parts: string[] = [];
  if (form.origin) parts.push(`从${form.origin}出发`);
  if (form.destination) parts.push(`前往${form.destination}`);
  if (form.departureDate && form.returnDate) {
    parts.push(`出行时间为${formatDateForMessage(form.departureDate)}至${formatDateForMessage(form.returnDate)}`);
  } else if (form.departureDate) {
    parts.push(`出发时间为${formatDateForMessage(form.departureDate)}`);
  }
  if (form.tripDuration) parts.push(`旅行时长为${form.tripDuration}`);
  if (form.travelerCount) parts.push(`${form.travelerCount}人出行`);
  if (form.budget) parts.push(`${form.budgetScope === "per_person" ? "每人" : "总"}预算${form.budget}元`);
  if (form.transport) parts.push(`交通优先${form.transport}`);
  if (form.interests.length) parts.push(`偏好${form.interests.join("、")}`);
  return parts.length ? `规划行程：${parts.join("，")}。` : "";
}

function requirementsToForm(requirements: TripRequirements, current: QuickForm): QuickForm {
  return {
    origin: requirements.origin ?? current.origin,
    destination: requirements.destination ?? current.destination,
    departureDate: requirements.departure_date ?? current.departureDate,
    returnDate: requirements.return_date ?? current.returnDate,
    tripDuration: requirements.trip_duration?.raw_text ?? current.tripDuration,
    travelerCount: requirements.traveler_count?.toString() ?? current.travelerCount,
    budget: requirements.budget?.replace(/[^\d.]/g, "") || current.budget,
    budgetScope: requirements.budget?.includes("总") ? "total" : current.budgetScope,
    transport: requirements.transport_preferences[0] ?? current.transport,
    interests: requirements.attraction_preferences.length
      ? requirements.attraction_preferences
      : current.interests,
  };
}

function readableIntent(intent: Intent | undefined): string {
  if (intent === "accommodation_search") return "酒店查询";
  if (intent === "intercity_transport_search") return "交通查询";
  if (intent === "trip_planning") return "行程规划";
  return "旅差对话";
}

function reviewTone(status: ReviewResult["status"]): string {
  if (status === "ready_for_confirmation") return "ready";
  return "attention";
}

function reviewLabel(_status: ReviewResult["status"]): string {
  return "待确认";
}

function planTitle(_status: ReviewResult["status"]): string {
  return "待确认方案";
}

type CompactReviewNote = {
  text: string;
  kind: "risk" | "pending";
};

const REVIEW_NOTE_TOPICS: Array<[string, RegExp]> = [
  ["transport", /城际|高铁|飞机|火车|票价|余票|班次|交通/],
  ["accommodation", /酒店|住宿|房价|库存|房型|预订|预定/],
  ["weather", /天气|预警|降雨|气温|紫外线/],
  ["budget", /预算|费用|价格|报价/],
  ["schedule", /日期|时间|行程|日程|安排/],
  ["dining", /餐|美食|料理/],
];

function compactReviewNotes(review: ReviewResult): CompactReviewNote[] {
  const notes: CompactReviewNote[] = [];
  const seenTexts = new Set<string>();
  const seenTopics = new Set<string>();
  const candidates: CompactReviewNote[] = [
    ...review.pending_items.map((text) => ({ text, kind: "pending" as const })),
    ...review.risks.map((text) => ({ text, kind: "risk" as const })),
  ];

  for (const candidate of candidates) {
    const text = candidate.text.trim().replace(/\s+/g, " ");
    if (!text || INTERNAL_REVIEW_DETAIL.test(text)) continue;
    const normalizedText = text.toLocaleLowerCase();
    if (seenTexts.has(normalizedText)) continue;
    const topic = REVIEW_NOTE_TOPICS.find(([, pattern]) => pattern.test(text))?.[0];
    if (topic && seenTopics.has(topic)) continue;
    seenTexts.add(normalizedText);
    if (topic) seenTopics.add(topic);
    notes.push({ ...candidate, text });
    if (notes.length === 4) break;
  }
  return notes;
}

function displayReviewSummary(summary: string): string {
  return INTERNAL_REVIEW_DETAIL.test(summary)
    ? "方案已完成自动校验，请确认路线顺序与行程安排是否符合你的出行偏好。"
    : summary;
}

function displayPlanProposal(proposal: string, confirmed = false): string {
  let skipInternalSection = false;
  const visibleLines = proposal.split(/\r?\n/).filter((line) => {
    if (confirmed && CONFIRMED_PENDING_SECTION.test(line.trim())) {
      skipInternalSection = true;
      return false;
    }
    if (INTERNAL_PLAN_SECTION.test(line.trim())) {
      skipInternalSection = true;
      return false;
    }
    if (/^#{1,6}\s+/.test(line)) {
      skipInternalSection = false;
      return true;
    }
    return !skipInternalSection
      && !INTERNAL_PLAN_LINE.test(line)
      && !(confirmed && CONFIRMED_PENDING_LINE.test(line));
  });
  const cleanedLines = visibleLines
    .map((line) => line
      .replace(/^\s*#{1,6}\s+/, "")
      .replace(/^\s*>\s?/, "")
      .replace(/^\s*[-*+]\s+/, "")
      .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
      .replace(/`([^`]+)`/g, "$1")
      .replace(/(\*\*|__)(.*?)\1/g, "$2")
      .replace(/~~(.*?)~~/g, "$1")
      .trim())
    .filter((line) => !/^[-*_]{3,}$/.test(line));
  const cleanedProposal = cleanedLines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
  return cleanedProposal || "行程内容正在整理。";
}

function progressStateLabel(status: ProgressStatus): string {
  if (status === "running") return "运行中";
  if (status === "unavailable") return "服务不可用";
  if (status === "failed") return "未完成";
  if (status === "rejected") return "未通过";
  return "已完成";
}

function progressVisualState(status: ProgressStatus): "active" | "complete" | "failed" | "unavailable" | "rejected" {
  if (status === "completed") return "complete";
  if (status === "failed") return "failed";
  if (status === "unavailable") return "unavailable";
  if (status === "rejected") return "rejected";
  return "active";
}

function compactProgressEvents(events: ChatProgressEvent[]): ChatProgressEvent[] {
  const eventsById = new Map<string, ChatProgressEvent>();
  events.forEach((event) => eventsById.set(event.id, event));
  return [...eventsById.values()].sort((left, right) => left.sequence - right.sequence);
}

function RobotIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <path d="M12 8V4H8" />
      <rect x="4" y="8" width="16" height="12" rx="2" />
      <path d="M9 13v.01M15 13v.01M8 17h8" />
    </svg>
  );
}

function DateField({
  value,
  min,
  onChange,
}: {
  value: string;
  min?: string;
  onChange: (value: string) => void;
}) {
  const pickerRef = useRef<HTMLInputElement | null>(null);

  function openPicker() {
    const picker = pickerRef.current;
    if (!picker) return;
    if (picker.showPicker) {
      picker.showPicker();
      return;
    }
    picker.focus();
  }

  return (
    <div className="date-field">
      <button type="button" className={value ? "has-value" : ""} onClick={openPicker}>
        <span>{value ? formatDateForMessage(value) : "选择日期"}</span>
        <i aria-hidden="true" />
      </button>
      <input
        ref={pickerRef}
        className="date-field__native"
        type="date"
        value={value}
        min={min}
        onChange={(event) => onChange(event.target.value)}
        tabIndex={-1}
      />
    </div>
  );
}

function AgentTraceStep({
  stage,
  events,
  openTools,
  onToggleTools,
}: {
  stage: ChatProgressEvent;
  events: ChatProgressEvent[];
  openTools: Record<string, boolean>;
  onToggleTools: (eventId: string) => void;
}) {
  const state = progressVisualState(stage.status);
  const tools = events.filter((event) => event.parent_id === stage.id && event.tool !== null);
  const childAgents = events
    .filter((event) => event.parent_id === stage.id && event.tool === null)
    .sort((left, right) => left.sequence - right.sequence);

  return (
    <li className={`agent-trace__step is-${state}`}>
      <span className="agent-trace__marker" aria-hidden="true" />
      <div>
        <strong>{stage.agent}</strong>
        <span>{stage.action}</span>
        {stage.reason && stage.status !== "completed" && (
          <small className="agent-trace__reason">{stage.reason}</small>
        )}
        {tools.length > 0 && (
          <>
            <button
              className="agent-tools__toggle"
              type="button"
              onClick={() => onToggleTools(stage.id)}
              aria-expanded={Boolean(openTools[stage.id])}
            >
              <span>查看工具运行</span>
              <span aria-hidden="true">{openTools[stage.id] ? "-" : "+"}</span>
            </button>
            {openTools[stage.id] && (
              <ul className="agent-tools__list">
                {tools.map((tool) => {
                  const toolState = tool.status === "completed"
                    ? "complete"
                    : progressVisualState(tool.status);
                  return (
                    <li className={`agent-tools__item is-${toolState}`} key={tool.id}>
                      <span aria-hidden="true" />
                      <div>
                        <strong>{tool.tool}</strong>
                        <small>{tool.action}</small>
                        {tool.reason && tool.status !== "completed" && (
                          <small className="agent-tools__reason">{tool.reason}</small>
                        )}
                      </div>
                      <em>{progressStateLabel(tool.status)}</em>
                    </li>
                  );
                })}
              </ul>
            )}
          </>
        )}
        {childAgents.length > 0 && (
          <section className="agent-subagents">
            <ol>
              {childAgents.map((childAgent) => (
                <AgentTraceStep
                  key={childAgent.id}
                  stage={childAgent}
                  events={events}
                  openTools={openTools}
                  onToggleTools={onToggleTools}
                />
              ))}
            </ol>
          </section>
        )}
      </div>
      <em>{progressStateLabel(stage.status)}</em>
    </li>
  );
}

function AgentTrace({
  events,
  isLive = false,
}: {
  events: ChatProgressEvent[];
  isLive?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const [openTools, setOpenTools] = useState<Record<string, boolean>>({});
  const visibleEvents = compactProgressEvents(events.filter((event) => event.tool !== "天气预警"));
  const stages = visibleEvents.filter((event) => event.tool === null);
  const rootStages = stages.filter((stage) => stage.parent_id === null);
  const completedCount = stages.filter((stage) => stage.status === "completed").length;
  const hasRunningEvent = visibleEvents.some((event) => event.status === "running");
  const summary = isLive && hasRunningEvent ? "查看助手处理进度" : "查看助手处理过程";

  function toggleTools(eventId: string) {
    setOpenTools((current) => ({
      ...current,
      [eventId]: !current[eventId],
    }));
  }

  return (
    <div className={`agent-trace ${isLive ? "is-live" : "is-complete"}`}>
      <button
        className="agent-trace__toggle"
        type="button"
        onClick={() => setIsOpen((current) => !current)}
        aria-expanded={isOpen}
      >
        <span className="agent-trace__status" aria-hidden="true" />
        <span>{summary}</span>
        <span className="agent-trace__chevron" aria-hidden="true">{isOpen ? "-" : "+"}</span>
      </button>
      {isOpen && (
        <ol className="agent-trace__list">
          {rootStages.map((stage) => (
            <AgentTraceStep
              key={stage.id}
              stage={stage}
              events={visibleEvents}
              openTools={openTools}
              onToggleTools={toggleTools}
            />
          ))}
        </ol>
      )}
      {!isOpen && !isLive && <span className="agent-trace__complete-count">{completedCount} 个步骤已完成</span>}
    </div>
  );
}

function RequirementItem({
  label,
  value,
  required,
}: {
  label: string;
  value: string;
  required?: boolean;
}) {
  return (
    <div className={`requirement-item ${required ? "is-required" : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function SearchResultsView({
  results,
  embedded = false,
}: {
  results: ConversationAnalysis["search_results"];
  embedded?: boolean;
}) {
  const [isOpen, setIsOpen] = useState(false);
  const groups = Object.entries(results)
    .map(([name, result]) => {
      const offers = Array.isArray(result.offers) ? result.offers : [];
      return {
        name,
        label: name === "accommodation"
          ? "住宿参考"
          : name === "intercity_transport"
            ? "城际交通参考"
            : "偏好参考",
        offerCount: offers.length,
      };
    })
    .filter((group) => group.offerCount > 0);
  if (!groups.length) return null;
  const totalOffers = groups.reduce((total, group) => total + group.offerCount, 0);

  return (
    <section className={`search-results ${embedded ? "is-embedded" : ""}`} aria-label="查询参考">
      <button
        className="reference-toggle"
        type="button"
        onClick={() => setIsOpen((current) => !current)}
        aria-expanded={isOpen}
      >
        <span>
          <strong>查询参考</strong>
          <small>已整理 {totalOffers} 条候选</small>
        </span>
        <span className="reference-toggle__chevron" aria-hidden="true">{isOpen ? "-" : "+"}</span>
      </button>
      {isOpen && (
        <div className="reference-summary">
          {groups.map((group) => (
            <div className="reference-summary__row" key={group.name}>
              <span>{group.label}</span>
              <strong>{group.offerCount} 条候选</strong>
            </div>
          ))}
          <p>候选价格、库存和余票均为查询参考，请以实际服务为准。</p>
        </div>
      )}
    </section>
  );
}

type PlanListItem = {
  id: string;
  title: string;
  status: string;
  tone: "pending" | "confirmed" | "attention";
  confirmedPlan?: ConfirmedTripPlan;
};

function planRouteTitle(requirements: TripRequirements): string {
  return `${requirements.origin ?? "出发地待定"} → ${requirements.destination ?? "目的地待定"}`;
}

function planListStatus(review: ReviewResult): { label: string; tone: PlanListItem["tone"] } {
  if (review.status === "needs_replanning") return { label: "需调整", tone: "attention" };
  if (review.status === "needs_user_decision") return { label: "待补充", tone: "attention" };
  return { label: "待确认", tone: "pending" };
}

function collectPlanListItems(messages: Message[]): PlanListItem[] {
  const items: PlanListItem[] = [];
  messages.forEach((message) => {
    if (message.plan && !message.planConfirmed) {
      const status = planListStatus(message.plan.review_result);
      items.push({
        id: `plan-${message.id}-pending`,
        title: planRouteTitle(message.plan.requirements),
        status: status.label,
        tone: status.tone,
      });
    }
    if (message.confirmedPlan) {
      items.push({
        id: `plan-${message.id}-confirmed`,
        title: planRouteTitle(message.confirmedPlan.requirements),
        status: "已确认",
        tone: "confirmed",
        confirmedPlan: message.confirmedPlan,
      });
    }
  });
  return items.reverse();
}

function planPdfFileName(plan: ConfirmedTripPlan): string {
  return `${planRouteTitle(plan.requirements)}-TripWeave最终方案`
    .replace(/[\\/:*?"<>|]/g, "-");
}

function exportConfirmedPlanPdf(plan: ConfirmedTripPlan) {
  const exportWindow = window.open("", "_blank", "width=920,height=720");
  if (!exportWindow) {
    window.alert("浏览器阻止了打印窗口，请允许弹窗后重试。");
    return;
  }
  const details = plan.details ?? emptyPlanDetails();
  const overview = details.overview_route;
  const routeLines = details.map_points
    .map((point) => `${point.sequence}. ${point.category === "food" ? "美食" : "景点"}：${point.name}${point.address ? `（${point.address}）` : ""}`)
    .join("\n");
  const cityRouteLines = details.routes
    .map((route) => {
      const options = [...route.options]
        .filter((option) => option.duration_text)
        .sort((left, right) => TRANSPORT_MODE_ORDER[left.mode] - TRANSPORT_MODE_ORDER[right.mode])
        .map((option) => `${option.mode_label}${option.duration_text ? ` ${option.duration_text}` : ""}`)
        .join("；");
      return options ? `${route.origin} → ${route.destination}：${options}` : "";
    })
    .filter(Boolean)
    .join("\n");
  const overviewText = overview
    ? `${overview.origin} → ${overview.destination}${overview.distance_text ? `，${overview.distance_text}` : ""}${overview.duration_text ? `，驾车${overview.duration_text}` : ""}`
    : "";
  const proposal = displayPlanProposal(plan.proposal, true);
  const title = planPdfFileName(plan);
  exportWindow.document.title = title;
  exportWindow.document.write(`<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <title>${escapeHtml(title)}</title>
    <style>
      @page { size: A4; margin: 18mm 16mm; }
      body { color: #1f3440; font: 14px/1.75 "Microsoft YaHei UI", "PingFang SC", sans-serif; }
      h1 { margin: 0 0 4px; font-size: 23px; }
      h2 { margin: 25px 0 8px; color: #286c7a; font-size: 16px; }
      .meta { margin: 0; color: #637985; }
      .route { padding: 11px 13px; border: 1px solid #bdd8df; background: #f2fafb; white-space: pre-wrap; }
      .content { white-space: pre-wrap; }
      .footer { margin-top: 28px; color: #7d8f98; font-size: 11px; }
    </style>
  </head>
  <body>
    <h1>${escapeHtml(planRouteTitle(plan.requirements))}</h1>
    <p class="meta">TripWeave 已确认旅差方案</p>
    ${overviewText ? `<h2>出发与到达</h2><p>${escapeHtml(overviewText)}</p>` : ""}
    ${routeLines ? `<h2>路线规划</h2><div class="route">${escapeHtml(routeLines)}</div>` : ""}
    ${cityRouteLines ? `<h2>城市内路线</h2><div class="route">${escapeHtml(cityRouteLines)}</div>` : ""}
    <h2>最终计划</h2>
    <div class="content">${escapeHtml(proposal)}</div>
    <p class="footer">本文件为行程决策参考；酒店、交通和价格以实际服务为准。</p>
  </body>
</html>`);
  exportWindow.document.close();
  window.setTimeout(() => {
    exportWindow.focus();
    exportWindow.print();
  }, 180);
}

function PlanLibrary({
  items,
  onSelect,
}: {
  items: PlanListItem[];
  onSelect: (itemId: string) => void;
}) {
  return (
    <section className="plan-library" aria-label="方案列表">
      <div className="plan-library__header">
        <div>
          <span className="section-kicker">方案列表</span>
          <h2>本次对话方案</h2>
        </div>
        <span>{items.length}</span>
      </div>
      {items.length === 0 ? (
        <p className="plan-library__empty">生成方案后会显示在这里。</p>
      ) : (
        <ol className="plan-library__list">
          {items.map((item) => (
            <li className="plan-library__entry" key={item.id}>
              <button className="plan-library__item" type="button" onClick={() => onSelect(item.id)}>
                <span className="plan-library__route">{item.title}</span>
                <span className={`plan-library__status is-${item.tone}`}>{item.status}</span>
              </button>
              {item.confirmedPlan && (
                <button
                  className="plan-library__download"
                  type="button"
                  onClick={() => exportConfirmedPlanPdf(item.confirmedPlan!)}
                >
                  导出 PDF
                </button>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function PlanSnapshotView({
  plan,
  onConfirm,
  onPlanChange,
  disabled,
  isConfirmed,
  searchResults,
  anchorId,
}: {
  plan: TripPlanSnapshot;
  onConfirm: () => void;
  onPlanChange: (plan: TripPlanSnapshot) => void;
  disabled: boolean;
  isConfirmed: boolean;
  searchResults: ConversationAnalysis["search_results"];
  anchorId: string;
}) {
  const { review_result: review } = plan;
  const reviewNotes = compactReviewNotes(review);
  const proposal = displayPlanProposal(plan.proposal, true);
  const details = plan.details ?? emptyPlanDetails();
  return (
    <section className="plan-snapshot" id={anchorId} aria-label={planTitle(review.status)}>
      <div className="plan-snapshot__header">
        <div>
          <span className="section-kicker">行程草案</span>
          <h2>{planTitle(review.status)}</h2>
        </div>
        <span className={`review-badge ${reviewTone(review.status)}`}>{reviewLabel(review.status)}</span>
      </div>
      <div className="plan-window__body">
        <p className="plan-snapshot__summary">{displayReviewSummary(review.summary)}</p>
        <RoutePlanner
          details={details}
          readOnly={isConfirmed}
          onChange={(nextDetails) => onPlanChange({ ...plan, details: nextDetails })}
        />
        <div className="confirmation-rewrite-notice">
          <strong>确认后将重新生成最终方案</strong>
          <span>系统会基于你确认的路线顺序、地图距离、景点美食和天气参考，重写一版正式文字计划，替代当前草稿。</span>
        </div>
        <div className="proposal-text">{proposal}</div>
        {reviewNotes.length > 0 && (
          <div className="plan-notes">
            <strong className="plan-notes__heading">温馨提示</strong>
            {reviewNotes.map((note) => <p key={`${note.kind}-${note.text}`} className={`${note.kind}-note`}>{note.text}</p>)}
          </div>
        )}
        <SearchResultsView results={searchResults} embedded />
      </div>
      <button
        className={`primary-button plan-confirm-button ${isConfirmed ? "is-confirmed" : ""}`}
        type="button"
        onClick={onConfirm}
        disabled={disabled || isConfirmed}
      >
        {isConfirmed ? "已确认此方案" : "确认此方案"}
      </button>
    </section>
  );
}

function loadAmap(key: string, securityCode: string | undefined): Promise<any> {
  if (window.AMap) return Promise.resolve(window.AMap);
  if (securityCode) {
    window._AMapSecurityConfig = { securityJsCode: securityCode };
  }
  if (!amapLoaderPromise) {
    amapLoaderPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      const params = new URLSearchParams({
        v: "2.0",
        key,
        plugin: "AMap.Marker,AMap.Polyline,AMap.InfoWindow",
      });
      script.src = `https://webapi.amap.com/maps?${params.toString()}`;
      script.async = true;
      script.onload = () => window.AMap ? resolve(window.AMap) : reject(new Error("AMap missing"));
      script.onerror = () => reject(new Error("AMap load failed"));
      document.head.appendChild(script);
    });
  }
  return amapLoaderPromise;
}

function hasOverviewCoordinates(route: TripOverviewRoute | null): route is TripOverviewRoute {
  return Boolean(
    route
    && typeof route.origin_longitude === "number"
    && typeof route.origin_latitude === "number"
    && typeof route.destination_longitude === "number"
    && typeof route.destination_latitude === "number",
  );
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => (
    {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#39;",
    }[char] ?? char
  ));
}

function AmapLiveMap({
  overviewRoute,
  points,
  title = "高德实时地图",
  subtitle = "出发地、目的地与方案地点",
  showPointList = true,
}: {
  overviewRoute: TripOverviewRoute | null;
  points: TripMapPoint[];
  title?: string;
  subtitle?: string;
  showPointList?: boolean;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "missing" | "failed">("idle");
  const canDrawOverview = hasOverviewCoordinates(overviewRoute);
  const routeSignature = useMemo(() => JSON.stringify({
    overview: canDrawOverview
      ? [
        overviewRoute.origin_longitude,
        overviewRoute.origin_latitude,
        overviewRoute.destination_longitude,
        overviewRoute.destination_latitude,
      ]
      : null,
    points: points.map((point) => [
      point.sequence,
      point.name,
      point.longitude,
      point.latitude,
    ]),
  }), [canDrawOverview, overviewRoute, points]);

  useEffect(() => {
    const key = import.meta.env.VITE_AMAP_WEB_JS_KEY as string | undefined;
    const securityCode = import.meta.env.VITE_AMAP_SECURITY_CODE as string | undefined;
    const container = containerRef.current;
    if (!container || (!canDrawOverview && points.length === 0)) return;
    if (!key) {
      setStatus("missing");
      return;
    }

    let disposed = false;
    let map: any = null;
    setStatus("loading");

    loadAmap(key, securityCode)
      .then((AMap) => {
        if (disposed || !containerRef.current) return;
        const markers: any[] = [];
        const cityPath: number[][] = points.map((point) => [point.longitude, point.latitude]);
        const mapCenter = cityPath[0]
          ?? (canDrawOverview ? [overviewRoute.destination_longitude, overviewRoute.destination_latitude] : undefined)
          ?? [116.397128, 39.916527];

        map = new AMap.Map(containerRef.current, {
          zoom: cityPath.length > 0 ? 12 : 6,
          center: mapCenter,
          viewMode: "2D",
          resizeEnable: false,
        });

        const infoWindow = new AMap.InfoWindow({ offset: new AMap.Pixel(0, -28) });
        const addMarker = (
          position: number[],
          title: string,
          kind: "origin" | "destination" | "attraction" | "food",
          sequence?: number,
          address?: string | null,
        ) => {
          const marker = new AMap.Marker({
            position,
            title,
            content: `<div class="amap-marker amap-marker--${kind}">${sequence ?? ""}</div>`,
            offset: new AMap.Pixel(-13, -13),
          });
          marker.on("click", () => {
            const escapedTitle = escapeHtml(title);
            const escapedAddress = address ? escapeHtml(address) : "";
            infoWindow.setContent(
              `<div class="amap-info"><strong>${escapedTitle}</strong>${escapedAddress ? `<span>${escapedAddress}</span>` : ""}</div>`,
            );
            infoWindow.open(map, position);
          });
          markers.push(marker);
        };

        if (canDrawOverview) {
          const origin: number[] = [overviewRoute.origin_longitude!, overviewRoute.origin_latitude!];
          const destination: number[] = [overviewRoute.destination_longitude!, overviewRoute.destination_latitude!];
          addMarker(origin, overviewRoute.origin, "origin");
          addMarker(destination, overviewRoute.destination, "destination");
          map.add(new AMap.Polyline({
            path: [origin, destination],
            strokeColor: "#b87942",
            strokeWeight: 4,
            strokeOpacity: 0.76,
            strokeStyle: "dashed",
          }));
        }

        points.forEach((point) => {
          addMarker(
            [point.longitude, point.latitude],
            point.name,
            point.category,
            point.sequence,
            point.address,
          );
        });

        if (cityPath.length > 1) {
          map.add(new AMap.Polyline({
            path: cityPath,
            strokeColor: "#286c7a",
            strokeWeight: 5,
            strokeOpacity: 0.82,
            showDir: true,
          }));
        }
        if (markers.length > 0) {
          map.add(markers);
          map.setFitView(markers, false, [34, 34, 34, 34]);
        }
        setStatus("ready");
      })
      .catch(() => {
        if (!disposed) setStatus("failed");
      });

    return () => {
      disposed = true;
      if (map) map.destroy();
    };
  }, [routeSignature]);

  return (
    <section className="confirmed-section">
      <div className="section-heading">
        <h3>{title}</h3>
        <span>{subtitle}</span>
      </div>
      <div className="live-map-panel">
        <div className="live-map" ref={containerRef}>
          {status === "loading" && <span>地图加载中...</span>}
          {status === "missing" && <span>配置前端高德 JS Key 后显示实时地图</span>}
          {status === "failed" && <span>地图暂时加载失败</span>}
        </div>
        {showPointList && points.length > 0 && (
          <div className="map-point-list">
            {points.map((point) => (
              <div className="map-point" key={`${point.category}-${point.name}-${point.sequence}`}>
                <b>{point.sequence}</b>
                <span>{point.category === "food" ? "美食" : "景点"}</span>
                <strong>{point.name}</strong>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function emptyPlanDetails(): TripPlanDetails {
  return {
    overview_route: null,
    map_points: [],
    routes: [],
    weather: {},
    tool_status: {},
  };
}

function normalizeRoutePoints(points: TripMapPoint[]): TripMapPoint[] {
  const seenPlaceKeys = new Set<string>();
  return points
    .filter((point) => {
      const placeKey = `${point.category}|${routePlaceBaseName(point.name)}`;
      if (seenPlaceKeys.has(placeKey)) return false;
      seenPlaceKeys.add(placeKey);
      return true;
    })
    .map((point, index) => ({
      ...point,
      sequence: index + 1,
    }));
}

function routePlaceBaseName(name: string): string {
  return name
    .replace(/(?:\s*(?:\([^()]{1,40}\)|（[^（）]{1,40}）|\[[^\[\]]{1,40}\]))+\s*$/, "")
    .replace(/[\s()（）\[\]【】\-—_·、,，.。]/g, "");
}

function reorderPoints(points: TripMapPoint[], fromIndex: number, toIndex: number): TripMapPoint[] {
  const nextPoints = [...points];
  const [movedPoint] = nextPoints.splice(fromIndex, 1);
  nextPoints.splice(toIndex, 0, movedPoint);
  return normalizeRoutePoints(nextPoints);
}

function routePointKey(point: TripMapPoint): string {
  return [
    point.category,
    point.name,
    point.address ?? "",
    point.longitude.toFixed(6),
    point.latitude.toFixed(6),
  ].join("|");
}

function RoutePlanner({
  details,
  readOnly,
  onChange,
}: {
  details: TripPlanDetails;
  readOnly: boolean;
  onChange?: (details: TripPlanDetails) => void;
}) {
  const [dragPointKey, setDragPointKey] = useState<string | null>(null);
  const cardRefs = useRef(new Map<string, HTMLElement>());
  const previousCardRectsRef = useRef<Map<string, DOMRect> | null>(null);
  const mapPoints = useMemo(
    () => normalizeRoutePoints(details.map_points ?? []),
    [details.map_points],
  );

  useLayoutEffect(() => {
    const previousCardRects = previousCardRectsRef.current;
    if (!previousCardRects) return;
    previousCardRectsRef.current = null;

    for (const [pointKey, card] of cardRefs.current) {
      if (pointKey === dragPointKey) continue;
      const previousRect = previousCardRects.get(pointKey);
      if (!previousRect) continue;
      const currentRect = card.getBoundingClientRect();
      const deltaX = previousRect.left - currentRect.left;
      const deltaY = previousRect.top - currentRect.top;
      if (Math.abs(deltaX) < 1 && Math.abs(deltaY) < 1) continue;
      card.animate(
        [
          { transform: `translate(${deltaX}px, ${deltaY}px)` },
          { transform: "translate(0, 0)" },
        ],
        {
          duration: 220,
          easing: "cubic-bezier(0.22, 1, 0.36, 1)",
        },
      );
    }
  }, [dragPointKey, mapPoints]);

  if (!mapPoints.length && !hasOverviewCoordinates(details.overview_route)) return null;

  function updatePoints(nextPoints: TripMapPoint[]) {
    onChange?.({
      ...details,
      map_points: nextPoints,
    });
  }

  function moveDraggedPoint(toIndex: number) {
    if (readOnly || dragPointKey === null) return;
    const fromIndex = mapPoints.findIndex((point) => routePointKey(point) === dragPointKey);
    if (fromIndex === -1 || fromIndex === toIndex) return;
    previousCardRectsRef.current = new Map(
      [...cardRefs.current].map(([pointKey, card]) => [
        pointKey,
        card.getBoundingClientRect(),
      ]),
    );
    updatePoints(reorderPoints(mapPoints, fromIndex, toIndex));
  }

  return (
    <section className={`route-planner ${readOnly ? "is-readonly" : "is-editable"}`}>
      <div className="section-heading">
        <h3>路线规划</h3>
        <span>{readOnly ? "已按确认顺序锁定" : "拖动卡片调整地图路线顺序"}</span>
      </div>

      {mapPoints.length > 0 && (
        <div
          className="route-card-strip"
          aria-label="路线规划顺序"
          onPointerUp={() => setDragPointKey(null)}
          onPointerCancel={() => setDragPointKey(null)}
          onPointerLeave={() => setDragPointKey(null)}
        >
          {mapPoints.map((point, index) => {
            const pointKey = routePointKey(point);
            return (
              <article
                className={`route-card ${dragPointKey === pointKey ? "is-dragging" : ""}`}
                key={pointKey}
                ref={(card) => {
                  if (card) cardRefs.current.set(pointKey, card);
                  else cardRefs.current.delete(pointKey);
                }}
                onPointerDown={(event) => {
                  if (readOnly) return;
                  event.preventDefault();
                  setDragPointKey(pointKey);
                }}
                onPointerEnter={() => {
                  moveDraggedPoint(index);
                }}
              >
                <div className="route-card__image">
                  <span>{point.name.slice(0, 2)}</span>
                  <b>{point.sequence}</b>
                </div>
                <div className="route-card__body">
                  <span>{point.category === "food" ? "美食" : "景点"}</span>
                  <strong>{point.name}</strong>
                  {point.address && <small>{point.address}</small>}
                </div>
              </article>
            );
          })}
        </div>
      )}

      <AmapLiveMap
        overviewRoute={details.overview_route}
        points={mapPoints}
        title="高德实时地图"
        subtitle={readOnly ? "按最终路线顺序展示" : "顺序调整后地图路线同步更新"}
        showPointList={false}
      />
    </section>
  );
}

function ConfirmedPlanView({
  plan,
  searchResults,
  anchorId,
}: {
  plan: ConfirmedTripPlan;
  searchResults: ConversationAnalysis["search_results"];
  anchorId: string;
}) {
  const details = plan.details ?? emptyPlanDetails();
  const proposal = displayPlanProposal(plan.proposal, true);
  const overviewRoute = details.overview_route;
  const cityRoutes = details.routes
    .map((route) => ({
      ...route,
      options: route.options.filter((option) => Boolean(option.duration_text)),
    }))
    .filter((route) => route.options.length > 0);

  return (
    <section className="confirmed-plan" id={anchorId} aria-label="已确认行程方案">
      <div className="confirmed-plan__header">
        <div>
          <span className="section-kicker">已确认</span>
          <h2>完整旅差安排</h2>
        </div>
        <div className="confirmed-plan__actions">
          <span className="confirmation-badge">已确认</span>
          <button
            className="confirmed-plan__download"
            type="button"
            onClick={() => exportConfirmedPlanPdf(plan)}
          >
            导出 PDF
          </button>
        </div>
      </div>
      <div className="plan-window__body">
        <RoutePlanner details={details} readOnly />
        <div className="proposal-text">{proposal}</div>

        {overviewRoute && (
          <section className="confirmed-section">
            <div className="section-heading">
              <h3>高德地图距离</h3>
              <span>出发地到目的地</span>
            </div>
            <article className="amap-overview" aria-label={`${overviewRoute.origin}到${overviewRoute.destination}的高德地图距离`}>
              <div className="amap-overview__map" aria-hidden="true">
                <span>{overviewRoute.origin}</span>
                <i />
                <span>{overviewRoute.destination}</span>
              </div>
              <div className="amap-overview__detail">
                <strong>{overviewRoute.origin} <span aria-hidden="true">-&gt;</span> {overviewRoute.destination}</strong>
                <p>
                  {overviewRoute.distance_text ?? "距离待定"}
                  {overviewRoute.duration_text ? ` · 驾车${overviewRoute.duration_text}` : ""}
                </p>
                {overviewRoute.navigation_url && (
                  <a href={overviewRoute.navigation_url} target="_blank" rel="noreferrer">
                    打开高德导航
                  </a>
                )}
              </div>
            </article>
          </section>
        )}

        {cityRoutes.length > 0 && (
          <section className="confirmed-section">
            <div className="section-heading">
              <h3>城市内路线</h3>
              <span>不含购票或打车订单</span>
            </div>
            <div className="route-list">
              {cityRoutes.map((route) => (
                <article className="route-item" key={`${route.category}-${route.destination}`}>
                  <strong>{route.origin} <span aria-hidden="true">-&gt;</span> {route.destination}</strong>
                  <div>
                    {[...route.options]
                      .sort((left, right) => TRANSPORT_MODE_ORDER[left.mode] - TRANSPORT_MODE_ORDER[right.mode])
                      .map((option) => (
                        option.navigation_url ? (
                          <a className="route-option" key={option.mode} href={option.navigation_url} target="_blank" rel="noreferrer">
                            {option.mode_label} {option.duration_text ?? "时间待定"} · 导航
                          </a>
                        ) : (
                          <span className="route-option" key={option.mode}>
                            {option.mode_label} {option.duration_text ?? "时间待定"}
                          </span>
                        )
                      ))}
                  </div>
                </article>
              ))}
            </div>
          </section>
        )}

        <SearchResultsView results={searchResults} embedded />
      </div>
    </section>
  );
}

function App() {
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState("");
  const [analysis, setAnalysis] = useState<ConversationAnalysis | null>(null);
  const [tripContext, setTripContext] = useState<ConversationAnalysis | null>(null);
  const [shortTermMemory, setShortTermMemory] = useState<ShortTermMemory | null>(null);
  // 会话只在当前页面生命周期内保持，避免刷新后将不可见的旧方案状态带入新对话。
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [quickForm, setQuickForm] = useState<QuickForm>(createEmptyQuickForm);
  const [isQuickFormOpen, setIsQuickFormOpen] = useState(true);
  const [liveProgressEvents, setLiveProgressEvents] = useState<ChatProgressEvent[]>([]);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);
  const nextMessageId = useRef(0);
  const liveProgressEventsRef = useRef<ChatProgressEvent[]>([]);
  const failedMessage = messages.find((message) => message.delivery === "failed");
  const activeRequirements = analysis?.requirements ?? tripContext?.requirements ?? null;
  const missingFields = useMemo(() => new Set(analysis?.missing_fields ?? []), [analysis]);
  const planListItems = useMemo(() => collectPlanListItems(messages), [messages]);
  const activeProgressEvent = useMemo(
    () => [...liveProgressEvents].reverse().find((event) => event.status === "running") ?? null,
    [liveProgressEvents],
  );

  useEffect(() => {
    if (!analysis?.requirements) return;
    setQuickForm((current) => requirementsToForm(analysis.requirements!, current));
    if (analysis.intent === "trip_planning" && !analysis.is_complete) setIsQuickFormOpen(true);
  }, [analysis]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, isSending]);

  async function requestAssistantReply(
    nextMessages: Message[],
    pendingMessageId: number,
    options: { pendingPlan?: TripPlanSnapshot } = {},
  ) {
    setError("");
    setIsSending(true);
    const clientRequestId = crypto.randomUUID();
    let isPolling = true;
    let progressTimer: number | undefined;
    const updateProgressEvents = (events: ChatProgressEvent[]) => {
      liveProgressEventsRef.current = events;
      setLiveProgressEvents(events);
    };
    const pollProgress = async () => {
      try {
        const progressResponse = await fetch(`${API_BASE_URL}/api/v1/chat/progress/${clientRequestId}`);
        if (!progressResponse.ok) return;
        const progress = await progressResponse.json() as ChatProgressResponse;
        if (isPolling && progress.found) updateProgressEvents(progress.events);
      } catch {
        // 进度接口不可用时保留中性等待状态，不能伪造 Agent 执行过程。
      }
    };
    updateProgressEvents([]);
    void pollProgress();
    progressTimer = window.setInterval(() => void pollProgress(), 650);
    try {
      const pendingMessage = nextMessages.find((message) => message.id === pendingMessageId);
      if (!pendingMessage) throw new Error("找不到待发送消息。");
      const hasExistingPlan = nextMessages.some((message) => message.plan || message.confirmedPlan);
      const knownRequirements = options.pendingPlan
        ? options.pendingPlan.requirements
        : hasExistingPlan
          ? undefined
          : tripContext?.requirements ?? undefined;

      const response = await fetch(`${API_BASE_URL}/api/v1/chat/messages`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: [{ role: pendingMessage.role, content: pendingMessage.content }],
          client_request_id: clientRequestId,
          conversation_id: conversationId ?? undefined,
          short_term_memory: shortTermMemory ?? undefined,
          known_requirements: knownRequirements,
          pending_plan: options.pendingPlan ?? undefined,
        }),
      });
      const data = await response.json().catch(() => null) as ChatApiResponse | ChatApiError | null;
      if (!response.ok) {
        const errorMessage = data && "error" in data ? data.error?.message : undefined;
        throw new Error(errorMessage || "暂时无法获取回复。");
      }
      if (!data || !("analysis" in data) || !data.analysis || !("conversation_id" in data) || !("short_term_memory" in data)) {
        throw new Error("对话服务返回的数据不完整。");
      }
      await pollProgress();
      isPolling = false;
      const finalProgressEvents = data.progress_events ?? liveProgressEventsRef.current;
      updateProgressEvents(finalProgressEvents);

      const hasPlanWindow = Boolean(data.analysis.pending_plan || data.analysis.confirmed_plan);
      const assistantReply = hasPlanWindow
        ? data.analysis.confirmed_plan
          ? "行程已确认，完整安排已整理在下方窗口。"
          : "行程方案已生成，请在下方窗口查看详情。"
        : data.analysis.reply;
      const assistantMessageId = nextMessageId.current++;
      const confirmationCompleted = Boolean(options.pendingPlan && data.analysis.confirmed_plan);
      setMessages((current) => [
        ...current.map((message) => {
          const nextMessage = message.id === pendingMessageId
            ? { ...message, delivery: undefined }
            : message;
          return confirmationCompleted && message.plan === options.pendingPlan
            ? { ...nextMessage, planConfirmed: true }
            : nextMessage;
        }),
        {
          id: assistantMessageId,
          role: "assistant",
          content: assistantReply,
          process: finalProgressEvents,
          plan: data.analysis.pending_plan ?? undefined,
          confirmedPlan: data.analysis.confirmed_plan ?? undefined,
          searchResults: data.analysis.search_results,
        },
      ]);
      setConversationId(data.conversation_id);
      setAnalysis(data.analysis);
      if (data.analysis.requirements) setTripContext(data.analysis);
      setShortTermMemory(data.short_term_memory);
    } catch (requestError) {
      setMessages((current) => current.map((message) => (
        message.id === pendingMessageId ? { ...message, delivery: "failed" } : message
      )));
      setError(requestError instanceof Error ? requestError.message : "暂时无法获取回复。");
    } finally {
      isPolling = false;
      if (progressTimer !== undefined) window.clearInterval(progressTimer);
      setIsSending(false);
    }
  }

  async function sendUserMessage(content: string, options: { pendingPlan?: TripPlanSnapshot } = {}) {
    if (!content || isSending) return;
    const latestPendingPlan = [...messages]
      .reverse()
      .find((message) => message.plan)?.plan;
    const pendingPlan = options.pendingPlan ?? (
      latestPendingPlan && /确认当前方案|确认方案|就这样|按这个/.test(content)
        ? latestPendingPlan
        : undefined
    );
    const userMessage: Message = {
      id: nextMessageId.current++,
      role: "user",
      content,
      delivery: "pending",
    };
    const nextMessages = [...messages, userMessage];
    setMessages(nextMessages);
    setDraft("");
    await requestAssistantReply(nextMessages, userMessage.id, { pendingPlan });
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await sendUserMessage(draft.trim());
  }

  async function handleQuickFormSubmit() {
    await sendUserMessage(buildQuickRequirementMessage(quickForm));
  }

  async function handleRetry() {
    if (!failedMessage || isSending) return;
    const retryMessages = messages.map((message) => (
      message.id === failedMessage.id ? { ...message, delivery: "pending" as const } : message
    ));
    setMessages(retryMessages);
    await requestAssistantReply(retryMessages, failedMessage.id);
  }

  function handleNewSession() {
    setMessages([]);
    setDraft("");
    setError("");
    setAnalysis(null);
    setTripContext(null);
    setShortTermMemory(null);
    setLiveProgressEvents([]);
    liveProgressEventsRef.current = [];
    setConversationId(null);
    setQuickForm(createEmptyQuickForm());
    setIsQuickFormOpen(true);
  }

  function updateMessagePlan(messageId: number, plan: TripPlanSnapshot) {
    setMessages((current) => current.map((message) => (
      message.id === messageId ? { ...message, plan } : message
    )));
    if (analysis?.pending_plan) {
      setAnalysis({
        ...analysis,
        pending_plan: plan,
      });
    }
  }

  function toggleInterest(interest: string) {
    setQuickForm((current) => ({
      ...current,
      interests: current.interests.includes(interest)
        ? current.interests.filter((item) => item !== interest)
        : [...current.interests, interest],
    }));
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true"><span>T</span></div>
          <div>
            <strong>TripWeave</strong>
            <span>旅差智能助手</span>
          </div>
        </div>

        <button className="new-session-button" type="button" onClick={handleNewSession}>
          <span aria-hidden="true">+</span>
          新建对话
        </button>

        <nav className="sidebar-nav" aria-label="工作台导航">
          <span className="nav-item is-active"><i aria-hidden="true" />旅差工作台</span>
          <span className="nav-item"><i aria-hidden="true" />对话记录</span>
        </nav>

        <section className="sidebar-section trip-status">
          <div className="section-heading">
            <h2>当前行程</h2>
            <span className={conversationId ? "saved-state" : "draft-state"}>
              {conversationId ? "已保存" : "草稿"}
            </span>
          </div>
          <p className="trip-title">
            {activeRequirements?.destination ? `${activeRequirements.destination} 行程` : "等待你的出行计划"}
          </p>
          <div className="requirement-list">
            <RequirementItem label="出发" value={activeRequirements?.origin ?? "待补充"} />
            <RequirementItem label="目的地" value={activeRequirements?.destination ?? "待补充"} required={missingFields.has("destination")} />
            <RequirementItem
              label="日期"
              value={activeRequirements?.departure_date ? `${displayDate(activeRequirements.departure_date)} - ${displayDate(activeRequirements.return_date)}` : activeRequirements?.trip_duration?.raw_text ?? "待补充"}
              required={missingFields.has("departure_date") || missingFields.has("trip_schedule")}
            />
            <RequirementItem
              label="同行"
              value={activeRequirements?.traveler_count ? `${activeRequirements.traveler_count} 人` : "待补充"}
              required={missingFields.has("traveler_count")}
            />
          </div>
        </section>

        <section className="sidebar-section service-note">
          <span className="section-kicker">服务说明</span>
          <p>酒店和交通结果为查询参考，确认方案不包含购票、订房或支付。</p>
        </section>

        <footer className="sidebar-footer">
          <strong>TripWeave 旅程智能助手 v1.0.0</strong>
          <span>
            版权归属{" "}
            <a href="https://github.com/GuLu37/TripWeave" target="_blank" rel="noreferrer">
              GuLu37/TripWeave
            </a>
          </span>
        </footer>
      </aside>

      <section className="workspace">
        <header className="topbar">
          <div>
            <span className="section-kicker">{readableIntent(analysis?.intent ?? tripContext?.intent)}</span>
            <h1>{activeRequirements?.destination ? `${activeRequirements.destination} 旅差安排` : "开始一段更从容的行程"}</h1>
          </div>
          <span className="connection-status"><i />对话服务在线</span>
        </header>

        <div className="workspace-body">
          <section className="chat-panel" aria-label="TripWeave 对话">
            <div className="messages" aria-live="polite">
              {messages.length === 0 && (
                <section className="welcome-state">
                  <span className="section-kicker">TRIPWEAVE</span>
                  <h2>把出差说出来，剩下的交给我。</h2>
                  <p>描述目的地、时间和同行人数；也可以直接查询酒店或城际交通。</p>
                  <div className="starter-list">
                    {STARTER_PROMPTS.map((prompt) => (
                      <button key={prompt} type="button" onClick={() => void sendUserMessage(prompt)} disabled={isSending}>
                        {prompt}
                      </button>
                    ))}
                  </div>
                </section>
              )}

              {messages.map((message) => (
                <Fragment key={message.id}>
                  <article className={`message ${message.role} ${message.delivery ?? ""}`}>
                    <div className="avatar" aria-hidden="true">{message.role === "assistant" ? <RobotIcon /> : "我"}</div>
                    <div className="message__body">
                      <div className="message__meta">
                        <span>{message.role === "assistant" ? "TripWeave" : "你"}</span>
                        {message.delivery === "failed" && <b>未送达</b>}
                      </div>
                      <p>{message.content}</p>
                      {message.role === "assistant" && message.process && message.process.length > 0 && (
                        <AgentTrace events={message.process} />
                      )}
                    </div>
                  </article>
                  {message.plan && (
                    <PlanSnapshotView
                      plan={message.plan}
                      onConfirm={() => void sendUserMessage("确认当前方案", { pendingPlan: message.plan })}
                      onPlanChange={(plan) => updateMessagePlan(message.id, plan)}
                      disabled={isSending}
                      isConfirmed={Boolean(message.planConfirmed)}
                      searchResults={message.searchResults ?? {}}
                      anchorId={`plan-${message.id}-pending`}
                    />
                  )}
                  {message.confirmedPlan && (
                    <ConfirmedPlanView
                      plan={message.confirmedPlan}
                      searchResults={message.searchResults ?? {}}
                      anchorId={`plan-${message.id}-confirmed`}
                    />
                  )}
                </Fragment>
              ))}

              {isSending && (
                <article className="message assistant">
                  <div className="avatar" aria-hidden="true"><RobotIcon /></div>
                  <div className="message__body">
                    <div className="message__meta"><span>TripWeave</span></div>
                    <div className="thinking-status">
                      <i />
                      <span>
                        {activeProgressEvent
                          ? `${activeProgressEvent.agent} 正在${activeProgressEvent.action}`
                          : "正在连接处理服务"}
                      </span>
                    </div>
                    {liveProgressEvents.length > 0 && <AgentTrace events={liveProgressEvents} isLive />}
                  </div>
                </article>
              )}

              <div ref={messagesEndRef} />
            </div>

            <form className="composer" onSubmit={handleSubmit}>
              {error && (
                <div className="error-row" role="alert">
                  <p>{error}</p>
                  <button type="button" onClick={() => void handleRetry()} disabled={!failedMessage || isSending}>重试</button>
                </div>
              )}
              <textarea
                aria-label="输入消息"
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    if (draft.trim()) void sendUserMessage(draft.trim());
                  }
                }}
                placeholder="描述你的出行需求，或直接问酒店和交通..."
                rows={2}
                disabled={isSending}
              />
              <div className="composer-footer">
                <span>Enter 发送，Shift + Enter 换行</span>
                <button className="send-button" type="submit" disabled={!draft.trim() || isSending}>
                  发送
                </button>
              </div>
            </form>
          </section>

          <aside className="right-rail">
            <PlanLibrary
              items={planListItems}
              onSelect={(itemId) => document.getElementById(itemId)?.scrollIntoView({
                behavior: "smooth",
                block: "start",
              })}
            />
            <section className={`rail-section planning-section ${isQuickFormOpen ? "is-expanded" : ""}`}>
              <div className="section-heading">
                <div>
                  <span className="section-kicker">快捷录入</span>
                  <h2>快捷填写旅程信息</h2>
                </div>
                <button className="collapse-button" type="button" onClick={() => setIsQuickFormOpen((current) => !current)} aria-expanded={isQuickFormOpen}>
                  <span aria-hidden="true">{isQuickFormOpen ? "-" : "+"}</span>
                </button>
              </div>
              {isQuickFormOpen && (
                <div className="quick-form">
                  <div className="field-grid">
                    <label className={missingFields.has("destination") ? "is-required" : ""}>
                      目的地
                      <input value={quickForm.destination} onChange={(event) => setQuickForm((current) => ({ ...current, destination: event.target.value }))} placeholder="北京" />
                    </label>
                    <label>
                      出发地
                      <input value={quickForm.origin} onChange={(event) => setQuickForm((current) => ({ ...current, origin: event.target.value }))} placeholder="广州" />
                    </label>
                    <label className={missingFields.has("departure_date") ? "is-required" : ""}>
                      出发日期
                      <DateField
                        value={quickForm.departureDate}
                        onChange={(departureDate) => setQuickForm((current) => ({ ...current, departureDate }))}
                      />
                    </label>
                    <label>
                      返程日期
                      <DateField
                        value={quickForm.returnDate}
                        min={quickForm.departureDate || undefined}
                        onChange={(returnDate) => setQuickForm((current) => ({ ...current, returnDate }))}
                      />
                    </label>
                    <label className={missingFields.has("traveler_count") ? "is-required" : ""}>
                      同行人数
                      <input type="number" min="1" max="100" value={quickForm.travelerCount} onChange={(event) => setQuickForm((current) => ({ ...current, travelerCount: event.target.value }))} placeholder="2" />
                    </label>
                    <label>
                      预算
                      <input type="number" min="0" value={quickForm.budget} onChange={(event) => setQuickForm((current) => ({ ...current, budget: event.target.value }))} placeholder="3000" />
                    </label>
                  </div>
                  <div className="form-row">
                    <span className="field-label">预算口径</span>
                    <div className="segmented-control">
                      <button type="button" className={quickForm.budgetScope === "per_person" ? "is-active" : ""} onClick={() => setQuickForm((current) => ({ ...current, budgetScope: "per_person" }))}>每人</button>
                      <button type="button" className={quickForm.budgetScope === "total" ? "is-active" : ""} onClick={() => setQuickForm((current) => ({ ...current, budgetScope: "total" }))}>总额</button>
                    </div>
                  </div>
                  <div className="form-row">
                    <span className="field-label">交通偏好</span>
                    <div className="choice-group">
                      {TRANSPORT_OPTIONS.map((option) => (
                        <button key={option} type="button" className={quickForm.transport === option ? "is-active" : ""} onClick={() => setQuickForm((current) => ({ ...current, transport: current.transport === option ? "" : option }))}>{option}</button>
                      ))}
                    </div>
                  </div>
                  <div className="form-row">
                    <span className="field-label">兴趣偏好</span>
                    <div className="choice-group">
                      {INTEREST_OPTIONS.map((interest) => (
                        <button key={interest} type="button" className={quickForm.interests.includes(interest) ? "is-active" : ""} onClick={() => toggleInterest(interest)}>{interest}</button>
                      ))}
                    </div>
                  </div>
                  <button className="primary-button full-width" type="button" onClick={() => void handleQuickFormSubmit()} disabled={!buildQuickRequirementMessage(quickForm) || isSending}>
                    提交行程信息
                  </button>
                </div>
              )}
            </section>
          </aside>
        </div>
      </section>
    </main>
  );
}

export default App;
