"""旅差规划的第三方取证与证据收敛函数。"""

import asyncio
import logging
from datetime import date, timedelta

from app.api.exception.error_handler import record_error
from app.core.trip_duration import duration_to_days
from app.schemas import TripDuration, TripRequirements
from app.tools.accommodation_tool import search_hotels_in_city
from app.tools.attraction_tool import search_attractions_in_city
from app.tools.food_tool import search_restaurants_in_city
from app.tools.map_route_tool import AmapMapRouteTool
from app.tools.transport_tool import TransportPlanningTool
from app.tools.weather_tool import QWeatherTool

logger = logging.getLogger(__name__)

POI_CANDIDATE_LIMIT = 5
ROUTE_CANDIDATE_LIMIT = 2
MAX_FORECAST_DAYS = 10
WEATHER_OUTSIDE_FORECAST_WINDOW_MESSAGE = (
    "距离出发时间较远，当前无法精确提供该日期的天气预报。"
    "建议在出发前十天内再次查询。"
)
WEATHER_FORECAST_DATA_INCOMPLETE_MESSAGE = (
    "天气预报数据不完整，当前无法据此调整行程。"
    "建议临近出发时再次查询。"
)


async def collect_trip_evidence(
    requirements: TripRequirements,
    *,
    map_route_tool: AmapMapRouteTool,
    weather_tool: QWeatherTool,
) -> dict[str, object]:
    """并发收集并收敛生成方案所需的地点、天气和本地交通证据。"""

    destination = requirements.destination
    assert destination is not None
    unavailable_tools: list[dict[str, str]] = []

    # 第一步：先解析目的地中心坐标；后续天气和交通仅在得到可信坐标后才执行。
    logger.info("规划 Agent 调用工具：tool=map_route_tool.geocode")
    destination_location = await _resolve_destination_location(
        map_route_tool,
        destination,
        unavailable_tools,
    )
    # 第二步：城市级 POI 查询相互独立，即使地理编码失败仍可为方案提供地点候选。
    logger.info(
        "规划 Agent 调用工具：tool=accommodation_tool.search_hotels_in_city"
    )
    logger.info(
        "规划 Agent 调用工具：tool=attraction_tool.search_attractions_in_city"
    )
    logger.info(
        "规划 Agent 调用工具：tool=food_tool.search_restaurants_in_city"
    )
    poi_results = await asyncio.gather(
        search_hotels_in_city(
            map_route_tool,
            destination,
            _select_preference(requirements.accommodation_preferences, "酒店"),
            limit=POI_CANDIDATE_LIMIT,
        ),
        search_attractions_in_city(
            map_route_tool,
            destination,
            _select_preference(requirements.attraction_preferences, "景点"),
            limit=POI_CANDIDATE_LIMIT,
        ),
        search_restaurants_in_city(
            map_route_tool,
            destination,
            _select_preference(requirements.dining_preferences, "餐厅"),
            limit=POI_CANDIDATE_LIMIT,
        ),
        return_exceptions=True,
    )
    accommodation_candidates = _extract_poi_result(
        "accommodation_search",
        poi_results[0],
        unavailable_tools,
    )
    accommodation_candidates = _rank_candidates(
        accommodation_candidates,
        requirements.accommodation_preferences,
    )
    attraction_candidates = _extract_poi_result(
        "attraction_search",
        poi_results[1],
        unavailable_tools,
    )
    attraction_candidates = _rank_candidates(
        attraction_candidates,
        requirements.attraction_preferences,
    )
    food_candidates = _extract_poi_result(
        "food_search",
        poi_results[2],
        unavailable_tools,
    )
    food_candidates = _rank_candidates(
        food_candidates,
        requirements.dining_preferences,
    )
    logger.info(
        "规划 Agent 工具调用完成：tool=accommodation_tool.search_hotels_in_city "
        "candidate_count=%s",
        len(accommodation_candidates),
    )
    logger.info(
        "规划 Agent 工具调用完成：tool=attraction_tool.search_attractions_in_city "
        "candidate_count=%s",
        len(attraction_candidates),
    )
    logger.info(
        "规划 Agent 工具调用完成：tool=food_tool.search_restaurants_in_city "
        "candidate_count=%s",
        len(food_candidates),
    )

    # 第三步：天气预报只在出行日落入未来十天窗口时查询，避免把无关日期天气写入方案。
    weather_evidence = await _collect_weather_evidence(
        requirements,
        destination_location,
        weather_tool,
        unavailable_tools,
    )
    # 第四步：以首个住宿候选或目的地中心为交通锚点，比较少量代表性景点和餐饮候选。
    transport_evidence = await _collect_transport_evidence(
        destination,
        destination_location,
        accommodation_candidates,
        attraction_candidates,
        food_candidates,
        map_route_tool,
        unavailable_tools,
    )
    return {
        "destination_location": destination_location,
        "accommodation_candidates": accommodation_candidates,
        "attraction_candidates": attraction_candidates,
        "food_candidates": food_candidates,
        "recommended_candidates": _build_recommended_candidates(
            accommodation_candidates,
            attraction_candidates,
            food_candidates,
        ),
        "weather": weather_evidence,
        "local_transport": transport_evidence,
        "unavailable_tools": unavailable_tools,
    }


