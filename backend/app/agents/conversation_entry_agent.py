"""TripWeave 的统一对话入口 Agent。"""

import json
import logging
import re
from datetime import date, timedelta

from pydantic import ValidationError

from app.agents.prompts import load_prompt
from app.api.exception.exceptions import AppException
from app.integrations.llm.client import chat_with_llm
from app.integrations.llm.response_cleaner import extract_json_response
from app.schemas import (
    ClientChatMessage,
    ConversationAnalysis,
    TripPlanSnapshot,
    TripRequirements,
)

logger = logging.getLogger(__name__)

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


async def analyze_conversation(
    messages: list[ClientChatMessage],
    *,
    known_requirements: TripRequirements | None = None,
    pending_plan: TripPlanSnapshot | None = None,
) -> ConversationAnalysis:
    """由单一入口 Agent 判断意图、回复用户并提取旅行需求。"""

    # 第一步：近期会话、需求快照与待确认方案共同构成 Agent 的唯一判断上下文。
    context_messages = messages[-CONTEXT_MESSAGE_LIMIT:]
    response_text = await chat_with_llm(
        context_messages,
        system_prompt=_build_conversation_system_prompt(
            known_requirements,
            pending_plan,
        ),
        response_validator=_validate_conversation_payload,
        temperature=STRUCTURED_OUTPUT_TEMPERATURE,
        max_tokens=STRUCTURED_OUTPUT_MAX_TOKENS,
        max_attempts=INTERACTIVE_MAX_ATTEMPTS,
        json_mode=True,
        caller_name="conversation_entry_agent",
    )
    # 第二步：Agent 的结构化输出是聊天与旅差规划分流的唯一来源。
    payload = _parse_conversation_payload(response_text)
    if payload.intent == "chat":
        return ConversationAnalysis(intent="chat", reply=payload.reply)

    # 第三步：旅差规划必须有需求对象，完整性由确定性规则计算。
    if payload.requirements is None:
        raise ConversationAnalysisException.invalid_model_output()
    requirements = _merge_requirements(
        known_requirements,
        payload.requirements,
    )
    requirements = _normalize_trip_dates(requirements)
    plan_action = _resolve_plan_action(payload.plan_action, pending_plan)
    missing_fields = _get_missing_fields(requirements)
    # 第四步：缺失项由本地规则决定，覆盖模型回复以确保每轮只追问一个真正缺少的字段。
    reply = _build_follow_up_reply(missing_fields) if missing_fields else payload.reply
    return ConversationAnalysis(
        intent="trip_planning",
        reply=reply,
        requirements=requirements,
        plan_action=plan_action,
        missing_fields=missing_fields,
        is_complete=not missing_fields,
    )


def _build_conversation_system_prompt(
    known_requirements: TripRequirements | None,
    pending_plan: TripPlanSnapshot | None = None,
) -> str:
    """组合入口 Agent 指令与可选需求快照。"""

    # 第一步：固定指令定义意图边界和结构化输出契约。
    prompt = load_prompt("conversation_entry_prompt.md")
    prompt_parts = [prompt]
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
        # 第二步：不再猜测或修复意图、人数、时长等业务字段，直接按契约拒绝异常输出。
        return ConversationAnalysis.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError) as error:
        logger.warning(
            "统一入口 Agent 返回结构化数据失败：error_type=%s",
            type(error).__name__,
        )
        raise ConversationAnalysisException.invalid_model_output() from error


def _validate_conversation_payload(response_text: str) -> None:
    """验证 Agent 输出满足意图对应的最小领域契约。"""

    # 第一步：验证 JSON、字段类型与 intent 的取值均符合公开数据契约。
    payload = _parse_conversation_payload(response_text)


def _merge_requirements(
    known_requirements: TripRequirements | None,
    current_requirements: TripRequirements,
) -> TripRequirements:
    """将本轮明确提取的字段覆盖到已确认需求快照。"""

    # 第一步：首次收集无需合并，直接保留模型已通过 Pydantic 校验的需求。
    if known_requirements is None:
        return current_requirements
    # 第二步：只覆盖模型本轮实际输出的字段，不能用 Pydantic 默认空值覆盖已确认约束。
    updates = current_requirements.model_dump(exclude_unset=True)
    # 第三步：嵌套模型经 model_dump 后会变为字典，必须重新校验以恢复 TripDuration 等字段类型。
    merged_data = known_requirements.model_dump()
    merged_data.update(updates)
    return TripRequirements.model_validate(merged_data)


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
