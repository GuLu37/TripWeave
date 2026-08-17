"""用户确认后的路线取证。"""

import asyncio
import logging
import re
from urllib.parse import urlencode

from app.core.settings import get_settings
from app.core.trip_duration import recommended_poi_limit
from app.schemas import (
    ConfirmedTripDetails,
    TripMapPoint,
    TripOverviewRoute,
    TripRequirements,
    TripRoute,
    TripRouteOption,
)
from app.tools.attraction_tool import search_attractions_in_city
from app.tools.food_tool import search_restaurants_in_city
from app.tools.map_route_tool import AmapMapRouteTool
from app.tools.transport_tool import TransportPlanningTool

logger = logging.getLogger(__name__)

ROUTE_MODES = ("driving", "transit", "walking")
ROUTE_MODE_ORDER = {
    "driving": 0,
    "transit": 1,
    "walking": 2,
    "bicycling": 3,
}


async def build_confirmed_trip_details(
    requirements: TripRequirements,
    *,
    proposal: str | None = None,
    trip_evidence: dict[str, object] | None = None,
) -> ConfirmedTripDetails:
    """收集确认方案的高德地图和城市内路线数据。"""

    destination = requirements.destination
    if not destination:
        return ConfirmedTripDetails(
            tool_status={"amap": "skipped"},
        )

    try:
        routes, overview_route, map_points, amap_status = await _build_amap_details(
            requirements,
            proposal=proposal,
            trip_evidence=trip_evidence,
        )
    except Exception as error:
        logger.warning(
            "确认方案路线取证失败：error_type=%s",
            type(error).__name__,
        )
        routes, overview_route, map_points, amap_status = [], None, [], "unavailable"
    return ConfirmedTripDetails(
        overview_route=overview_route,
        map_points=map_points,
        routes=routes,
        tool_status={"amap": amap_status},
    )


async def rebuild_city_routes_for_points(
    requirements: TripRequirements,
    map_points: list[TripMapPoint],
) -> list[TripRoute]:
    """按用户确认后的路线规划顺序重建相邻点城市内路线。"""

    settings = get_settings()
    destination = requirements.destination
    if not settings.amap_web_service_key or not destination:
        return []
    targets = [
        {
            "category": point.category,
            "name": point.name,
            "location": f"{point.longitude},{point.latitude}",
            "address": point.address or "",
        }
        for point in sorted(map_points, key=lambda item: item.sequence)
    ]
    return await _build_sequential_city_routes(
        targets,
        city=destination,
        transport_tool=TransportPlanningTool(AmapMapRouteTool()),
        point_limit=recommended_poi_limit(requirements),
    )


async def _build_amap_details(
    requirements: TripRequirements,
    *,
    proposal: str | None = None,
    trip_evidence: dict[str, object] | None = None,
) -> tuple[list[TripRoute], TripOverviewRoute | None, list[TripMapPoint], str]:
    """生成出发地到目的地概览，以及目的地内代表性路线摘要。"""

    settings = get_settings()
    if not settings.amap_web_service_key:
        logger.info("确认方案路线跳过：reason=amap_config_missing")
        return [], None, [], "unavailable"

    destination = requirements.destination
    if not destination:
        return [], None, [], "skipped"

    map_tool = AmapMapRouteTool()
    try:
        destination_location = _evidence_destination_location(trip_evidence)
        if destination_location is None:
            geocode_result = await map_tool.geocode(destination, city=destination)
            destination_location = _first_location(geocode_result)
        if destination_location is None:
            return [], None, [], "unavailable"
        overview_task = _build_overview_route(
            map_tool,
            requirements,
            destination_location,
        )
        point_limit = recommended_poi_limit(requirements)
        targets = _select_evidence_route_targets(
            proposal,
            trip_evidence,
            limit=point_limit,
        )
        if not targets and not isinstance(trip_evidence, dict):
            poi_results = await _search_plan_pois(
                map_tool,
                requirements,
                proposal,
                destination,
                point_limit=point_limit,
            )
            targets = _select_route_targets(poi_results, limit=point_limit)
        elif not targets:
            logger.info(
                "确认方案路线跳过：reason=verified_poi_evidence_empty_or_unmatched"
            )
        if not targets:
            overview_route = await overview_task
            status = "available" if overview_route else "unavailable"
            return [], overview_route, [], status

        map_points = _build_map_points(targets, limit=point_limit)
        transport_tool = TransportPlanningTool(map_tool)
        route_results, overview_route = await asyncio.gather(
            _build_sequential_city_routes(
                targets,
                city=destination,
                transport_tool=transport_tool,
                point_limit=point_limit,
            ),
            overview_task,
        )
    except Exception as error:
        logger.warning(
            "确认方案高德路线查询失败：error_type=%s",
            type(error).__name__,
        )
        return [], None, [], "unavailable"

    routes = route_results
    status = "available" if routes or overview_route or map_points else "unavailable"
    return routes, overview_route, map_points, status


