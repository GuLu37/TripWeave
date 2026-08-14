"""聊天接口路由。"""

from fastapi import APIRouter

from app.agents.conversation_entry_agent import analyze_conversation
from app.schemas import ChatRequest, ChatResponse

router = APIRouter(prefix="/api/v1/chat", tags=["对话"])


@router.post("/messages", response_model=ChatResponse)
async def create_chat_message(request: ChatRequest) -> ChatResponse:
    """根据提交的对话历史生成回复与统一入口分析结果。"""

    # 第一步：由统一入口 Agent 使用近期消息和已确认需求完成意图识别、回复生成与需求提取。
    analysis = await analyze_conversation(
        request.messages,
        known_requirements=request.known_requirements,
    )
    # 第二步：analysis.reply 是前端展示和后续工作流共用的唯一回复来源。
    return ChatResponse(analysis=analysis)
