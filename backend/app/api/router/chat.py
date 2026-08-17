"""聊天接口路由。"""

import logging
from uuid import uuid4

from fastapi import APIRouter

from app.memory.short_term_memory import (
    append_short_term_message,
    update_short_term_memory,
)
from app.services.chat_progress import (
    bind_progress,
    finish_progress,
    get_progress,
    start_progress,
)
from app.workflows.trip_conversation_graph import run_trip_conversation
from app.schemas import (
    ChatProgressResponse,
    ChatRequest,
    ChatResponse,
    ClientChatMessage,
)

router = APIRouter(prefix="/api/v1/chat", tags=["对话"])
logger = logging.getLogger(__name__)


@router.get("/progress/{client_request_id}", response_model=ChatProgressResponse)
async def get_chat_progress(client_request_id: str) -> ChatProgressResponse:
    """返回本轮请求实际执行到的工作流与工具状态。"""

    return ChatProgressResponse(**get_progress(client_request_id))


@router.post("/messages", response_model=ChatResponse)
async def create_chat_message(request: ChatRequest) -> ChatResponse:
    """根据提交的对话历史生成回复与统一入口分析结果。"""

    # 第一步：首轮创建会话 ID，后续请求复用它以定位服务端 LangGraph 检查点。
    conversation_id = request.conversation_id or uuid4()
    client_request_id = str(request.client_request_id or uuid4())
    start_progress(client_request_id)
    logger.info(
        "聊天会话开始：conversation_id=%s client_request_id=%s resumed=%s message_count=%s",
        conversation_id,
        client_request_id,
        request.conversation_id is not None,
        len(request.messages),
    )
    # 第二步：保留兼容窗口；服务端 Checkpointer 会在工作流内继续合并窗口外历史。
    short_term_memory = update_short_term_memory(
        request.short_term_memory,
        request.messages,
    )
    # 第三步：由 LangGraph 按 conversation_id 恢复状态并编排入口、规划和审核节点。
    try:
        with bind_progress(client_request_id):
            analysis = await run_trip_conversation(
                short_term_memory.recent_messages,
                conversation_id=str(conversation_id),
                known_requirements=request.known_requirements,
                pending_plan=request.pending_plan,
            )
    finally:
        finish_progress(client_request_id)
    progress_snapshot = get_progress(client_request_id)
    # 第四步：将本轮助手回复写回兼容窗口；服务端历史由工作流同步保存。
    short_term_memory = append_short_term_message(
        short_term_memory,
        ClientChatMessage(role="assistant", content=analysis.reply),
    )
    # 第五步：返回会话 ID、分析结果和短期记忆；完整工作流状态由 Checkpointer 保存。
    return ChatResponse(
        conversation_id=conversation_id,
        analysis=analysis,
        short_term_memory=short_term_memory,
        progress_events=progress_snapshot["events"],
    )