async def _build_overview_route(
    map_tool: AmapMapRouteTool,
    requirements: TripRequirements,
    destination_location: str,
) -> TripOverviewRoute | None:
    """用高德驾车路线生成出发地到目的地的概览距离。"""

    origin = requirements.origin
    destination = requirements.destination
    if not origin or not destination:
        return None
    try:
        origin_result = await map_tool.geocode(origin)
        origin_location = _first_location(origin_result)
        if origin_location is None:
            return None
        route = await map_tool.plan_route(
            "driving",
            origin_location,
            destination_location,
        )
    except Exception as error:
        logger.warning(
            "确认方案高德概览路线查询失败：error_type=%s",
            type(error).__name__,
        )
        return None
    summary = _extract_amap_route_summary(route)
    origin_coordinate = _parse_coordinate(origin_location)
    destination_coordinate = _parse_coordinate(destination_location)
    return TripOverviewRoute(
        origin=origin,
        destination=destination,
        origin_longitude=origin_coordinate[0] if origin_coordinate else None,
        origin_latitude=origin_coordinate[1] if origin_coordinate else None,
        destination_longitude=destination_coordinate[0] if destination_coordinate else None,
        destination_latitude=destination_coordinate[1] if destination_coordinate else None,
        distance_text=_format_distance(summary.get("distance_meters")),
        duration_text=_format_duration(summary.get("duration_seconds")),
        navigation_url=_build_amap_navigation_url(
            origin_location=origin_location,
            destination_location=destination_location,
            origin_name=origin,
            destination_name=destination,
            mode="driving",
        ),
    )


async def _search_plan_pois(
    map_tool: AmapMapRouteTool,
    requirements: TripRequirements,
    proposal: str | None,
    destination_scope: str,
    *,
    point_limit: int,
) -> list[object]:
    destination = requirements.destination or ""
    named_targets = _extract_route_targets(
        proposal or "",
        destination,
        limit=point_limit,
    )
    jobs: list[tuple[str, object]] = []
    for category, name in named_targets[:point_limit]:
        if category == "food":
            search_job = search_restaurants_in_city(
                map_tool,
                destination_scope,
                name,
                limit=1,
            )
        else:
            search_job = search_attractions_in_city(
                map_tool,
                destination_scope,
                name,
                limit=1,
            )
        jobs.append((
            category,
            search_job,
        ))

    if not jobs:
        return []

    results = await asyncio.gather(
        *[job for _, job in jobs],
        return_exceptions=True,
    )
    ordered_results: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for (category, _), result in zip(jobs, results, strict=True):
        pois: list[dict[str, object]] = []
        for poi in _extract_pois(result):
            name = _optional_string(poi.get("name"))
            if name and name not in seen_names:
                seen_names.add(name)
                pois.append(poi)
        if pois:
            ordered_results.append({"category": category, "pois": pois})
    return ordered_results


def _evidence_destination_location(
    trip_evidence: dict[str, object] | None,
) -> str | None:
    if not isinstance(trip_evidence, dict):
        return None
    location = _optional_string(trip_evidence.get("destination_location"))
    return location if location and _parse_coordinate(location) else None