async def _resolve_destination_location(
    map_route_tool: AmapMapRouteTool,
    destination: str,
    unavailable_tools: list[dict[str, str]],
) -> str | None:
    """解析目的地中心坐标，并将失败转换为可展示的工具状态。"""

    try:
        # 第一步：地理编码只用于获取目的地中心点，不把可能存在歧义的原始地址详情交给模型。
        result = await map_route_tool.geocode(destination, city=destination)
    except Exception as error:
        _record_tool_failure("destination_geocode", error, unavailable_tools)
        return None
    # 第二步：只接受首个候选的合法高德坐标；空结果应被视为不可用证据而非默认成功。
    geocodes = result.get("geocodes")
    if not isinstance(geocodes, list) or not geocodes:
        _record_tool_failure(
            "destination_geocode",
            ValueError("地理编码结果为空。"),
            unavailable_tools,
            error_code="TOOL_RESULT_EMPTY",
            error_message="地图工具未返回可用的地理编码结果。",
        )
        return None
    first_geocode = geocodes[0]
    if not isinstance(first_geocode, dict):
        _record_tool_failure(
            "destination_geocode",
            ValueError("地理编码首项不是对象。"),
            unavailable_tools,
            error_code="TOOL_RESULT_INVALID",
            error_message="地图工具返回的地理编码结构无效。",
        )
        return None
    location = first_geocode.get("location")
    if not _is_coordinate(location):
        _record_tool_failure(
            "destination_geocode",
            ValueError("地理编码结果缺少合法坐标。"),
            unavailable_tools,
            error_code="TOOL_RESULT_INVALID",
            error_message="地图工具返回的地理编码缺少合法坐标。",
        )
        return None
    logger.info("规划 Agent 工具调用完成：tool=map_route_tool.geocode result_count=1")
    return location


