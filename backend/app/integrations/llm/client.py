"""按当前配置分发大模型调用。"""

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable

from app.api.exception.exceptions import ModelException
from app.core.settings import Settings, get_settings
from app.integrations.llm.log_safety import redact_debug_text
from app.integrations.llm.openai_compatible import request_openai_compatible_chat
from app.schemas import ClientChatMessage

logger = logging.getLogger(__name__)
# 结构化调用首次输出未通过契约时，补充固定约束后再请求同一供应商，避免偶发格式漂移直接耗尽兜底链路。
STRUCTURED_OUTPUT_RETRY_INSTRUCTION = (
    "你上一轮输出未通过后端结构化校验。"
    "请严格遵守本次任务的 JSON 契约：只返回单个合法 JSON 对象，"
    "字段名和字段类型必须完全匹配，不要输出 Markdown、解释或额外文本。"
)
# Agent 可传入响应校验器，将结构化输出错误纳入供应商兜底策略。
ResponseValidator = Callable[[str], object]
# 三家供应商都使用同一兼容协议，差异仅保留展示名称和对应配置字段。
_PROVIDER_SETTINGS: dict[str, tuple[str, str, str, str]] = {
    "deepseek": (
        "DeepSeek",
        "deepseek_api_key",
        "deepseek_base_url",
        "deepseek_model",
    ),
    "openai": (
        "OpenAI",
        "openai_api_key",
        "openai_base_url",
        "openai_model",
    ),
    "proxy": (
        "第三方中转站",
        "proxy_api_key",
        "proxy_base_url",
        "proxy_model",
    ),
}


async def chat_with_llm(
    messages: list[ClientChatMessage],
    *,
    system_prompt: str | None = None,
    response_validator: ResponseValidator | None = None,
    temperature: float = 0.7,
    max_tokens: int | None = None,
    max_attempts: int | None = None,
    json_mode: bool = False,
    disable_thinking: bool = False,
    provider_override: str | None = None,
    model_override: str | None = None,
    enable_thinking: bool = False,
    caller_name: str = "unspecified",
) -> str:
    """按优先级调用大模型，并在可恢复失败时进行重试与兜底。"""

    settings = get_settings()
    attempt_limit = max_attempts or settings.llm_max_retries
    # 主供应商排在第一位；特定 Agent 可固定供应商，避免模型覆盖错误发往其他兼容接口。
    providers = (
        [provider_override.strip().lower()]
        if provider_override and provider_override.strip()
        else _get_provider_sequence(settings)
    )
    # 汇总无敏感信息的失败记录，所有备用项耗尽后返回给统一异常处理器。
    failures: list[dict[str, object]] = []

    for provider in providers:
        if provider not in _PROVIDER_SETTINGS:
            # 未支持的名称不发起请求，记录后继续尝试后续备用项。
            error = ModelException.provider_unsupported(provider)
            failures.append(_failure_record(provider, 0, error))
            logger.warning(
                "跳过不支持的 LLM 供应商：provider=%s",
                provider,
            )
            continue

        try:
            # 当前供应商内部先完成有限重试，成功后立即结束整个兜底链路。
            return await _call_with_retries(
                provider,
                settings,
                messages,
                failures,
                system_prompt,
                response_validator,
                temperature,
                max_tokens,
                attempt_limit,
                json_mode,
                caller_name,
                disable_thinking,
                model_override,
                enable_thinking,
            )
        except ModelException as error:
            if error.fallbackable:
                # 可切换错误交由后续备用供应商继续处理，避免单个供应商阻断所有 Agent。
                logger.warning(
                    "切换备用 LLM 供应商：provider=%s code=%s",
                    provider,
                    error.code,
                )
                continue
            # 认证失败、无效请求和未知业务错误不切换，避免掩盖真实配置问题。
            raise

    # 所有候选都失败时提供统一错误码，details 仅描述失败链路。
    raise ModelException.fallback_exhausted(failures)


async def _request_configured_provider(
    provider: str,
    settings: Settings,
    messages: list[ClientChatMessage],
    system_prompt: str | None,
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
    disable_thinking: bool = False,
    model_override: str | None = None,
    enable_thinking: bool = False,
) -> str:
    """按供应商配置调用同一 OpenAI 兼容客户端。"""

    # 第一步：供应商名称已由外层注册表校验，读取其唯一对应的连接配置字段。
    display_name, api_key_field, base_url_field, model_field = _PROVIDER_SETTINGS[
        provider
    ]
    # 第二步：只在此处完成字段映射，重试、提示词与协议校验继续由既有公共模块负责。
    return await request_openai_compatible_chat(
        provider=display_name,
        api_key=getattr(settings, api_key_field),
        base_url=getattr(settings, base_url_field),
        model=model_override or getattr(settings, model_field),
        messages=messages,
        system_prompt=system_prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        debug_log_raw_output=settings.llm_debug_log_raw_output,
        # 第三步：DeepSeek 的结构化调用默认关闭思考，指定 Agent 也可显式关闭以保证正文输出。
        disable_thinking=provider == "deepseek"
        and not enable_thinking
        and (json_mode or disable_thinking),
        enable_thinking=provider == "deepseek" and enable_thinking,
    )


