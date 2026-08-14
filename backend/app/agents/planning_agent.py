"""TripWeave 的旅差规划 Agent。"""

import json
import logging

from app.agents.prompts import load_prompt
from app.api.exception.exceptions import AppException
from app.integrations.llm.client import chat_with_llm
from app.schemas import ClientChatMessage, TripRequirements

logger = logging.getLogger(__name__)

PLANNING_TEMPERATURE = 0.4
PLANNING_MAX_TOKENS = 1_600
PLANNING_MAX_ATTEMPTS = 2
REQUIRED_FIELDS = (
    "destination",
    "departure_date",
    "traveler_count",
)
FIELD_LABELS = {
    "destination": "目的地",
    "departure_date": "出发时间",
    "traveler_count": "出行人数",
    "trip_schedule": "返程时间或旅行时长",
}


class TripPlanningException(AppException):
    """旅差规划 Agent 无法生成草案时抛出的异常。"""

    @classmethod
    def requirements_incomplete(
        cls,
        missing_fields: list[str],
    ) -> "TripPlanningException":
        """创建需求不完整异常。"""

        # 第一步：明确阻止未完成需求绕过统一入口直接进入规划。
        labels = "、".join(FIELD_LABELS[field_name] for field_name in missing_fields)
        return cls(
            status_code=422,
            code="TRIP_REQUIREMENTS_INCOMPLETE",
            message=f"生成行程前还需要补充：{labels}。",
            details={"missing_fields": missing_fields},
        )

    @classmethod
    def empty_proposal(cls) -> "TripPlanningException":
        """创建空白规划草案异常。"""

        # 第一步：避免将空白文本视为成功结果继续传给后续工作流。
        return cls(
            status_code=502,
            code="TRIP_PROPOSAL_EMPTY",
            message="行程规划服务未生成有效草案，请稍后重试。",
        )


async def plan_trip(requirements: TripRequirements) -> str:
    """根据已确认的旅行需求生成待外部数据核验的行程草案。"""

    # 第一步：规划只接收完整需求，信息不足时由统一入口继续追问。
    missing_fields = _get_missing_fields(requirements)
    if missing_fields:
        raise TripPlanningException.requirements_incomplete(missing_fields)

    # 第二步：将领域模型压缩为 JSON 用户上下文，避免在提示词内重复拼装字段。
    requirements_text = json.dumps(
        requirements.model_dump(exclude_none=True, exclude_defaults=True),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    logger.info(
        "规划 Agent 开始生成草案：destination_present=%s departure_present=%s "
        "traveler_count=%s has_budget=%s fixed_schedule_count=%s",
        bool(requirements.destination),
        bool(requirements.departure_date),
        requirements.traveler_count,
        bool(requirements.budget),
        len(requirements.fixed_schedule),
    )
    # 第三步：当前 Tools 仍未接入，Agent 仅生成待核验草案，不调用外部能力或编造时效数据。
    proposal = await chat_with_llm(
        [
            ClientChatMessage(
                role="user",
                content=f"已确认旅行需求如下：\n{requirements_text}",
            )
        ],
        system_prompt=load_prompt("planning_agent_prompt.md"),
        temperature=PLANNING_TEMPERATURE,
        max_tokens=PLANNING_MAX_TOKENS,
        max_attempts=PLANNING_MAX_ATTEMPTS,
        caller_name="planning_agent",
    )

    # 第四步：空白文本不能作为可展示草案，统一转换为可识别业务异常。
    normalized_proposal = proposal.strip()
    if not normalized_proposal:
        raise TripPlanningException.empty_proposal()
    return normalized_proposal


def _get_missing_fields(requirements: TripRequirements) -> list[str]:
    """按规划最低门槛找出缺失的旅行需求。"""

    # 第一步：目的地、出发时间和人数是生成草案的固定前提。
    missing_fields = [
        field_name
        for field_name in REQUIRED_FIELDS
        if not getattr(requirements, field_name)
    ]
    # 第二步：返程时间与旅行时长二选一，避免在日期表达上重复要求用户。
    if not requirements.return_date and not requirements.trip_duration:
        missing_fields.insert(2, "trip_schedule")
    return missing_fields
