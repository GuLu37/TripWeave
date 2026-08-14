"""TripWeave 的旅差规划 Agent。"""

import json
import logging
from typing import cast

from app.agents.planning_evidence import collect_trip_evidence
from app.agents.prompts import load_planning_skill_sections, load_prompt
from app.api.exception.exceptions import AppException
from app.integrations.llm.client import chat_with_llm
from app.schemas import ClientChatMessage, TripRequirements
from app.tools.map_route_tool import AmapMapRouteTool
from app.tools.weather_tool import QWeatherTool

logger = logging.getLogger(__name__)

PLANNING_TEMPERATURE = 0.4
PLANNING_MAX_TOKENS = 2_400
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


async def plan_trip(
    requirements: TripRequirements,
    *,
    map_route_tool: AmapMapRouteTool | None = None,
    weather_tool: QWeatherTool | None = None,
    replan_context: dict[str, object] | None = None,
) -> str:
    """根据完整需求和可信证据生成中文旅差方案。"""

    # 第一步：规划只接收完整需求，信息不足时由统一入口继续追问。
    missing_fields = _get_missing_fields(requirements)
    if missing_fields:
        raise TripPlanningException.requirements_incomplete(missing_fields)

    # 第二步：取证函数独立完成第三方调用和结果收敛，Agent 只负责生成阶段。
    tool_evidence = await collect_trip_evidence(
        requirements,
        map_route_tool=map_route_tool or AmapMapRouteTool(),
        weather_tool=weather_tool or QWeatherTool(),
    )
    system_prompt = _build_planning_system_prompt(tool_evidence)
    logger.info(
        "规划 Agent 开始生成方案：destination_present=%s departure_present=%s "
        "traveler_count=%s has_budget=%s fixed_schedule_count=%s unavailable_tool_count=%s",
        bool(requirements.destination),
        bool(requirements.departure_date),
        requirements.traveler_count,
        bool(requirements.budget),
        len(requirements.fixed_schedule),
        len(cast(list[object], tool_evidence["unavailable_tools"])),
    )

    # 第三步：模型只接收已验证、可追溯的需求、工具证据和有限重规划反馈，不自行调用外部能力。
    replan_text = ""
    if replan_context:
        replan_text = (
            "\n\n本轮重规划反馈如下：\n"
            f"{json.dumps(replan_context, ensure_ascii=False, separators=(',', ':'))}"
        )
    proposal = await chat_with_llm(
        [
            ClientChatMessage(
                role="user",
                content=(
                    "已确认旅行需求如下：\n"
                    f"{json.dumps(requirements.model_dump(exclude_none=True, exclude_defaults=True), ensure_ascii=False, separators=(',', ':'))}\n\n"
                    "可信 Tool 证据如下：\n"
                    f"{json.dumps(tool_evidence, ensure_ascii=False, separators=(',', ':'))}"
                    f"{replan_text}"
                ),
            )
        ],
        system_prompt=system_prompt,
        temperature=PLANNING_TEMPERATURE,
        max_tokens=PLANNING_MAX_TOKENS,
        max_attempts=PLANNING_MAX_ATTEMPTS,
        # 第四步：DeepSeek 的长推理会占用正文额度，规划场景明确优先输出可展示方案。
        disable_thinking=True,
        caller_name="planning_agent",
    )

    # 第五步：空白文本不能作为可展示方案，统一转换为可识别业务异常。
    normalized_proposal = proposal.strip()
    if not normalized_proposal:
        raise TripPlanningException.empty_proposal()
    return normalized_proposal


def _build_planning_system_prompt(tool_evidence: dict[str, object]) -> str:
    """组合规划 Agent 固定提示词与当前证据对应的 Skill 小节。"""

    # 第一步：固定提示词约束事实边界和 Markdown 输出结构。
    planning_prompt = load_prompt("planning_agent_prompt.md")
    # 第二步：只有实际可用的工具说明进入上下文，避免无关 Skill 长期占用 Token。
    skill_sections = load_planning_skill_sections(
        _get_relevant_skill_sections(tool_evidence)
    )
    return f"{planning_prompt}\n\n{skill_sections}"


def _get_relevant_skill_sections(
    tool_evidence: dict[str, object],
) -> tuple[str, ...]:
    """根据当前工具证据选择需要注入规划上下文的 Skill 小节。"""

    # 第一步：地图定位能力始终适用，其他工具说明仅在对应证据实际可用时加入。
    sections = ["map_route_tool"]
    if _has_non_empty_list(tool_evidence.get("accommodation_candidates")):
        sections.append("accommodation_tool")
    if _has_non_empty_list(tool_evidence.get("attraction_candidates")):
        sections.append("attraction_tool")
    if _has_non_empty_list(tool_evidence.get("food_candidates")):
        sections.append("food_tool")
    weather = tool_evidence.get("weather")
    if isinstance(weather, dict) and weather.get("status") == "available":
        sections.append("weather_tool")
    local_transport = tool_evidence.get("local_transport")
    if isinstance(local_transport, dict) and local_transport.get("status") == "available":
        sections.append("transport_tool")
    return tuple(sections)


def _has_non_empty_list(value: object) -> bool:
    """判断工具证据是否包含至少一个可用候选项。"""

    # 第一步：只接受非空列表，防止错误对象或空数组触发无关 Skill 小节加载。
    return isinstance(value, list) and bool(value)


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