async def _collect_weather_evidence(
    requirements: TripRequirements,
    destination_location: str | None,
    weather_tool: QWeatherTool,
    unavailable_tools: list[dict[str, str]],
) -> dict[str, object]:
    """按出行日期窗口收集每日预报与当前预警摘要。"""

    # 第一步：没有可信坐标时不向天气供应商发送无效请求，并明确原因供模型输出待确认项。
    if destination_location is None:
        return {"status": "skipped", "reason": "destination_location_unavailable"}
    forecast_days = _get_forecast_days(requirements.departure_date, requirements.trip_duration)
    if forecast_days is None:
        # 第二步：超出供应商预报窗口时明确记录跳过原因，并提供前端可直接展示的用户提示。
        logger.info(
            "规划 Agent 跳过工具：tool=weather_tool reason=outside_forecast_window"
        )
        return {
            "status": "skipped",
            "reason": "outside_forecast_window",
            "message": WEATHER_OUTSIDE_FORECAST_WINDOW_MESSAGE,
        }

    # 第三步：预报与当前预警互不依赖，并发查询缩短规划等待；预警必须注明它是当前状态。
    logger.info("规划 Agent 调用工具：tool=weather_tool.get_daily_forecast")
    logger.info("规划 Agent 调用工具：tool=weather_tool.get_weather_alerts")
    results = await asyncio.gather(
        weather_tool.get_daily_forecast(destination_location, days=forecast_days),
        weather_tool.get_weather_alerts(destination_location),
        return_exceptions=True,
    )
    daily_result, alerts_result = results
    evidence: dict[str, object] = {"status": "available", "forecast": []}
    if isinstance(daily_result, Exception):
        _record_tool_failure(
            "weather_forecast",
            daily_result,
            unavailable_tools,
            error_already_logged=True,
        )
        evidence.update(
            {
                "status": "unavailable",
                "reason": "forecast_request_failed",
                "message": WEATHER_FORECAST_DATA_INCOMPLETE_MESSAGE,
            }
        )
    else:
        _log_daily_forecast_shape(daily_result)
        forecast = _filter_forecast_to_trip_window(
            _compact_daily_forecast(daily_result),
            requirements.departure_date,
            requirements.trip_duration,
        )
        evidence["forecast"] = forecast
        if not _has_usable_forecast(forecast):
            evidence.update(
                {
                    "status": "unavailable",
                    "reason": "forecast_data_empty",
                    "message": WEATHER_FORECAST_DATA_INCOMPLETE_MESSAGE,
                }
            )
        logger.info(
            "规划 Agent 工具调用完成：tool=weather_tool.get_daily_forecast "
            "result_count=%s",
            len(forecast),
        )
    if isinstance(alerts_result, Exception):
        _record_tool_failure(
            "weather_alerts",
            alerts_result,
            unavailable_tools,
            error_already_logged=True,
        )
        evidence["current_alerts"] = []
    else:
        evidence["current_alerts"] = _compact_weather_alerts(alerts_result)
        logger.info(
            "规划 Agent 工具调用完成：tool=weather_tool.get_weather_alerts "
            "result_count=%s",
            len(evidence["current_alerts"]),
        )
    return evidence


async def _collect_transport_evidence(
    city: str,
    destination_location: str | None,
    accommodation_candidates: list[dict[str, object]],
    attraction_candidates: list[dict[str, object]],
    food_candidates: list[dict[str, object]],
    map_route_tool: AmapMapRouteTool,
    unavailable_tools: list[dict[str, str]],
) -> dict[str, object]:
    """以住宿或目的地中心为锚点生成少量可比较的本地交通摘要。"""

    # 第一步：优先以住宿候选为每天出发锚点；没有住宿坐标时才退回目的地中心。
    anchor = _select_transport_anchor(accommodation_candidates, destination_location)
    if anchor is None:
        return {"status": "skipped", "reason": "route_anchor_unavailable", "routes": []}
    targets = _select_route_targets(attraction_candidates, food_candidates)
    if not targets:
        return {"status": "skipped", "reason": "route_target_unavailable", "routes": []}

    transport_tool = TransportPlanningTool(map_route_tool)
    # 第二步：每个候选路线独立并发，单一路线失败由交通工具内部降级为 unavailable_modes。
    for target in targets:
        logger.info(
            "规划 Agent 调用工具：tool=transport_tool.plan_local_transport "
            "target_category=%s",
            target["category"],
        )
    results = await asyncio.gather(
        *[
            transport_tool.plan_local_transport(
                anchor["location"],
                target["location"],
                city=city,
            )
            for target in targets
        ],
        return_exceptions=True,
    )
    routes: list[dict[str, object]] = []
    for target, result in zip(targets, results, strict=True):
        if isinstance(result, Exception):
            _record_tool_failure("local_transport", result, unavailable_tools)
            continue
        routes.append(
            {
                "target": {
                    "category": target["category"],
                    "name": target["name"],
                    "address": target["address"],
                },
                "options": _compact_transport_options(result),
                "unavailable_modes": result.get("unavailable_modes", []),
            }
        )
    logger.info(
        "规划 Agent 工具调用完成：tool=transport_tool.plan_local_transport "
        "result_count=%s",
        len(routes),
    )
    return {
        "status": "available",
        "anchor": {
            "category": anchor["category"],
            "name": anchor["name"],
            "address": anchor["address"],
        },
        "routes": routes,
    }