def _select_evidence_route_targets(
    proposal: str | None,
    trip_evidence: dict[str, object] | None,
    *,
    limit: int,
) -> list[dict[str, str]]:
    """按方案文本中的出现顺序，直接复用规划证据中的 POI 坐标。"""

    if not proposal or not isinstance(trip_evidence, dict):
        return []
    matched_targets: list[tuple[int, dict[str, str]]] = []
    seen_locations: set[str] = set()
    normalized_proposal = _normalize_place_name(proposal)
    for category, evidence_key in (
        ("attraction", "attraction_candidates"),
        ("food", "food_candidates"),
    ):
        candidates = trip_evidence.get(evidence_key)
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            name = _optional_string(candidate.get("name"))
            location = _optional_string(candidate.get("location"))
            if not name or not location or _parse_coordinate(location) is None:
                continue
            text_index = normalized_proposal.find(_normalize_place_name(name))
            if text_index < 0 or location in seen_locations:
                continue
            seen_locations.add(location)
            matched_targets.append(
                (
                    text_index,
                    {
                        "category": category,
                        "name": name,
                        "location": location,
                        "address": _optional_string(candidate.get("address")) or "",
                    },
                )
            )
    matched_targets.sort(key=lambda item: item[0])
    targets: list[dict[str, str]] = []
    for _, target in matched_targets:
        _append_unique_target(targets, target)
        if len(targets) == limit:
            break
    return targets


def _select_route_targets(
    poi_results: list[object],
    *,
    limit: int,
) -> list[dict[str, str]]:
    """优先保留方案中的景点和餐饮目标，供地图展示和少量路线摘要使用。"""

    targets: list[dict[str, str]] = []
    for result in poi_results:
        category = "attraction"
        if isinstance(result, dict) and result.get("category") == "food":
            category = "food"
        for poi in _extract_pois(result):
            name = _optional_string(poi.get("name"))
            location = _optional_string(poi.get("location"))
            if name and location:
                _append_unique_target(
                    targets,
                    {
                        "category": category,
                        "name": name,
                        "location": location,
                        "address": _optional_string(poi.get("address")) or "",
                    },
                )
    return targets[:limit]


def _extract_pois(result: object) -> list[dict[str, object]]:
    if not isinstance(result, dict):
        return []
    pois = result.get("pois")
    if not isinstance(pois, list):
        return []
    return [poi for poi in pois if isinstance(poi, dict)]


def _build_map_points(
    targets: list[dict[str, str]],
    *,
    limit: int,
) -> list[TripMapPoint]:
    points: list[TripMapPoint] = []
    unique_targets: list[dict[str, str]] = []
    for target in targets:
        _append_unique_target(unique_targets, target)
    for index, target in enumerate(unique_targets[:limit], start=1):
        coordinate = _parse_coordinate(target["location"])
        if coordinate is None:
            continue
        points.append(
            TripMapPoint(
                category=target["category"],
                name=target["name"],
                address=target.get("address") or None,
                longitude=coordinate[0],
                latitude=coordinate[1],
                sequence=index,
            )
        )
    return points


def _append_unique_target(
    targets: list[dict[str, str]],
    target: dict[str, str],
) -> None:
    """在路线中保留同类地点的首个代表项，避免不同分店与子景点重复。"""

    if any(
        existing["category"] == target["category"]
        and _same_or_nested_place_name(existing["name"], target["name"])
        for existing in targets
    ):
        return
    targets.append(target)


def _same_or_nested_place_name(first: str, second: str) -> bool:
    normalized_first = _normalize_place_name(first)
    normalized_second = _normalize_place_name(second)
    if not normalized_first or not normalized_second:
        return False
    base_first = _place_base_name(first)
    base_second = _place_base_name(second)
    return (
        normalized_first == normalized_second
        or (base_first and base_first == base_second)
        or (
            min(len(normalized_first), len(normalized_second)) >= 4
            and (
                normalized_first in normalized_second
                or normalized_second in normalized_first
            )
        )
    )


def _normalize_place_name(value: str) -> str:
    return re.sub(r"[\s()（）\[\]【】\-—_·、,，.。]", "", value)


def _place_base_name(value: str) -> str:
    """去掉名称末尾的分店或门店括号信息，用于路线点去重。"""

    without_branch_suffix = re.sub(
        r"(?:\s*(?:\([^()]{1,40}\)|（[^（）]{1,40}）|\[[^\[\]]{1,40}\]))+\s*$",
        "",
        value,
    )
    return _normalize_place_name(without_branch_suffix)


