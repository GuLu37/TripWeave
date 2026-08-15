"""TripWeave 的统一对话入口 Agent。"""

import json
import re
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents.prompts import load_prompt
from app.api.exception.error_handler import record_error
from app.api.exception.exceptions import AppException
from app.integrations.llm.client import chat_with_llm
from app.integrations.llm.response_cleaner import extract_json_response
from app.memory.short_term_memory import build_memory_prompt_context
from app.schemas import (
    ClientChatMessage,
    ConversationAnalysis,
    TripPlanSnapshot,
    TripRequirements,
)

# MVP 阶段将目的地、出行时间和人数作为进入行程规划的最低信息门槛。
CORE_REQUIREMENT_FIELDS = (
    "destination",
    "departure_date",
    "traveler_count",
)
FOLLOW_UP_QUESTIONS = {
    "destination": "请问此次出差或旅行的目的地是哪里？",
    "departure_date": "请问您计划哪天出发？",
    "trip_schedule": "请问您的返程日期，或计划出行几天？",
    "traveler_count": "请问此次一共几人出行？",
}
_RELATIVE_DAY_OFFSETS = {
    "今天": 0,
    "明天": 1,
    "后天": 2,
}
_WEEKDAY_VALUES = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
_RELATIVE_WEEKDAY_PATTERN = re.compile(
    r"^(?P<week>本周|这周|下周)(?:周|星期)?(?P<weekday>[一二三四五六日天])$"
)
_CHINESE_FULL_DATE_PATTERN = re.compile(
    r"^(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日?$"
)
_CHINESE_MONTH_DAY_PATTERN = re.compile(
    r"^(?P<month>\d{1,2})月(?P<day>\d{1,2})日?$"
)
# 保留近期对话即可覆盖当前追问，历史需求由 known_requirements 快照补充。
CONTEXT_MESSAGE_LIMIT = 8
STRUCTURED_OUTPUT_TEMPERATURE = 0.1
STRUCTURED_OUTPUT_MAX_TOKENS = 1024
INTERACTIVE_MAX_ATTEMPTS = 2


class ConversationAnalysisException(AppException):
    """统一入口 Agent 无法生成有效结构化结果时抛出的异常。"""

    @classmethod
    def invalid_model_output(cls) -> "ConversationAnalysisException":
        """创建统一入口结果格式异常。"""

        return cls(
            status_code=502,
            code="CONVERSATION_ANALYSIS_INVALID_OUTPUT",
            message="对话分析服务返回了无法识别的结果，请稍后重试。",
        )


class IntentDecision(BaseModel):
    """入口意图节点返回的最小结构化结果。"""

    intent: Literal[
        "chat",
        "trip_planning",
        "accommodation_search",
        "intercity_transport_search",
        "uncertain",
    ]
    plan_action: Literal["plan", "modify", "confirm"] | None = None
    reply: str = Field(min_length=1, max_length=4_000)


async def analyze_intent(
    messages: list[ClientChatMessage],
    *,
    known_requirements: TripRequirements | None = None,
    pending_plan: TripPlanSnapshot | None = None,
    memory_summary: str | None = None,
) -> IntentDecision:
    """只判断当前消息意图，不提取旅差字段。"""

    # 第一步：意图节点只读取近期消息和状态快照，避免提前承担需求完整性判断。
    context_messages = messages[-CONTEXT_MESSAGE_LIMIT:]
    try:
        response_text = await chat_with_llm(
            context_messages,
            system_prompt=_build_intent_system_prompt(
                known_requirements,
                pending_plan,
                memory_summary,
            ),
            response_validator=_validate_intent_payload,
            temperature=STRUCTURED_OUTPUT_TEMPERATURE,
            max_tokens=STRUCTURED_OUTPUT_MAX_TOKENS,
            max_attempts=INTERACTIVE_MAX_ATTEMPTS,
            json_mode=True,
            caller_name="conversation_intent_agent",
        )
        model_decision = _parse_intent_payload(response_text)
        if model_decision.intent != "uncertain":
            # 第二步：模型给出明确意图时直接沿用其语义结果和回复。
            return model_decision
    except AppException as error:
        record_error(
            error,
            component="agent",
            source="conversation_entry",
            operation="intent_detection",
            context={"fallback": "rules"},
            default_code="CONVERSATION_INTENT_FAILED",
            default_message="意图判断 Agent 调用失败，已进入规则兜底。",
        )
    # 第三步：模型不确定或调用失败时扫描当前用户整句和结构化上下文。
    return _resolve_intent_with_rules(
        context_messages,
        known_requirements=known_requirements,
        pending_plan=pending_plan,
    )


