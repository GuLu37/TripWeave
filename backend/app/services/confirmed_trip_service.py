"""用户确认后的图片与路线取证。"""

import asyncio
import logging

from app.core.settings import get_settings
from app.schemas import (
    ConfirmedTripDetails,
    TripImage,
    TripRequirements,
    TripRoute,
    TripRouteOption,
)
from app.tools.attraction_tool import search_attractions_in_city
from app.tools.food_tool import search_restaurants_in_city
from app.tools.map_route_tool import AmapMapRouteTool
from app.tools.transport_tool import TransportPlanningTool
from app.services.unsplash_service import UnsplashService

logger = logging.getLogger(__name__)

PHOTO_LIMIT_PER_CATEGORY = 3
ROUTE_TARGET_LIMIT = 2
ROUTE_MODES = ("transit", "walking", "driving")


async def build_confirmed_trip_details(
    requirements: TripRequirements,
) -> ConfirmedTripDetails:
    """并发收集确认方案的图片和路线数据，单个来源失败不阻断确认。"""

    destination = requirements.destination
    if not destination:
        return ConfirmedTripDetails(
            tool_status={"unsplash": "skipped", "amap": "skipped"},
        )

    unsplash_task = _build_images(requirements)
    amap_task = _build_routes(requirements)
    image_result, route_result = await asyncio.gather(
        unsplash_task,
        amap_task,
        return_exceptions=True,
    )

    images = (
        image_result
        if isinstance(image_result, list)
        else []
    )
    routes, amap_status = (
        route_result
        if isinstance(route_result, tuple)
        else ([], "unavailable")
    )
    if isinstance(image_result, Exception):
        logger.warning(
            "确认方案图片取证失败：error_type=%s",
            type(image_result).__name__,
        )
    if isinstance(route_result, Exception):
        logger.warning(
            "确认方案路线取证失败：error_type=%s",
            type(route_result).__name__,
        )
    return ConfirmedTripDetails(
        images=images,
        routes=routes,
        tool_status={
            "unsplash": "available" if images else "unavailable",
            "amap": amap_status,
        },
    )


async def _build_images(
    requirements: TripRequirements,
) -> list[TripImage]:
    """为景点和美食各取少量图片，并保留 Unsplash 署名信息。"""

    destination = requirements.destination or ""
    attraction_query = _build_photo_query(
        destination,
        requirements.attraction_preferences,
        "景点",
    )
    food_query = _build_photo_query(
        destination,
        requirements.dining_preferences,
        "美食",
    )
    service = UnsplashService()
    results = await asyncio.gather(
        service.search_photos(
            attraction_query,
            per_page=PHOTO_LIMIT_PER_CATEGORY,
        ),
        service.search_photos(
            food_query,
            per_page=PHOTO_LIMIT_PER_CATEGORY,
        ),
    )
    images: list[TripImage] = []
    for category, query, photos in (
        ("attraction", attraction_query, results[0]),
        ("food", food_query, results[1]),
    ):
        for photo in photos:
            url = photo.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            images.append(
                TripImage(
                    category=category,
                    query=query,
                    url=url,
                    thumb_url=_optional_string(photo.get("thumb_url")),
                    alt_text=_optional_string(photo.get("alt_text")),
                    photographer=_optional_string(photo.get("photographer")),
                    source_url=_optional_string(photo.get("source_url")),
                )
            )
    return images


async def _build_routes(
    requirements: TripRequirements,
) -> tuple[list[TripRoute], str]:
    """从目的地中心到少量景点和餐饮候选生成高德路线摘要。"""

    settings = get_settings()
    if not settings.amap_web_service_key:
        logger.info("确认方案路线跳过：reason=amap_config_missing")
        return [], "unavailable"

    destination = requirements.destination
    if not destination:
        return [], "skipped"

    map_tool = AmapMapRouteTool()
    try:
        geocode_result = await map_tool.geocode(destination, city=destination)
        destination_location = _first_location(geocode_result)
        if destination_location is None:
            return [], "unavailable"
        poi_results = await asyncio.gather(
            search_attractions_in_city(
                map_tool,
                destination,
                _first_preference(requirements.attraction_preferences, "景点"),
                limit=2,
            ),
            search_restaurants_in_city(
                map_tool,
                destination,
                _first_preference(requirements.dining_preferences, "餐厅"),
                limit=2,
            ),
            return_exceptions=True,
        )
        targets = _select_route_targets(poi_results)
        if not targets:
            return [], "unavailable"

        transport_tool = TransportPlanningTool(map_tool)
        route_results = await asyncio.gather(
            *[
                transport_tool.plan_local_transport(
                    destination_location,
                    target["location"],
                    city=destination,
                    modes=ROUTE_MODES,
                )
                for target in targets
            ],
            return_exceptions=True,
        )
    except Exception as error:
        logger.warning(
            "确认方案高德路线查询失败：error_type=%s",
            type(error).__name__,
        )
        return [], "unavailable"

    routes: list[TripRoute] = []
    for target, result in zip(targets, route_results, strict=True):
        if not isinstance(result, dict):
            continue
        routes.append(
            TripRoute(
                category=target["category"],
                origin=f"{destination}中心",
                destination=target["name"],
                options=_compact_route_options(result),
                unavailable_modes=[
                    str(item.get("mode"))
                    for item in result.get("unavailable_modes", [])
                    if isinstance(item, dict) and item.get("mode")
                ],
            )
        )
    return routes, "available" if routes else "unavailable"


def _select_route_targets(
    poi_results: list[object],
) -> list[dict[str, str]]:
    """优先保留一个景点和一个餐饮目标，限制确认阶段的高德调用量。"""

    targets: list[dict[str, str]] = []
    for category, result in (
        ("attraction", poi_results[0] if len(poi_results) > 0 else None),
        ("food", poi_results[1] if len(poi_results) > 1 else None),
    ):
        if not isinstance(result, dict):
            continue
        pois = result.get("pois")
        if not isinstance(pois, list):
            continue
        for poi in pois:
            if not isinstance(poi, dict):
                continue
            name = _optional_string(poi.get("name"))
            location = _optional_string(poi.get("location"))
            if name and location:
                targets.append(
                    {
                        "category": category,
                        "name": name,
                        "location": location,
                    }
                )
                break
    return targets[:ROUTE_TARGET_LIMIT]


def _compact_route_options(
    result: dict[str, object],
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
        options.append(
            TripRouteOption(
                mode=mode,
                mode_label=_mode_label(mode),
                distance_text=_format_distance(summary.get("distance_meters")),
                duration_text=_format_duration(summary.get("duration_seconds")),
            )
        )
    return options


def _build_photo_query(
    destination: str,
    preferences: list[str],
    fallback: str,
) -> str:
    preference = _first_preference(preferences, fallback)
    return f"{destination} {preference}".strip()


def _first_preference(preferences: list[str], fallback: str) -> str:
    return next(
        (
            preference.strip()
            for preference in preferences
            if isinstance(preference, str) and preference.strip()
        ),
        fallback,
    )


def _first_location(payload: dict[str, object]) -> str | None:
    geocodes = payload.get("geocodes")
    if not isinstance(geocodes, list) or not geocodes:
        return None
    first = geocodes[0]
    return _optional_string(first.get("location")) if isinstance(first, dict) else None


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
