"""DeepSeek OpenAI 兼容接口客户端。"""

from app.core.settings import Settings
from app.integrations.llm.openai_compatible import request_openai_compatible_chat
from app.schemas import ClientChatMessage


async def chat_with_deepseek(
    settings: Settings,
    messages: list[ClientChatMessage],
    system_prompt: str | None,
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
) -> str:
    """使用 DeepSeek 配置生成一条助手文本回复。"""

    # DeepSeek 使用 OpenAI 兼容协议，只负责提供当前供应商的三项配置。
    return await request_openai_compatible_chat(
        provider="DeepSeek",
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        messages=messages,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        debug_log_raw_output=settings.llm_debug_log_raw_output,
    )
