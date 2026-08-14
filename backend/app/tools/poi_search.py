"""三个 POI 领域工具复用的高德查询函数。"""

from typing import Literal

from app.tools.map_route_tool import AmapMapRouteTool


async def search_nearby_pois(
    map_route_tool: AmapMapRouteTool,
    location: str,
    *,
    poi_type: str,
    keywords: str | None = None,
    radius_meters: int,
    limit: int = 10,
    sort_rule: Literal["distance", "weight"] = "distance",
) -> dict[str, object]:
    """按固定 POI 分类查询坐标附近候选。"""

    # 第一步：领域工具只提供分类和默认半径，通用地图工具继续负责参数校验与高德异常转换。
    return await map_route_tool.search_nearby(
        location,
        keywords=keywords,
        types=poi_type,
        radius_meters=radius_meters,
        sort_rule=sort_rule,
        page_size=limit,
    )


async def search_city_pois(
    map_route_tool: AmapMapRouteTool,
    city: str,
    keywords: str,
    *,
    poi_type: str,
    limit: int = 10,
    page: int = 1,
) -> dict[str, object]:
    """按固定 POI 分类查询城市内候选。"""

    # 第一步：统一限定城市范围，避免同名地点或商户跨城市混入规划候选。
    return await map_route_tool.search_places(
        keywords,
        city,
        types=poi_type,
        city_limit=True,
        page_size=limit,
        page=page,
    )
