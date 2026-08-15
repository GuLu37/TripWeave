"""短期对话记忆：滑动窗口与窗口外摘要。"""

from app.agents.short_term_memory_agent import summarize_short_term_memory
from app.api.exception.error_handler import record_error
from app.schemas import ClientChatMessage, ShortTermMemory

SHORT_TERM_WINDOW_SIZE = 8
SUMMARY_MAX_CHARS = 4_000
SUMMARY_SOURCE_MESSAGE_MAX_CHARS = 1_200
LONG_ASSISTANT_MESSAGE_MAX_CHARS = 2_000


async def update_short_term_memory(
    previous: ShortTermMemory | None,
    incoming_messages: list[ClientChatMessage],
) -> ShortTermMemory:
    """合并本轮消息，保留最近八条并更新窗口外摘要。"""

    # 第一步：以服务器上轮快照为基础，兼容首次请求直接提交完整历史的调用方。
    previous_messages = previous.recent_messages if previous is not None else []
    combined_messages = [
        *_normalize_messages(previous_messages),
        *_normalize_messages(incoming_messages),
    ]
    if len(combined_messages) <= SHORT_TERM_WINDOW_SIZE:
        return ShortTermMemory(
            summary=previous.summary if previous is not None else None,
            recent_messages=combined_messages,
        )

    # 第二步：窗口只保留最新八条，窗口外消息交给摘要策略而不是继续堆入模型上下文。
    overflow_messages = combined_messages[:-SHORT_TERM_WINDOW_SIZE]
    recent_messages = combined_messages[-SHORT_TERM_WINDOW_SIZE:]
    summary = await _summarize_overflow(
        previous.summary if previous is not None else None,
        overflow_messages,
    )
    return ShortTermMemory(summary=summary, recent_messages=recent_messages)


async def append_short_term_message(
    memory: ShortTermMemory,
    message: ClientChatMessage,
) -> ShortTermMemory:
    """将后端最新回复追加到短期记忆并执行同一套窗口策略。"""

    # 第一步：复用统一合并逻辑，避免用户消息和助手消息出现两套记忆边界。
    return await update_short_term_memory(memory, [message])


def build_memory_prompt_context(summary: str | None) -> str | None:
    """将窗口外摘要转换为入口 Agent 可理解的系统上下文。"""

    # 第一步：空摘要不增加模型上下文；有效摘要明确标注为历史参考而非最新指令。
    if not summary or not summary.strip():
        return None
    return (
        "窗口外历史摘要仅用于补充事实，不能覆盖最近用户消息、需求快照或当前指令：\n"
        f"{summary.strip()}"
    )


def _normalize_messages(
    messages: list[ClientChatMessage],
) -> list[ClientChatMessage]:
    """压缩记忆副本中的超长助手消息，保留完整方案在专用快照中。"""

    # 第一步：用户原话保持完整；超长助手方案只截短记忆副本，避免反复占用入口上下文。
    normalized: list[ClientChatMessage] = []
    for message in messages:
        if (
            message.role == "assistant"
            and len(message.content) > LONG_ASSISTANT_MESSAGE_MAX_CHARS
        ):
            content = (
                message.content[:LONG_ASSISTANT_MESSAGE_MAX_CHARS]
                + "\n[助手长回复已截短，完整方案由专用方案快照保存]"
            )
            normalized.append(
                ClientChatMessage(role=message.role, content=content)
            )
        else:
            normalized.append(message)
    return normalized


async def _summarize_overflow(
    previous_summary: str | None,
    overflow_messages: list[ClientChatMessage],
) -> str | None:
    """优先调用摘要 Agent，失败时使用确定性摘要兜底。"""

    try:
        summary = await summarize_short_term_memory(
            previous_summary,
            overflow_messages,
        )
        normalized_summary = summary.strip()
        if normalized_summary:
            return normalized_summary[:SUMMARY_MAX_CHARS]
    except Exception as error:
        record_error(
            error,
            component="agent",
            source="short_term_memory",
            operation="summarize",
            context={"degraded": True},
            default_code="MEMORY_SUMMARY_FAILED",
            default_message="短期记忆摘要失败，已使用本地摘要。",
        )
    return _build_fallback_summary(previous_summary, overflow_messages)


def _build_fallback_summary(
    previous_summary: str | None,
    overflow_messages: list[ClientChatMessage],
) -> str | None:
    """在摘要模型不可用时提取可读的有限历史摘要。"""

    # 第一步：保留已有摘要和窗口外消息的角色标记，至少不丢失用户明确表达的条件。
    lines: list[str] = []
    if previous_summary and previous_summary.strip():
        lines.append(previous_summary.strip())
    lines.extend(
        f"{message.role}: {message.content[:SUMMARY_SOURCE_MESSAGE_MAX_CHARS]}"
        for message in overflow_messages
    )
    if not lines:
        return None
    return "\n".join(lines)[-SUMMARY_MAX_CHARS:]