async def _request_requirement_payload(
    messages: list[ClientChatMessage],
    *,
    system_prompt: str,
    caller_name: str,
) -> ConversationAnalysis:
    """调用需求分析模型并解析统一的需求契约。"""

    # 需求分析和直接查询共用同一套 JSON 重试、长度和解析规则，差异只在提示词与意图校验。
    response_text = await chat_with_llm(
        messages,
        system_prompt=system_prompt,
        response_validator=_validate_conversation_payload,
        temperature=STRUCTURED_OUTPUT_TEMPERATURE,
        max_tokens=STRUCTURED_OUTPUT_MAX_TOKENS,
        max_attempts=INTERACTIVE_MAX_ATTEMPTS,
        json_mode=True,
        caller_name=caller_name,
    )
    return _parse_conversation_payload(response_text)


async def analyze_requirements(
    messages: list[ClientChatMessage],
    *,
    intent: IntentDecision,
    known_requirements: TripRequirements | None = None,
    pending_plan: TripPlanSnapshot | None = None,
    memory_summary: str | None = None,
) -> ConversationAnalysis:
    """在旅差意图确定后分析、合并和校验旅行需求。"""

    if intent.intent != "trip_planning":
        raise ConversationAnalysisException.invalid_model_output()

    # 第一步：需求节点单独读取近期消息和快照，专注字段提取而不是重新判断意图。
    context_messages = messages[-CONTEXT_MESSAGE_LIMIT:]
    try:
        payload = await _request_requirement_payload(
            context_messages,
            system_prompt=_build_conversation_system_prompt(
                known_requirements,
                pending_plan,
                memory_summary,
            ),
            caller_name="conversation_requirement_agent",
        )
        if payload.intent != "trip_planning" or payload.requirements is None:
            raise ConversationAnalysisException.invalid_model_output()
    except AppException as error:
        # 模型格式漂移时保留用户原话中的确定字段，避免完整请求直接变成 502。
        record_error(
            error,
            component="agent",
            source="conversation_entry",
            operation="requirement_analysis",
            context={"fallback": "local_fields"},
            default_code="CONVERSATION_REQUIREMENT_FAILED",
            default_message="需求分析 Agent 调用失败，已使用本地字段兜底。",
        )
        requirements = _repair_missing_requirements(
            known_requirements or TripRequirements(),
            context_messages,
        )
        requirements = _normalize_trip_dates(requirements)
        missing_fields = _get_missing_fields(requirements)
        return ConversationAnalysis(
            intent="trip_planning",
            reply=(
                _build_follow_up_reply(missing_fields)
                if missing_fields
                else "已读取您的最新行程信息，正在重新规划。"
            ),
            requirements=requirements,
            plan_action=_resolve_plan_action(intent.plan_action, pending_plan),
            missing_fields=missing_fields,
            is_complete=not missing_fields,
        )

    # 第二步：先合并结构化结果，再从用户原话补回模型漏掉的确定性日期。
    requirements = _merge_requirements(
        known_requirements,
        payload.requirements,
    )
    requirements = _repair_missing_requirements(requirements, context_messages)
    requirements = _normalize_trip_dates(requirements)
    plan_action = _resolve_plan_action(intent.plan_action, pending_plan)
    missing_fields = _get_missing_fields(requirements)
    # 第三步：完整性只由本地规则决定，模型回复不能绕过缺失字段追问。
    reply = _build_follow_up_reply(missing_fields) if missing_fields else payload.reply
    return ConversationAnalysis(
        intent="trip_planning",
        reply=reply,
        requirements=requirements,
        plan_action=plan_action,
        missing_fields=missing_fields,
        is_complete=not missing_fields,
    )


