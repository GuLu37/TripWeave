"""TripWeave 的统一对话入口 Agent。"""

import json
import logging

from pydantic import ValidationError

from app.agents.prompts import load_prompt
from app.api.exception.exceptions import AppException
from app.integrations.llm.client import chat_with_llm
from app.integrations.llm.response_cleaner import extract_json_response
from app.schemas import (
    ClientChatMessage,
    ConversationAnalysis,
    TripRequirements,
)

logger = logging.getLogger(__name__)

# MVP 阶段将目的地、出行时间和人数作为进入行程规划的最低信息门槛。
CORE_REQUIREMENT_FIELDS = (
    "destination",
    "departure_date",
    "traveler_count",
)
# 保留近期对话即可覆盖当前追问，历史需求由 known_requirements 快照补充。
CONTEXT_MESSAGE_LIMIT = 8
STRUCTURED_OUTPUT_TEMPERATURE = 0.1
STRUCTURED_OUTPUT_MAX_TOKENS = 768
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
) -> ConversationAnalysis:
    """由单一入口 Agent 判断意图、回复用户并提取旅行需求。"""

    # 第一步：近期会话与已确认需求快照共同构成 Agent 的唯一判断上下文。
    context_messages = messages[-CONTEXT_MESSAGE_LIMIT:]
    response_text = await chat_with_llm(
        context_messages,
        system_prompt=_build_conversation_system_prompt(known_requirements),
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
    missing_fields = _get_missing_fields(payload.requirements)
    return ConversationAnalysis(
        intent="trip_planning",
        reply=payload.reply,
        requirements=payload.requirements,
        missing_fields=missing_fields,
        is_complete=not missing_fields,
    )


def _build_conversation_system_prompt(
    known_requirements: TripRequirements | None,
) -> str:
    """组合入口 Agent 指令与可选需求快照。"""

    # 第一步：固定指令定义意图边界和结构化输出契约。
    prompt = load_prompt("conversation_entry_prompt.md")
    if known_requirements is None:
        return prompt

    # 第二步：仅注入已确认字段，且明确当前消息优先于历史快照。
    snapshot = known_requirements.model_dump(
        exclude_none=True,
        exclude_defaults=True,
    )
    if not snapshot:
        return prompt
    snapshot_text = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    return (
        f"{prompt}\n\n"
        "已确认需求快照如下，仅用于补充较早上下文；最新用户消息和近期对话优先：\n"
        f"{snapshot_text}"
    )


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
