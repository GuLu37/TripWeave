"""基于高德地图 Web 服务的地点检索与路线查询工具。

规划 Agent 可将此工具作为统一的地理能力入口：

1. 先用 ``geocode`` 将出发地、酒店或景点地址转换为经纬度。
2. 再用 ``search_places`` 查询餐饮、住宿或景点候选 POI。
3. 最后用 ``plan_route`` 计算候选地点间的步行、公交、驾车或骑行路线。

所有公开方法返回已经通过高德业务状态校验的原始 JSON 字典；调用失败时抛出
``AmapException``，调用方可使用其 ``code``、``retryable`` 和 ``details`` 决定重试、
降级或向用户追问。
"""

from decimal import Decimal, InvalidOperation
from typing import Literal

import httpx

from app.api.exception.exceptions import AmapException
from app.core.settings import get_settings

# Agent 只能从这四种官方支持的出行方式中选择，不能传入自然语言或未支持的模式。
RouteMode = Literal["walking", "transit", "driving", "bicycling"]

_ROUTE_ENDPOINTS: dict[RouteMode, str] = {
    "walking": "/v3/direction/walking",
    "transit": "/v3/direction/transit/integrated",
    "driving": "/v3/direction/driving",
    "bicycling": "/v4/direction/bicycling",
}
_V3_ROUTE_MODES: set[RouteMode] = {"walking", "transit", "driving"}