async def analyze_search_requirements(
    messages: list[ClientChatMessage],
    *,
    intent: IntentDecision,
    known_requirements: TripRequirements | None = None,
    pending_plan: TripPlanSnapshot | None = None,
    memory_summary: str | None = None,
) -> ConversationAnalysis:
    """提取酒店或铁路直接查询所需的最小字段。"""

    if intent.intent not in {
        "accommodation_search",
        "intercity_transport_search",
    }:
        raise ConversationAnalysisException.invalid_model_output()

    context_messages = messages[-CONTEXT_MESSAGE_LIMIT:]
    try:
        payload = await _request_requirement_payload(
            context_messages,
            system_prompt=_build_search_system_prompt(
                intent.intent,
                known_requirements,
                pending_plan,
                memory_summary,
            ),
            caller_name=f"conversation_{intent.intent}_agent",
        )
        if payload.intent != intent.intent or payload.requirements is None:
            raise ConversationAnalysisException.invalid_model_output()
    except AppException as error:
        # 直接查询格式失败时也保留路线、日期和人数，继续生成确定性追问。
        record_error(
            error,
            component="agent",
            source="conversation_entry",
            operation="search_requirement_analysis",
            context={
                "intent": intent.intent,
                "fallback": "local_fields",
            },
            default_code="CONVERSATION_SEARCH_REQUIREMENT_FAILED",
            default_message="直接查询需求分析 Agent 调用失败，已使用本地字段兜底。",
        )
        requirements = _repair_search_requirements(
            known_requirements or TripRequirements(),
            context_messages,
        )
        requirements = _normalize_trip_dates(requirements)
        missing_fields = _get_search_missing_fields(intent.intent, requirements)
        return ConversationAnalysis(
            intent=intent.intent,
            reply=(
                _build_search_follow_up_reply(intent.intent, missing_fields)
                if missing_fields
                else "已读取查询条件，正在查询最新信息。"
            ),
            requirements=requirements,
            pending_plan=pending_plan,
            missing_fields=missing_fields,
            is_complete=not missing_fields,
        )

    requirements = _merge_requirements(known_requirements, payload.requirements)
    requirements = _repair_search_requirements(requirements, context_messages)
    requirements = _normalize_trip_dates(requirements)
    missing_fields = _get_search_missing_fields(intent.intent, requirements)
    reply = (
        _build_search_follow_up_reply(intent.intent, missing_fields)
        if missing_fields
        else payload.reply
    )
    return payload.model_copy(
        update={
            "reply": reply,
            "requirements": requirements,
            "plan_action": None,
            "pending_plan": pending_plan,
            "missing_fields": missing_fields,
            "is_complete": not missing_fields,
        }
    )


async def analyze_conversation(
    messages: list[ClientChatMessage],
    *,
    known_requirements: TripRequirements | None = None,
    pending_plan: TripPlanSnapshot | None = None,
    memory_summary: str | None = None,
) -> ConversationAnalysis:
    """兼容旧调用方，按意图节点和需求节点顺序完成入口分析。"""

    # 第一步：先完成意图判断，普通聊天不进入需求分析节点。
    intent = await analyze_intent(
        messages,
        known_requirements=known_requirements,
        pending_plan=pending_plan,
        memory_summary=memory_summary,
    )
    if intent.intent == "chat":
        return ConversationAnalysis(intent="chat", reply=intent.reply)
    if intent.intent in {
        "accommodation_search",
        "intercity_transport_search",
    }:
        return await analyze_search_requirements(
            messages,
            intent=intent,
            known_requirements=known_requirements,
            pending_plan=pending_plan,
            memory_summary=memory_summary,
        )
    # 第二步：旅差意图进入独立需求分析，保证后续流程按状态机顺序执行。
    return await analyze_requirements(
        messages,
        intent=intent,
        known_requirements=known_requirements,
        pending_plan=pending_plan,
        memory_summary=memory_summary,
    )


def _build_intent_system_prompt(
    known_requirements: TripRequirements | None,
    pending_plan: TripPlanSnapshot | None,
    memory_summary: str | None = None,
) -> str:
    """组合意图节点所需的最小状态上下文。"""

    # 第一步：意图节点只加载意图提示词，减少与字段分析无关的规则干扰。
    prompt_parts = [load_prompt("intent_detection_prompt.md")]
    memory_context = build_memory_prompt_context(memory_summary)
    if memory_context is not None:
        prompt_parts.append(memory_context)
    if known_requirements is not None:
        snapshot = known_requirements.model_dump(
            exclude_none=True,
            exclude_defaults=True,
        )
        if snapshot:
            prompt_parts.append(
                "已确认需求快照如下：\n"
                f"{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}"
            )
    if pending_plan is not None:
        prompt_parts.append("当前存在待确认方案快照，请判断用户是确认还是修改。")
    return "\n\n".join(prompt_parts)


def _build_search_system_prompt(
    intent: str,
    known_requirements: TripRequirements | None,
    pending_plan: TripPlanSnapshot | None,
    memory_summary: str | None = None,
) -> str:
    """组合直接查询节点所需的提示词和上下文。"""

    prompt_parts = [
        load_prompt("search_requirement_prompt.md"),
        f"当前查询类型：{intent}",
    ]
    memory_context = build_memory_prompt_context(memory_summary)
    if memory_context is not None:
        prompt_parts.append(memory_context)
    if known_requirements is not None:
        snapshot = known_requirements.model_dump(
            exclude_none=True,
            exclude_defaults=True,
        )
        if snapshot:
            prompt_parts.append(
                "已确认需求快照如下，仅用于补充当前查询缺项；最新用户消息优先：\n"
                f"{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}"
            )
    if pending_plan is not None:
        prompt_parts.append(
            "当前存在待确认行程方案。直接查询只读取其中的地点、日期和人数，"
            "不得把本次查询改写为行程修改。"
        )
    return "\n\n".join(prompt_parts)


