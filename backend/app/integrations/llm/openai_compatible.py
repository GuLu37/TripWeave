"""OpenAI 兼容聊天接口的公共调用逻辑。"""

import json
import logging

import httpx

from app.agents.prompts import load_prompt
from app.api.exception.exceptions import ModelException
from app.integrations.llm.log_safety import redact_debug_text
from app.schemas import ChatMessage, ClientChatMessage

logger = logging.getLogger(__name__)


async def request_openai_compatible_chat(
    provider: str,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    messages: list[ClientChatMessage],
    system_prompt: str | None,
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
    debug_log_raw_output: bool = False,
) -> str:
    """调用 OpenAI 兼容聊天接口，并返回助手文本回复。"""

    # 三类供应商共享此入口；先只校验当前实际要调用的供应商配置。
    missing_settings = [
        setting_name
        for setting_name, value in {
            "API_KEY": api_key,
            "BASE_URL": base_url,
            "MODEL": model,
        }.items()
        if not value
    ]
    if missing_settings:
        # 配置缺失不可通过网络重试修复，交给上层决定是否切换备用供应商。
        raise ModelException.config_missing(provider, "、".join(missing_settings))

    # 第一步：组合全局边界与可选的 Agent 专用指令，二者均以 system 角色发送。
    global_system_prompt = load_prompt("global_system_prompt.md")
    combined_system_prompt = "\n\n".join(
        prompt
        for prompt in (global_system_prompt, system_prompt.strip() if system_prompt else "")
        if prompt
    )
    # 第二步：后端始终把组合后的系统提示词放在第一条，前端提交的历史不能覆盖边界。
    conversation = [
        ChatMessage(
            role="system",
            content=combined_system_prompt,
        ),
        *[
            ChatMessage(role=message.role, content=message.content)
            for message in messages
        ],
    ]
    # 按 OpenAI Chat Completions 协议构造统一请求体，便于复用到 DeepSeek 和中转站。
    payload = {
        "model": model,
        "messages": [message.model_dump() for message in conversation],
        "temperature": temperature,
    }
    if max_tokens is not None:
        # 第三步：仅在调用方指定时限制输出长度，控制结构化调用的响应规模与成本。
        payload["max_tokens"] = max_tokens
    if json_mode:
        # 第四步：请求 OpenAI 兼容供应商强制输出 JSON 对象，降低结构化 Agent 的格式漂移概率。
        payload["response_format"] = {"type": "json_object"}
    # API Key 仅放入请求头，不写入日志、错误详情或响应体。
    headers = {"Authorization": f"Bearer {api_key}"}
    url = f"{base_url.rstrip('/')}/chat/completions"

    try:
        # 每次调用单独创建异步客户端，60 秒上限同时覆盖连接、读取和写入等待。
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        # 限流和服务端错误通常短暂可恢复；认证、参数等其他 4xx 不重试。
        raise ModelException.api_rejected(
            provider,
            error.response.text or None,
            retryable=error.response.status_code == 429
            or error.response.status_code >= 500,
        ) from error
    except httpx.HTTPError as error:
        # DNS、连接和超时等 httpx 异常统一视为可恢复的不可达故障。
        raise ModelException.api_unreachable(provider) from error

    try:
        # 第五步：解析上游 JSON；非 JSON 的 2xx 响应同样视为可恢复的协议异常。
        data = response.json()
    except (ValueError, TypeError) as error:
        raise _invalid_upstream_response(
            provider,
            reason="response_not_json",
            status_code=response.status_code,
            request_id=_get_upstream_request_id(response),
            data=response.text,
            debug_log_raw_output=debug_log_raw_output,
            api_key=api_key,
        ) from error

    # 第六步：只接受标准且非空的 choices[0].message.content，避免空正文进入 Agent JSON 解析。
    return _extract_chat_content(
        provider,
        data,
        status_code=response.status_code,
        request_id=_get_upstream_request_id(response),
        debug_log_raw_output=debug_log_raw_output,
        api_key=api_key,
    )


