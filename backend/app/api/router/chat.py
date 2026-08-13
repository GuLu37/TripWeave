"""聊天路由和 DeepSeek 模型调用逻辑。"""

import httpx
from fastapi import APIRouter
from pydantic import ValidationError

from app.api.exception.exceptions import AppException
from app.config import get_settings
from app.schemas import ChatMessage, ChatRequest, ChatResponse

router = APIRouter(prefix="/api/v1/chat", tags=["对话"])


def _get_model_settings():
    """读取 .env 中的模型配置，缺少配置时返回中文接口错误。"""

    try:
        return get_settings()
    except ValidationError as error:
        missing_settings = "、".join(
            str(item["loc"][0]).upper() for item in error.errors()
        )
        raise AppException(
            status_code=503,
            code="MODEL_CONFIG_MISSING",
            message=f"backend/.env 缺少或未填写配置：{missing_settings}。",
        ) from error


async def _request_deepseek(messages: list[ChatMessage]) -> str:
    """将对话历史发送至 DeepSeek，并返回文本回复。"""

    settings = _get_model_settings()
    payload = {
        "model": settings.deepseek_model,
        "messages": [message.model_dump() for message in messages],
        "temperature": 0.7,
    }
    headers = {"Authorization": f"Bearer {settings.deepseek_api_key}"}
    url = f"{settings.deepseek_base_url.rstrip('/')}/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        details = error.response.text or None
        raise AppException(
            status_code=502,
            code="MODEL_API_REJECTED",
            message="DeepSeek 拒绝了本次请求。",
            details=details,
        ) from error
    except httpx.HTTPError as error:
        raise AppException(
            status_code=502,
            code="MODEL_API_UNREACHABLE",
            message="暂时无法连接 DeepSeek API。",
        ) from error

    data = response.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (IndexError, KeyError, TypeError, AttributeError) as error:
        raise AppException(
            status_code=502,
            code="MODEL_API_BAD_RESPONSE",
            message="DeepSeek 返回了无法识别的响应。",
        ) from error


@router.post("/messages", response_model=ChatResponse)
async def create_chat_message(request: ChatRequest) -> ChatResponse:
    """根据提交的对话历史生成一条助手回复。"""

    content = await _request_deepseek(request.messages)
    return ChatResponse(message=ChatMessage(role="assistant", content=content))
