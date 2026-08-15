"""短期记忆摘要 Agent。"""

from app.agents.prompts import load_prompt
from app.integrations.llm.client import chat_with_llm
from app.schemas import ClientChatMessage

SUMMARY_SOURCE_MESSAGE_MAX_CHARS = 1_200


async def summarize_short_term_memory(
    previous_summary: str | None,
    overflow_messages: list[ClientChatMessage],
) -> str:
    """调用模型提取窗口外历史摘要。"""

    # 第一步：每条历史只保留有限长度，避免摘要任务自身被长方案占满上下文。
    lines = [
        f"{message.role}: {message.content[:SUMMARY_SOURCE_MESSAGE_MAX_CHARS]}"
        for message in overflow_messages
    ]
    previous = previous_summary.strip() if previous_summary else "无"
    source = f"已有摘要：\n{previous}\n\n新增窗口外消息：\n" + "\n".join(lines)

    # 第二步：摘要 Agent 只输出可恢复事实，不参与主对话意图和旅行需求判断。
    return await chat_with_llm(
        [ClientChatMessage(role="user", content=source)],
        system_prompt=load_prompt("short_term_memory_prompt.md"),
        temperature=0.1,
        max_tokens=600,
        max_attempts=1,
        disable_thinking=True,
        caller_name="short_term_memory",
    )