def _extract_poi_result(
    tool_name: str,
    result: object,
    unavailable_tools: list[dict[str, str]],
) -> list[dict[str, object]]:
    """将 POI 原始响应裁剪为规划可安全消费的候选字段。"""

    # 第一步：工具异常不阻断其他类别候选，统一记录安全错误码后返回空候选。
    if isinstance(result, Exception):
        _record_tool_failure(tool_name, result, unavailable_tools)
        return []
    if not isinstance(result, dict):
        _record_tool_failure(
            tool_name,
            ValueError("工具结果不是对象。"),
            unavailable_tools,
            error_code="TOOL_RESULT_INVALID",
            error_message="地点工具返回了无法识别的结果结构。",
        )
        return []
    pois = result.get("pois")
    if not isinstance(pois, list):
        _record_tool_failure(
            tool_name,
            ValueError("工具结果缺少 pois 列表。"),
            unavailable_tools,
            error_code="TOOL_RESULT_INVALID",
            error_message="地点工具结果缺少可用候选列表。",
        )
        return []

    # 第二步：只保留名称、地址、坐标、分类和距离，过滤缺坐标项以确保可进入路线规划。
    candidates: list[dict[str, object]] = []
    for poi in pois[:POI_CANDIDATE_LIMIT]:
        if not isinstance(poi, dict):
            continue
        location = poi.get("location")
        name = _safe_text(poi.get("name"))
        if name is None or not _is_coordinate(location):
            continue
        candidates.append(
            {
                "name": name,
                "location": location,
                "address": _safe_text(poi.get("address")),
                "type": _safe_text(poi.get("type")),
                "distance_meters": _safe_non_negative_int(poi.get("distance")),
            }
        )
    return candidates


def _rank_candidates(
    candidates: list[dict[str, object]],
    preferences: list[str],
) -> list[dict[str, object]]:
    """按用户偏好与已知距离稳定排序地点候选。"""

    # 第一步：保留全部非空偏好，避免 POI 查询只使用首个关键词后其余偏好完全丢失。
    normalized_preferences = [
        preference.strip().lower()
        for preference in preferences
        if preference.strip()
    ]

    # 第二步：优先名称、分类或地址匹配更多偏好的候选；距离缺失与并列时保持供应商原始顺序。
    return sorted(
        candidates,
        key=lambda candidate: (
            -_candidate_preference_score(candidate, normalized_preferences),
            _candidate_distance_sort_value(candidate),
        ),
    )


def _candidate_preference_score(
    candidate: dict[str, object],
    preferences: list[str],
) -> int:
    """计算单个候选与用户偏好的简单匹配分。"""

    # 第一步：只使用已收敛的文本字段，避免将供应商原始对象或坐标参与偏好匹配。
    searchable_text = " ".join(
        text
        for text in (
            _safe_text(candidate.get("name")),
            _safe_text(candidate.get("type")),
            _safe_text(candidate.get("address")),
        )
        if text is not None
    ).lower()
    # 第二步：每个偏好最多记一次分，重复偏好不会放大候选优先级。
    return sum(preference in searchable_text for preference in set(preferences))


