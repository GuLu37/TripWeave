"""OpenAI 聊天接口客户端。"""

from app.core.settings import Settings
from app.integrations.llm.openai_compatible import request_openai_compatible_chat
from app.schemas import ClientChatMessage


async def chat_with_openai(
    settings: Settings,
    messages: list[ClientChatMessage],
    system_prompt: str | None,
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
) -> str:
    """使用 OpenAI 配置生成一条助手文本回复。"""

    # OpenAI 的网络调用、提示词注入和错误分类全部复用公共兼容客户端。
    return await request_openai_compatible_chat(
        provider="OpenAI",
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        model=settings.openai_model,
        messages=messages,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        debug_log_raw_output=settings.llm_debug_log_raw_output,
    )