def _extract_chat_content(
    provider: str,
    data: object,
    *,
    status_code: int,
    request_id: str | None,
    debug_log_raw_output: bool,
    api_key: str | None,
) -> str:
    """提取 OpenAI 兼容响应的非空助手正文。"""

    if not isinstance(data, dict):
        # 第一步：顶层非对象不符合 Chat Completions 协议，记录摘要后触发当前供应商重试。
        raise _invalid_upstream_response(
            provider,
            reason="response_root_not_object",
            status_code=status_code,
            request_id=request_id,
            data=data,
            debug_log_raw_output=debug_log_raw_output,
            api_key=api_key,
        )

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        # 第二步：缺少候选结果无法生成回复，保留上游 ID、用量等元数据供排查。
        raise _invalid_upstream_response(
            provider,
            reason="choices_missing_or_empty",
            status_code=status_code,
            request_id=request_id,
            data=data,
            debug_log_raw_output=debug_log_raw_output,
            api_key=api_key,
        )

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        # 第三步：首个候选不是对象时拒绝继续索引，避免兼容层异常掩盖真实响应形态。
        raise _invalid_upstream_response(
            provider,
            reason="first_choice_not_object",
            status_code=status_code,
            request_id=request_id,
            data=data,
            debug_log_raw_output=debug_log_raw_output,
            api_key=api_key,
        )

    message = first_choice.get("message")
    if not isinstance(message, dict):
        # 第四步：消息对象缺失时无法读取 content，统一归类为可重试坏响应。
        raise _invalid_upstream_response(
            provider,
            reason="message_missing_or_not_object",
            status_code=status_code,
            request_id=request_id,
            data=data,
            debug_log_raw_output=debug_log_raw_output,
            api_key=api_key,
        )

    content = message.get("content")
    if not isinstance(content, str):
        # 第五步：content 为 null、数组或缺失时不能交给下游文本清洗器猜测。
        raise _invalid_upstream_response(
            provider,
            reason="content_missing_or_not_string",
            status_code=status_code,
            request_id=request_id,
            data=data,
            debug_log_raw_output=debug_log_raw_output,
            api_key=api_key,
        )

    normalized_content = content.strip()
    if not normalized_content:
        # 第六步：HTTP 200 的空或纯空白 content 视为可恢复坏响应，直接触发同供应商重试。
        raise _invalid_upstream_response(
            provider,
            reason="content_empty_or_whitespace",
            status_code=status_code,
            request_id=request_id,
            data=data,
            debug_log_raw_output=debug_log_raw_output,
            api_key=api_key,
        )
    return normalized_content


def _invalid_upstream_response(
    provider: str,
    *,
    reason: str,
    status_code: int,
    request_id: str | None,
    data: object,
    debug_log_raw_output: bool,
    api_key: str | None,
) -> ModelException:
    """记录上游异常响应摘要，并创建可重试的模型异常。"""

    # 第一步：记录协议字段和长度等安全摘要，默认不包含上游模型正文。
    metadata = _build_upstream_response_metadata(
        data,
        reason=reason,
        status_code=status_code,
        request_id=request_id,
    )
    logger.warning(
        "上游 LLM 响应无可用正文：provider=%s metadata=%s",
        provider,
        json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
    )
    if debug_log_raw_output:
        # 第二步：开发排障时输出完整上游 JSON 或原始文本，并使用当前 API Key 进行脱敏。
        logger.warning(
            "LLM 调试上游原始响应（仅上游异常）：provider=%s response=%s",
            provider,
            redact_debug_text(_serialize_upstream_data(data), (api_key,)),
        )
    # 第三步：空正文和协议漂移多为瞬态问题，交由上层执行当前供应商重试与备用切换。
    return ModelException.bad_response(provider, retryable=True)


def _build_upstream_response_metadata(
    data: object,
    *,
    reason: str,
    status_code: int,
    request_id: str | None,
) -> dict[str, object]:
    """提取不含模型正文的上游响应诊断摘要。"""

    # 第一步：仅在标准对象结构下读取字段，异常形态全部保留为 None，避免日志函数再抛错误。
    payload = data if isinstance(data, dict) else {}
    choices = payload.get("choices")
    choice_count = len(choices) if isinstance(choices, list) else None
    first_choice = choices[0] if isinstance(choices, list) and choices else {}
    first_choice = first_choice if isinstance(first_choice, dict) else {}
    message = first_choice.get("message")
    message = message if isinstance(message, dict) else {}
    content = message.get("content")
    reasoning_content = message.get("reasoning_content")
    tool_calls = message.get("tool_calls")

    # 第二步：输出请求关联标识、结束原因、内容长度和用量，不写入模型正文或用户输入。
    return {
        "reason": reason,
        "http_status_code": status_code,
        "upstream_request_id": request_id,
        "upstream_response_id": payload.get("id"),
        "model": payload.get("model"),
        "choice_count": choice_count,
        "finish_reason": first_choice.get("finish_reason"),
        "content_type": type(content).__name__ if content is not None else None,
        "content_chars": len(content) if isinstance(content, str) else None,
        "content_trimmed_chars": len(content.strip())
        if isinstance(content, str)
        else None,
        "reasoning_content_chars": len(reasoning_content)
        if isinstance(reasoning_content, str)
        else None,
        "has_tool_calls": bool(tool_calls),
        "usage": payload.get("usage"),
    }


def _serialize_upstream_data(data: object) -> str:
    """将上游 JSON 或文本转换为可写入调试日志的字符串。"""

    # 第一步：优先保留 JSON 的缩进结构，便于开发者检查 choices、message 和 finish_reason 字段。
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)
    except (TypeError, ValueError):
        # 第二步：遇到非常规对象时回退到字符串表示，确保诊断日志不会遮蔽原始异常。
        return str(data)


def _get_upstream_request_id(response: httpx.Response) -> str | None:
    """从兼容供应商响应头读取可用于工单排查的请求标识。"""

    # 第一步：兼容常见的两种请求 ID 头名称，不记录认证头等敏感请求信息。
    return response.headers.get("x-request-id") or response.headers.get("request-id")
