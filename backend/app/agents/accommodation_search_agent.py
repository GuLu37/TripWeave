"""酒店价格估算 Agent。"""

from datetime import date, timedelta

from app.core.trip_duration import duration_to_days
from app.schemas import TripRequirements
from app.tools.hotel_search_tool import search_hotels


async def search_accommodation(
    requirements: TripRequirements,
) -> dict[str, object]:
    """把完整住宿需求交给本地酒店估算工具。"""

    check_out = _get_check_out_date(requirements)
    return await search_hotels(
        city=requirements.destination or "",
        check_in=requirements.departure_date or "",
        check_out=check_out or "",
        travelers=requirements.traveler_count or 1,
        style=_get_hotel_style(requirements),
    )


def _get_check_out_date(requirements: TripRequirements) -> str | None:
    """优先使用返程日期，否则按旅行时长计算退房日期。"""

    if requirements.return_date:
        return requirements.return_date
    if not requirements.departure_date or requirements.trip_duration is None:
        return None
    try:
        departure = date.fromisoformat(requirements.departure_date)
    except ValueError:
        return None
    duration = requirements.trip_duration
    stay_days = duration_to_days(duration)
    if stay_days is None:
        return None
    return (departure + timedelta(days=stay_days)).isoformat()


def _get_hotel_style(requirements: TripRequirements) -> str:
    """从用户住宿偏好中选择估算价格档位。"""

    preferences = " ".join(requirements.accommodation_preferences)
    if any(word in preferences for word in ("豪华", "五星", "高端")):
        return "luxury"
    if any(word in preferences for word in ("经济", "便宜", "预算")):
        return "budget"
    return "comfort"
