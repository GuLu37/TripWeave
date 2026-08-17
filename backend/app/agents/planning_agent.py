"""TripWeave 的旅差规划 Agent。"""

import json
import logging
import re
from datetime import date
from typing import cast

from app.agents.planning_evidence import collect_trip_evidence
from app.agents.prompts import load_prompt
from app.api.exception.exceptions import AppException
from app.core.settings import get_settings
from app.core.trip_duration import recommended_poi_limit
from app.integrations.llm.client import chat_with_llm
from app.schemas import ClientChatMessage, ConfirmedTripDetails, TripRequirements
from app.services.chat_progress import track_progress
from app.tools.map_route_tool import AmapMapRouteTool
from app.tools.weather_tool import QWeatherTool

logger = logging.getLogger(__name__)

PLANNING_TEMPERATURE = 0.4
PLANNING_MAX_TOKENS = 2_400
PLANNING_MAX_ATTEMPTS = 2
FINAL_PLAN_MAX_TOKENS = 2_200
MODEL_INPUT_CHAR_LIMIT = 11_000
MODEL_ACCOMMODATION_CANDIDATE_LIMIT = 4
MODEL_FORECAST_LIMIT = 4
MODEL_ROUTE_LIMIT = 2
MODEL_TEXT_LIMIT = 280
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
_INTERNAL_TOOL_SECTION_PATTERN = re.compile(
    r"(?ms)^#{1,6}\s*(?:[a-z][a-z0-9_]*_tool|.*(?:工具调用|工具说明))\s*\n.*?(?=^#{1,6}\s|\Z)"
)
_INTERNAL_TOOL_LINE_PATTERN = re.compile(
    r"(?:\b(?:map_route_tool|accommodation_tool|attraction_tool|food_tool|"
    r"weather_tool|transport_tool)\b|(?:高德地图|住宿查询|景点查询|餐饮查询|"
    r"天气查询|本地交通)工具|工具调用|\b(?:Agent|Skill|API)\b)",
    re.IGNORECASE,
)
_MARKDOWN_HEADING_PATTERN = re.compile(r"^\s*#{1,6}\s+")
_MARKDOWN_QUOTE_PATTERN = re.compile(r"^\s*>\s?")
_MARKDOWN_BULLET_PATTERN = re.compile(r"^\s*[-*+]\s+")
_MARKDOWN_RULE_PATTERN = re.compile(r"^[-*_]{3,}$")
_MARKDOWN_LINK_PATTERN = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MARKDOWN_CODE_PATTERN = re.compile(r"`([^`]+)`")
_MARKDOWN_EMPHASIS_PATTERN = re.compile(r"(\*\*|__)(.*?)\1")
_MARKDOWN_STRIKETHROUGH_PATTERN = re.compile(r"~~(.*?)~~")


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


def _short_model_text(value: object, limit: int = MODEL_TEXT_LIMIT) -> object:
    """截短模型不需要完整读取的长文本，保留结构化输入可解析。"""

    if not isinstance(value, str) or len(value) <= limit:
        return value
    return f"{value[:limit]}…"


def _compact_model_record(
    value: object,
    fields: tuple[str, ...],
) -> dict[str, object]:
    """仅保留规划模型实际需要的候选字段。"""

    if not isinstance(value, dict):
        return {}
    return {
        field: _short_model_text(value[field])
        for field in fields
        if value.get(field) is not None
    }


def _compact_model_records(
    value: object,
    fields: tuple[str, ...],
    *,
    limit: int,
) -> list[dict[str, object]]:
    """压缩候选或报价列表，避免单个工具响应占满模型上下文。"""

    if not isinstance(value, list):
        return []
    return [
        record
        for item in value[:limit]
        if (record := _compact_model_record(item, fields))
    ]