def _get_provider_sequence(settings: Settings) -> list[str]:
    """按主供应商和备用供应商配置生成去重后的调用顺序。"""

    # 主供应商即使也被写入备用列表也只会调用一次，防止重复消耗重试次数。
    providers = [
        settings.llm_provider.strip().lower(),
        *settings.fallback_providers,
    ]
    return list(dict.fromkeys(provider for provider in providers if provider))


async def _call_with_retries(
    provider: str,
    settings: Settings,
    messages: list[ClientChatMessage],
    failures: list[dict[str, object]],
    system_prompt: str | None,
    response_validator: ResponseValidator | None,
    temperature: float,
    max_tokens: int | None,
    max_attempts: int,
    json_mode: bool,
    caller_name: str,
    disable_thinking: bool = False,
    model_override: str | None = None,
    enable_thinking: bool = False,
) -> str:
    """对单个供应商进行最多指定次数的可恢复失败重试。"""

    last_error: ModelException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            # 第一步：结构化响应重试时追加格式约束，原始业务提示词与用户历史保持不变。
            effective_system_prompt = _get_effective_system_prompt(
                system_prompt,
                response_validator,
                attempt,
            )
            # 每次尝试都会记录供应商和序号，便于从日志确认实际兜底路径。
            model_name = model_override or getattr(
                settings,
                _PROVIDER_SETTINGS[provider][3],
                None,
            )
            logger.info(
                "调用 LLM：caller=%s provider=%s model=%s attempt=%s/%s message_count=%s "
                "system_prompt_chars=%s json_mode=%s temperature=%s max_tokens=%s "
                "structured_retry=%s",
                caller_name,
                provider,
                model_name,
                attempt,
                max_attempts,
                len(messages),
                len(effective_system_prompt or ""),
                json_mode,
                temperature,
                max_tokens,
                response_validator is not None and attempt > 1,
            )
            # 将 Agent 专用指令作为后端 system prompt 传递，避免被误当作用户历史消息。
            response = await _request_configured_provider(
                provider,
                settings,
                messages,
                effective_system_prompt,
                temperature,
                max_tokens,
                json_mode,
                disable_thinking,
                model_override,
                enable_thinking,
            )
            # 记录响应指纹和形态，不记录原文，便于关联格式失败而不泄露对话内容。
            response_metadata = _response_metadata(response)
            logger.info(
                "收到 LLM 响应：caller=%s provider=%s chars=%s fingerprint=%s shape=%s",
                caller_name,
                provider,
                response_metadata["chars"],
                response_metadata["fingerprint"],
                response_metadata["shape"],
            )
            if response_validator is not None:
                # 校验器由具体 Agent 提供，用于把 JSON 或领域契约失败转为供应商级兜底信号。
                _validate_response(
                    settings,
                    caller_name,
                    provider,
                    response,
                    response_validator,
                    messages=messages,
                    system_prompt=effective_system_prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=json_mode,
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
            # 当前供应商成功返回后记录实际命中的模型与请求次数，便于排查兜底链路。
            logger.info(
                "大模型调用成功：caller=%s provider=%s model=%s 请求次数=%s/%s",
                caller_name,
                provider,
                model_name,
                attempt,
                max_attempts,
            )
            return response
        except ModelException as error:
            last_error = error
            failures.append(_failure_record(provider, attempt, error))
            if not error.retryable:
                # 不可恢复错误立即交回外层，不能因为重试掩盖认证或请求问题。
                raise
            logger.warning(
                "LLM 可恢复调用失败：provider=%s attempt=%s/%s code=%s",
                provider,
                attempt,
                max_attempts,
                error.code,
            )
            if attempt < max_attempts:
                # 使用短线性退避，避免故障瞬间密集重放请求。
                await asyncio.sleep(0.5 * attempt)

    # 循环只会在至少一次调用失败后走到这里，因此 last_error 必然存在。
    assert last_error is not None
    raise last_error


def _validate_response(
    settings: Settings,
    caller_name: str,
    provider: str,
    response: str,
    response_validator: ResponseValidator,
    *,
    messages: list[ClientChatMessage],
    system_prompt: str | None,
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
    attempt: int,
    max_attempts: int,
) -> None:
    """校验模型文本是否满足调用 Agent 的领域契约。"""

    try:
        # 第一步：执行 Agent 提供的校验器，不要求客户端依赖具体领域模型。
        response_validator(response)
    except Exception as error:
        # 第二步：不记录模型原文，避免用户内容或提示词出现在日志中。
        logger.warning(
            "LLM 响应未通过 Agent 契约校验：caller=%s provider=%s error_type=%s",
            caller_name,
            provider,
            type(error).__name__,
        )
        # 第三步：开发者显式开启后记录失败现场，便于定位模型的字段类型或 JSON 格式漂移。
        _log_contract_failure_debug_data(
            settings,
            caller_name,
            provider,
            response,
            messages=messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            attempt=attempt,
            max_attempts=max_attempts,
        )
        # 第四步：契约漂移通常可通过同供应商重新采样修复，耗尽重试后再切换备用供应商。
        raise ModelException.bad_response(provider, retryable=True) from error


def _log_contract_failure_debug_data(
    settings: Settings,
    caller_name: str,
    provider: str,
    response: str,
    *,
    messages: list[ClientChatMessage],
    system_prompt: str | None,
    temperature: float,
    max_tokens: int | None,
    json_mode: bool,
    attempt: int,
    max_attempts: int,
) -> None:
    """按调试开关记录契约失败时的请求上下文和模型原始响应。"""

    # 第一步：兼容测试替身或未来的轻量配置对象缺少新字段时，仍默认不输出原文。
    if getattr(settings, "llm_debug_log_raw_request", False):
        # 第二步：序列化 Agent 实际传入的上下文和生成参数，不包含 HTTP 请求头或供应商配置。
        request_context = {
            "caller": caller_name,
            "provider": provider,
            "attempt": f"{attempt}/{max_attempts}",
            "system_prompt": system_prompt,
            "messages": [message.model_dump() for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "json_mode": json_mode,
        }
        logger.warning(
            "LLM 调试请求上下文（仅契约失败）：%s",
            _redact_debug_text(
                json.dumps(request_context, ensure_ascii=False, indent=2),
                settings,
            ),
        )
    if getattr(settings, "llm_debug_log_raw_output", False):
        # 第三步：输出模型原始文本，保留空格与换行以便复现 JSON 解析和字段类型问题。
        logger.warning(
            "LLM 调试原始响应（仅契约失败）：caller=%s provider=%s attempt=%s/%s "
            "response=%s",
            caller_name,
            provider,
            attempt,
            max_attempts,
            _redact_debug_text(response, settings),
        )


def _redact_debug_text(text: str, settings: Settings) -> str:
    """替换调试文本中的已配置密钥和常见内联凭据。"""

    # 第一步：收集当前进程实际加载的三类供应商密钥，统一交给共享脱敏工具处理。
    return redact_debug_text(
        text,
        (
        settings.deepseek_api_key,
        settings.openai_api_key,
        settings.proxy_api_key,
        ),
    )


def _get_effective_system_prompt(
    system_prompt: str | None,
    response_validator: ResponseValidator | None,
    attempt: int,
) -> str | None:
    """按尝试次数生成本次调用实际使用的系统提示词。"""

    if response_validator is None or attempt == 1:
        # 第一步：非结构化调用和首次结构化调用保持调用方原始提示词不变。
        return system_prompt
    # 第二步：仅对已经发生契约失败的结构化调用追加固定格式提醒，不拼接模型原始输出。
    prompt_parts = [
        prompt
        for prompt in (system_prompt, STRUCTURED_OUTPUT_RETRY_INSTRUCTION)
        if prompt
    ]
    return "\n\n".join(prompt_parts)


def _response_metadata(response: str) -> dict[str, object]:
    """生成不包含模型原文的响应诊断元数据。"""

    # 第一步：计算稳定短指纹，便于在多条日志中关联同一输出而不写入正文。
    fingerprint = hashlib.sha256(response.encode("utf-8")).hexdigest()[:12]
    # 第二步：根据首个非空字符记录响应形态，辅助判断 JSON、代码围栏或普通文本。
    stripped_response = response.lstrip()
    if not stripped_response:
        shape = "empty"
    elif stripped_response.startswith("{"):
        shape = "json_object"
    elif stripped_response.startswith("["):
        shape = "json_array"
    elif stripped_response.startswith("```"):
        shape = "code_fence"
    else:
        shape = "text"
    # 第三步：只返回长度、指纹和形态，避免日志泄露用户对话或模型内容。
    return {
        "chars": len(response),
        "fingerprint": fingerprint,
        "shape": shape,
    }


def _failure_record(
    provider: str,
    attempt: int,
    error: ModelException,
) -> dict[str, object]:
    """生成不包含敏感配置的供应商失败记录。"""

    # 只保留定位兜底路径所需字段，避免 API Key、上游响应内容进入日志或接口响应。
    return {
        "provider": provider,
        "attempt": attempt,
        "code": error.code,
    }