def _candidate_distance_sort_value(candidate: dict[str, object]) -> int:
    """返回候选距离的排序值，缺失距离始终排在已知距离之后。"""

    # 第一步：城市级搜索通常没有距离字段，使用较大值保留原始顺序而不伪造距离。
    distance = candidate.get("distance_meters")
    return distance if isinstance(distance, int) else 2**31 - 1


def _build_recommended_candidates(
    accommodation_candidates: list[dict[str, object]],
    attraction_candidates: list[dict[str, object]],
    food_candidates: list[dict[str, object]],
) -> dict[str, dict[str, object] | None]:
    """从已排序候选中生成各类别的优先地点摘要。"""

    # 第一步：列表首项已经过偏好与距离排序，作为路线锚点和模型排程的优先候选。
    return {
        "accommodation": _compact_candidate(accommodation_candidates),
        "attraction": _compact_candidate(attraction_candidates),
        "food": _compact_candidate(food_candidates),
    }


def _compact_candidate(
    candidates: list[dict[str, object]],
) -> dict[str, object] | None:
    """压缩单类优先候选，避免在推荐摘要中重复传递坐标。"""

    # 第一步：没有可用候选时明确返回 None，模型不得将空类别补造成具体地点。
    if not candidates:
        return None
    candidate = candidates[0]
    return {
        "name": candidate.get("name"),
        "address": candidate.get("address"),
        "type": candidate.get("type"),
    }


def _compact_daily_forecast(payload: dict[str, object]) -> list[dict[str, object]]:
    """提取每日预报中可支撑行程安排的有限字段。"""

    # 第一步：兼容当前工具契约的 days 与常见的 daily 列表字段，缺失时返回空列表。
    days = _get_daily_items(payload)
    if not isinstance(days, list):
        return []
    forecasts: list[dict[str, object]] = []
    for item in days[:MAX_FORECAST_DAYS]:
        if not isinstance(item, dict):
            continue
        forecast = {
            "date": _first_text(item, "forecastDate", "fxDate", "date"),
            "day_condition": _first_text(item, "textDay")
            or _nested_text(item, "condition", "day", "text"),
            "night_condition": _first_text(item, "textNight")
            or _nested_text(item, "condition", "night", "text"),
            "temperature_max": _first_non_none(
                item.get("tempMax"),
                _nested_value(item, "temperature", "max"),
            ),
            "temperature_min": _first_non_none(
                item.get("tempMin"),
                _nested_value(item, "temperature", "min"),
            ),
            "precipitation_probability": _first_non_none(
                item.get("pop"),
                _nested_value(item, "precipitation", "probability"),
            ),
            "uv_index": item.get("uvIndex"),
        }
        # 第二步：整条记录没有可用天气字段时不传给模型，避免“可用但全空”的矛盾证据。
        if _has_usable_weather_fields(forecast):
            forecasts.append(forecast)
    return forecasts


def _filter_forecast_to_trip_window(
    forecast: list[dict[str, object]],
    departure_date: str | None,
    trip_duration: TripDuration | None,
) -> list[dict[str, object]]:
    """只保留实际旅行日期范围内的天气预报。"""

    # 第一步：出发日期无法解析时不把供应商返回的今天预报误当成行程天气。
    if not departure_date:
        return []
    try:
        departure = date.fromisoformat(departure_date)
    except ValueError:
        return []
    days_until_departure = (departure - date.today()).days
    trip_days = duration_to_days(trip_duration)
    if trip_days is None:
        trip_days = max(1, MAX_FORECAST_DAYS - max(0, days_until_departure))
    trip_end = departure + timedelta(days=trip_days)

    # 第二步：按日期过滤，避免今天到出发日前的预报进入规划模型。
    filtered_forecast: list[dict[str, object]] = []
    for item in forecast:
        forecast_date = item.get("date")
        if not isinstance(forecast_date, str):
            continue
        try:
            parsed_forecast_date = date.fromisoformat(forecast_date)
        except ValueError:
            continue
        if departure <= parsed_forecast_date < trip_end:
            filtered_forecast.append(item)
    return filtered_forecast