class AmapMapRouteTool:
    """封装高德地点检索、地理编码与路线规划 Web 服务。

    Agent 调用建议：

    - 用户只给出地址或地名时，先调用 ``geocode``。
    - 需要按城市找“酒店”“川菜”“博物馆”等候选点时，调用 ``search_places``。
    - ``plan_route`` 的起终点必须是 ``"经度,纬度"``，不可直接传入地址。

    Example:
        ```python
        amap_tool = AmapMapRouteTool()
        hotel = await amap_tool.search_places("酒店", city="杭州", page_size=10)
        route = await amap_tool.plan_route(
            "walking",
            origin="120.15507,30.274085",
            destination="120.1614,30.2798",
        )
        ```
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """初始化地图工具与可替换的 HTTP 传输层。

        Args:
            api_key: 高德 Web 服务 Key。省略时读取 ``AMAP_WEB_SERVICE_KEY``。
            base_url: 高德 Web 服务基础地址。省略时读取
                ``AMAP_WEB_SERVICE_BASE_URL``。
            timeout_seconds: 单次 HTTP 请求的总超时时间，单位为秒。
            transport: 可选的 httpx 异步传输层，仅用于离线测试或私有网关适配。

        Raises:
            AmapException: 未提供有效的高德 Web 服务 Key 时，在实际调用阶段抛出
                ``AMAP_CONFIG_MISSING``。
        """

        # 第一步：仅在调用方未传入配置时读取应用设置，便于单元测试注入隔离配置。
        settings = None
        if api_key is None or base_url is None:
            settings = get_settings()
        self._api_key = api_key if api_key is not None else settings.amap_web_service_key
        self._base_url = (
            base_url if base_url is not None else settings.amap_web_service_base_url
        ).rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def geocode(
        self,
        address: str,
        city: str | None = None,
    ) -> dict[str, object]:
        """将结构化或自然语言地址转换为经纬度坐标。

        Args:
            address: 必填地址文本，例如 ``"北京市朝阳区阜通东大街6号"``。
            city: 可选城市或城市编码，用于缩小同名地址的检索范围，例如 ``"北京"``。

        Returns:
            高德 v3 地理编码响应。Agent 通常读取 ``geocodes`` 数组中首项的
            ``location``，其格式为后续路线工具可直接使用的 ``"经度,纬度"``。

        Raises:
            AmapException: 地址为空时抛出 ``AMAP_PARAMETER_INVALID``；上游服务、
                Key 或业务状态异常时抛出对应的 ``AMAP_*`` 错误。

        Example:
            ```python
            result = await amap_tool.geocode("西湖景区", city="杭州")
            coordinate = result["geocodes"][0]["location"]
            ```
        """

        # 第一步：校验高德地理编码所需地址，并过滤空白城市限定条件。
        params: dict[str, str] = {"address": _require_text(address, "address")}
        if city is not None:
            params["city"] = _require_text(city, "city")
        # 第二步：调用官方 v3 地理编码端点，返回高德原始但已验真的 JSON 对象。
        return await self._request("/v3/geocode/geo", params, response_version="v3")

    async def search_places(
        self,
        keywords: str,
        city: str | None = None,
        *,
        types: str | None = None,
        city_limit: bool = False,
        page_size: int = 20,
        page: int = 1,
        extensions: Literal["base", "all"] = "base",
    ) -> dict[str, object]:
        """按关键词检索高德 POI，并支持城市与分页过滤。

        Args:
            keywords: 必填搜索词。可传地点名称或业务类别，例如 ``"西湖景区"``、
                ``"杭州菜"``、``"高档酒店"``。
            city: 可选城市或城市编码。规划餐饮、住宿、景点时通常应传入目的地。
            types: 可选高德 POI 分类编码。仅在 Agent 已有可靠分类编码时传入。
            city_limit: 是否只返回指定 ``city`` 内的结果；未传 ``city`` 时保持 ``False``。
            page_size: 单页数量，范围为 1 到 25，默认 20。
            page: 页码，从 1 开始，默认 1。
            extensions: ``"base"`` 返回基础字段；``"all"`` 同时请求营业时间、评分等
                更丰富的 POI 信息。

        Returns:
            高德 v3 POI 文本搜索响应。Agent 应从 ``pois`` 数组读取候选项，并优先使用
            每项的 ``name``、``location``、``address``、``type`` 等字段形成计划依据。

        Raises:
            AmapException: 关键词、城市、分类或分页参数非法时抛出
                ``AMAP_PARAMETER_INVALID``；高德服务异常时抛出相应 ``AMAP_*`` 错误。

        Example:
            ```python
            result = await amap_tool.search_places(
                "亲子友好酒店",
                city="上海",
                city_limit=True,
                page_size=10,
                extensions="all",
            )
            candidates = result["pois"]
            ```
        """

        # 第一步：按官方文本搜索参数规则校验关键词、分页大小和扩展信息层级。
        if not 1 <= page_size <= 25:
            raise AmapException.invalid_parameter("page_size")
        if page < 1:
            raise AmapException.invalid_parameter("page")
        if extensions not in {"base", "all"}:
            raise AmapException.invalid_parameter("extensions")

        params = {
            "keywords": _require_text(keywords, "keywords"),
            "citylimit": str(city_limit).lower(),
            "offset": str(page_size),
            "page": str(page),
            "extensions": extensions,
        }
        if city is not None:
            params["city"] = _require_text(city, "city")
        if types is not None:
            params["types"] = _require_text(types, "types")
        # 第二步：调用官方 v3 POI 关键字搜索端点，供餐饮、住宿和景点工具复用。
        return await self._request("/v3/place/text", params, response_version="v3")

    async def plan_route(
        self,
        mode: RouteMode,
        origin: str,
        destination: str,
        *,
        city: str | None = None,
        destination_city: str | None = None,
        strategy: int | None = None,
        waypoints: list[str] | None = None,
    ) -> dict[str, object]:
        """根据官方路线规划端点计算两点之间的出行方案。

        Args:
            mode: 出行方式，只能为 ``"walking"``、``"transit"``、``"driving"`` 或
                ``"bicycling"``。
            origin: 必填起点坐标，格式为 ``"经度,纬度"``，小数点后最多 6 位。
            destination: 必填终点坐标，格式与 ``origin`` 相同。
            city: 公交 ``"transit"`` 模式必填的起点城市或城市编码；其他模式不得传入。
            destination_city: 公交跨城路线的可选终点城市或城市编码，仅能与 ``city`` 一起使用。
            strategy: 可选官方路线策略整数。具体含义按 ``mode`` 由高德接口解释。
            waypoints: 驾车 ``"driving"`` 模式专用的途经点坐标列表，最多 16 个；每项格式
                必须为 ``"经度,纬度"``。

        Returns:
            已校验的高德路线响应：

            - ``walking``、``transit``、``driving`` 返回 v3 ``route`` 数据。
            - ``bicycling`` 返回 v4 ``data`` 数据。

            Agent 应从结果中提取距离、耗时、收费、换乘或路径步骤，而非将原始 JSON 直接展示
            给用户。

        Raises:
            AmapException: 非法模式、坐标精度超限、公交未传 ``city`` 或非法途经点时抛出
                ``AMAP_PARAMETER_INVALID``；高德服务异常时抛出相应 ``AMAP_*`` 错误。

        Examples:
            ```python
            walking = await amap_tool.plan_route(
                "walking",
                origin="120.15507,30.274085",
                destination="120.1614,30.2798",
            )

            transit = await amap_tool.plan_route(
                "transit",
                origin="121.4737,31.2304",
                destination="121.4998,31.2397",
                city="上海",
            )

            driving = await amap_tool.plan_route(
                "driving",
                origin="116.397,39.908",
                destination="116.407,39.918",
                waypoints=["116.401,39.912"],
            )
            ```
        """

        # 第一步：校验坐标使用官方规定的“经度,纬度”格式，避免地址误传入路线接口。
        if mode not in _ROUTE_ENDPOINTS:
            raise AmapException.invalid_parameter("mode")
        normalized_origin = _require_coordinate(origin, "origin")
        normalized_destination = _require_coordinate(destination, "destination")
        params = {
            "origin": normalized_origin,
            "destination": normalized_destination,
        }

        # 第二步：公交规划必须提供出发城市；终点城市与策略在用户明确时才传入。
        if mode == "transit":
            if city is None:
                raise AmapException.invalid_parameter("city")
            params["city"] = _require_text(city, "city")
            if destination_city is not None:
                params["cityd"] = _require_text(destination_city, "destination_city")
        elif city is not None or destination_city is not None:
            raise AmapException.invalid_parameter("city")

        # 第三步：路线策略由高德官方枚举解释，工具仅接受整数并按模式限制途经点。
        if strategy is not None:
            if not isinstance(strategy, int) or isinstance(strategy, bool):
                raise AmapException.invalid_parameter("strategy")
            params["strategy"] = str(strategy)
        if waypoints is not None:
            if mode != "driving" or not waypoints or len(waypoints) > 16:
                raise AmapException.invalid_parameter("waypoints")
            params["waypoints"] = ";".join(
                _require_coordinate(point, "waypoints") for point in waypoints
            )

        # 第四步：v3 路线端点使用 status 字段，v4 骑行端点使用 errcode 字段。
        response_version = "v3" if mode in _V3_ROUTE_MODES else "v4"
        return await self._request(
            _ROUTE_ENDPOINTS[mode],
            params,
            response_version=response_version,
        )

    async def _request(
        self,
        path: str,
        params: dict[str, str],
        *,
        response_version: Literal["v3", "v4"],
    ) -> dict[str, object]:
        """发送高德 GET 请求并校验 HTTP 与高德业务状态。"""

        # 第一步：阻止空 Key 出站，并在请求参数中统一附加官方要求的 key。
        if not self._api_key or not self._api_key.strip():
            raise AmapException.config_missing()
        request_params = {"key": self._api_key, **params}

        try:
            # 第二步：按当前请求建立短生命周期异步客户端，超时覆盖连接与读取等待。
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.get(path, params=request_params)
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            # 第三步：仅保留上游状态码，避免把请求 URL 中的 Key 写入异常详情或日志。
            raise AmapException.api_rejected(error.response.status_code) from error
        except httpx.HTTPError as error:
            # 第四步：连接、超时和协议错误统一交给上层 ToolExecutor 决定是否重试。
            raise AmapException.api_unreachable() from error

        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            # 第五步：高德成功 HTTP 响应也必须是 JSON 对象，避免非结构化数据污染规划上下文。
            raise AmapException.bad_response() from error
        if not isinstance(payload, dict):
            raise AmapException.bad_response()

        # 第六步：分别校验高德 v3 和 v4 的官方业务成功字段。
        if response_version == "v3" and payload.get("status") not in {"1", 1}:
            raise AmapException.business_rejected(
                _optional_text(payload.get("info")),
                payload.get("infocode"),
            )
        if response_version == "v4" and payload.get("errcode") != 0:
            raise AmapException.business_rejected(
                _optional_text(payload.get("errmsg")),
                payload.get("errcode"),
            )
        return payload


def _require_text(value: str, parameter_name: str) -> str:
    """返回去除空白后的必填文本参数。"""

    # 第一步：拒绝空字符串和非字符串，避免让第三方服务返回难以定位的参数错误。
    if not isinstance(value, str):
        raise AmapException.invalid_parameter(parameter_name)
    normalized_value = value.strip()
    if not normalized_value:
        raise AmapException.invalid_parameter(parameter_name)
    return normalized_value


def _require_coordinate(value: str, parameter_name: str) -> str:
    """校验并规范化高德要求的“经度,纬度”坐标参数。"""

    # 第一步：按逗号拆分经度与纬度，拒绝地址、分号坐标串和缺失坐标。
    normalized_value = _require_text(value, parameter_name)
    parts = [part.strip() for part in normalized_value.split(",")]
    if len(parts) != 2:
        raise AmapException.invalid_parameter(parameter_name)
    try:
        longitude, latitude = (Decimal(part) for part in parts)
    except InvalidOperation as error:
        raise AmapException.invalid_parameter(parameter_name) from error

    # 第二步：高德要求经纬度小数点后不超过 6 位，并且必须落在合法经纬度范围内。
    if (
        not longitude.is_finite()
        or not latitude.is_finite()
        or -longitude.as_tuple().exponent > 6
        or -latitude.as_tuple().exponent > 6
        or not Decimal("-180") <= longitude <= Decimal("180")
        or not Decimal("-90") <= latitude <= Decimal("90")
    ):
        raise AmapException.invalid_parameter(parameter_name)
    # 第三步：移除无意义的末尾零，保留高德规定的经度在前、纬度在后的坐标形式。
    return f"{_format_coordinate(longitude)},{_format_coordinate(latitude)}"


def _format_coordinate(value: Decimal) -> str:
    """将已验证的十进制坐标转换为普通字符串形式。"""

    # 第一步：避免科学计数法进入 HTTP 参数，同时去除数值表示中无意义的末尾零。
    normalized_value = format(value, "f").rstrip("0").rstrip(".")
    return normalized_value if normalized_value else "0"


def _optional_text(value: object) -> str | None:
    """将高德错误摘要安全转换为可展示文本。"""

    # 第一步：仅接受非空字符串，避免响应字段异常时错误处理器再次抛出类型异常。
    if not isinstance(value, str):
        return None
    normalized_value = value.strip()
    return normalized_value or None
