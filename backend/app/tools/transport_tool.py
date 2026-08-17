"""基于高德路线能力的本地交通规划工具。

本工具只处理城市内或城市周边的路线选择，不查询火车票、机票、打车订单或其他交易价格。
规划 Agent 传入已经由地图工具确认的起终点坐标后，可一次获得公交、步行、驾车与骑行方案，
再基于距离、耗时和路线明细安排景点间的出行。
"""

import asyncio
import logging

from app.api.exception.exceptions import AmapException
from app.api.exception.error_handler import record_error
from app.tools.map_route_tool import AmapMapRouteTool, RouteMode

_DEFAULT_MODES: tuple[RouteMode, ...] = (
    "driving",
    "transit",
    "walking",
    "bicycling",
)
logger = logging.getLogger(__name__)


class TransportPlanningTool:
    """并发比较高德公交、步行、驾车和骑行路线的交通规划工具。

    Agent 调用建议：

    - 先通过 ``AmapMapRouteTool.geocode`` 或 ``search_places`` 获得 POI 的 ``location``。
    - 以 ``plan_local_transport`` 生成多个本地出行选项。
    - 将 ``options`` 中的 ``summary`` 用于比较，将 ``route`` 用于解释换乘与路线细节。
    - ``unavailable_modes`` 表示该方式本次未获取成功，不能据此推断没有路线。

    Example:
        ```python
        transport_tool = TransportPlanningTool()
        plan = await transport_tool.plan_local_transport(
            origin="120.15507,30.274085",
            destination="120.1614,30.2798",
            city="杭州",
        )
        fastest = min(
            plan["options"],
            key=lambda option: option["summary"]["duration_seconds"] or float("inf"),
        )
        ```
    """

    def __init__(
        self,
        map_route_tool: AmapMapRouteTool | None = None,
    ) -> None:
        """初始化交通规划工具。

        Args:
            map_route_tool: 可选的高德路线工具实例。省略时自动创建，测试时可注入模拟实现。
        """

        # 第一步：复用同一个地图工具，避免交通规划层重复管理 API Key、HTTP 客户端和异常转换。
        self._map_route_tool = map_route_tool or AmapMapRouteTool()

    async def plan_local_transport(
        self,
        origin: str,
        destination: str,
        city: str,
        *,
        destination_city: str | None = None,
        modes: tuple[RouteMode, ...] = _DEFAULT_MODES,
    ) -> dict[str, object]:
        """并发生成同城或跨城接驳的多种本地交通方案。

        Args:
            origin: 必填起点坐标，格式为 ``"经度,纬度"``。
            destination: 必填终点坐标，格式与 ``origin`` 相同。
            city: 必填起点城市或城市编码，用于高德公交路线规划。
            destination_city: 可选终点城市或城市编码，仅传递给公交跨城接驳规划。
            modes: 要比较的方式。只支持 ``"transit"``、``"walking"``、``"driving"``
                和 ``"bicycling"``；默认同时查询四种方式。

        Returns:
            包含 ``options`` 与 ``unavailable_modes`` 的字典：

            - ``options``：每项包含 ``mode``、可比较的 ``summary`` 和高德原始 ``route``。
            - ``summary.duration_seconds``：路线总耗时，缺失时为 ``None``。
            - ``summary.distance_meters``：路线总距离，缺失时为 ``None``。
            - ``unavailable_modes``：单一方式的安全错误码，不含 API Key、坐标或原始响应。

        Raises:
            AmapException: 没有传入有效方式、坐标或城市参数时，由底层高德工具返回统一异常。

        Example:
            ```python
            result = await transport_tool.plan_local_transport(
                "116.397,39.908",
                "116.407,39.918",
                city="北京",
                modes=("transit", "walking"),
            )
            ```
        """

        # 第一步：拒绝空方式列表和未支持方式，避免无任何实际查询却被误判为成功规划。
        selected_modes = _validate_modes(modes)
        normalized_city = _require_city(city, "city")
        normalized_destination_city = (
            _require_city(destination_city, "destination_city")
            if destination_city is not None
            else None
        )
        logger.info(
            "交通规划开始：mode_count=%s has_destination_city=%s",
            len(selected_modes),
            normalized_destination_city is not None,
        )

        # 第二步：路线调用彼此独立，使用并发减少规划 Agent 等待所有方式结果的总耗时。
        results = await asyncio.gather(
            *[
                self._plan_mode(
                    mode,
                    origin,
                    destination,
                    normalized_city,
                    normalized_destination_city,
                )
                for mode in selected_modes
            ],
            return_exceptions=True,
        )

        # 第三步：保留可用方案并单独记录失败方式，避免骑行等单路失败阻断公交或驾车推荐。
        options: list[dict[str, object]] = []
        unavailable_modes: list[dict[str, str]] = []
        for mode, result in zip(selected_modes, results, strict=True):
            if isinstance(result, AmapException):
                record_error(
                    result,
                    component="tool",
                    source="transport_tool",
                    operation=f"plan_route.{mode}",
                    context={"mode": mode, "degraded": True},
                    default_code="TRANSPORT_ROUTE_UNAVAILABLE",
                    default_message="本地交通路线查询失败，已跳过当前出行方式。",
                )
                unavailable_modes.append({"mode": mode, "code": result.code})
                continue
            if isinstance(result, Exception):
                info = record_error(
                    result,
                    component="tool",
                    source="transport_tool",
                    operation=f"plan_route.{mode}",
                    context={"mode": mode, "degraded": True},
                    default_code="TRANSPORT_ROUTE_UNAVAILABLE",
                    default_message="本地交通路线查询失败，已跳过当前出行方式。",
                )
                unavailable_modes.append(
                    {"mode": mode, "code": str(info["code"])}
                )
                continue
            options.append(
                {
                    "mode": mode,
                    "summary": _build_route_summary(mode, result),
                    "route": result,
                }
            )

        # 第四步：只记录方案数量与失败方式，不将精确坐标、高德 Key 或完整路线写入日志。
        logger.info(
            "交通规划完成：available_options=%s unavailable_modes=%s",
            len(options),
            ",".join(item["mode"] for item in unavailable_modes) or "none",
        )
        return {
            "options": options,
            "unavailable_modes": unavailable_modes,
        }

    async def _plan_mode(
        self,
        mode: RouteMode,
        origin: str,
        destination: str,
        city: str,
        destination_city: str | None,
    ) -> dict[str, object]:
        """将单个交通方式映射为高德路线工具调用。"""

        # 第一步：公交规划必须带城市参数，其他方式不能携带该参数以符合底层工具契约。
        if mode == "transit":
            return await self._map_route_tool.plan_route(
                mode,
                origin,
                destination,
                city=city,
                destination_city=destination_city,
            )
        # 第二步：步行、驾车与骑行直接复用同一对坐标，保持不同方式的可比较性。
        return await self._map_route_tool.plan_route(
            mode,
            origin,
            destination,
        )


