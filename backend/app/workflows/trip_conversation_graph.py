"""统一入口、旅差规划与审核总结 Agent 的 LangGraph 编排图。"""

import asyncio
import logging
import math
import re
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache, wraps
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from app.agents.conversation_entry_agent import (
    IntentDecision,
    analyze_intent,
    analyze_requirements,
    analyze_search_requirements,
)
from app.agents.execution_agent import collect_trip_information
from app.agents.planning_agent import generate_confirmed_trip_plan, plan_trip
from app.agents.review_agent import review_trip
from app.agents.accommodation_search_agent import search_accommodation
from app.agents.intercity_transport_search_agent import search_intercity_transport
from app.core.settings import get_settings
from app.services.confirmed_trip_service import (
    build_confirmed_trip_details,
    rebuild_city_routes_for_points,
)
from app.services.chat_progress import track_progress
from app.schemas import (
    ClientChatMessage,
    ConfirmedTripDetails,
    ConfirmedTripPlan,
    ConversationAnalysis,
    ReviewResult,
    TripPlanSnapshot,
    TripRequirements,
    ValidationIssue,
)

logger = logging.getLogger(__name__)
MAX_REPLAN_ATTEMPTS = 1
CHECKPOINT_MESSAGE_LIMIT = 120
_CONFIRMED_PENDING_SECTION_PATTERN = re.compile(
    r"(?ms)^\s*#{1,6}\s*(?:待确认事项?|需要确认事项|需确认事项|待核验事项|"
    r"待外部数据核验)\s*[：:]?\s*$.*?(?=^\s*#{1,6}\s|\Z)"
)
_CONFIRMED_PENDING_LINE_PATTERN = re.compile(
    r"(?:待确认|需要确认|需确认|待核验|待外部数据核验|等待确认|请确认)"
)
HOTEL_OFFER_LABELS = (
    ("name", "酒店"),
    ("room_type", "房型"),
    ("price", "价格"),
    ("currency", "币种"),
    ("availability", "库存"),
)
_checkpoint_context = None
_checkpointer: AsyncSqliteSaver | None = None
_graph_initialization_lock = asyncio.Lock()
_CHECKPOINT_ALLOWED_MSGPACK_MODULES = [
    ("app.schemas", "ClientChatMessage"),
    ("app.schemas", "ConfirmedTripDetails"),
    ("app.schemas", "ConfirmedTripPlan"),
    ("app.schemas", "ConversationAnalysis"),
    ("app.schemas", "ReviewResult"),
    ("app.schemas", "TripRoute"),
    ("app.schemas", "TripRouteOption"),
    ("app.schemas", "TripDuration"),
    ("app.schemas", "TripPlanSnapshot"),
    ("app.schemas", "TripRequirements"),
    ("app.schemas", "ValidationIssue"),
    ("app.agents.conversation_entry_agent", "IntentDecision"),
]


class TripConversationState(TypedDict, total=False):
    """入口、规划和审核节点在图中传递的内部状态。"""

    messages: list[ClientChatMessage]
    known_requirements: TripRequirements | None
    pending_plan: TripPlanSnapshot | None
    intent_decision: IntentDecision
    analysis: ConversationAnalysis
    proposal: str
    trip_evidence: dict[str, object]
    prepared_details: ConfirmedTripDetails | None
    accommodation_search: dict[str, object]
    intercity_transport_search: dict[str, object]
    review_result: ReviewResult
    replan_context: dict[str, object] | None
    replan_attempts: int