async def _build_sequential_city_routes(
    targets: list[dict[str, str]],
    *,
    city: str,
    transport_tool: TransportPlanningTool,
    point_limit: int,
) -> list[TripRoute]:
    """按路线规划顺序生成相邻景点和美食之间的城市内路线。"""

    ordered_targets = targets[:point_limit]
    if len(ordered_targets) < 2:
        return []
    pairs = list(zip(ordered_targets, ordered_targets[1:]))
    route_results = await asyncio.gather(
        *[
            transport_tool.plan_local_transport(
                origin["location"],
                destination["location"],
                city=city,
                modes=ROUTE_MODES,
            )
            for origin, destination in pairs
        ],
        return_exceptions=True,
    )
    routes: list[TripRoute] = []
    for (origin, destination), result in zip(pairs, route_results, strict=True):
        if not isinstance(result, dict):
            continue
        options = _compact_route_options(
            result,
            origin_location=origin["location"],
            destination_location=destination["location"],
            origin_name=origin["name"],
            destination_name=destination["name"],
        )
        if not options:
            continue
        routes.append(
            TripRoute(
                category=destination["category"],
                origin=origin["name"],
                destination=destination["name"],
                options=options,
                unavailable_modes=[
                    str(item.get("mode"))
                    for item in result.get("unavailable_modes", [])
                    if isinstance(item, dict) and item.get("mode")
                ],
            )
        )
    return routes


def _compact_route_options(
    result: dict[str, object],
    *,
    origin_location: str,
    destination_location: str,
    origin_name: str,
    destination_name: str,
) -> list[TripRouteOption]:
    """只保留前端展示所需的路线方式、距离和耗时。"""

    options: list[TripRouteOption] = []
    for item in result.get("options", []):
        if not isinstance(item, dict):
            continue
        mode = item.get("mode")
        summary = item.get("summary")
        if mode not in {"transit", "walking", "driving", "bicycling"}:
            continue
        if not isinstance(summary, dict):
            continue
        duration_text = _format_duration(summary.get("duration_seconds"))
        if duration_text is None:
            continue
        options.append(
            TripRouteOption(
                mode=mode,
                mode_label=_mode_label(mode),
                distance_text=_format_distance(summary.get("distance_meters")),
                duration_text=duration_text,
                navigation_url=_build_amap_navigation_url(
                    origin_location=origin_location,
                    destination_location=destination_location,
                    origin_name=origin_name,
                    destination_name=destination_name,
                    mode=mode,
                ),
            )
        )
    return sorted(
        options,
        key=lambda option: ROUTE_MODE_ORDER.get(option.mode, len(ROUTE_MODE_ORDER)),
    )


def _extract_route_targets(
    proposal: str,
    destination: str,
    *,
    limit: int,
) -> list[tuple[str, str]]:
    targets: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    section: str | None = None
    for raw_line in proposal.splitlines():
        line = _strip_plan_line(raw_line)
        if not line:
            continue
        food_hint = _has_any(line, ("美食", "餐厅", "餐饮", "用餐", "午餐", "晚餐", "早餐"))
        attraction_hint = _has_any(line, ("景点", "游览", "参观", "景区", "博物馆", "公园"))
        if food_hint:
            section = "food"
        elif attraction_hint:
            section = "attraction"
        elif _has_any(line, ("住宿", "酒店", "交通", "预算", "温馨提示", "注意", "路线", "费用")):
            section = None
        category = "food" if food_hint else "attraction" if attraction_hint else section
        if category not in {"attraction", "food"}:
            continue
        for name in _extract_names_from_plan_line(line, category, destination):
            if name not in seen_names:
                seen_names.add(name)
                targets.append((category, name))
    return targets[:limit]


def _strip_plan_line(line: str) -> str:
    line = re.sub(r"^\s*(?:#{1,6}|[-*+>]|\d+[.)、])\s*", "", line)
    return line.replace("**", "").replace("`", "").strip()


