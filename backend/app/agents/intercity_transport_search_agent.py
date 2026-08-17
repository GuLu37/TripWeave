"""飞机与火车班次、票价估算 Agent。"""

from app.schemas import TripRequirements
from app.services.chat_progress import track_progress
from app.tools.traffic_search_tool import search_traffic


async def search_intercity_transport(
    requirements: TripRequirements,
) -> dict[str, object]:
    """把完整城际交通需求交给本地估算工具。"""

    async with track_progress(
        "城际交通查询 Agent",
        "生成飞机和高铁候选参考",
        tool="交通价格估算",
    ) as progress:
        if not (
            requirements.origin
            and requirements.destination
            and requirements.departure_date
        ):
            progress.mark_unavailable()
            return {
                "status": "unavailable",
                "reason": "requirements_incomplete",
                "message": "缺少出发地、目的地或出发时间，当前无法查询飞机和火车班次。",
                "offers": [],
                "sources": [],
                "is_estimate": True,
                "price_type": "estimated",
            }
        return await search_traffic(
            origin=requirements.origin,
            destination=requirements.destination,
            departure_date=requirements.departure_date,
            travelers=requirements.traveler_count or 1,
            preferences=requirements.transport_preferences,
        )
