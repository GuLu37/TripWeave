"""第三方中转站的 OpenAI 兼容聊天接口客户端。"""

from app.core.settings import Settings
from app.integrations.llm.openai_compatible import request_openai_compatible_chat
from app.schemas import ClientChatMessage


async def chat_with_proxy(
    settings: Settings,
    messages: list[ClientChatMessage],
    system_prompt: str | None,
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
) -> str:
    """使用第三方中转站配置生成一条助手文本回复。"""

    # 第三方中转站必须实现 OpenAI Chat Completions 协议，其他细节由其 BASE_URL 决定。
    return await request_openai_compatible_chat(
        provider="第三方中转站",
        api_key=settings.proxy_api_key,
        base_url=settings.proxy_base_url,
        model=settings.proxy_model,
        messages=messages,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        debug_log_raw_output=settings.llm_debug_log_raw_output,
    )
