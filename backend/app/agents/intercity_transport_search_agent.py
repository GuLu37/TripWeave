"""飞机与火车班次、票价查询 Agent。"""

from app.integrations.browser_search import run_browser_search
from app.schemas import TripRequirements

ALLOWED_TRANSPORT_DOMAINS = (
    "12306.cn",
    "ctrip.com",
    "qunar.com",
)


async def search_intercity_transport(
    requirements: TripRequirements,
) -> dict[str, object]:
    """把飞机和铁路查询任务交给 MCP 浏览 Agent。"""

    if not (
        requirements.origin
        and requirements.destination
        and requirements.departure_date
    ):
        return {
            "status": "unavailable",
            "reason": "requirements_incomplete",
            "message": "缺少出发地、目的地或出发时间，当前无法查询飞机和火车班次。",
            "sources": [],
        }
    return await run_browser_search(
        search_type="intercity_transport",
        requirements={
            "origin": requirements.origin or "",
            "destination": requirements.destination or "",
            "departure_date": requirements.departure_date or "",
            "return_date": requirements.return_date or "",
            "travelers": requirements.traveler_count or "",
        },
        allowed_domains=ALLOWED_TRANSPORT_DOMAINS,
        prompt_filename="intercity_transport_search_agent_prompt.md",
        response_schema={
            "offers": [
                "mode",
                "operator",
                "service_no",
                "origin",
                "destination",
                "departure_time",
                "arrival_time",
                "price",
                "currency",
                "availability",
                "source",
                "fetched_at",
            ]
        },
        start_urls=(
            "https://www.12306.cn/index/",
            "https://flights.ctrip.com/",
            "https://flight.qunar.com/",
        ),
    )
