"""TripWeave 的 Pydantic 数据契约。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class ChatMessage(BaseModel):
    """后端发送给模型的一条上下文消息。"""

    # system 角色只用于后端构造的上下文；浏览器请求使用 ClientChatMessage。
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=12_000)


class ClientChatMessage(BaseModel):
    """浏览器提交的一条会话消息。"""

    # 禁止客户端提交 system 角色，防止覆盖后端固定注入的全局提示词。
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=12_000)


class ShortTermMemory(BaseModel):
    """浏览器与后端之间传递的本地上下文窗口。"""

    # summary 为旧客户端兼容字段，当前不再生成或传递模型摘要。
    summary: str | None = Field(default=None, max_length=4_000)
    recent_messages: list[ClientChatMessage] = Field(
        default_factory=list,
        max_length=8,
    )


class HealthResponse(BaseModel):
    """健康检查接口返回的服务状态。"""

    service: str
    status: Literal["ok"]
    version: str


class TripDuration(BaseModel):
    """用户表达的旅行时长。"""

    raw_text: str = Field(min_length=1, max_length=100)
    amount: float = Field(gt=0, le=365)
    unit: Literal["hour", "day", "week", "month"]
    is_approximate: bool = False


class TripRequirements(BaseModel):
    """从对话中整理出的旅行需求。"""

    # 未明确的信息保留为 None，后续可由追问结果逐步补齐。
    origin: str | None = None
    destination: str | None = None
    departure_date: str | None = None
    return_date: str | None = None
    trip_duration: TripDuration | None = None
    traveler_count: int | None = Field(default=None, ge=1, le=100)
    budget: str | None = None
    transport_preferences: list[str] = Field(default_factory=list)
    accommodation_preferences: list[str] = Field(default_factory=list)
    dining_preferences: list[str] = Field(default_factory=list)
    attraction_preferences: list[str] = Field(default_factory=list)
    general_preferences: list[str] = Field(default_factory=list)
    fixed_schedule: list[str] = Field(default_factory=list)


class ValidationIssue(BaseModel):
    """确定性规则校验发现的一项问题。"""

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1_000)
    severity: Literal["error", "warning"]
    retryable: bool = False


class ReviewResult(BaseModel):
    """审核总结 Agent 输出的方案状态与用户可读结论。"""

    status: Literal[
        "ready_for_confirmation",
        "needs_replanning",
        "needs_user_decision",
    ]
    summary: str = Field(min_length=1, max_length=4_000)
    risks: list[str] = Field(default_factory=list, max_length=20)
    pending_items: list[str] = Field(default_factory=list, max_length=20)


class TripPlanSnapshot(BaseModel):
    """浏览器在待确认阶段保存并回传的完整方案快照。"""

    requirements: TripRequirements
    proposal: str = Field(min_length=1, max_length=20_000)
    review_result: ReviewResult


class TripImage(BaseModel):
    """确认方案中的 Unsplash 图片。"""

    category: Literal["attraction", "food"]
    query: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2_000)
    thumb_url: str | None = Field(default=None, max_length=2_000)
    alt_text: str | None = Field(default=None, max_length=500)
    photographer: str | None = Field(default=None, max_length=200)
    source_url: str | None = Field(default=None, max_length=2_000)


class TripRouteOption(BaseModel):
    """确认方案中的单种高德路线方式。"""

    mode: Literal["transit", "walking", "driving", "bicycling"]
    mode_label: str
    distance_text: str | None = None
    duration_text: str | None = None


class TripRoute(BaseModel):
    """确认方案中的一段本地路线。"""

    category: Literal["attraction", "food"]
    origin: str
    destination: str
    options: list[TripRouteOption] = Field(default_factory=list, max_length=8)
    unavailable_modes: list[str] = Field(default_factory=list, max_length=8)


class ConfirmedTripDetails(BaseModel):
    """用户确认后供前端展示的图片和路线附加数据。"""

    images: list[TripImage] = Field(default_factory=list, max_length=12)
    routes: list[TripRoute] = Field(default_factory=list, max_length=6)
    tool_status: dict[str, Literal["available", "unavailable", "skipped"]] = Field(
        default_factory=dict,
    )


class ConfirmedTripPlan(TripPlanSnapshot):
    """用户确认后的完整行程方案结果。"""

    confirmed_at: datetime
    details: ConfirmedTripDetails = Field(default_factory=ConfirmedTripDetails)


class ChatRequest(BaseModel):
    """聊天接口接收的请求。"""

    # 首轮为空时由后端创建；后续请求必须回传同一个 ID 以恢复 LangGraph 状态。
    conversation_id: UUID | None = None
    messages: list[ClientChatMessage] = Field(min_length=1, max_length=40)
    # 本地上下文窗口由前端回传，后端每轮只保留最近消息。
    short_term_memory: ShortTermMemory | None = None
    # 浏览器保存的上轮已确认需求，用于在截短历史后维持旅差上下文。
    known_requirements: TripRequirements | None = None
    # 浏览器保存待确认方案，用于下一轮识别确认、修改并向规划 Agent 提供重规划上下文。
    pending_plan: TripPlanSnapshot | None = None


class ConversationAnalysis(BaseModel):
    """统一对话入口 Agent 的结构化输出。"""

    intent: Literal[
        "chat",
        "trip_planning",
        "accommodation_search",
        "intercity_transport_search",
    ]
    reply: str = Field(min_length=1, max_length=4_000)
    requirements: TripRequirements | None = None
    # 由统一入口 Agent 判断当前旅差会话是首次规划、修改方案还是确认方案。
    plan_action: Literal["plan", "modify", "confirm"] | None = None
    # 仅由 LangGraph 写入，前端在下一轮请求中原样提交以恢复待确认状态。
    pending_plan: TripPlanSnapshot | None = None
    # 用户确认成功后写入，后续接入数据库时可作为持久化载荷。
    confirmed_plan: ConfirmedTripPlan | None = None
    # 浏览器查询的原始结构化结果；reply 只负责口语化展示，不再承担解析职责。
    search_results: dict[str, dict[str, object]] = Field(default_factory=dict)
    # 以下两项由入口 Agent 根据 requirements 的本地规则计算，不依赖模型判断。
    missing_fields: list[str] = Field(default_factory=list)
    is_complete: bool | None = None

    @model_validator(mode="after")
    def validate_intent_requirements(self) -> "ConversationAnalysis":
        """确保意图与旅行需求对象保持一致。"""

        # 第一步：普通聊天不得混入行程需求，避免下游误进入规划分支。
        if self.intent == "chat" and self.requirements is not None:
            raise ValueError("chat 意图不能包含 requirements。")
        if self.intent == "chat" and self.plan_action is not None:
            raise ValueError("chat 意图不能包含 plan_action。")
        # 第二步：旅差规划必须带结构化需求，缺失时交由入口 Agent 触发重试。
        if self.intent == "trip_planning" and self.requirements is None:
            raise ValueError("trip_planning 意图必须包含 requirements。")
        # 第三步：直接查询也必须带需求对象，字段暂不完整时用于生成追问。
        if self.intent in {
            "accommodation_search",
            "intercity_transport_search",
        } and self.requirements is None:
            raise ValueError("直接查询意图必须包含 requirements。")
        return self


class ChatResponse(BaseModel):
    """聊天接口返回的统一入口分析结果。"""

    # 对外暴露会话 ID，前端和其他客户端用它关联服务端检查点。
    conversation_id: UUID
    # analysis.reply 同时是前端展示的助手回复和后续工作流的唯一回复来源。
    analysis: ConversationAnalysis
    # 返回更新后的本地上下文窗口，兼容现有前端请求流程。
    short_term_memory: ShortTermMemory