def _extract_names_from_plan_line(
    line: str,
    category: str,
    destination: str,
) -> list[str]:
    content = line
    for separator in ("：", ":"):
        if separator in content:
            prefix, suffix = content.split(separator, 1)
            if len(prefix.strip()) <= 14 and suffix.strip():
                content = suffix
            break
    content = re.sub(r"[（(【\[].*?[）)】\]]", "", content)
    content = re.sub(r"\b\d{1,2}[:：]\d{2}\b", " ", content)
    parts = re.split(r"[、，,；;。/|]+|(?:\s*[>→]\s*)", content)
    names: list[str] = []
    for part in parts:
        name = _clean_route_target_name(part, category, destination)
        if name and name not in names:
            names.append(name)
    return names


def _clean_route_target_name(
    value: str,
    category: str,
    destination: str,
) -> str | None:
    value = re.sub(r"\s+", "", value)
    value = re.sub(
        r"^(?:第[一二三四五六七八九十\d]+天|上午|下午|晚上|中午|早上|早餐|午餐|晚餐)",
        "",
        value,
    )
    value = re.sub(
        r"^(?:安排|推荐|选择|前往|抵达|到达|游览|参观|打卡|体验|品尝|逛|去|在)",
        "",
        value,
    )
    value = re.sub(r"(?:附近|周边|一带|为主|可选|可调整|等.*)$", "", value)
    value = value.strip(" -—:：。；，、")
    if category == "food":
        value = re.sub(r"(?:用餐|就餐|餐厅|美食|菜品|菜系)$", "", value)
    if not 2 <= len(value) <= 30:
        return None
    if destination and value == destination:
        return None
    if _has_any(
        value,
        (
            "建议",
            "预算",
            "交通",
            "住宿",
            "酒店",
            "路线",
            "时间",
            "根据",
            "如果",
            "确认",
            "待定",
        ),
    ):
        return None
    return value


def _has_any(value: str, words: tuple[str, ...]) -> bool:
    return any(word in value for word in words)


def _first_location(payload: dict[str, object]) -> str | None:
    geocodes = payload.get("geocodes")
    if not isinstance(geocodes, list) or not geocodes:
        return None
    first = geocodes[0]
    return _optional_string(first.get("location")) if isinstance(first, dict) else None


def _parse_coordinate(value: str) -> tuple[float, float] | None:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 2:
        return None
    try:
        longitude = float(parts[0])
        latitude = float(parts[1])
    except ValueError:
        return None
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        return None
    return longitude, latitude


def _extract_amap_route_summary(route: dict[str, object]) -> dict[str, int | None]:
    route_root = route.get("route")
    if not isinstance(route_root, dict):
        return {"distance_meters": None, "duration_seconds": None}
    paths = route_root.get("paths")
    if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
        return {"distance_meters": None, "duration_seconds": None}
    return {
        "distance_meters": _as_non_negative_int(paths[0].get("distance")),
        "duration_seconds": _as_non_negative_int(paths[0].get("duration")),
    }


def _build_amap_navigation_url(
    *,
    origin_location: str,
    destination_location: str,
    origin_name: str,
    destination_name: str,
    mode: str,
) -> str:
    mode_value = {
        "driving": "car",
        "transit": "bus",
        "walking": "walk",
        "bicycling": "ride",
    }.get(mode, "car")
    query = urlencode(
        {
            "from": f"{origin_location},{origin_name}",
            "to": f"{destination_location},{destination_name}",
            "mode": mode_value,
            "coordinate": "gaode",
            "callnative": "0",
            "src": "TripWeave",
        }
    )
    return f"https://uri.amap.com/navigation?{query}"


def _optional_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _mode_label(mode: str) -> str:
    return {
        "transit": "公交",
        "walking": "步行",
        "driving": "驾车",
        "bicycling": "骑行",
    }[mode]


def _format_duration(value: object) -> str | None:
    seconds = _as_non_negative_int(value)
    if seconds is None:
        return None
    total_minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"约{minutes}分钟"
    return f"约{hours}小时" if minutes == 0 else f"约{hours}小时{minutes}分钟"


def _format_distance(value: object) -> str | None:
    meters = _as_non_negative_int(value)
    if meters is None:
        return None
    if meters < 1_000:
        return f"约{meters}米"
    kilometers = f"{meters / 1_000:.1f}".rstrip("0").rstrip(".")
    return f"约{kilometers}公里"


def _as_non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