def _compact_planning_tool_evidence(
    evidence: dict[str, object],
    requirements: TripRequirements,
) -> dict[str, object]:
    """将完整工具证据压缩为规划模型可消费的最小摘要。"""

    candidate_fields = ("name", "address", "type", "city", "distance_meters")
    weather = evidence.get("weather")
    weather_summary: dict[str, object] = {}
    if isinstance(weather, dict):
        weather_summary = _compact_model_record(
            weather,
            ("status", "reason", "message"),
        )
        weather_summary["forecast"] = _compact_model_records(
            weather.get("forecast"),
            (
                "date",
                "day_condition",
                "night_condition",
                "temperature_max",
                "temperature_min",
                "precipitation_probability",
            ),
            limit=MODEL_FORECAST_LIMIT,
        )

    local_transport = evidence.get("local_transport")
    transport_summary: dict[str, object] = {}
    if isinstance(local_transport, dict):
        transport_summary = _compact_model_record(
            local_transport,
            ("status", "reason"),
        )
        transport_summary["anchor"] = _compact_model_record(
            local_transport.get("anchor"),
            ("category", "name", "address"),
        )
        routes: list[dict[str, object]] = []
        for route in local_transport.get("routes", [])[:MODEL_ROUTE_LIMIT] if isinstance(local_transport.get("routes"), list) else []:
            if not isinstance(route, dict):
                continue
            routes.append(
                {
                    "target": _compact_model_record(
                        route.get("target"),
                        ("category", "name", "address"),
                    ),
                    "options": _compact_model_records(
                        route.get("options"),
                        ("mode", "mode_label", "distance_text", "duration_text"),
                        limit=3,
                    ),
                }
            )
        transport_summary["routes"] = routes

    recommended_candidates = evidence.get("recommended_candidates")
    recommended_summary = {
        category: _compact_model_record(
            recommended_candidates.get(category)
            if isinstance(recommended_candidates, dict)
            else None,
            ("name", "address", "type"),
        )
        for category in ("accommodation", "attraction", "food")
    }

    return {
        "poi_recommendation_limit": recommended_poi_limit(requirements),
        "destination": {
            "city": _short_model_text(evidence.get("destination_city")),
            "location": _short_model_text(evidence.get("destination_location")),
        },
        "accommodation_candidates": _compact_model_records(
            evidence.get("accommodation_candidates"),
            candidate_fields,
            limit=MODEL_ACCOMMODATION_CANDIDATE_LIMIT,
        ),
        "attraction_candidates": _compact_model_records(
            evidence.get("attraction_candidates"),
            candidate_fields,
            limit=recommended_poi_limit(requirements),
        ),
        "food_candidates": _compact_model_records(
            evidence.get("food_candidates"),
            candidate_fields,
            limit=recommended_poi_limit(requirements),
        ),
        "recommended_candidates": recommended_summary,
        "weather": weather_summary,
        "local_transport": transport_summary,
        "unavailable_tools": _compact_model_records(
            evidence.get("unavailable_tools"),
            ("tool", "error_code", "message"),
            limit=4,
        ),
    }


def _compact_external_search_evidence(
    evidence: dict[str, object] | None,
) -> dict[str, object]:
    """保留少量酒店和城际报价参考，排除来源原始字段。"""

    if not evidence:
        return {}
    summaries: dict[str, object] = {}
    offer_fields = (
        "name",
        "room_type",
        "service_no",
        "origin",
        "destination",
        "departure_time",
        "arrival_time",
        "price",
        "total_price",
        "currency",
    )
    for name, result in evidence.items():
        if not isinstance(result, dict):
            continue
        summaries[name] = {
            **_compact_model_record(
                result,
                ("status", "message", "price_type", "is_estimate"),
            ),
            "offers": _compact_model_records(
                result.get("offers"),
                offer_fields,
                limit=4,
            ),
        }
    return summaries