_INTENT_RULE_PATTERNS = {
    "greeting": re.compile(
        r"^\s*(?:你好|您好|早上好|中午好|晚上好|嗨|hello|hi|谢谢|感谢|再见|晚安)"
        r"[！!。.\s]*$",
        re.IGNORECASE,
    ),
    "trip_action": re.compile(
        r"(?:计划|打算|安排|准备|想去|出发|返程|行程|旅游|旅行|出差|游玩|去玩|"
        r"玩几天|住几晚|预订|预定)"
    ),
    "trip_detail": re.compile(
        r"(?:今天|明天|后天|本周|这周|下周|"
        r"\d{4}年\d{1,2}月\d{1,2}日?|\d{1,2}月\d{1,2}日?|"
        r"\d+(?:\.\d+)?\s*(?:天|日|周|星期|月|个月|小时)|"
        r"\d{1,3}\s*(?:人|个人|位)|从[\u4e00-\u9fffA-Za-z·]{2,20}出发|"
        r"(?:去|到|前往)[\u4e00-\u9fffA-Za-z·]{2,20})"
    ),
    "modify": re.compile(
        r"(?:修改|调整|改成|改为|更换|换成|换个|取消|增加|减少|删除|重新规划|重写)"
    ),
    "confirm": re.compile(r"(?:确认|就这样|按这个|没问题|可以|好的)"),
    "weather": re.compile(r"(?:天气|气温|下雨|降雨|晴天|多云|温度)"),
    "accommodation_query": re.compile(
        r"(?=.*(?:酒店|住宿|房价|房型|入住|退房))"
        r"(?=.*(?:查|查询|搜索|看看|有没有|价格|房价|房型|入住|退房|多少钱|预订|预定))"
    ),
    "transport_query": re.compile(
        r"(?=.*(?:12306|火车|高铁|动车|车次|班次|余票|火车票|飞机|航班|机票|航空))"
        r"(?=.*(?:查|查询|搜索|看看|有哪些|有没有|多少钱|班次|车次|余票|时刻))"
    ),
    "question": re.compile(r"(?:什么|怎么|如何|哪里|哪些|能不能|可以吗|推荐|介绍|为什么)"),
}


def _resolve_intent_with_rules(
    messages: list[ClientChatMessage],
    *,
    known_requirements: TripRequirements | None,
    pending_plan: TripPlanSnapshot | None,
) -> IntentDecision:
    """在模型不确定或不可用时，用整句标签和上下文收敛意图。"""

    # 第一步：只扫描最近一条用户句子，避免历史摘要中的关键词截断当前意图。
    latest_user_text = next(
        (
            message.content.strip()
            for message in reversed(messages)
            if message.role == "user"
        ),
        "",
    )
    tags = _scan_intent_tags(latest_user_text)
    has_active_trip = known_requirements is not None or pending_plan is not None
    has_trip_context = bool(tags & {"trip_action", "trip_detail"})
    is_modify = "modify" in tags
    is_confirm = "confirm" in tags and not is_modify

    # 第二步：没有旅行动作的独立天气问句优先按聊天处理，避免“今天”误触发行程规划。
    if "weather" in tags and "trip_action" not in tags and not is_modify:
        return IntentDecision(
            intent="chat",
            plan_action=None,
            reply="我先按普通对话处理，您可以继续描述想了解的内容。",
        )

    # 第三步：独立查询优先进入专用查询分支，但明确修改方案仍属于旅差规划。
    if "accommodation_query" in tags and not is_modify:
        return IntentDecision(
            intent="accommodation_search",
            plan_action=None,
            reply="开始查询酒店信息。",
        )
    if "transport_query" in tags and not is_modify:
        return IntentDecision(
            intent="intercity_transport_search",
            plan_action=None,
            reply="开始查询飞机或火车班次信息。",
        )

    # 第四步：待确认方案下的确认和修改必须优先于普通聊天标签。
    if pending_plan is not None and is_confirm:
        return IntentDecision(
            intent="trip_planning",
            plan_action="confirm",
            reply="正在确认当前方案。",
        )
    if has_active_trip and (is_modify or has_trip_context):
        return IntentDecision(
            intent="trip_planning",
            plan_action="modify" if pending_plan is not None else "plan",
            reply="开始分析您的行程调整。",
        )

    # 第五步：没有活动行程时，旅行动作必须和日期、人数、地点等细节形成有效句式。
    if "trip_action" in tags and "trip_detail" in tags:
        return IntentDecision(
            intent="trip_planning",
            plan_action="plan",
            reply="开始分析需求。",
        )
    if has_active_trip and "trip_detail" in tags:
        return IntentDecision(
            intent="trip_planning",
            plan_action="modify" if pending_plan is not None else "plan",
            reply="开始分析您补充的行程信息。",
        )

    # 第六步：规则只对简单问候直接生成安全回复；其他普通聊天由兜底回复保守承接。
    if "greeting" in tags:
        return IntentDecision(
            intent="chat",
            plan_action=None,
            reply="您好！有什么可以帮您的吗？",
        )
    return IntentDecision(
        intent="chat",
        plan_action=None,
        reply="我先按普通对话处理，您可以继续描述想了解的内容。",
    )


