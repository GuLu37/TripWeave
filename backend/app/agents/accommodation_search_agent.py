"""酒店价格估算 Agent。"""

from datetime import date, timedelta

from app.core.trip_duration import duration_to_days
from app.schemas import TripRequirements
from app.services.chat_progress import track_progress
from app.tools.hotel_search_tool import search_hotels


async def search_accommodation(
    requirements: TripRequirements,
) -> dict[str, object]:
    """把完整住宿需求交给本地酒店估算工具。"""

    check_in, check_out = _get_reference_stay_dates(requirements)
    async with track_progress(
        "住宿查询 Agent",
        "生成酒店价格、房型和库存参考",
        tool="酒店价格估算",
    ):
        return await search_hotels(
            city=requirements.destination or "",
            check_in=check_in,
            check_out=check_out,
            travelers=requirements.traveler_count or 1,
            style=_get_hotel_style(requirements),
        )


def _get_reference_stay_dates(requirements: TripRequirements) -> tuple[str, str]:
    """为酒店参考查询补出稳定的入住和退房日期。"""

    today = date.today()
    check_in = today
    if requirements.departure_date:
        try:
            check_in = date.fromisoformat(requirements.departure_date)
        except ValueError:
            check_in = today
    check_out = _get_check_out_date(requirements, check_in)
    if check_out is None:
        check_out = (check_in + timedelta(days=1)).isoformat()
    return check_in.isoformat(), check_out


def _get_check_out_date(
    requirements: TripRequirements,
    departure: date,
) -> str | None:
    """优先使用返程日期，否则按旅行时长计算退房日期。"""

    if requirements.return_date:
        try:
            return_date = date.fromisoformat(requirements.return_date)
        except ValueError:
            return None
        if return_date > departure:
            return return_date.isoformat()
        return None
    if requirements.trip_duration is None:
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