def _compact_replan_context(
    context: dict[str, object] | None,
) -> dict[str, object]:
    """限制重规划反馈长度，保留本轮变更与审核原因。"""

    if not context:
        return {}
    compacted = _compact_model_record(
        context,
        (
            "source",
            "instruction",
            "user_message",
            "updated_preferences",
            "replacement_categories",
            "excluded_place_names",
            "reused_replacement_places",
        ),
    )
    previous_proposal = context.get("previous_proposal")
    if isinstance(previous_proposal, str):
        compacted["previous_proposal"] = _short_model_text(previous_proposal, 1_800)
    review = context.get("review")
    if isinstance(review, dict):
        compacted["review"] = {
            "summary": _short_model_text(review.get("summary"), 500),
            "risks": _compact_model_records(review.get("risks"), (), limit=3),
            "pending_items": _compact_model_records(review.get("pending_items"), (), limit=3),
        }
    return compacted


def _build_planning_request_content(
    requirements: TripRequirements,
    tool_evidence: dict[str, object],
    external_search_evidence: dict[str, object] | None,
    replan_context: dict[str, object] | None,
) -> str:
    """构造受长度保护的规划模型输入，完整证据继续留在后端工作流状态。"""

    payload = {
        "requirements": requirements.model_dump(
            exclude_none=True,
            exclude_defaults=True,
        ),
        "tool_evidence": _compact_planning_tool_evidence(
            tool_evidence,
            requirements,
        ),
        "external_search_evidence": _compact_external_search_evidence(
            external_search_evidence
        ),
        "replan_context": _compact_replan_context(replan_context),
    }
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(content) > MODEL_INPUT_CHAR_LIMIT:
        payload["tool_evidence"]["local_transport"] = {}
        payload["tool_evidence"]["accommodation_candidates"] = payload[
            "tool_evidence"
        ]["accommodation_candidates"][:2]
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(content) > MODEL_INPUT_CHAR_LIMIT:
        payload["replan_context"] = {
            "instruction": _short_model_text(
                payload["replan_context"].get("instruction")
                if isinstance(payload["replan_context"], dict)
                else None,
                500,
            )
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(content) > MODEL_INPUT_CHAR_LIMIT:
        tool_summary = payload["tool_evidence"]
        payload = {
            "requirements": payload["requirements"],
            "tool_evidence": {
                "destination": tool_summary.get("destination"),
                "recommended_candidates": tool_summary.get(
                    "recommended_candidates"
                ),
                "weather": tool_summary.get("weather"),
                "unavailable_tools": tool_summary.get("unavailable_tools"),
            },
            "replan_context": payload["replan_context"],
        }
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(content) > MODEL_INPUT_CHAR_LIMIT:
        raise RuntimeError("规划模型输入摘要超过安全长度上限。")
    return f"已确认旅行需求与可信证据如下：\n{content}"


async def plan_trip(
    requirements: TripRequirements,
    *,
    map_route_tool: AmapMapRouteTool | None = None,
    weather_tool: QWeatherTool | None = None,
    replan_context: dict[str, object] | None = None,
    tool_evidence: dict[str, object] | None = None,
    external_search_evidence: dict[str, object] | None = None,
) -> str:
    """根据完整需求和可信证据生成中文旅差方案。"""

    # 第一步：规划只接收完整需求，信息不足时由统一入口继续追问。
    missing_fields = _get_missing_fields(requirements)
    if missing_fields:
        raise TripPlanningException.requirements_incomplete(missing_fields)

    # 第二步：取证函数独立完成第三方调用和结果收敛，Agent 只负责生成阶段。
    if tool_evidence is None:
        tool_evidence = await collect_trip_evidence(
            requirements,
            map_route_tool=map_route_tool or AmapMapRouteTool(),
            weather_tool=weather_tool or QWeatherTool(),
        )
    system_prompt = _build_planning_system_prompt()
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

    # 第三步：模型只接收压缩后的可信证据，完整工具响应继续留在工作流状态。
    planning_input = _build_planning_request_content(
        requirements,
        tool_evidence,
        external_search_evidence,
        replan_context,
    )
    logger.info("规划 Agent 模型输入已压缩：content_chars=%s", len(planning_input))
    async with track_progress(
        "规划 Agent",
        "基于已收集证据生成行程草案",
        tool="方案生成",
    ):
        proposal = await chat_with_llm(
            [
                ClientChatMessage(
                    role="user",
                    content=planning_input,
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

    # 第五步：在规划 Agent 内完成最终输出清洗，审批和前端只接收用户可读方案。
    normalized_proposal = _clean_plan_output(proposal)
    if not normalized_proposal:
        raise TripPlanningException.empty_proposal()
    return normalized_proposal


async def generate_confirmed_trip_plan(
    requirements: TripRequirements,
    *,
    draft_proposal: str,
    details: ConfirmedTripDetails,
) -> str:
    """按用户确认后的路线顺序重写最终方案正文。"""

    context = {
        "requirements": requirements.model_dump(exclude_none=True, exclude_defaults=True),
        "draft_proposal": draft_proposal,
        "route_planning": _build_final_route_context(details),
    }
    async with track_progress(
        "审批 Agent",
        "基于确认路线重写最终方案",
        tool="最终方案生成",
    ):
        try:
            final_plan = await chat_with_llm(
                [
                    ClientChatMessage(
                        role="user",
                        content=json.dumps(context, ensure_ascii=False, separators=(",", ":")),
                    )
                ],
                system_prompt=(
                    "你是 TripWeave 的最终旅差方案编辑。请基于用户已确认的需求、原始草案和已确认路线规划，"
                    "重新生成一篇给前端用户展示的中文最终计划。\n"
                    "硬性要求：\n"
                    "1. 必须以“路线规划”作为正文第一部分，并严格按照 route_planning.points 的顺序组织景点和美食。\n"
                    "2. 后续内容按路线顺序展开每日或分段安排，说明交通、住宿、用餐。\n"
                    "3. 不要输出 Markdown 标题符号、项目符号、代码块、JSON、工具名、Agent 名、API 调用说明。\n"
                    "4. 不要出现“待确认事项”“需确认”“待核验”等字样；最终计划应是已确认口吻。\n"
                    "5. 酒店、票价、库存和图片只作为参考信息描述，不得写成已经预订或可直接购买。\n"
                    "6. 必须包含“天气参考”部分：如果 route_planning.weather.forecast 有数据，概括每日天气、温度和对路线的影响；"
                    "如果天气不可用或超出预报窗口，也必须说明原因和临近出发前复查建议。\n"
                    "7. “注意事项”不是必填，只有存在真实风险、天气影响、预订参考或用户约束时才写。\n"
                    "8. 文本要清晰、完整、适合直接替换旧规划草稿。"
                ),
                temperature=0.25,
                max_tokens=FINAL_PLAN_MAX_TOKENS,
                max_attempts=PLANNING_MAX_ATTEMPTS,
                provider_override="deepseek",
                model_override=get_settings().deepseek_review_model,
                disable_thinking=True,
                caller_name="confirmed_plan_writer",
            )
        except Exception as error:
            logger.warning(
                "最终方案正文生成失败，使用含天气的确定性兜底：error_type=%s",
                type(error).__name__,
            )
            return _build_fallback_final_plan(requirements, draft_proposal, details)
    normalized_plan = _clean_plan_output(final_plan)
    return normalized_plan or _build_fallback_final_plan(requirements, draft_proposal, details)


def _build_planning_system_prompt() -> str:
    """返回规划 Agent 的固定输出约束。"""

    # 工具调用已在规划前完成，模型只消费收敛后的证据，不应接收工具说明正文。
    return load_prompt("planning_agent_prompt.md")


def _build_final_route_context(details: ConfirmedTripDetails) -> dict[str, object]:
    return {
        "overview_route": (
            details.overview_route.model_dump(exclude_none=True)
            if details.overview_route
            else None
        ),
        "points": [
            point.model_dump(exclude_none=True)
            for point in details.map_points
        ],
        "local_routes": [
            route.model_dump(exclude_none=True)
            for route in details.routes
        ],
        "weather": details.weather,
    }


def _build_fallback_final_plan(
    requirements: TripRequirements,
    draft_proposal: str,
    details: ConfirmedTripDetails,
) -> str:
    lines = ["路线规划"]
    if details.map_points:
        for index, point in enumerate(details.map_points, start=1):
            category = "美食" if point.category == "food" else "景点"
            address = f"（{point.address}）" if point.address else ""
            lines.append(f"{index}. {category}：{point.name}{address}")
    elif requirements.origin and requirements.destination:
        lines.append(f"1. {requirements.origin} 到 {requirements.destination}")

    if details.overview_route:
        overview = details.overview_route
        route_text = f"{overview.origin} 到 {overview.destination}"
        metrics = "，".join(
            item for item in (overview.distance_text, overview.duration_text) if item
        )
        lines.extend(["", "出发与到达", f"{route_text}{f'，{metrics}' if metrics else ''}。"])

    lines.extend(["", "天气参考"])
    weather = details.weather
    forecast = weather.get("forecast") if isinstance(weather, dict) else None
    if isinstance(forecast, list) and forecast:
        for item in forecast[:5]:
            if not isinstance(item, dict):
                continue
            condition = item.get("day_condition") or item.get("night_condition") or "天气待定"
            temperature = " / ".join(
                str(value)
                for value in (item.get("temperature_min"), item.get("temperature_max"))
                if value is not None
            )
            lines.append(
                f"{item.get('date', '行程日')}：{condition}"
                f"{f'，约{temperature}℃' if temperature else ''}。"
            )
    else:
        message = weather.get("message") if isinstance(weather, dict) else None
        lines.append(str(message or "当前暂无可用天气预报，建议临近出发前再次复查。"))

    cleaned_draft = _clean_plan_output(draft_proposal)
    if cleaned_draft:
        lines.extend(["", "完整安排", cleaned_draft])
    return "\n".join(lines).strip()


def _clean_plan_output(proposal: str) -> str:
    """清理完整方案中的内部说明与 Markdown 展示符号。"""

    # 先移除整个内部工具章节，再过滤零散调用说明，最后保留用户可读的纯文本结构。
    without_sections = _INTERNAL_TOOL_SECTION_PATTERN.sub("", proposal)
    cleaned_lines: list[str] = []
    for line in without_sections.splitlines():
        if _INTERNAL_TOOL_LINE_PATTERN.search(line):
            continue
        cleaned_line = _MARKDOWN_HEADING_PATTERN.sub("", line)
        cleaned_line = _MARKDOWN_QUOTE_PATTERN.sub("", cleaned_line)
        cleaned_line = _MARKDOWN_BULLET_PATTERN.sub("", cleaned_line)
        cleaned_line = _MARKDOWN_LINK_PATTERN.sub(r"\1", cleaned_line)
        cleaned_line = _MARKDOWN_CODE_PATTERN.sub(r"\1", cleaned_line)
        cleaned_line = _MARKDOWN_EMPHASIS_PATTERN.sub(r"\2", cleaned_line)
        cleaned_line = _MARKDOWN_STRIKETHROUGH_PATTERN.sub(r"\1", cleaned_line)
        cleaned_line = cleaned_line.strip()
        if not _MARKDOWN_RULE_PATTERN.fullmatch(cleaned_line):
            cleaned_lines.append(cleaned_line)
    return "\n".join(cleaned_lines).strip()


def _get_missing_fields(requirements: TripRequirements) -> list[str]:
    """按规划最低门槛找出缺失的旅行需求。"""

    # 第一步：目的地、出发时间和人数是生成草案的固定前提。
    missing_fields = [
        field_name
        for field_name in REQUIRED_FIELDS
        if (
            not getattr(requirements, field_name)
            or (
                field_name == "departure_date"
                and not _is_iso_date(requirements.departure_date)
            )
        )
    ]
    # 第二步：返程时间与旅行时长二选一，避免在日期表达上重复要求用户。
    if (
        (not requirements.return_date or not _is_iso_date(requirements.return_date))
        and not requirements.trip_duration
    ):
        missing_fields.insert(2, "trip_schedule")
    return missing_fields


def _is_iso_date(value: str | None) -> bool:
    """规划阶段只接受已经落到自然日的日期。"""

    if not isinstance(value, str):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True