def _get_daily_items(payload: dict[str, object]) -> object:
    """读取供应商每日预报列表，不传递原始响应内容。"""

    # 第一步：优先使用当前工具契约中的 days，兼容另一类常见的 daily 命名。
    days = payload.get("days")
    return days if isinstance(days, list) else payload.get("daily")


def _has_usable_forecast(forecast: list[dict[str, object]]) -> bool:
    """判断每日预报是否至少包含一项可用于安排的天气字段。"""

    # 第一步：日期本身不能支撑天气调整，必须存在现象、温度、降水或紫外线数据。
    return any(_has_usable_weather_fields(item) for item in forecast)


def _has_usable_weather_fields(forecast: dict[str, object]) -> bool:
    """判断单日预报是否存在除日期外的有效天气数据。"""

    # 第一步：拒绝空白文本和空值，数值零仍是有效降水概率或温度。
    return any(
        value is not None and (not isinstance(value, str) or bool(value.strip()))
        for field_name, value in forecast.items()
        if field_name != "date"
    )


def _log_daily_forecast_shape(payload: dict[str, object]) -> None:
    """记录每日预报的字段形态，便于排查供应商响应契约变化。"""

    # 第一步：只记录键名和数量，不输出天气正文、坐标或请求认证信息。
    daily_items = _get_daily_items(payload)
    first_item = (
        daily_items[0]
        if isinstance(daily_items, list) and daily_items
        else None
    )
    logger.info(
        "天气预报响应结构：top_level_keys=%s daily_list_key=%s daily_count=%s "
        "first_item_keys=%s",
        sorted(str(key) for key in payload.keys()),
        "days" if isinstance(payload.get("days"), list) else "daily",
        len(daily_items) if isinstance(daily_items, list) else 0,
        sorted(str(key) for key in first_item.keys())
        if isinstance(first_item, dict)
        else [],
    )


def _first_non_none(*values: object) -> object | None:
    """返回首个非空值，保留温度零值等有效数据。"""

    # 第一步：不能使用 or 选择器，否则数值零会被误判为缺失。
    return next((value for value in values if value is not None), None)


def _compact_weather_alerts(payload: dict[str, object]) -> list[dict[str, object]]:
    """提取当前生效预警中的事件、等级和时间，避免注入完整预警正文。"""

    # 第一步：兼容 v7 的 warning 和旧版测试桩的 alerts，当前无预警仍是正常业务状态。
    alerts = payload.get("alerts")
    if alerts is None:
        alerts = payload.get("warning")
    if not isinstance(alerts, list):
        return []
    compact_alerts: list[dict[str, object]] = []
    for item in alerts[:3]:
        if not isinstance(item, dict):
            continue
        compact_alerts.append(
            {
                "event": _safe_text(item.get("event")),
                "severity": _safe_text(item.get("severity")),
                "effective_time": _first_text(item, "effectiveTime", "publishTime"),
            }
        )
    return compact_alerts


def _compact_transport_options(payload: dict[str, object]) -> list[dict[str, object]]:
    """保留交通比较所需的方式、距离与耗时，不传递高德完整路线明细。"""

    # 第一步：交通工具已经标准化 summary，本层只做字段裁剪以控制规划上下文体积。
    options = payload.get("options")
    if not isinstance(options, list):
        return []
    compact_options: list[dict[str, object]] = []
    for option in options:
        if not isinstance(option, dict):
            continue
        summary = option.get("summary")
        if not isinstance(summary, dict):
            continue
        duration_seconds = _safe_non_negative_int(summary.get("duration_seconds"))
        distance_meters = _safe_non_negative_int(summary.get("distance_meters"))
        compact_options.append(
            {
                "mode_label": _transport_mode_label(option.get("mode")),
                "duration_text": _format_duration(duration_seconds),
                "distance_text": _format_distance(distance_meters),
            }
        )
    return compact_options


