"""收集目的地、天气与本地交通证据的执行 Agent。"""

from app.agents.planning_evidence import collect_trip_evidence
from app.schemas import TripRequirements
from app.tools.map_route_tool import AmapMapRouteTool
from app.tools.weather_tool import QWeatherTool


async def collect_trip_information(
    requirements: TripRequirements,
    *,
    map_route_tool: AmapMapRouteTool | None = None,
    weather_tool: QWeatherTool | None = None,
) -> dict[str, object]:
    """执行规划 Agent 分派的地点、天气和本地路线信息收集。"""

    return await collect_trip_evidence(
        requirements,
        map_route_tool=map_route_tool or AmapMapRouteTool(),
        weather_tool=weather_tool or QWeatherTool(),
        progress_agent="执行 Agent",
    )
