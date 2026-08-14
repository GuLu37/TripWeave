"""聊天接口路由。"""

from fastapi import APIRouter

from app.workflows.trip_conversation_graph import run_trip_conversation
from app.schemas import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/v1/chat", tags=["对话"])


@router.post("/messages", response_model=ChatResponse)
async def create_chat_message(request: ChatRequest) -> ChatResponse:
    """根据提交的对话历史生成回复与统一入口分析结果。"""

    # 第一步：由 LangGraph 编排入口 Agent 与规划 Agent，按需求完整性决定是否进入规划。
    analysis = await run_trip_conversation(
        request.messages,
        known_requirements=request.known_requirements,
        pending_plan=request.pending_plan,
    )
    # 第二步：analysis.reply 仍是前端展示和后续工作流共用的唯一回复来源。
    return ChatResponse(analysis=analysis)