def _transport_mode_label(mode: object) -> str | None:
    """将内部路线方式转换为面向用户的中文名称。"""

    # 第一步：只映射交通工具公开的四种路线方式，未知值保留为空避免补造出行方式。
    return {
        "walking": "步行",
        "transit": "公交",
        "driving": "驾车",
        "bicycling": "骑行",
    }.get(mode) if isinstance(mode, str) else None


def _format_duration(duration_seconds: int | None) -> str | None:
    """将路线秒数转换为用户可读的分钟或小时。"""

    # 第一步：路线耗时向最近一分钟取整，避免将工具原始秒数直接暴露给用户。
    if duration_seconds is None:
        return None
    total_minutes = max(1, round(duration_seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"约{minutes}分钟"
    return f"约{hours}小时" if minutes == 0 else f"约{hours}小时{minutes}分钟"


def _format_distance(distance_meters: int | None) -> str | None:
    """将路线米数转换为用户可读的米或公里。"""

    # 第一步：一公里以下保留米，其他距离按一位小数公里展示并移除无意义末尾零。
    if distance_meters is None:
        return None
    if distance_meters < 1_000:
        return f"约{distance_meters}米"
    distance_kilometers = f"{distance_meters / 1_000:.1f}".rstrip("0").rstrip(".")
    return f"约{distance_kilometers}公里"


def _select_transport_anchor(
    accommodation_candidates: list[dict[str, object]],
    destination_location: str | None,
) -> dict[str, str | None] | None:
    """选择交通路线的住宿优先锚点。"""

    # 第一步：首个有效住宿候选更贴近每天实际出发点，因此优先作为景点和餐饮的比较起点。
    if accommodation_candidates:
        candidate = accommodation_candidates[0]
        location = candidate.get("location")
        if isinstance(location, str):
            return {
                "category": "accommodation",
                "name": _safe_text(candidate.get("name")),
                "address": _safe_text(candidate.get("address")),
                "location": location,
            }
    # 第二步：住宿候选不足时使用目的地中心，保留交通估算而不虚构酒店位置。
    if destination_location is not None:
        return {
            "category": "destination_center",
            "name": "目的地中心",
            "address": None,
            "location": destination_location,
        }
    return None


def _select_route_targets(
    attraction_candidates: list[dict[str, object]],
    food_candidates: list[dict[str, object]],
) -> list[dict[str, str | None]]:
    """从景点和餐饮候选中选择有限数量的路线比较目标。"""

    # 第一步：每类各取一个代表候选，避免一轮规划对所有 POI 发起过多第三方路线请求。
    selected_targets: list[dict[str, str | None]] = []
    for category, candidates in (
        ("attraction", attraction_candidates),
        ("food", food_candidates),
    ):
        for candidate in candidates:
            location = candidate.get("location")
            if not isinstance(location, str):
                continue
            selected_targets.append(
                {
                    "category": category,
                    "name": _safe_text(candidate.get("name")),
                    "address": _safe_text(candidate.get("address")),
                    "location": location,
                }
            )
            break
    # 第二步：将路线查询数量限制在固定上限，防止候选数量增长时放大第三方 API 消耗。
    return selected_targets[:ROUTE_CANDIDATE_LIMIT]


def _get_forecast_days(
    departure_date: str | None,
    trip_duration: TripDuration | None,
) -> int | None:
    """根据出行日期与时长判断是否应查询未来十天预报。"""

    # 第一步：仅接受 ISO 日期；入口暂未保证日期格式时，保守地跳过天气而非请求无关预测。
    if not departure_date:
        return None
    try:
        departure = date.fromisoformat(departure_date)
    except ValueError:
        return None
    days_until_departure = (departure - date.today()).days
    if not 0 <= days_until_departure < MAX_FORECAST_DAYS:
        return None
    # 第二步：请求从今天开始覆盖到旅行结束的窗口，供应商返回后再按行程日期过滤。
    requested_days = duration_to_days(trip_duration)
    remaining_days = MAX_FORECAST_DAYS - days_until_departure
    forecast_window = days_until_departure + (requested_days or remaining_days)
    return max(1, min(forecast_window, MAX_FORECAST_DAYS))


def _select_preference(preferences: list[str], default_value: str) -> str:
    """从用户偏好中选择一个可作为 POI 搜索关键词的文本。"""

    # 第一步：只选择第一个非空偏好，防止多个自然语言偏好直接拼接后降低 POI 搜索准确性。
    for preference in preferences:
        normalized_preference = preference.strip()
        if normalized_preference:
            return normalized_preference
    return default_value


def _record_tool_failure(
    tool_name: str,
    error: Exception,
    unavailable_tools: list[dict[str, str]],
    *,
    error_code: str = "TOOL_UNEXPECTED_ERROR",
    error_message: str = "规划工具执行失败，已跳过当前证据。",
    error_already_logged: bool = False,
) -> None:
    """将工具失败压缩为安全错误码，供方案标记待确认项。"""

    # 第一步：统一错误码、供应商详情和异常堆栈，证据层只保留模型可消费的工具名与错误码。
    if error_already_logged and hasattr(error, "code"):
        unavailable_tools.append(
            {"tool": tool_name, "code": str(getattr(error, "code"))}
        )
        return
    info = record_error(
        error,
        component="tool",
        source="planning_agent",
        operation=tool_name,
        context={"degraded": True},
        default_code=error_code,
        default_message=error_message,
    )
    unavailable_tools.append({"tool": tool_name, "code": str(info["code"])})


def _is_coordinate(value: object) -> bool:
    """判断工具结果是否包含可传给路线和天气工具的坐标文本。"""

    # 第一步：只检查基本经纬度格式，具体范围与精度仍由底层地图、天气工具负责校验。
    if not isinstance(value, str):
        return False
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2 or not all(parts):
        return False
    try:
        float(parts[0])
        float(parts[1])
    except ValueError:
        return False
    return True


def _safe_text(value: object) -> str | None:
    """将供应商字段安全收敛为非空文本或 None。"""

    # 第一步：拒绝非文本与空白文本，避免异常字段进入模型上下文后被误解为有效证据。
    if not isinstance(value, str):
        return None
    normalized_value = value.strip()
    return normalized_value or None


def _safe_non_negative_int(value: object) -> int | None:
    """将供应商数值字段转换为非负整数或 None。"""

    # 第一步：兼容高德字符串数值和交通工具整数摘要，拒绝布尔、负数与小数文本。
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    normalized_value = value.strip()
    return int(normalized_value) if normalized_value.isdigit() else None


def _first_text(payload: dict[str, object], *keys: str) -> str | None:
    """按字段优先级读取首个非空文本。"""

    # 第一步：兼容供应商字段命名的有限差异，避免把整段原始对象传入证据。
    for key in keys:
        value = _safe_text(payload.get(key))
        if value is not None:
            return value
    return None


def _nested_value(
    payload: dict[str, object],
    *keys: str,
) -> object | None:
    """安全读取嵌套供应商字段。"""

    # 第一步：逐层确认字典结构，字段漂移时返回 None 而不是让规划流程中断。
    value: object = payload
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _nested_text(payload: dict[str, object], *keys: str) -> str | None:
    """安全读取嵌套供应商文本字段。"""

    # 第一步：复用通用嵌套读取与文本收敛，确保天气描述为空时不会变成字符串化对象。
    return _safe_text(_nested_value(payload, *keys))
