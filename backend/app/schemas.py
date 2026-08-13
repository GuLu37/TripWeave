"""聊天接口的 Pydantic 数据契约。"""

from typing import Literal

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """浏览器当前会话中的一条消息。"""

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=12_000)


class ChatRequest(BaseModel):
    """聊天 Demo 接收的请求数据。"""

    messages: list[ChatMessage] = Field(min_length=1, max_length=40)


class ChatResponse(BaseModel):
    """聊天 Demo 返回的助手回复。"""

    message: ChatMessage


class HealthResponse(BaseModel):
    """健康检查接口返回的服务状态。"""

    service: str
    status: Literal["ok"]
    version: str

