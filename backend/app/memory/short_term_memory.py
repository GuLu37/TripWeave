"""本地对话上下文窗口。"""

from app.schemas import ClientChatMessage, ShortTermMemory

SHORT_TERM_WINDOW_SIZE = 8
LONG_ASSISTANT_MESSAGE_MAX_CHARS = 2_000


def update_short_term_memory(
    previous: ShortTermMemory | None,
    incoming_messages: list[ClientChatMessage],
) -> ShortTermMemory:
    """合并本轮消息，只保留最近八条。"""

    # 第一步：以客户端上轮窗口为基础，兼容首次请求直接提交完整历史的调用方。
    previous_messages = previous.recent_messages if previous is not None else []
    combined_messages = [
        *_normalize_messages(previous_messages),
        *_normalize_messages(incoming_messages),
    ]
    # 第二步：旧摘要字段只为兼容客户端保留，当前不再调用模型生成或传递摘要。
    return ShortTermMemory(
        summary=None,
        recent_messages=combined_messages[-SHORT_TERM_WINDOW_SIZE:],
    )


def append_short_term_message(
    memory: ShortTermMemory,
    message: ClientChatMessage,
) -> ShortTermMemory:
    """将后端最新回复追加到上下文窗口。"""

    return update_short_term_memory(memory, [message])


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
