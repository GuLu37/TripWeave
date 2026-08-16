"""统一入口、旅差规划与审核总结 Agent 的 LangGraph 编排图。"""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
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
from app.agents.planning_agent import plan_trip
from app.agents.review_agent import review_trip
from app.agents.accommodation_search_agent import search_accommodation
from app.agents.intercity_transport_search_agent import search_intercity_transport
from app.core.settings import get_settings
from app.services.confirmed_trip_service import build_confirmed_trip_details
from app.schemas import (
    ClientChatMessage,
    ConfirmedTripPlan,
    ConversationAnalysis,
    ReviewResult,
    TripPlanSnapshot,
    TripRequirements,
    ValidationIssue,
)

logger = logging.getLogger(__name__)
MAX_REPLAN_ATTEMPTS = 2
CHECKPOINT_MESSAGE_LIMIT = 120
HOTEL_OFFER_LABELS = (
    ("name", "酒店"),
    ("room_type", "房型"),
    ("price", "价格"),
    ("currency", "币种"),
    ("availability", "库存"),
)
TRANSPORT_OFFER_LABELS = (
    ("mode", "方式"),
    ("operator", "承运方"),
    ("service_no", "班次"),
    ("departure_time", "出发"),
    ("arrival_time", "到达"),
    ("price", "价格"),
    ("currency", "币种"),
    ("availability", "余票"),
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
    ("app.schemas", "TripImage"),
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
    accommodation_search: dict[str, object]
    intercity_transport_search: dict[str, object]
    validation_issues: list[ValidationIssue]
    review_result: ReviewResult
    replan_context: dict[str, object]
    replan_attempts: int


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
    stored_pending_plan = _get_pending_plan(checkpoint.values)
    input_state: dict[str, object] = {
        "messages": merged_messages,
    }
    # 第二步：只覆盖本轮明确提供的可选状态，避免空值清除 Checkpointer 中的待确认方案。
    if known_requirements is not None:
        input_state["known_requirements"] = known_requirements
    if pending_plan is not None:
        input_state["pending_plan"] = pending_plan
    # 第三步：通过 thread_id 恢复同一会话的历史状态，并执行本轮用户消息。
    logger.info(
        "LangGraph 会话执行：thread_id=%s incoming_message_count=%s "
        "stored_message_count=%s merged_message_count=%s has_pending_snapshot=%s",
        thread_id,
        len(messages),
        len(stored_messages) if isinstance(stored_messages, list) else 0,
        len(merged_messages),
        pending_plan is not None or stored_pending_plan is not None,
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
    graph.add_node("trip_planning", _trip_planning_node)
    graph.add_node("accommodation_search", _accommodation_search_node)
    graph.add_node("intercity_transport_search", _intercity_transport_search_node)
    graph.add_node(
        "direct_accommodation_search",
        _direct_accommodation_search_node,
    )
    graph.add_node(
        "direct_intercity_transport_search",
        _direct_intercity_transport_search_node,
    )
    graph.add_node("review_summary", _review_summary_node)
    graph.add_node("rule_validation", _rule_validation_node)
    graph.add_node("review_feedback", _review_feedback_node)
    graph.add_node("await_confirmation", _await_confirmation_node)
    graph.add_node("user_decision", _user_decision_node)
    graph.add_node("confirm_trip", _confirm_trip_node)
    graph.add_node("clarify_intent", _clarify_intent_node)
    graph.add_node("blocked_confirmation", _blocked_confirmation_node)
    graph.add_edge(START, "intent_detection")
    # 第二步：普通聊天结束；旅差意图进入需求分析；确认动作直接结束确认流程。
    graph.add_conditional_edges(
        "intent_detection",
        _route_after_intent_detection,
        {
            "requirement_analysis": "requirement_analysis",
            "search_requirement_analysis": "search_requirement_analysis",
            "confirm_trip": "confirm_trip",
            "blocked_confirmation": "blocked_confirmation",
            "clarify_intent": "clarify_intent",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "requirement_analysis",
        _route_after_requirement_analysis,
        {
            "trip_planning": "trip_planning",
            "end": END,
        },
    )
    graph.add_conditional_edges(
        "search_requirement_analysis",
        _route_after_search_requirement_analysis,
        {
            "direct_accommodation_search": "direct_accommodation_search",
            "direct_intercity_transport_search": "direct_intercity_transport_search",
            "end": END,
        },
    )
    # 第三步：规划完成后并行查询酒店和城际交通，再进入统一校验与审核。
    graph.add_edge("trip_planning", "accommodation_search")
    graph.add_edge("trip_planning", "intercity_transport_search")
    graph.add_edge("accommodation_search", "rule_validation")
    graph.add_edge("intercity_transport_search", "rule_validation")
    graph.add_edge("rule_validation", "review_summary")
    graph.add_conditional_edges(
        "review_summary",
        _route_after_review,
        {
            "replan": "review_feedback",
            "await_confirmation": "await_confirmation",
            "user_decision": "user_decision",
        },
    )
    graph.add_edge("review_feedback", "trip_planning")
    graph.add_edge("direct_accommodation_search", END)
    graph.add_edge("direct_intercity_transport_search", END)
    graph.add_edge("await_confirmation", END)
    graph.add_edge("user_decision", END)
    graph.add_edge("confirm_trip", END)
    graph.add_edge("clarify_intent", END)
    graph.add_edge("blocked_confirmation", END)
    # 第四步：使用持久化 Checkpointer 编译图，使同一 thread_id 可跨请求恢复状态。
    return graph.compile(checkpointer=_checkpointer)


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


def _route_after_conversation_entry(
    state: TripConversationState,
) -> str:
    """兼容旧测试与调用方的需求完整性路由函数。"""

    # 第一步：旧调用方传入完整 analysis 时复用新的需求分析路由规则。
    analysis = state["analysis"]
    pending_plan = _get_pending_plan(state)
    if analysis.plan_action == "confirm" and pending_plan is not None:
        return (
            "confirm_trip"
            if pending_plan.review_result.status == "ready_for_confirmation"
            else "blocked_confirmation"
        )
    route = (
        "trip_planning"
        if analysis.is_complete and analysis.requirements is not None
        else "end"
    )
    # 第二步：仅记录分流判定和缺失字段名称，不写入完整对话或用户旅行内容。
    logger.info(
        "需求分析分流：intent=%s plan_action=%s is_complete=%s missing_fields=%s route=%s",
        analysis.intent,
        analysis.plan_action,
        analysis.is_complete,
        analysis.missing_fields,
        route,
    )
    return route


def _route_after_intent_detection(state: TripConversationState) -> str:
    """根据意图节点结果选择需求分析、确认或结束分支。"""

    # 第一步：普通聊天和不确定意图都不进入需求分析，避免错误追问旅行字段。
    intent_decision = state["intent_decision"]
    if intent_decision.intent == "chat":
        return "end"
    if intent_decision.intent == "uncertain":
        return "clarify_intent"
    if intent_decision.intent in {
        "accommodation_search",
        "intercity_transport_search",
    }:
        return "search_requirement_analysis"
    if (
        intent_decision.plan_action == "confirm"
        and _get_pending_plan(state) is not None
    ):
        pending_plan = _get_pending_plan(state)
        return (
            "confirm_trip"
            if pending_plan.review_result.status == "ready_for_confirmation"
            else "blocked_confirmation"
        )
    return "requirement_analysis"


def _route_after_requirement_analysis(
    state: TripConversationState,
) -> str:
    """根据需求分析结果决定追问结束或进入规划 Agent。"""

    # 第一步：需求不完整时 analysis.reply 已经是确定性追问，图在此轮结束。
    return _route_after_conversation_entry(state)


def _route_after_search_requirement_analysis(
    state: TripConversationState,
) -> str:
    """根据直接查询字段完整性决定追问或调用对应查询 Agent。"""

    analysis = state["analysis"]
    if not analysis.is_complete:
        return "end"
    if analysis.intent == "accommodation_search":
        return "direct_accommodation_search"
    if analysis.intent == "intercity_transport_search":
        return "direct_intercity_transport_search"
    return "end"


def _get_effective_requirements(
    state: TripConversationState,
) -> TripRequirements | None:
    """按待确认方案、外部快照和图内分析结果的顺序读取需求。"""

    # 第一步：待确认方案代表最新一次规划结果，不能被前端携带的旧快照覆盖。
    pending_plan = _get_pending_plan(state)
    if pending_plan is not None:
        return pending_plan.requirements
    known_requirements = state.get("known_requirements")
    if known_requirements is not None:
        return known_requirements
    # 第二步：同一 thread_id 恢复出的历史分析也是有效需求快照，支持用户只回复“一个人”等短句。
    analysis = state.get("analysis")
    if isinstance(analysis, ConversationAnalysis):
        return analysis.requirements
    return None


def _get_pending_plan(state: object) -> TripPlanSnapshot | None:
    """读取顶层待确认快照，并兼容旧检查点中的嵌套快照。"""

    if not isinstance(state, dict):
        return None
    pending_plan = state.get("pending_plan")
    if isinstance(pending_plan, TripPlanSnapshot):
        return pending_plan
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


async def _trip_planning_node(
    state: TripConversationState,
) -> dict[str, str]:
    """调用现有规划 Agent，并保存供审核节点使用的草案。"""

    analysis = state["analysis"]
    requirements = analysis.requirements
    if requirements is None:
        raise RuntimeError("完整旅差分支缺少 TripRequirements。")
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
            )
    # 第一步：复用规划 Agent 的 Tool 编排；重规划时仅附带上一版方案和本轮明确反馈。
    proposal = await (
        plan_trip(requirements, replan_context=replan_context)
        if replan_context
        else plan_trip(requirements)
    )
    return {"proposal": proposal}


async def _rule_validation_node(
    state: TripConversationState,
) -> dict[str, list[ValidationIssue]]:
    """对完整需求和规划草案执行确定性校验。"""

    # 第一步：规则节点只检查可确定的硬约束，不让模型参与状态判断。
    analysis = state["analysis"]
    issues = _validate_trip_plan(
        analysis.requirements,
        state.get("proposal"),
    )
    return {"validation_issues": issues}


async def _accommodation_search_node(
    state: TripConversationState,
) -> dict[str, dict[str, object]]:
    """规划完成后查询酒店价格、房型和库存快照。"""

    requirements = state["analysis"].requirements
    if requirements is None:
        raise RuntimeError("酒店查询分支缺少完整旅行需求。")
    # 第一步：单个来源失败由 Agent 转为 unavailable，不阻断城际交通查询和审核流程。
    return {"accommodation_search": await search_accommodation(requirements)}


async def _intercity_transport_search_node(
    state: TripConversationState,
) -> dict[str, dict[str, object]]:
    """规划完成后查询飞机与火车的班次、时间和票价快照。"""

    requirements = state["analysis"].requirements
    if requirements is None:
        raise RuntimeError("城际交通查询分支缺少完整旅行需求。")
    # 第一步：单个来源失败由 Agent 转为 unavailable，不阻断酒店查询和审核流程。
    return {
        "intercity_transport_search": await search_intercity_transport(requirements)
    }


async def _direct_accommodation_search_node(
    state: TripConversationState,
) -> dict[str, object]:
    """完成用户直接发起的酒店查询并返回口语化结果。"""

    analysis = state["analysis"]
    requirements = analysis.requirements
    if requirements is None:
        raise RuntimeError("直接酒店查询缺少查询需求。")
    result = await search_accommodation(requirements)
    return {
        "accommodation_search": result,
        "analysis": analysis.model_copy(
            update={
                "reply": _format_direct_search_result("酒店查询结果", result),
                "search_results": {"accommodation": result},
            }
        ),
    }


async def _direct_intercity_transport_search_node(
    state: TripConversationState,
) -> dict[str, object]:
    """完成用户直接发起的飞机与火车查询并返回口语化结果。"""

    analysis = state["analysis"]
    requirements = analysis.requirements
    if requirements is None:
        raise RuntimeError("直接铁路查询缺少查询需求。")
    result = await search_intercity_transport(requirements)
    return {
        "intercity_transport_search": result,
        "analysis": analysis.model_copy(
            update={
                "reply": _format_direct_search_result(
                    "飞机/火车查询结果",
                    result,
                ),
                "search_results": {"intercity_transport": result},
            }
        ),
    }


async def _review_summary_node(
    state: TripConversationState,
) -> dict[str, ReviewResult]:
    """调用审核总结 Agent，并保存其结果供后续路由使用。"""

    analysis = state["analysis"]
    requirements = analysis.requirements
    proposal = state.get("proposal")
    if requirements is None or not proposal:
        raise RuntimeError("审核总结分支缺少完整需求或规划草案。")
    # 第一步：审核 Agent 只整理确定性校验结果，不自行猜测规则或改变流程状态。
    review_kwargs: dict[str, object] = {
        "validation_issues": state.get("validation_issues", []),
    }
    external_search_evidence = _get_external_search_evidence(state)
    # 第二步：没有配置来源时保持旧调用契约；配置来源后才把抓取快照交给审核 Agent。
    if _has_configured_search_result(external_search_evidence):
        review_kwargs["external_search_evidence"] = external_search_evidence
    review_result = await review_trip(
        requirements,
        proposal,
        **review_kwargs,
    )
    return {"review_result": review_result}


def _route_after_review(state: TripConversationState) -> str:
    """根据审核状态、重规划次数决定回流、待确认或用户决策分支。"""

    review_result = state["review_result"]
    replan_attempts = state.get("replan_attempts", 0)
    route = "await_confirmation"
    if review_result.status == "needs_replanning":
        route = (
            "replan"
            if replan_attempts < MAX_REPLAN_ATTEMPTS
            else "user_decision"
        )
    elif review_result.status == "needs_user_decision":
        route = "user_decision"
    # 第一步：日志只记录状态和次数，避免将审核正文、草案或用户条件写入日志。
    logger.info(
        "审核 Agent 分流：status=%s replan_attempts=%s route=%s",
        review_result.status,
        replan_attempts,
        route,
    )
    return route


def _review_feedback_node(
    state: TripConversationState,
) -> dict[str, dict[str, object] | int]:
    """将审核缺陷压缩为规划 Agent 的重规划上下文。"""

    proposal = state.get("proposal")
    review_result = state.get("review_result")
    if not proposal or review_result is None:
        raise RuntimeError("审核回流分支缺少草案或审核结果。")
    # 第一步：只传递提示词约定的三项审核 JSON 和上一版草案，明确要求规划 Agent 修复缺陷。
    replan_context = {
        "source": "review",
        "instruction": "审核未通过，请根据审核结论指出的缺陷重新规划。",
        "previous_proposal": proposal,
        "review": _review_payload(review_result),
    }
    # 第二步：每次审核回流只增加一次计数，达到上限后改由用户决定避免无限循环。
    return {
        "replan_context": replan_context,
        "replan_attempts": state.get("replan_attempts", 0) + 1,
    }


def _await_confirmation_node(
    state: TripConversationState,
) -> dict[str, object]:
    """生成待用户确认的完整方案，并将快照回传给前端。"""

    analysis = state["analysis"]
    pending_plan = _build_pending_plan(state, "待确认分支缺少完整方案状态。")
    # 第一步：审核通过后保留需求、草案和审核结论，让下一轮确认或修改可恢复上下文。
    reply = _build_reviewed_plan_reply(
        pending_plan.proposal,
        pending_plan.review_result,
        external_search_evidence=_get_external_search_evidence(state),
        risk_heading="风险提示",
        status_text="该方案已完成规划、规则校验和审核总结，等待您确认。",
    )
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


def _user_decision_node(
    state: TripConversationState,
) -> dict[str, object]:
    """在不可自动修复或达到次数上限时返回缺陷与可继续修改的方案快照。"""

    analysis = state["analysis"]
    pending_plan = _build_pending_plan(state, "用户决策分支缺少完整方案状态。")
    # 第一步：保留当前快照，用户补充修改后仍能携带上一版方案进入下一轮重规划。
    reply = _build_reviewed_plan_reply(
        pending_plan.proposal,
        pending_plan.review_result,
        external_search_evidence=_get_external_search_evidence(state),
        risk_heading="需处理缺陷",
        status_text="该方案暂不能自动通过审核，请补充修改要求后重新规划。",
    )
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


async def _confirm_trip_node(
    state: TripConversationState,
) -> dict[str, object]:
    """确认方案并补充图片、路线等前端展示数据。"""

    analysis = state["analysis"]
    pending_plan = _get_pending_plan(state)
    if pending_plan is None:
        raise RuntimeError("确认分支缺少待确认方案。")
    confirmed_details = await build_confirmed_trip_details(
        pending_plan.requirements,
    )
    confirmed_plan = ConfirmedTripPlan(
        requirements=pending_plan.requirements,
        proposal=pending_plan.proposal,
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


def _clarify_intent_node(
    state: TripConversationState,
) -> dict[str, ConversationAnalysis]:
    """为无法稳定判断的消息生成澄清回复。"""

    # 第一步：不确定意图只结束当前轮，不让需求分析节点凭关键词追问旅行字段。
    pending_plan = _get_pending_plan(state)
    return {
        "analysis": ConversationAnalysis(
            intent="chat",
            reply="我还不确定您是想继续调整行程，还是咨询其他内容，请再具体说明一下。",
            pending_plan=pending_plan,
        )
    }


def _blocked_confirmation_node(
    state: TripConversationState,
) -> dict[str, ConversationAnalysis]:
    """处理尚未满足确认条件的确认请求。"""

    analysis = state["analysis"]
    pending_plan = _get_pending_plan(state)
    if pending_plan is None:
        raise RuntimeError("确认阻断分支缺少待确认方案。")
    if pending_plan.review_result.status == "needs_replanning":
        reply = "当前方案仍有可自动修复的问题，不能确认，请稍后重新规划。"
    else:
        reply = "当前方案仍有待处理风险，不能确认，请先补充修改要求或明确接受相关风险。"
    return {
        "analysis": analysis.model_copy(
            update={
                "reply": reply,
                "pending_plan": pending_plan,
            }
        )
    }


def _validate_trip_plan(
    requirements: TripRequirements | None,
    proposal: str | None,
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
    return issues


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


def _build_reviewed_plan_reply(
    proposal: str,
    review_result: ReviewResult,
    *,
    external_search_evidence: dict[str, object] | None = None,
    risk_heading: str,
    status_text: str,
) -> str:
    """将规划草案和审核总结整理为前端可直接展示的文本。"""

    sections = [
        proposal.strip(),
        f"## 审核总结\n{review_result.summary}",
    ]
    external_section = _format_external_search_evidence(
        external_search_evidence or {}
    )
    if external_section:
        sections.append(external_section)
    if review_result.risks:
        sections.append(
            f"## {risk_heading}\n"
            + "\n".join(f"- {risk}" for risk in review_result.risks)
        )
    if review_result.pending_items:
        sections.append(
            "## 待确认项\n"
            + "\n".join(f"- {item}" for item in review_result.pending_items)
        )
    # 第一步：显式保留当前能力边界，避免把尚未实现的规则校验误称为已通过。
    sections.append(
        f"## 当前状态\n{status_text}"
    )
    return "\n\n".join(sections)


def _get_external_search_evidence(
    state: TripConversationState,
) -> dict[str, object]:
    """读取两个并行查询 Agent 的结果，供审核和最终回复复用。"""

    return {
        "accommodation": state.get("accommodation_search", {}),
        "intercity_transport": state.get("intercity_transport_search", {}),
    }


def _has_configured_search_result(evidence: dict[str, object]) -> bool:
    """判断是否至少有一个酒店或交通估算结果。"""

    return any(
        isinstance(result, dict)
        and result.get("status") in {"available", "estimated"}
        for result in evidence.values()
    )


def _format_direct_search_result(
    title: str,
    result: dict[str, object],
) -> str:
    """把单个直接查询结果整理成用户可读的口头结果。"""

    if result.get("status") == "unavailable":
        message = result.get("message")
        return str(message or "当前查询暂时没有可用结果。")

    section = _format_available_search_result(
        title,
        result,
        HOTEL_OFFER_LABELS if "酒店" in title else TRANSPORT_OFFER_LABELS,
    )
    return section or "当前查询暂时没有可用结果。"


def _format_external_search_evidence(
    evidence: dict[str, object],
) -> str:
    """把查询快照压缩为用户可直接阅读的口头结果。"""

    sections: list[str] = []
    for key, title, labels in (
        (
            "accommodation",
            "酒店查询结果",
            HOTEL_OFFER_LABELS,
        ),
        (
            "intercity_transport",
            "飞机/火车查询结果",
            TRANSPORT_OFFER_LABELS,
        ),
    ):
        result = evidence.get(key)
        if not isinstance(result, dict):
            continue
        status = result.get("status")
        if status == "unavailable":
            message = result.get("message")
            if isinstance(message, str) and message.strip():
                sections.append(f"## {title}\n{message}")
            continue
        section = _format_available_search_result(title, result, labels)
        if section:
            sections.append(section)
    return "\n\n".join(sections)


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
) -> dict[str, object]:
    """构造用户修改触发的重规划反馈。"""

    # 第一步：保留上一版草案、审核结论和最新修改消息，避免规划 Agent 忽略已整理信息。
    return {
        "source": "user",
        "instruction": "用户已修改行程条件，请保留未冲突信息并重新规划。",
        "previous_proposal": pending_plan.proposal,
        "review": _review_payload(pending_plan.review_result),
        "user_message": message.content,
    }


def _review_payload(review_result: ReviewResult) -> dict[str, object]:
    """按审核提示词约定的三字段格式构造重规划反馈。"""

    # 第一步：状态属于图层路由信息，不传给规划 Agent，避免其误将状态文本当作用户需求。
    return {
        "summary": review_result.summary,
        "risks": review_result.risks,
        "pending_items": review_result.pending_items,
    }