def _track_agent(
    agent: str,
    action: str,
    *,
    parent_agent: str | None = None,
    resume_action: str | None = None,
):
    """为异步 LangGraph 节点记录真实的开始与结束状态。"""

    def decorator(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            state = args[0] if args else kwargs.get("state")
            resume_latest = _is_review_replan_state(state)
            async with track_progress(
                agent,
                action,
                parent_agent=parent_agent,
                resume_latest=resume_latest,
                resume_action=resume_action,
            ):
                return await function(*args, **kwargs)

        return wrapped

    return decorator


def _is_review_replan_state(state: object) -> bool:
    """仅在审批退回时复用原 Agent 进度项，用户主动修改仍保留新一轮记录。"""

    if not isinstance(state, dict):
        return False
    replan_context = state.get("replan_context")
    return (
        isinstance(replan_context, dict)
        and replan_context.get("source") == "review"
    )


async def run_trip_conversation(
    messages: list[ClientChatMessage],
    *,
    conversation_id: str | None = None,
    known_requirements: TripRequirements | None = None,
    pending_plan: TripPlanSnapshot | None = None,
) -> ConversationAnalysis:
    """运行入口、规划和审核节点，并返回统一对话分析结果。"""

    # 第一步：确保 SQLite Checkpointer 已打开；测试或非 FastAPI 调用也能按需初始化。
    await _ensure_trip_conversation_graph()
    thread_id = conversation_id or str(uuid4())
    graph = get_trip_conversation_graph()
    config = {"configurable": {"thread_id": thread_id}}
    checkpoint = await graph.aget_state(config)
    stored_messages = checkpoint.values.get("messages", [])
    merged_messages = _merge_messages(stored_messages, messages)
    input_state: dict[str, object] = {
        "messages": merged_messages,
        "known_requirements": known_requirements,
        "pending_plan": pending_plan,
    }
    # 第二步：每轮以浏览器显式提交的快照为准；未提交时清空旧待确认方案，避免新规划继承旧方案。
    # 第三步：通过 thread_id 恢复同一会话的历史状态，并执行本轮用户消息。
    logger.info(
        "LangGraph 会话执行：thread_id=%s incoming_message_count=%s "
        "stored_message_count=%s merged_message_count=%s has_pending_snapshot=%s",
        thread_id,
        len(messages),
        len(stored_messages) if isinstance(stored_messages, list) else 0,
        len(merged_messages),
        pending_plan is not None,
    )
    result = await graph.ainvoke(
        input_state,
        config=config,
    )
    analysis = result.get("analysis")
    # 第四步：图的两个终点都必须由入口节点产出结构化分析，缺失时说明图实现出现了内部错误。
    if not isinstance(analysis, ConversationAnalysis):
        raise RuntimeError("旅差对话图未返回 ConversationAnalysis。")
    # 第五步：将助手回复写回服务端历史；下一轮即使只提交当前用户消息也能恢复上下文。
    persisted_messages = result.get("messages", merged_messages)
    if not isinstance(persisted_messages, list):
        persisted_messages = merged_messages
    await graph.aupdate_state(
        config,
        {
            "messages": _limit_messages(
                [
                    *persisted_messages,
                    ClientChatMessage(role="assistant", content=analysis.reply),
                ]
            )
        },
    )
    return analysis


def _merge_messages(
    stored_messages: object,
    incoming_messages: list[ClientChatMessage],
) -> list[ClientChatMessage]:
    """合并检查点历史和客户端窗口，避免重复并限制服务端历史长度。"""

    # 第一步：只接受检查点中已反序列化的客户端消息，异常状态不阻断当前请求。
    previous = (
        [
            message
            for message in stored_messages
            if isinstance(message, ClientChatMessage)
        ]
        if isinstance(stored_messages, list)
        else []
    )
    incoming = [
        message
        for message in incoming_messages
        if isinstance(message, ClientChatMessage)
    ]
    if not previous:
        return _limit_messages(incoming)
    if not incoming:
        return _limit_messages(previous)

    # 第二步：寻找历史后缀与客户端窗口前缀的最长重叠，兼容前端回传最近八条和只传当前消息两种模式。
    overlap = 0
    max_overlap = min(len(previous), len(incoming))
    for size in range(max_overlap, 0, -1):
        if all(
            _messages_equal(left, right)
            for left, right in zip(previous[-size:], incoming[:size])
        ):
            overlap = size
            break
    return _limit_messages([*previous, *incoming[overlap:]])


def _messages_equal(
    left: ClientChatMessage,
    right: ClientChatMessage,
) -> bool:
    """兼容前端对超长助手回复的截短副本。"""

    if left.role != right.role:
        return False
    if left.content == right.content:
        return True
    if left.role != "assistant":
        return False
    return (
        left.content.startswith(right.content)
        or right.content.startswith(left.content)
    )


def _limit_messages(messages: list[ClientChatMessage]) -> list[ClientChatMessage]:
    """限制检查点保存的消息数量，保留足够历史供 Agent 二次检索。"""

    return messages[-CHECKPOINT_MESSAGE_LIMIT:]


async def initialize_trip_conversation_graph() -> None:
    """初始化 LangGraph SQLite Checkpointer 与编译后的工作流。"""

    await _ensure_trip_conversation_graph()


async def close_trip_conversation_graph() -> None:
    """关闭 LangGraph SQLite Checkpointer，释放服务退出时的连接。"""

    global _checkpoint_context, _checkpointer
    async with _graph_initialization_lock:
        # 第一步：先清理编译图缓存，避免后续误用已经关闭的检查点连接。
        get_trip_conversation_graph.cache_clear()
        if _checkpoint_context is not None:
            await _checkpoint_context.__aexit__(None, None, None)
        _checkpoint_context = None
        _checkpointer = None


async def _ensure_trip_conversation_graph() -> None:
    """按需打开持久化检查点并编译共享工作流。"""

    global _checkpoint_context, _checkpointer
    if _checkpointer is not None:
        return
    async with _graph_initialization_lock:
        if _checkpointer is not None:
            return
        checkpoint_file = get_settings().langgraph_checkpoint_file
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        context = _open_checkpoint_context(checkpoint_file)
        try:
            # 第一步：打开 SQLite 连接并创建 Checkpointer 所需的数据表。
            saver = await context.__aenter__()
            await saver.setup()
        except Exception:
            await context.__aexit__(*sys.exc_info())
            raise
        _checkpoint_context = context
        _checkpointer = saver
        # 第二步：将新打开的持久化 Checkpointer 注入编译图。
        get_trip_conversation_graph.cache_clear()
        logger.info(
            "LangGraph Checkpointer 已初始化：backend=sqlite path=%s",
            checkpoint_file,
        )


@asynccontextmanager
async def _open_checkpoint_context(
    checkpoint_file: Path,
) -> AsyncIterator[AsyncSqliteSaver]:
    """打开带类型白名单的 SQLite Checkpointer 连接。"""

    # 第一步：使用独立异步连接，避免同步 SQLite I/O 阻塞 FastAPI 事件循环。
    async with aiosqlite.connect(str(checkpoint_file)) as connection:
        # 第二步：只允许当前项目定义的结构化类型反序列化，避免开启全量模块加载。
        yield AsyncSqliteSaver(
            connection,
            serde=JsonPlusSerializer(
                allowed_msgpack_modules=_CHECKPOINT_ALLOWED_MSGPACK_MODULES,
            ),
        )


@lru_cache
def get_trip_conversation_graph():
    """构建并缓存入口、规划和审核总结的已编译 LangGraph。"""

    if _checkpointer is None:
        raise RuntimeError("LangGraph Checkpointer 尚未初始化。")
    # 第一步：将意图判断与需求分析拆成两个轻量节点，图层只负责顺序和路由。
    graph = StateGraph(TripConversationState)
    graph.add_node("intent_detection", _intent_detection_node)
    graph.add_node("requirement_analysis", _requirement_analysis_node)
    graph.add_node("search_requirement_analysis", _search_requirement_analysis_node)
    graph.add_node("planning_dispatch", _planning_dispatch_node)
    graph.add_node("execution_agent", _execution_agent_node)
    graph.add_node("trip_planning", _trip_planning_node)
    graph.add_node("accommodation_search", _accommodation_search_node)
    graph.add_node("intercity_transport_search", _intercity_transport_search_node)
    graph.add_node("direct_search", _direct_search_node)
    graph.add_node("review_summary", _review_summary_node)
    graph.add_node("publish_plan", _publish_plan_node)
    graph.add_node("confirm_trip", _confirm_trip_node)
    graph.add_edge(START, "intent_detection")
    # 第二步：普通聊天结束；旅差意图进入需求分析；确认动作直接结束确认流程。
    graph.add_conditional_edges(
        "intent_detection",
        _route_after_intent_detection,
        {
            "requirement_analysis": "requirement_analysis",
            "search_requirement_analysis": "search_requirement_analysis",
            "confirm_trip": "confirm_trip",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "requirement_analysis",
        _route_after_requirement_analysis,
        {
            "planning_dispatch": "planning_dispatch",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "search_requirement_analysis",
        _route_after_search_requirement_analysis,
        {
            "direct_search": "direct_search",
            "end": END,
        },
    )
    # 第三步：规划 Agent 先拆分三类取证任务，三个子 Agent 并发完成后再合并方案。
    graph.add_edge("planning_dispatch", "execution_agent")
    graph.add_edge("planning_dispatch", "accommodation_search")
    graph.add_edge("planning_dispatch", "intercity_transport_search")
    graph.add_edge("execution_agent", "trip_planning")
    graph.add_edge("accommodation_search", "trip_planning")
    graph.add_edge("intercity_transport_search", "trip_planning")
    graph.add_edge("trip_planning", "review_summary")
    graph.add_conditional_edges(
        "review_summary",
        _route_after_review,
        {
            "replan": "planning_dispatch",
            "publish_plan": "publish_plan",
        },
    )
    graph.add_edge("direct_search", END)
    graph.add_edge("publish_plan", END)
    graph.add_edge("confirm_trip", END)
    # 第四步：使用持久化 Checkpointer 编译图，使同一 thread_id 可跨请求恢复状态。
    return graph.compile(checkpointer=_checkpointer)


@_track_agent("入口 Agent", "识别本轮任务与处理分支")
async def _intent_detection_node(
    state: TripConversationState,
) -> dict[str, object]:
    """调用意图 Agent，只判断当前消息属于聊天还是旅差流程。"""

    pending_plan = _get_pending_plan(state)
    known_requirements = _get_effective_requirements(state)
    # 第一步：意图节点不提取需求字段，确保旅差流程必然经过需求分析节点。
    intent_decision = await analyze_intent(
        state["messages"],
        known_requirements=known_requirements,
        pending_plan=pending_plan,
    )
    if intent_decision.intent == "chat":
        return {
            "intent_decision": intent_decision,
            "analysis": ConversationAnalysis(
                intent="chat",
                reply=intent_decision.reply,
            ),
        }
    if intent_decision.intent == "uncertain":
        return {
            "intent_decision": intent_decision,
            "analysis": ConversationAnalysis(
                intent="chat",
                reply=intent_decision.reply,
                pending_plan=pending_plan,
            ),
        }
    if intent_decision.plan_action == "confirm" and pending_plan is not None:
        return {
            "intent_decision": intent_decision,
            "analysis": ConversationAnalysis(
                intent="trip_planning",
                reply=intent_decision.reply,
                requirements=pending_plan.requirements,
                plan_action="confirm",
                pending_plan=pending_plan,
                missing_fields=[],
                is_complete=True,
            ),
        }
    return {"intent_decision": intent_decision}


@_track_agent("需求分析 Agent", "整理行程时间、人数与偏好")
async def _requirement_analysis_node(
    state: TripConversationState,
) -> dict[str, ConversationAnalysis]:
    """在旅差意图确定后分析需求，并把结果交给条件路由。"""

    intent_decision = state["intent_decision"]
    pending_plan = _get_pending_plan(state)
    known_requirements = _get_effective_requirements(state)
    # 第一步：需求节点负责合并快照、归一化日期和计算真实缺失字段。
    analysis = await analyze_requirements(
        state["messages"],
        intent=intent_decision,
        known_requirements=known_requirements,
        pending_plan=pending_plan,
    )
    if pending_plan is not None and analysis.pending_plan is None:
        # 第二步：追问或修改条件未完成时继续保留旧方案，避免前端覆盖待确认状态。
        analysis = analysis.model_copy(update={"pending_plan": pending_plan})
    return {"analysis": analysis}


@_track_agent("查询需求分析 Agent", "整理直接查询所需条件")
async def _search_requirement_analysis_node(
    state: TripConversationState,
) -> dict[str, ConversationAnalysis]:
    """分析酒店或铁路直接查询所需的最小字段。"""

    analysis = await analyze_search_requirements(
        state["messages"],
        intent=state["intent_decision"],
        known_requirements=_get_effective_requirements(state),
        pending_plan=_get_pending_plan(state),
    )
    return {"analysis": analysis}


def _route_after_intent_detection(state: TripConversationState) -> str:
    """根据意图节点结果选择需求分析、确认或结束分支。"""

    # 第一步：普通聊天和不确定意图都不进入需求分析，避免错误追问旅行字段。
    intent_decision = state["intent_decision"]
    if intent_decision.intent == "chat":
        return "end"
    if intent_decision.intent == "uncertain":
        return "end"
    if intent_decision.intent in {
        "accommodation_search",
        "intercity_transport_search",
    }:
        return "search_requirement_analysis"
    if (
        intent_decision.plan_action == "confirm"
        and _get_pending_plan(state) is not None
    ):
        return "confirm_trip"
    return "requirement_analysis"


def _route_after_requirement_analysis(
    state: TripConversationState,
) -> str:
    """根据需求分析结果决定追问结束或进入规划 Agent。"""

    analysis = state["analysis"]
    route = (
        "planning_dispatch"
        if analysis.is_complete and analysis.requirements is not None
        else "end"
    )
    logger.info(
        "需求分析分流：intent=%s plan_action=%s is_complete=%s missing_fields=%s route=%s",
        analysis.intent,
        analysis.plan_action,
        analysis.is_complete,
        analysis.missing_fields,
        route,
    )
    return route


def _route_after_search_requirement_analysis(
    state: TripConversationState,
) -> str:
    """根据直接查询字段完整性决定追问或调用对应查询 Agent。"""

    analysis = state["analysis"]
    return (
        "direct_search"
        if analysis.intent in {"accommodation_search", "intercity_transport_search"}
        else "end"
    )


def _get_effective_requirements(
    state: TripConversationState,
) -> TripRequirements | None:
    """只读取本轮显式传入的待确认方案或需求快照。"""

    # 第一步：本轮显式提交的待确认方案只用于确认或修改；未提交时不得回退到旧方案。
    pending_plan = _get_pending_plan(state)
    if pending_plan is not None:
        return pending_plan.requirements
    known_requirements = state.get("known_requirements")
    if isinstance(known_requirements, TripRequirements):
        return known_requirements
    return None


def _get_pending_plan(state: object) -> TripPlanSnapshot | None:
    """读取本轮显式待确认快照，并兼容旧检查点中的嵌套快照。"""

    if not isinstance(state, dict):
        return None
    if "pending_plan" in state:
        pending_plan = state.get("pending_plan")
        return pending_plan if isinstance(pending_plan, TripPlanSnapshot) else None
    analysis = state.get("analysis")
    if isinstance(analysis, ConversationAnalysis):
        return analysis.pending_plan
    return None


def _get_latest_user_message(
    messages: list[ClientChatMessage],
) -> ClientChatMessage | None:
    """从近期上下文中读取最后一条用户消息。"""

    # 第一步：不假设调用方传入的最后一条消息一定是用户消息，避免把助手回复当作修改内容。
    return next(
        (message for message in reversed(messages) if message.role == "user"),
        None,
    )


@_track_agent(
    "规划 Agent",
    "拆分旅差信息收集任务",
    resume_action="根据审批反馈重新拆分旅差信息收集任务",
)
async def _planning_dispatch_node(
    state: TripConversationState,
) -> dict[str, object]:
    """确认完整需求后，将取证任务分派给三个并行子 Agent。"""

    if state["analysis"].requirements is None:
        raise RuntimeError("任务分派分支缺少完整旅行需求。")
    replan_context = state.get("replan_context")
    if (
        isinstance(replan_context, dict)
        and replan_context.get("source") == "review"
    ):
        return {"replan_attempts": state.get("replan_attempts", 0) + 1}
    return {}


@_track_agent(
    "执行 Agent",
    "收集地点、天气和本地交通信息",
    parent_agent="规划 Agent",
    resume_action="根据审批反馈重新收集地点、天气和本地交通信息",
)
async def _execution_agent_node(
    state: TripConversationState,
) -> dict[str, dict[str, object]]:
    """执行规划 Agent 分派的目的地、POI、天气和本地路线取证。"""

    requirements = state["analysis"].requirements
    if requirements is None:
        raise RuntimeError("执行 Agent 分支缺少完整旅行需求。")
    return {"trip_evidence": await collect_trip_information(requirements)}


@_track_agent(
    "规划 Agent",
    "合并子 Agent 结果并生成具体方案",
    resume_action="根据审批反馈重新合并结果并生成具体方案",
)
async def _trip_planning_node(
    state: TripConversationState,
) -> dict[str, object]:
    """调用现有规划 Agent，并保存供审核节点使用的草案。"""

    analysis = state["analysis"]
    requirements = analysis.requirements
    if requirements is None:
        raise RuntimeError("完整旅差分支缺少 TripRequirements。")
    trip_evidence = state.get("trip_evidence")
    if not isinstance(trip_evidence, dict):
        raise RuntimeError("规划合并分支缺少执行 Agent 的旅差信息。")
    replan_context = state.get("replan_context")
    if replan_context is None and analysis.plan_action == "modify":
        pending_plan = _get_pending_plan(state)
        if pending_plan is not None:
            latest_user_message = _get_latest_user_message(state["messages"])
            if latest_user_message is None:
                raise RuntimeError("用户修改分支缺少当前用户消息。")
            replan_context = _build_user_replan_context(
                pending_plan,
                latest_user_message,
                requirements,
            )
    planning_evidence, replan_context = _exclude_replaced_candidates(
        trip_evidence,
        replan_context,
    )
    # 第一步：仅合并三个子 Agent 的可信结果并生成方案，不在此重复调用外部工具。
    proposal = await plan_trip(
        requirements,
        replan_context=replan_context,
        tool_evidence=planning_evidence,
        external_search_evidence=_get_external_search_evidence(state),
    )
    reused_places = _find_reused_replacement_places(proposal, replan_context)
    if reused_places:
        logger.info(
            "重规划草案仍包含待替换地点，执行一次纠偏生成：category_count=%s",
            len(reused_places),
        )
        proposal = await plan_trip(
            requirements,
            replan_context={
                **(replan_context or {}),
                "previous_proposal": _redact_replaced_places(
                    str((replan_context or {}).get("previous_proposal") or ""),
                    reused_places,
                ),
                "instruction": (
                    "上一次重规划草案仍沿用了用户要求替换的旧地点。"
                    "以下地点不得出现在新草案中，必须从当前剩余候选中重新选择："
                    f"{'；'.join(reused_places)}。"
                ),
                "reused_replacement_places": reused_places,
            },
            tool_evidence=planning_evidence,
            external_search_evidence=_get_external_search_evidence(state),
        )
    return {"proposal": proposal, "replan_context": None, "prepared_details": None}


@_track_agent(
    "住宿查询 Agent",
    "查询酒店价格与房型参考",
    parent_agent="规划 Agent",
    resume_action="根据审批反馈重新查询酒店价格与房型参考",
)
async def _accommodation_search_node(
    state: TripConversationState,
) -> dict[str, dict[str, object]]:
    """作为规划 Agent 的并行子任务查询酒店参考。"""

    requirements = state["analysis"].requirements
    if requirements is None:
        raise RuntimeError("酒店查询分支缺少完整旅行需求。")
    # 第一步：单个来源失败由 Agent 转为 unavailable，不阻断城际交通查询和审核流程。
    return {"accommodation_search": await search_accommodation(requirements)}


@_track_agent(
    "城际交通查询 Agent",
    "核对条件并查询飞机与高铁价格参考",
    parent_agent="规划 Agent",
    resume_action="根据审批反馈重新核对条件并查询交通价格参考",
)
async def _intercity_transport_search_node(
    state: TripConversationState,
) -> dict[str, dict[str, object]]:
    """作为规划 Agent 的并行子任务查询城际交通参考。"""

    requirements = state["analysis"].requirements
    if requirements is None:
        raise RuntimeError("城际交通查询分支缺少完整旅行需求。")
    # 第一步：城际估算需要完整起终点和出发日期；缺失时跳过，不能将正常缺项误报为服务故障。
    if not (
        requirements.origin
        and requirements.destination
        and requirements.departure_date
    ):
        logger.info("城际交通估算跳过：reason=requirements_incomplete")
        async with track_progress(
            "城际交通查询 Agent",
            "缺少出发地、目的地或出发时间，未执行价格估算",
            tool="交通价格估算",
        ) as progress:
            progress.mark_failed("缺少出发地、目的地或出发时间。")
        return {
            "intercity_transport_search": {
                "status": "skipped",
                "reason": "requirements_incomplete",
            }
        }
    # 第二步：单个来源失败由 Agent 转为 unavailable，不阻断酒店查询和审核流程。
    return {
        "intercity_transport_search": await search_intercity_transport(requirements)
    }


async def _direct_search_node(
    state: TripConversationState,
) -> dict[str, object]:
    """执行已完成字段校验的酒店或城际交通直接查询。"""

    analysis = state["analysis"]
    requirements = analysis.requirements
    if requirements is None:
        raise RuntimeError("直接查询缺少查询需求。")
    if analysis.intent == "accommodation_search":
        agent = "住宿查询 Agent"
        action = "查询酒店价格与房型参考"
        result_key = "accommodation"
        title = "酒店查询结果"
        operation = search_accommodation(requirements)
    elif analysis.intent == "intercity_transport_search":
        agent = "城际交通查询 Agent"
        action = "查询飞机与高铁价格参考"
        result_key = "intercity_transport"
        title = "飞机/火车查询结果"
        operation = search_intercity_transport(requirements)
    else:
        raise RuntimeError("直接查询节点收到不支持的查询意图。")
    async with track_progress(agent, action):
        result = await operation
    return {
        f"{result_key}_search": result,
        "analysis": analysis.model_copy(
            update={
                "reply": _format_direct_search_result(title, result),
                "search_results": {result_key: result},
            }
        ),
    }

async def _review_summary_node(
    state: TripConversationState,
) -> dict[str, object]:
    """调用审核总结 Agent，并保存其结果供后续路由使用。"""

    async with track_progress("审批 Agent", "执行规则校验并整理待确认事项") as progress:
        analysis = state["analysis"]
        requirements = analysis.requirements
        proposal = state.get("proposal")
        if requirements is None or not proposal:
            raise RuntimeError("审核总结分支缺少完整需求或规划草案。")
        # 第一步：审批阶段先执行确定性规则校验，再基于校验结果整理风险与待确认项。
        validation_issues = _validate_trip_plan(
            requirements,
            proposal,
            state.get("trip_evidence"),
        )
        review_kwargs: dict[str, object] = {
            "validation_issues": validation_issues,
        }
        external_search_evidence = _get_external_search_evidence(state)
        # 第二步：只有可用查询结果才进入审核上下文。
        if external_search_evidence:
            review_kwargs["external_search_evidence"] = external_search_evidence
        review_result = await review_trip(
            requirements,
            proposal,
            **review_kwargs,
        )
        result: dict[str, object] = {
            "review_result": review_result,
            "prepared_details": None,
        }
        if (
            review_result.status == "needs_replanning"
            and state.get("replan_attempts", 0) < MAX_REPLAN_ATTEMPTS
        ):
            progress.mark_rejected("审批未通过，已退回规划 Agent 根据审核意见调整。")
            result["replan_context"] = _build_review_replan_context(
                proposal,
                review_result,
            )
        return result


def _route_after_review(state: TripConversationState) -> str:
    """根据审核状态、重规划次数决定回流、待确认或用户决策分支。"""

    review_result = state["review_result"]
    replan_attempts = state.get("replan_attempts", 0)
    route = "publish_plan"
    if review_result.status == "needs_replanning":
        route = (
            "replan" if replan_attempts < MAX_REPLAN_ATTEMPTS else "publish_plan"
        )
    # 第一步：日志只记录状态和次数，避免将审核正文、草案或用户条件写入日志。
    logger.info(
        "审批 Agent 分流：status=%s replan_attempts=%s route=%s",
        review_result.status,
        replan_attempts,
        route,
    )
    return route


async def _publish_plan_node(
    state: TripConversationState,
) -> dict[str, object]:
    """根据审批结果发布待确认方案或需要用户处理的方案。"""

    analysis = state["analysis"]
    pending_plan = _build_pending_plan(state, "方案发布分支缺少完整方案状态。")
    if pending_plan.review_result.status == "needs_replanning":
        pending_plan = pending_plan.model_copy(
            update={
                "review_result": pending_plan.review_result.model_copy(
                    update={
                        "status": "needs_user_decision",
                        "summary": (
                            "方案已完成自动调整，请确认路线顺序和行程安排；"
                            "确认后会据此生成最终方案。"
                        ),
                    }
                )
            }
        )
    details = state.get("prepared_details")
    if not isinstance(details, ConfirmedTripDetails):
        details = await build_confirmed_trip_details(
            pending_plan.requirements,
            proposal=_clean_confirmed_plan_proposal(pending_plan.proposal),
            trip_evidence=(
                state["trip_evidence"]
                if isinstance(state.get("trip_evidence"), dict)
                else None
            ),
        )
    details = details.model_copy(
        update={"weather": _get_trip_weather_evidence(state)}
    )
    pending_plan = pending_plan.model_copy(update={"details": details})
    reply = "行程方案已生成，请在下方窗口查看详情。"
    return {
        "pending_plan": pending_plan,
        "analysis": analysis.model_copy(
            update={
                "reply": reply,
                "pending_plan": pending_plan,
                "search_results": _get_external_search_evidence(state),
            }
        )
    }


@_track_agent("审批 Agent", "补充确认后的图片与路线信息")
async def _confirm_trip_node(
    state: TripConversationState,
) -> dict[str, object]:
    """确认方案并补充图片、路线等前端展示数据。"""

    analysis = state["analysis"]
    pending_plan = _get_pending_plan(state)
    if pending_plan is None:
        raise RuntimeError("确认分支缺少待确认方案。")
    cleaned_proposal = _clean_confirmed_plan_proposal(pending_plan.proposal)
    confirmed_details = pending_plan.details
    if (
        not confirmed_details.map_points
        and confirmed_details.overview_route is None
    ):
        confirmed_details = await build_confirmed_trip_details(
            pending_plan.requirements,
            proposal=cleaned_proposal,
            trip_evidence=(
                state["trip_evidence"]
                if isinstance(state.get("trip_evidence"), dict)
                else None
            ),
        )
    elif confirmed_details.map_points:
        confirmed_details = confirmed_details.model_copy(
            update={
                "routes": await rebuild_city_routes_for_points(
                    pending_plan.requirements,
                    confirmed_details.map_points,
                )
            }
        )
    try:
        final_proposal = await generate_confirmed_trip_plan(
            pending_plan.requirements,
            draft_proposal=cleaned_proposal,
            details=confirmed_details,
        )
    except Exception as error:
        logger.warning(
            "最终方案正文生成失败，使用确定性兜底：error_type=%s",
            type(error).__name__,
        )
        final_proposal = _build_route_ordered_proposal(
            cleaned_proposal,
            confirmed_details.map_points,
            confirmed_details.weather,
        )
    confirmed_plan = ConfirmedTripPlan(
        requirements=pending_plan.requirements,
        proposal=final_proposal,
        review_result=pending_plan.review_result,
        confirmed_at=datetime.now(timezone.utc),
        details=confirmed_details,
    )
    # 第一步：保留完整草案，并将图片与路线取证结果一并返回给前端。
    return {
        "pending_plan": None,
        "analysis": analysis.model_copy(
            update={
                "reply": "已确认当前行程方案，完整方案、图片和路线信息已生成。",
                "pending_plan": None,
                "confirmed_plan": confirmed_plan,
            }
        )
    }


def _clean_confirmed_plan_proposal(proposal: str) -> str:
    """移除最终方案中的待确认事项和未完成核验提示。"""

    without_pending_section = _CONFIRMED_PENDING_SECTION_PATTERN.sub("", proposal)
    cleaned_lines = [
        line.strip()
        for line in without_pending_section.splitlines()
        if not _CONFIRMED_PENDING_LINE_PATTERN.search(line)
    ]
    cleaned_proposal = "\n".join(cleaned_lines).strip()
    return cleaned_proposal or "已确认行程已生成。"


def _build_route_ordered_proposal(
    proposal: str,
    map_points: list[object],
    weather: dict[str, object] | None = None,
) -> str:
    """让最终方案正文先呈现用户确认的路线规划顺序。"""

    ordered_points = [
        point for point in map_points
        if hasattr(point, "name") and hasattr(point, "category")
    ]
    if not ordered_points:
        return proposal
    route_lines = ["路线规划"]
    for index, point in enumerate(ordered_points, start=1):
        category = "美食" if point.category == "food" else "景点"
        route_lines.append(f"{index}. {category}：{point.name}")
    weather_lines = ["", "天气参考", _format_weather_fallback(weather)]
    return "\n".join(route_lines + weather_lines + ["", proposal]).strip()


def _format_weather_fallback(weather: dict[str, object] | None) -> str:
    """把最终方案兜底路径中的天气信息压缩成用户可读文本。"""

    if not isinstance(weather, dict):
        return "当前暂无可用天气预报，建议临近出发前再次复查。"
    forecast = weather.get("forecast")
    if not isinstance(forecast, list) or not forecast:
        message = weather.get("message")
        return str(message or "当前暂无可用天气预报，建议临近出发前再次复查。")
    summaries: list[str] = []
    for item in forecast[:5]:
        if not isinstance(item, dict):
            continue
        condition = item.get("day_condition") or item.get("night_condition") or "天气待定"
        temperature = " / ".join(
            str(value)
            for value in (item.get("temperature_min"), item.get("temperature_max"))
            if value is not None
        )
        summaries.append(
            f"{item.get('date', '行程日')} {condition}"
            f"{f'，约{temperature}℃' if temperature else ''}"
        )
    return "；".join(summaries) + "。" if summaries else "当前暂无可用天气预报，建议临近出发前再次复查。"


def _validate_trip_plan(
    requirements: TripRequirements | None,
    proposal: str | None,
    trip_evidence: object = None,
) -> list[ValidationIssue]:
    """检查可由本地确定的行程硬约束。"""

    issues: list[ValidationIssue] = []
    if requirements is None:
        issues.append(
            ValidationIssue(
                code="TRIP_REQUIREMENTS_MISSING",
                message="规划结果缺少已确认的旅行需求。",
                severity="error",
                retryable=False,
            )
        )
        return issues
    if not proposal or not proposal.strip():
        issues.append(
            ValidationIssue(
                code="TRIP_PROPOSAL_EMPTY",
                message="规划 Agent 未生成可审核的行程草案。",
                severity="error",
                retryable=True,
            )
        )
    if not requirements.return_date and not requirements.trip_duration:
        issues.append(
            ValidationIssue(
                code="TRIP_SCHEDULE_MISSING",
                message="行程缺少返程日期或旅行时长。",
                severity="error",
                retryable=False,
            )
        )
    if requirements.departure_date and requirements.return_date:
        try:
            departure = datetime.fromisoformat(
                requirements.departure_date
            ).date()
            return_date = datetime.fromisoformat(
                requirements.return_date
            ).date()
        except ValueError:
            issues.append(
                ValidationIssue(
                    code="TRIP_DATE_INVALID",
                    message="出发日期或返程日期不是可识别的日期格式。",
                    severity="error",
                    retryable=False,
                )
            )
        else:
            if return_date < departure:
                issues.append(
                    ValidationIssue(
                        code="TRIP_DATE_ORDER_INVALID",
                        message="返程日期早于出发日期。",
                        severity="error",
                        retryable=False,
                    )
                )
    if requirements.destination:
        mismatch_issue = _validate_destination_candidate_match(
            requirements.destination,
            trip_evidence,
        )
        if mismatch_issue is not None:
            issues.append(mismatch_issue)
    return issues


def _validate_destination_candidate_match(
    destination: str,
    trip_evidence: object,
) -> ValidationIssue | None:
    """检查候选是否仍处于现实旅程可接受的周边范围。"""

    if not isinstance(trip_evidence, dict):
        return None
    destination_location = _parse_lng_lat(trip_evidence.get("destination_location"))
    destination_province = _normalized_region_name(
        trip_evidence.get("destination_province")
    )
    groups = [
        ("住宿", trip_evidence.get("accommodation_candidates")),
        ("景点", trip_evidence.get("attraction_candidates")),
        ("餐饮", trip_evidence.get("food_candidates")),
    ]
    far_groups: list[str] = []
    sample_names: list[str] = []
    for label, candidates in groups:
        if not isinstance(candidates, list) or not candidates:
            continue
        valid_distances: list[float] = []
        names: list[str] = []
        provinces: list[str] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_location = _parse_lng_lat(candidate.get("location"))
            if candidate_location is not None and destination_location is not None:
                valid_distances.append(
                    _distance_kilometers(destination_location, candidate_location)
                )
            name = candidate.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
            province = _normalized_region_name(candidate.get("province"))
            if province is not None:
                provinces.append(province)
        if destination_province is not None and any(
            province != destination_province for province in provinces
        ):
            far_groups.append(label)
        elif valid_distances and min(valid_distances) > 150:
            far_groups.append(label)
        else:
            continue
        if names:
            sample_names.append(f"{label}：{names[0]}")
    if not far_groups:
        return None
    reason = (
        f"当前草案与已确认目的地“{destination}”严重不匹配："
        f"{'、'.join(far_groups)}候选跨省或距离目的地过远"
        f"{'（例如' + '；'.join(sample_names[:3]) + '）' if sample_names else ''}。"
        "请重新检索目的地周边合理范围内的住宿、景点和餐饮后再生成方案。"
    )
    return ValidationIssue(
        code="TRIP_DESTINATION_CANDIDATES_MISMATCH",
        message=reason,
        severity="error",
        retryable=True,
    )


def _normalized_city_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(
        r"(?:自治州|地区|盟|市)$",
        "",
        re.sub(r"\s+", "", value),
    )


def _normalized_region_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return re.sub(
        r"(?:省|市|自治区|特别行政区|地区|盟|自治州)$",
        "",
        re.sub(r"\s+", "", value),
    )


def _parse_lng_lat(value: object) -> tuple[float, float] | None:
    if not isinstance(value, str) or "," not in value:
        return None
    left, right = value.split(",", 1)
    try:
        longitude = float(left.strip())
        latitude = float(right.strip())
    except ValueError:
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return longitude, latitude


def _distance_kilometers(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    first_lng, first_lat = first
    second_lng, second_lat = second
    radius_kilometers = 6371.0088
    lat_delta = math.radians(second_lat - first_lat)
    lng_delta = math.radians(second_lng - first_lng)
    first_lat_rad = math.radians(first_lat)
    second_lat_rad = math.radians(second_lat)
    haversine = (
        math.sin(lat_delta / 2) ** 2
        + math.cos(first_lat_rad)
        * math.cos(second_lat_rad)
        * math.sin(lng_delta / 2) ** 2
    )
    return radius_kilometers * 2 * math.atan2(
        math.sqrt(haversine),
        math.sqrt(1 - haversine),
    )


def _build_pending_plan(
    state: TripConversationState,
    error_message: str,
) -> TripPlanSnapshot:
    """从审核状态构造待确认方案快照。"""

    # 第一步：两个审核终点共享同一份快照校验，避免状态结构发生分叉。
    requirements = state["analysis"].requirements
    proposal = state.get("proposal")
    review_result = state.get("review_result")
    if requirements is None or not proposal or review_result is None:
        raise RuntimeError(error_message)
    return TripPlanSnapshot(
        requirements=requirements,
        proposal=proposal,
        review_result=review_result,
    )


def _get_external_search_evidence(
    state: TripConversationState,
) -> dict[str, object]:
    """读取可用的并行查询结果，避免正常跳过项污染方案正文。"""

    candidates = {
        "accommodation": state.get("accommodation_search"),
        "intercity_transport": state.get("intercity_transport_search"),
    }
    return {
        name: result
        for name, result in candidates.items()
        if isinstance(result, dict)
        and result.get("status") in {"available", "estimated"}
    }


def _get_trip_weather_evidence(state: TripConversationState) -> dict[str, object]:
    trip_evidence = state.get("trip_evidence")
    if not isinstance(trip_evidence, dict):
        return {}
    weather = trip_evidence.get("weather")
    return weather if isinstance(weather, dict) else {}


def _format_direct_search_result(
    title: str,
    result: dict[str, object],
) -> str:
    """把单个直接查询结果整理成用户可读的口头结果。"""

    if result.get("status") == "unavailable":
        message = result.get("message")
        return str(message or "当前查询暂时没有可用结果。")
    if "酒店" not in title:
        return _format_transport_search_result(title, result)

    section = _format_available_search_result(
        title,
        result,
        HOTEL_OFFER_LABELS,
    )
    return section or "当前查询暂时没有可用结果。"


def _format_transport_search_result(
    title: str,
    result: dict[str, object],
) -> str:
    """用精简字段展示飞机和火车查询参考。"""

    if result.get("status") not in {"available", "estimated"}:
        return "当前查询暂时没有可用结果。"
    offers = result.get("offers")
    if not isinstance(offers, list):
        return "当前查询暂时没有可用结果。"
    lines = [title]
    for offer in offers[:5]:
        if not isinstance(offer, dict):
            continue
        service_no = _readable_offer_value(offer.get("service_no"), "班次待定")
        origin = _readable_offer_value(offer.get("origin"), "出发地待定")
        destination = _readable_offer_value(offer.get("destination"), "到达地待定")
        departure_time = _readable_offer_value(offer.get("departure_time"), "出发时间待定")
        arrival_time = _readable_offer_value(offer.get("arrival_time"), "到达时间待定")
        price = _readable_price(offer)
        lines.append(
            f"{service_no}：{origin} → {destination}，"
            f"{departure_time} 出发，{arrival_time} 到达，{price}。"
        )
    if len(lines) == 1:
        return "当前查询暂时没有可用结果。"
    lines.append(
        "后端尚未接入飞机和火车实时 API，以下仅提供价格参考，"
        "不代表实时票价、余票或已确认班次。"
    )
    return "\n".join(lines)


def _readable_offer_value(value: object, fallback: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or fallback


def _readable_price(offer: dict[str, object]) -> str:
    total_price = offer.get("total_price")
    price = offer.get("price")
    selected_price = total_price if total_price is not None else price
    if selected_price is None or not str(selected_price).strip():
        return "价格待定"
    return f"约 {selected_price} 元"


def _format_available_search_result(
    title: str,
    result: dict[str, object],
    labels: tuple[tuple[str, str], ...],
) -> str | None:
    """格式化可用或估算查询结果；状态或字段无效时返回 None。"""

    if result.get("status") not in {"available", "estimated"}:
        return None
    offers = result.get("offers")
    if not isinstance(offers, list):
        return None

    lines = [f"## {title}"]
    for offer in offers[:5]:
        if not isinstance(offer, dict):
            continue
        fields = [
            f"{label}：{offer[field]}"
            for field, label in labels
            if offer.get(field) is not None and str(offer[field]).strip()
        ]
        if fields:
            lines.append("- " + "；".join(fields))
    if len(lines) == 1:
        return None
    message = result.get("message")
    if isinstance(message, str) and message.strip():
        lines.append(message)
    elif result.get("status") == "estimated":
        lines.append("后端尚未接入对应实时 API，以上仅提供价格参考，不代表实时价格、库存或余票。")
    else:
        lines.append("以上为查询时快照，价格、库存和余票需在下单前再次核验。")
    return "\n".join(lines)


def _build_user_replan_context(
    pending_plan: TripPlanSnapshot,
    message: ClientChatMessage,
    requirements: TripRequirements,
) -> dict[str, object]:
    """构造用户修改触发的重规划反馈。"""

    # 第一步：保留上一版草案、审核结论和最新修改消息，避免规划 Agent 忽略已整理信息。
    context: dict[str, object] = {
        "source": "user",
        "previous_proposal": pending_plan.proposal,
        "review": _review_payload(pending_plan.review_result),
        "user_message": message.content,
    }
    instructions = ["用户已修改行程条件，请保留未冲突信息并重新规划。"]
    preference_updates = _changed_preference_updates(
        pending_plan.requirements,
        requirements,
    )
    if preference_updates:
        instructions.append(
            "用户本轮已明确更新景点或餐饮偏好；对应类别必须以新偏好检索出的候选为准，"
            "不得继续用旧方案中的同类地点替代。"
        )
        context["updated_preferences"] = preference_updates
    replacement_categories = _requested_replacement_categories(message.content)
    if replacement_categories:
        instructions.append(
            "用户要求更换景点或美食。对应类别必须使用不同于旧方案的候选，"
            "其余未冲突安排可保留。"
        )
        context["replacement_categories"] = replacement_categories
    context["instruction"] = "".join(instructions)
    return context


def _changed_preference_updates(
    previous: TripRequirements,
    current: TripRequirements,
) -> dict[str, list[str]]:
    """提取本轮显式更新的景点和餐饮偏好，供重规划优先执行。"""

    updates: dict[str, list[str]] = {}
    for field_name in ("attraction_preferences", "dining_preferences"):
        current_values = getattr(current, field_name)
        if current_values and current_values != getattr(previous, field_name):
            updates[field_name] = current_values
    return updates


def _requested_replacement_categories(message: str) -> list[str]:
    """识别用户是否要求更换一批景点或美食。"""

    if not re.search(r"(?:换(?:一批|个|一些)?|更换|重新推荐|换掉)", message):
        return []
    categories: list[str] = []
    if re.search(r"(?:景点|景区|游览|打卡)", message):
        categories.append("attraction")
    if re.search(r"(?:美食|餐饮|餐厅|吃饭|用餐)", message):
        categories.append("food")
    return categories or ["attraction", "food"]


def _exclude_replaced_candidates(
    trip_evidence: dict[str, object],
    replan_context: dict[str, object] | None,
) -> tuple[dict[str, object], dict[str, object] | None]:
    """从用户要求更换的类别中排除旧方案已经使用的地点。"""

    if not isinstance(replan_context, dict):
        return trip_evidence, replan_context
    categories = replan_context.get("replacement_categories")
    previous_proposal = replan_context.get("previous_proposal")
    if (
        not isinstance(categories, list)
        or not isinstance(previous_proposal, str)
        or not previous_proposal
    ):
        return trip_evidence, replan_context

    category_keys = {
        "attraction": "attraction_candidates",
        "food": "food_candidates",
    }
    normalized_proposal = _normalize_place_text(previous_proposal)
    next_evidence = dict(trip_evidence)
    next_recommended = dict(
        trip_evidence.get("recommended_candidates", {})
        if isinstance(trip_evidence.get("recommended_candidates"), dict)
        else {}
    )
    excluded_place_names: dict[str, list[str]] = {}

    for category in categories:
        evidence_key = category_keys.get(category)
        if evidence_key is None:
            continue
        candidates = trip_evidence.get(evidence_key)
        if not isinstance(candidates, list):
            continue
        excluded_names = [
            name
            for candidate in candidates
            if isinstance(candidate, dict)
            for name in [_candidate_name(candidate)]
            if name and _normalize_place_text(name) in normalized_proposal
        ]
        if not excluded_names:
            continue
        remaining_candidates = [
            candidate
            for candidate in candidates
            if isinstance(candidate, dict)
            and not any(
                _same_or_nested_place_name(
                    _candidate_name(candidate) or "",
                    excluded_name,
                )
                for excluded_name in excluded_names
            )
        ]
        next_evidence[evidence_key] = remaining_candidates
        next_recommended[category] = (
            _compact_recommended_candidate(remaining_candidates[0])
            if remaining_candidates
            else None
        )
        excluded_place_names[category] = excluded_names

    if not excluded_place_names:
        return trip_evidence, replan_context
    next_evidence["recommended_candidates"] = next_recommended
    return (
        next_evidence,
        {
            **replan_context,
            "excluded_place_names": excluded_place_names,
        },
    )


def _candidate_name(candidate: dict[str, object]) -> str | None:
    name = candidate.get("name")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _compact_recommended_candidate(candidate: dict[str, object]) -> dict[str, object]:
    return {
        key: candidate.get(key)
        for key in ("name", "address", "type")
        if candidate.get(key) is not None
    }


def _same_or_nested_place_name(first: str, second: str) -> bool:
    normalized_first = _normalize_place_text(first)
    normalized_second = _normalize_place_text(second)
    if not normalized_first or not normalized_second:
        return False
    return (
        normalized_first == normalized_second
        or (
            min(len(normalized_first), len(normalized_second)) >= 4
            and (
                normalized_first in normalized_second
                or normalized_second in normalized_first
            )
        )
    )


def _normalize_place_text(value: str) -> str:
    return re.sub(r"[\s()（）\[\]【】\-—_·、,，.。]", "", value)


def _find_reused_replacement_places(
    proposal: str,
    replan_context: dict[str, object] | None,
) -> list[str]:
    """识别重规划草案是否仍引用用户要求替换的旧地点。"""

    if not isinstance(replan_context, dict):
        return []
    excluded_place_names = replan_context.get("excluded_place_names")
    if not isinstance(excluded_place_names, dict):
        return []
    normalized_proposal = _normalize_place_text(proposal)
    reused_places: list[str] = []
    for names in excluded_place_names.values():
        if not isinstance(names, list):
            continue
        for name in names:
            if (
                isinstance(name, str)
                and name.strip()
                and _normalize_place_text(name) in normalized_proposal
                and name not in reused_places
            ):
                reused_places.append(name)
    return reused_places


def _redact_replaced_places(proposal: str, place_names: list[str]) -> str:
    """在纠偏轮中隐藏旧地点名称，避免模型把旧文本当成可复用安排。"""

    redacted_proposal = proposal
    for name in place_names:
        if not name.strip():
            continue
        redacted_proposal = re.sub(
            re.escape(name),
            "已更换地点",
            redacted_proposal,
            flags=re.IGNORECASE,
        )
        alternate_name = name.replace("(", "（").replace(")", "）")
        if alternate_name != name:
            redacted_proposal = redacted_proposal.replace(
                alternate_name,
                "已更换地点",
            )
    return redacted_proposal


def _build_review_replan_context(
    proposal: str,
    review_result: ReviewResult,
) -> dict[str, object]:
    """构造审批未通过触发的重规划反馈。"""

    return {
        "source": "review",
        "instruction": "审批未通过，请根据审批结论指出的缺陷重新规划。",
        "previous_proposal": proposal,
        "review": _review_payload(review_result),
    }


def _review_payload(review_result: ReviewResult) -> dict[str, object]:
    """按审核提示词约定的三字段格式构造重规划反馈。"""

    # 第一步：状态属于图层路由信息，不传给规划 Agent，避免其误将状态文本当作用户需求。
    return {
        "summary": review_result.summary,
        "risks": review_result.risks,
        "pending_items": review_result.pending_items,
    }