def _scan_intent_tags(text: str) -> set[str]:
    """扫描当前整句中的意图标签，不截取局部片段做判断。"""

    # 第一步：每个标签都基于完整句子匹配，保留多标签结果供上层处理冲突。
    return {
        tag
        for tag, pattern in _INTENT_RULE_PATTERNS.items()
        if pattern.search(text)
    }


def _build_conversation_system_prompt(
    known_requirements: TripRequirements | None,
    pending_plan: TripPlanSnapshot | None = None,
    memory_summary: str | None = None,
) -> str:
    """组合需求分析节点指令与可选需求快照。"""

    # 第一步：固定指令定义意图边界和结构化输出契约。
    prompt = load_prompt("conversation_entry_prompt.md")
    prompt_parts = [prompt]
    memory_context = build_memory_prompt_context(memory_summary)
    if memory_context is not None:
        prompt_parts.append(memory_context)
    if known_requirements is not None:
        # 第二步：仅注入已确认字段，且明确当前消息优先于历史快照。
        snapshot = known_requirements.model_dump(
            exclude_none=True,
            exclude_defaults=True,
        )
        if snapshot:
            prompt_parts.append(
                "已确认需求快照如下，仅用于补充较早上下文；最新用户消息和近期对话优先：\n"
                f"{json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))}"
            )
    if pending_plan is not None:
        # 第三步：入口只读取待确认方案的需求和审核结论，完整草案留给规划 Agent 重规划时使用。
        pending_context = {
            "requirements": pending_plan.requirements.model_dump(
                exclude_none=True,
                exclude_defaults=True,
            ),
            "review": {
                "summary": pending_plan.review_result.summary,
                "risks": pending_plan.review_result.risks,
                "pending_items": pending_plan.review_result.pending_items,
            },
        }
        prompt_parts.append(
            "当前存在待用户确认的方案快照。请据此判断用户是确认当前方案还是修改方案：\n"
            f"{json.dumps(pending_context, ensure_ascii=False, separators=(',', ':'))}"
        )
    return "\n\n".join(prompt_parts)


def _resolve_plan_action(
    plan_action: str | None,
    pending_plan: TripPlanSnapshot | None,
) -> str:
    """按待确认状态收敛入口 Agent 的方案动作。"""

    # 第一步：没有待确认方案时不接受确认或修改分支，完整需求一律视为首次规划。
    if pending_plan is None:
        return "plan"
    # 第二步：待确认方案存在时只接受公开动作枚举，缺失动作保守地按修改处理。
    return plan_action if plan_action in {"modify", "confirm"} else "modify"


def _parse_conversation_payload(response_text: str) -> ConversationAnalysis:
    """解析单一入口 Agent 返回的 JSON 契约。"""

    try:
        # 第一步：统一清洗器处理代码围栏和前置说明后提取 JSON 对象。
        payload = extract_json_response(response_text)
        # 第二步：需求节点不负责方案动作；忽略模型误填的 plan_action，避免其阻断确定性需求提取。
        if isinstance(payload, dict) and "plan_action" in payload:
            payload = {**payload, "plan_action": None}
        # 第三步：不猜测或修复意图、人数、时长等业务字段，继续按契约校验领域结果。
        return ConversationAnalysis.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        record_error(
            error,
            component="agent",
            source="conversation_entry",
            operation="parse_conversation_payload",
            default_code="CONVERSATION_ANALYSIS_INVALID_OUTPUT",
            default_message="统一入口 Agent 返回结构化数据失败。",
        )
        raise ConversationAnalysisException.invalid_model_output() from error


def _parse_intent_payload(response_text: str) -> IntentDecision:
    """解析意图节点返回的 JSON 契约。"""

    try:
        # 第一步：只校验意图、方案动作和短回复，不让意图节点承担需求字段契约。
        payload = extract_json_response(response_text)
        return IntentDecision.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        record_error(
            error,
            component="agent",
            source="conversation_entry",
            operation="parse_intent_payload",
            default_code="CONVERSATION_INTENT_INVALID_OUTPUT",
            default_message="意图判断 Agent 返回结构化数据失败。",
        )
        raise ConversationAnalysisException.invalid_model_output() from error


def _validate_conversation_payload(response_text: str) -> None:
    """验证 Agent 输出满足意图对应的最小领域契约。"""

    # 第一步：验证 JSON、字段类型与 intent 的取值均符合公开数据契约。
    payload = _parse_conversation_payload(response_text)


