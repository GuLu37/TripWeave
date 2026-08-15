"""聊天接口路由。"""

from fastapi import APIRouter

from app.memory.short_term_memory import append_short_term_message, update_short_term_memory
from app.workflows.trip_conversation_graph import run_trip_conversation
from app.schemas import ChatRequest, ChatResponse, ClientChatMessage

router = APIRouter(prefix="/api/v1/chat", tags=["对话"])


@router.post("/messages", response_model=ChatResponse)
async def create_chat_message(request: ChatRequest) -> ChatResponse:
    """根据提交的对话历史生成回复与统一入口分析结果。"""

    # 第一步：合并本轮用户消息和上轮短期记忆，形成固定八条的入口上下文。
    short_term_memory = await update_short_term_memory(
        request.short_term_memory,
        request.messages,
    )
    # 第二步：由 LangGraph 编排入口 Agent 与规划 Agent，按需求完整性决定是否进入规划。
    analysis = await run_trip_conversation(
        short_term_memory.recent_messages,
        memory_summary=short_term_memory.summary,
        known_requirements=request.known_requirements,
        pending_plan=request.pending_plan,
    )
    # 第三步：将本轮助手回复写回记忆，确保下一轮仍保留完整的对话角色顺序。
    short_term_memory = await append_short_term_message(
        short_term_memory,
        ClientChatMessage(role="assistant", content=analysis.reply),
    )
    # 第四步：analysis.reply 仍是前端展示和后续工作流共用的唯一回复来源。
    return ChatResponse(
        analysis=analysis,
        short_term_memory=short_term_memory,
    )
