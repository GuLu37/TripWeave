"""景点查询工具：返回高德风景名胜 POI 候选，不代表门票或开放状态。"""

from typing import Literal

from app.tools.map_route_tool import AmapMapRouteTool
from app.tools.poi_search import search_city_pois, search_nearby_pois

SCENIC_SPOT_POI_TYPE = "110000"


async def search_nearby_attractions(
    map_route_tool: AmapMapRouteTool,
    location: str,
    *,
    keywords: str | None = None,
    radius_meters: int = 3_000,
    limit: int = 10,
    sort_rule: Literal["distance", "weight"] = "distance",
) -> dict[str, object]:
    """查询指定坐标附近的景点候选。"""

    # 第一步：固定风景名胜分类，复用共享 POI 查询函数。
    return await search_nearby_pois(
        map_route_tool,
        location,
        poi_type=SCENIC_SPOT_POI_TYPE,
        keywords=keywords,
        radius_meters=radius_meters,
        limit=limit,
        sort_rule=sort_rule,
    )


async def search_attractions_in_city(
    map_route_tool: AmapMapRouteTool,
    city: str,
    keywords: str = "景点",
    *,
    limit: int = 10,
    page: int = 1,
) -> dict[str, object]:
    """查询城市范围内的景点候选。"""

    # 第一步：固定风景名胜分类，城市范围和分页由共享函数统一传递。
    return await search_city_pois(
        map_route_tool,
        city,
        keywords,
        poi_type=SCENIC_SPOT_POI_TYPE,
        limit=limit,
        page=page,
    )