def _validate_intent_payload(response_text: str) -> None:
    """验证意图节点输出满足最小契约。"""

    # 第一步：执行意图 JSON 校验，失败时交由统一 LLM 重试链路处理。
    _parse_intent_payload(response_text)


def _merge_requirements(
    known_requirements: TripRequirements | None,
    current_requirements: TripRequirements,
) -> TripRequirements:
    """将本轮明确提取的字段覆盖到已确认需求快照。"""

    # 第一步：首次收集无需合并，直接保留模型已通过 Pydantic 校验的需求。
    if known_requirements is None:
        return current_requirements
    # 第二步：只覆盖模型本轮实际输出的字段，不能用 Pydantic 默认空值覆盖已确认约束。
    updates = {
        field_name: value
        for field_name, value in current_requirements.model_dump(
            exclude_unset=True
        ).items()
        if value is not None
    }
    # 第三步：嵌套模型经 model_dump 后会变为字典，必须重新校验以恢复 TripDuration 等字段类型。
    merged_data = known_requirements.model_dump()
    merged_data.update(updates)
    return TripRequirements.model_validate(merged_data)


_DATE_EXPRESSION_PATTERN = re.compile(
    r"(今天|明天|后天|"
    r"(?:本周|这周|下周)(?:周|星期)?[一二三四五六日天]|"
    r"\d{4}年\d{1,2}月\d{1,2}日?|"
    r"\d{1,2}月\d{1,2}日?)"
)
_TRAVELER_COUNT_PATTERN = re.compile(
    r"(?P<count>\d{1,3}|[零一二两三四五六七八九十百]+)\s*(?:个人|人|名同行者|位同行者)"
)
_DURATION_PATTERN = re.compile(
    r"(?P<amount>\d+(?:\.\d+)?|[零一二两三四五六七八九十百]+)"
    r"\s*(?P<unit>天|日|晚|周|星期|个月|月|小时)"
)
_ORIGIN_PATTERN = re.compile(
    r"从(?P<origin>[\u4e00-\u9fffA-Za-z·]{2,30}?)(?="
    r"出发|飞到|飞往|飞至|到|至|去)"
)
_ROUTE_PATTERN = re.compile(
    r"(?:帮我\s*)?(?:查一下|查询|查|请)?\s*(?:从)?"
    r"(?P<origin>[\u4e00-\u9fffA-Za-z·]{2,20}?)(?:到|至)"
    r"(?P<destination>[\u4e00-\u9fffA-Za-z·]{2,20}?)(?="
    r"今天|明天|后天|本周|这周|下周|\d{1,2}月|\d{4}年|"
    r"旅游|旅行|出差|酒店|住宿|高铁|火车|$)"
)
_DESTINATION_PATTERN = re.compile(
    r"(?:去|到|前往|飞到|飞往|飞至)"
    r"(?P<destination>[\u4e00-\u9fffA-Za-z·]{2,20}?)(?="
    r"[\s，,。；;、：:！!？?]*"
    r"(?:酒店|住宿|旅游|旅行|出差|游玩|玩|查|查询|搜索|"
    r"今天|明天|后天|本周|这周|下周|\d{1,2}月|\d{4}年|"
    r"[\d零一二两三四五六七八九十百]+\s*(?:天|日|晚|周|星期|个月|月)|$))"
)


def _repair_missing_requirements(
    requirements: TripRequirements,
    messages: list[ClientChatMessage],
) -> TripRequirements:
    """从近期用户原话补回模型遗漏的确定性需求字段。"""

    # 第一步：只检查用户消息中的日期表达，不从助手回复反推用户需求。
    user_text = "\n".join(
        message.content
        for message in reversed(messages)
        if message.role == "user"
    )
    updates: dict[str, object] = {}
    # 第二步：当前用户明确修改日期、人数或路线时覆盖旧快照，避免继续使用第一次方案。
    candidates = _DATE_EXPRESSION_PATTERN.findall(user_text)
    for candidate in reversed(candidates):
        normalized_date = _normalize_trip_date(candidate, date.today())
        if normalized_date is not None:
            updates["departure_date"] = normalized_date
            break
    traveler_count = _extract_traveler_count(user_text)
    if traveler_count is not None:
        updates["traveler_count"] = traveler_count
    trip_duration = _extract_trip_duration(user_text)
    if trip_duration is not None:
        updates["trip_duration"] = trip_duration
    origin_match = _ORIGIN_PATTERN.search(user_text)
    if origin_match is not None:
        updates["origin"] = origin_match["origin"]
    destination_match = _DESTINATION_PATTERN.search(user_text)
    if destination_match is not None and (
        not requirements.destination
        or destination_match["destination"] != requirements.destination
    ):
        updates["destination"] = destination_match["destination"]
    if not updates:
        return requirements
    # 第六步：重新经过 Pydantic 校验，确保补回的旅行时长恢复为 TripDuration 类型。
    repaired_data = requirements.model_dump()
    repaired_data.update(updates)
    return TripRequirements.model_validate(repaired_data)


