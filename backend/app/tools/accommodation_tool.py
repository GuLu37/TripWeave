"""住宿查询工具：返回高德住宿 POI 候选，不代表价格、库存或可预订状态。"""

from typing import Literal

from app.tools.map_route_tool import AmapMapRouteTool
from app.tools.poi_search import search_city_pois, search_nearby_pois

ACCOMMODATION_POI_TYPE = "100000"


async def search_nearby_hotels(
    map_route_tool: AmapMapRouteTool,
    location: str,
    *,
    keywords: str | None = None,
    radius_meters: int = 3_000,
    limit: int = 10,
    sort_rule: Literal["distance", "weight"] = "distance",
) -> dict[str, object]:
    """查询指定坐标附近的住宿候选。

    Args:
        map_route_tool: 已配置的高德地图工具。
        location: 中心点坐标，例如 ``"120.15507,30.274085"``。
        keywords: 可选偏好，例如 ``"商务酒店"``。
    """

    # 第一步：固定住宿服务分类，复用共享 POI 查询函数。
    return await search_nearby_pois(
        map_route_tool,
        location,
        poi_type=ACCOMMODATION_POI_TYPE,
        keywords=keywords,
        radius_meters=radius_meters,
        limit=limit,
        sort_rule=sort_rule,
    )


async def search_hotels_in_city(
    map_route_tool: AmapMapRouteTool,
    city: str,
    keywords: str = "酒店",
    *,
    limit: int = 10,
    page: int = 1,
) -> dict[str, object]:
    """查询城市范围内的住宿候选。"""

    # 第一步：固定住宿服务分类，城市范围和分页由共享函数统一传递。
    return await search_city_pois(
        map_route_tool,
        city,
        keywords,
        poi_type=ACCOMMODATION_POI_TYPE,
        limit=limit,
        page=page,
    )
