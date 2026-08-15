"""酒店价格与库存查询 Agent。"""

from datetime import date, timedelta

from app.core.trip_duration import duration_to_days
from app.integrations.browser_search import run_browser_search
from app.schemas import TripRequirements

ALLOWED_ACCOMMODATION_DOMAINS = ("ctrip.com", "qunar.com")


async def search_accommodation(
    requirements: TripRequirements,
) -> dict[str, object]:
    """把酒店查询任务交给 MCP 浏览 Agent。"""

    check_out = _get_check_out_date(requirements)
    return await run_browser_search(
        search_type="accommodation",
        requirements={
            "city": requirements.destination or "",
            "check_in": requirements.departure_date or "",
            "check_out": check_out or "",
            "travelers": requirements.traveler_count or "",
        },
        allowed_domains=ALLOWED_ACCOMMODATION_DOMAINS,
        prompt_filename="accommodation_search_agent_prompt.md",
        response_schema={
            "offers": [
                "name",
                "room_type",
                "check_in",
                "check_out",
                "travelers",
                "price",
                "currency",
                "availability",
                "cancellation_policy",
                "source",
                "fetched_at",
            ]
        },
        start_urls=(
            "https://hotels.ctrip.com/",
            "https://hotel.qunar.com/",
        ),
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