def _repair_search_requirements(
    requirements: TripRequirements,
    messages: list[ClientChatMessage],
) -> TripRequirements:
    """从查询原话补回路线、目的地和住宿晚数等确定性字段。"""

    user_text = "\n".join(
        message.content
        for message in reversed(messages)
        if message.role == "user"
    )
    updates: dict[str, object] = {}
    route_match = _ROUTE_PATTERN.search(user_text)
    if route_match is not None:
        updates["origin"] = route_match["origin"]
        updates["destination"] = route_match["destination"]
    origin_match = _ORIGIN_PATTERN.search(user_text)
    if origin_match is not None:
        updates["origin"] = origin_match["origin"]
    destination_match = _DESTINATION_PATTERN.search(user_text)
    if destination_match is not None:
        updates["destination"] = destination_match["destination"]
    candidates = _DATE_EXPRESSION_PATTERN.findall(user_text)
    for candidate in reversed(candidates):
        normalized_date = _normalize_trip_date(candidate, date.today())
        if normalized_date is not None:
            updates["departure_date"] = normalized_date
            break
    trip_duration = _extract_trip_duration(user_text)
    if trip_duration is not None:
        updates["trip_duration"] = trip_duration
    traveler_count = _extract_traveler_count(user_text)
    if traveler_count is not None:
        updates["traveler_count"] = traveler_count
    if not updates:
        return requirements
    repaired_data = requirements.model_dump()
    repaired_data.update(updates)
    return TripRequirements.model_validate(repaired_data)


def _get_search_missing_fields(
    intent: str,
    requirements: TripRequirements,
) -> list[str]:
    """按查询类型计算进入 MCP 查询所需的最低字段。"""

    if intent == "accommodation_search":
        missing_fields = [
            field_name
            for field_name in ("destination", "departure_date", "traveler_count")
            if not getattr(requirements, field_name)
        ]
        if not requirements.return_date and not requirements.trip_duration:
            missing_fields.insert(2, "trip_schedule")
        return missing_fields
    return [
        field_name
        for field_name in ("origin", "destination", "departure_date")
        if not getattr(requirements, field_name)
    ]


def _build_search_follow_up_reply(intent: str, missing_fields: list[str]) -> str:
    """为直接查询生成一条最小追问。"""

    questions = {
        "accommodation_search": {
            "destination": "请问要查询哪个城市的酒店？",
            "departure_date": "请问计划哪天入住？",
            "trip_schedule": "请问住几晚，或入住和退房日期分别是哪天？",
            "traveler_count": "请问酒店需要几位入住？",
        },
        "intercity_transport_search": {
            "origin": "请问从哪里出发？",
            "destination": "请问要前往哪里？",
            "departure_date": "请问计划哪天出发？",
        },
    }
    return questions[intent][missing_fields[0]]


def _extract_traveler_count(text: str) -> int | None:
    """从用户原话提取明确的出行人数。"""

    # 第一步：优先读取明确人数，避免“我和家人五个人”被误判成单人。
    matches = list(_TRAVELER_COUNT_PATTERN.finditer(text))
    if matches:
        count = _parse_chinese_integer(matches[-1]["count"])
        if count is not None and 1 <= count <= 100:
            return count

    # 第二步：明确的独自、单人和“我/本人”主语均归一为 1 人。
    if re.search(r"(?:我|本人)?(?:独自|单人|一个人|一人)", text):
        return 1
    if re.search(r"^\s*(?:我|本人)(?=\s|[，,。.!！?？]|今天|明天|后天|"
                 r"本周|这周|下周|\d)", text) and not re.search(
        r"(?:和|跟|与|及|带着|家人|朋友|同事|同行|我们|一家|夫妻|孩子|父母)",
        text,
    ):
        return 1
    return None