def _validate_modes(
    modes: tuple[RouteMode, ...],
) -> tuple[RouteMode, ...]:
    """校验交通方式集合并按原始顺序去重。"""

    # 第一步：调用方只能传入非空元组，防止字符串或空集合绕过类型提示进入并发任务创建。
    if not isinstance(modes, tuple) or not modes:
        raise AmapException.invalid_parameter("modes")
    normalized_modes: list[RouteMode] = []
    for mode in modes:
        if mode not in _DEFAULT_MODES:
            raise AmapException.invalid_parameter("modes")
        if mode not in normalized_modes:
            normalized_modes.append(mode)
    # 第二步：保留调用方定义的比较优先级，同时避免重复请求同一种高德路线。
    return tuple(normalized_modes)


def _require_city(value: str, parameter_name: str) -> str:
    """校验并返回非空城市或城市编码。"""

    # 第一步：公交规划依赖城市范围，空值或非文本不能交给高德接口猜测处理。
    if not isinstance(value, str):
        raise AmapException.invalid_parameter(parameter_name)
    normalized_value = value.strip()
    if not normalized_value:
        raise AmapException.invalid_parameter(parameter_name)
    return normalized_value


def _build_route_summary(
    mode: RouteMode,
    route: dict[str, object],
) -> dict[str, int | None]:
    """从不同高德路线响应中提取可比较的距离与耗时摘要。"""

    # 第一步：高德 v3 公交使用 route.transits，步行和驾车使用 route.paths。
    if mode == "transit":
        route_root = _as_dict(route.get("route"))
        first_route = _first_item(route_root.get("transits"))
    elif mode == "bicycling":
        # 第二步：高德 v4 骑行将路径放在 data.paths，与 v3 的 route.paths 不同。
        data = _as_dict(route.get("data"))
        first_route = _first_item(data.get("paths"))
    else:
        route_root = _as_dict(route.get("route"))
        first_route = _first_item(route_root.get("paths"))

    # 第三步：摘要只提取通用数值，换乘、道路收费和步骤信息仍保留在原始 route 中供 Agent 解释。
    return {
        "duration_seconds": _as_non_negative_int(first_route.get("duration")),
        "distance_meters": _as_non_negative_int(first_route.get("distance")),
    }


def _as_dict(value: object) -> dict[str, object]:
    """将非字典响应字段安全降级为空字典。"""

    # 第一步：高德响应字段缺失或类型漂移时不抛出二次异常，由摘要返回 None 表示未知。
    return value if isinstance(value, dict) else {}


def _first_item(value: object) -> dict[str, object]:
    """安全读取高德路径列表的首个候选方案。"""

    # 第一步：仅接受非空列表中的字典项，避免空路线或异常响应导致规划工具崩溃。
    if not isinstance(value, list) or not value:
        return {}
    first_item = value[0]
    return first_item if isinstance(first_item, dict) else {}


def _as_non_negative_int(value: object) -> int | None:
    """将高德字符串或整数距离、耗时转换为非负整数。"""

    # 第一步：拒绝布尔值、负数和非整数字符串，避免错误摘要参与路线排序。
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if not isinstance(value, str):
        return None
    normalized_value = value.strip()
    if not normalized_value.isdigit():
        return None
    return int(normalized_value)
