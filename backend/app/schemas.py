"""TripWeave 的 Pydantic 数据契约。"""

from typing import Literal

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


class ChatRequest(BaseModel):
    """聊天接口接收的请求。"""

    messages: list[ClientChatMessage] = Field(min_length=1, max_length=40)
    # 浏览器保存的上轮已确认需求，用于在截短历史后维持旅差上下文。
    known_requirements: TripRequirements | None = None


class ConversationAnalysis(BaseModel):
    """统一对话入口 Agent 的结构化输出。"""

    intent: Literal["chat", "trip_planning"]
    reply: str = Field(min_length=1, max_length=4_000)
    requirements: TripRequirements | None = None
    # 以下两项由入口 Agent 根据 requirements 的本地规则计算，不依赖模型判断。
    missing_fields: list[str] = Field(default_factory=list)
    is_complete: bool | None = None

    @model_validator(mode="after")
    def validate_intent_requirements(self) -> "ConversationAnalysis":
        """确保意图与旅行需求对象保持一致。"""

        # 第一步：普通聊天不得混入行程需求，避免下游误进入规划分支。
        if self.intent == "chat" and self.requirements is not None:
            raise ValueError("chat 意图不能包含 requirements。")
        # 第二步：旅差规划必须带结构化需求，缺失时交由入口 Agent 触发重试。
        if self.intent == "trip_planning" and self.requirements is None:
            raise ValueError("trip_planning 意图必须包含 requirements。")
        return self


class ChatResponse(BaseModel):
    """聊天接口返回的统一入口分析结果。"""

    # analysis.reply 同时是前端展示的助手回复和后续工作流的唯一回复来源。
    analysis: ConversationAnalysis