def _extract_trip_duration(text: str) -> object | None:
    """从用户原话提取明确的旅行时长对象。"""

    # 第一步：从最近的数字和时长单位组合中提取旅行时长。
    matches = list(_DURATION_PATTERN.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    amount_text = match["amount"]
    try:
        amount = float(amount_text)
    except ValueError:
        integer = _parse_chinese_integer(amount_text)
        if integer is None:
            return None
        amount = float(integer)
    if amount <= 0:
        return None
    unit = {
        "天": "day",
        "日": "day",
        "晚": "day",
        "周": "week",
        "星期": "week",
        "个月": "month",
        "月": "month",
        "小时": "hour",
    }[match["unit"]]
    return {
        "raw_text": f"{amount_text}{match['unit']}",
        "amount": amount,
        "unit": unit,
        "is_approximate": False,
    }


def _parse_chinese_integer(value: str) -> int | None:
    """将常见中文人数表达转换为整数。"""

    # 第一步：数字文本直接转换，避免对阿拉伯数字增加额外规则。
    if value.isdigit():
        return int(value)
    digit_values = {
        "零": 0,
        "一": 1,
        "两": 2,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
    }
    if value in digit_values:
        return digit_values[value]
    if value == "十":
        return 10
    if value.startswith("十"):
        tail = digit_values.get(value[1:])
        return 10 + tail if tail is not None else None
    if value.endswith("十"):
        head = digit_values.get(value[:-1])
        return head * 10 if head is not None else None
    if len(value) == 3 and value[1] == "十":
        head = digit_values.get(value[0])
        tail = digit_values.get(value[2])
        return head * 10 + tail if head is not None and tail is not None else None
    return None


def _normalize_trip_dates(
    requirements: TripRequirements,
    *,
    today: date | None = None,
) -> TripRequirements:
    """将可确定的相对日期和中文日期转换为 ISO 日期。"""

    # 第一步：仅规范化出发和返程字段，不能改写用户偏好、固定日程等原始文本。
    reference_date = today or date.today()
    updates = {
        field_name: normalized_date
        for field_name in ("departure_date", "return_date")
        if (
            normalized_date := _normalize_trip_date(
                getattr(requirements, field_name),
                reference_date,
            )
        )
        is not None
    }
    # 第二步：无法可靠换算的日期原样保留，避免为“下个月”等模糊表达编造具体日期。
    return requirements.model_copy(update=updates) if updates else requirements


def _normalize_trip_date(
    value: str | None,
    reference_date: date,
) -> str | None:
    """识别单个日期文本中的确定性表达并返回 ISO 日期。"""

    if not isinstance(value, str):
        return None
    normalized_value = value.strip()
    if not normalized_value:
        return None
    # 第一步：ISO 日期已可直接进入天气工具，规范化后避免保留多余空白。
    try:
        return date.fromisoformat(normalized_value).isoformat()
    except ValueError:
        pass
    # 第二步：今天、明天和后天完全依赖当前日期，直接转换为明确的自然日。
    relative_offset = _RELATIVE_DAY_OFFSETS.get(normalized_value)
    if relative_offset is not None:
        return (reference_date + timedelta(days=relative_offset)).isoformat()
    # 第三步：本周、这周和下周的星期表达按周一至周日的固定日历语义计算。
    relative_weekday = _RELATIVE_WEEKDAY_PATTERN.fullmatch(normalized_value)
    if relative_weekday is not None:
        weekday = _WEEKDAY_VALUES[relative_weekday["weekday"]]
        days_until_weekday = weekday - reference_date.weekday()
        if relative_weekday["week"] == "下周":
            days_until_weekday += 7
        elif days_until_weekday < 0:
            return None
        return (reference_date + timedelta(days=days_until_weekday)).isoformat()
    # 第四步：完整中文年月日可无歧义转换；仅月日只有落在当前或未来时才补当前年份。
    full_date = _CHINESE_FULL_DATE_PATTERN.fullmatch(normalized_value)
    if full_date is not None:
        return _build_iso_date(
            int(full_date["year"]),
            int(full_date["month"]),
            int(full_date["day"]),
        )
    month_day = _CHINESE_MONTH_DAY_PATTERN.fullmatch(normalized_value)
    if month_day is not None:
        iso_date = _build_iso_date(
            reference_date.year,
            int(month_day["month"]),
            int(month_day["day"]),
        )
        if iso_date is not None and date.fromisoformat(iso_date) >= reference_date:
            return iso_date
    return None


def _build_iso_date(year: int, month: int, day: int) -> str | None:
    """将已提取的年月日安全转换为 ISO 日期。"""

    # 第一步：非法月份、日期或闰年组合不抛给对话流程，继续按原始文本处理。
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _get_missing_fields(requirements: TripRequirements) -> list[str]:
    """按核心字段顺序找出尚未收集到的旅行需求。"""

    # 第一步：目的地、出发时间和人数是进入规划的固定最低条件。
    missing_fields = [
        field_name
        for field_name in CORE_REQUIREMENT_FIELDS
        if not getattr(requirements, field_name)
    ]
    # 第二步：返程日期与旅行时长二选一，避免要求用户重复表达同一约束。
    if not requirements.return_date and not requirements.trip_duration:
        missing_fields.insert(2, "trip_schedule")
    return missing_fields


def _build_follow_up_reply(missing_fields: list[str]) -> str:
    """为首个缺失需求生成确定性的单一追问。"""

    # 第一步：缺失字段已按业务优先级排序，只追问首项以避免用户一次面对多个问题。
    next_field = missing_fields[0]
    return FOLLOW_UP_QUESTIONS[next_field]
