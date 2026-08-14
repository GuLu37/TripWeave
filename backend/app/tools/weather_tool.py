"""基于和风天气 API 的行程天气预报工具。

规划 Agent 应先从高德地点检索或地理编码结果中取得 ``"经度,纬度"``，再调用本工具：

1. ``get_daily_forecast`` 用于按天安排景点、户外活动和备选日期。
2. ``get_hourly_forecast`` 用于安排具体时段、判断降水概率和风力风险。
3. ``get_current_weather`` 用于确认出发前或当日的实时天气。
4. ``get_weather_alerts`` 用于检查目的地是否存在生效中的极端天气预警。

所有公开方法返回已验证的和风天气原始 JSON。天气数据中的 ``metadata.attributions``
是供应商要求随数据展示的归因信息，后续展示层应保留该信息。
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
from typing import Literal

import httpx

from app.api.exception.exceptions import QWeatherException
from app.core.settings import get_settings

WeatherLanguage = Literal["zh", "en"]
_COORDINATE_PRECISION = Decimal("0.01")
logger = logging.getLogger(__name__)


class QWeatherTool:
    """封装和风天气的实时天气、预报与预警接口。

    Agent 调用建议：

    - 已有每日行程框架时调用 ``get_daily_forecast``，读取 ``days`` 评估高低温、降水和紫外线。
    - 需要把活动排到上午、下午或晚上时调用 ``get_hourly_forecast``，读取 ``hours``。
    - 输出最终方案前调用 ``get_weather_alerts``，存在 ``alerts`` 时必须在方案中显式提示。

    Example:
        ```python
        weather_tool = QWeatherTool()
        daily = await weather_tool.get_daily_forecast("120.15507,30.274085", days=3)
        alerts = await weather_tool.get_weather_alerts("120.15507,30.274085")
        ```
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_host: str | None = None,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """初始化天气工具与可替换的 HTTP 传输层。

        Args:
            api_key: 和风天气 API Key。省略时读取 ``QWEATHER_API_KEY``。
            api_host: 和风天气控制台分配的专属 API Host。可传主机名或完整 HTTPS 地址；
                省略时读取 ``QWEATHER_API_HOST``。
            timeout_seconds: 单次 HTTP 请求的总超时时间，单位为秒。
            transport: 可选 httpx 异步传输层，仅用于离线测试或私有网关适配。

        Raises:
            QWeatherException: 缺少 Key 或专属 API Host 时，在实际调用阶段抛出
                ``QWEATHER_CONFIG_MISSING``。
        """

        # 第一步：仅在调用方未注入配置时读取应用设置，保持测试和生产配置边界清晰。
        settings = None
        if api_key is None or api_host is None:
            settings = get_settings()
        self._api_key = (
            api_key if api_key is not None else settings.qweather_api_key
        )
        self._api_host = (
            api_host if api_host is not None else settings.qweather_api_host
        )
        self._timeout_seconds = timeout_seconds
        self._transport = transport

    async def get_current_weather(
        self,
        location: str,
        *,
        language: WeatherLanguage = "zh",
        local_time: bool = True,
    ) -> dict[str, object]:
        """查询指定坐标的实时天气，用于当日行程风险判断。

        Args:
            location: 必填 ``"经度,纬度"`` 坐标。可直接传高德返回的坐标，工具会按和风
                天气要求转换为最多两位小数。
            language: 返回文本语言，当前支持 ``"zh"`` 或 ``"en"``，默认中文。
            local_time: 是否使用目的地本地时间，默认 ``True``。

        Returns:
            实时天气 JSON。Agent 通常读取 ``condition.text``、``temperature.value``、
            ``feelsLike.value``、``precipitation``、``wind`` 和 ``visibility``。

        Raises:
            QWeatherException: 坐标或语言非法时抛出 ``QWEATHER_PARAMETER_INVALID``；
                网络、鉴权或供应商响应异常时抛出对应的 ``QWEATHER_*`` 错误。

        Example:
            ```python
            current = await weather_tool.get_current_weather("116.397,39.908")
            condition = current["condition"]["text"]
            ```
        """

        # 第一步：标准化坐标与语言后，调用和风天气的 1 公里分辨率实时天气端点。
        latitude, longitude = _require_coordinate(location)
        return await self._request(
            f"/weather/v1/current/{latitude}/{longitude}",
            _build_query_params(language, local_time),
        )

    async def get_daily_forecast(
        self,
        location: str,
        *,
        days: int = 7,
        language: WeatherLanguage = "zh",
        local_time: bool = True,
    ) -> dict[str, object]:
        """查询指定坐标未来 1 至 10 天的每日天气预报。

        Args:
            location: 必填 ``"经度,纬度"`` 坐标；工具会收敛到和风天气要求的两位小数。
            days: 预报天数，范围 1 到 10，默认 7。规划一周内行程时建议按实际天数传入。
            language: 返回文本语言，当前支持 ``"zh"`` 或 ``"en"``。
            local_time: 是否使用目的地本地时间，默认 ``True``。

        Returns:
            每日预报 JSON。Agent 应从 ``days`` 读取日期范围、最高/最低温度、白天与夜间
            ``condition``、``precipitation.probability``、阵风、紫外线和日出日落信息。

        Raises:
            QWeatherException: ``days`` 超出范围、坐标或语言非法时抛出
                ``QWEATHER_PARAMETER_INVALID``；上游失败时抛出相应 ``QWEATHER_*`` 错误。

        Example:
            ```python
            forecast = await weather_tool.get_daily_forecast(
                "120.15507,30.274085",
                days=3,
            )
            first_day = forecast["days"][0]
            ```
        """

        # 第一步：在请求前限制官方支持的预报天数，避免供应商返回难以解释的参数错误。
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 10:
            raise QWeatherException.invalid_parameter("days")
        latitude, longitude = _require_coordinate(location)
        query_params = _build_query_params(language, local_time)
        query_params["days"] = str(days)
        # 第二步：使用精确坐标每日预报端点，为景点安排提供可比较的未来天气依据。
        return await self._request(
            f"/weather/v1/daily/{latitude}/{longitude}",
            query_params,
        )

    async def get_hourly_forecast(
        self,
        location: str,
        *,
        hours: int = 24,
        language: WeatherLanguage = "zh",
        local_time: bool = True,
    ) -> dict[str, object]:
        """查询指定坐标未来 1 至 240 小时的逐小时天气预报。

        Args:
            location: 必填 ``"经度,纬度"`` 坐标；工具会收敛到最多两位小数。
            hours: 预报小时数，范围 1 到 240，默认 24。仅在排具体时段时请求较长范围。
            language: 返回文本语言，当前支持 ``"zh"`` 或 ``"en"``。
            local_time: 是否使用目的地本地时间，默认 ``True``。

        Returns:
            逐小时预报 JSON。Agent 应从 ``hours`` 中读取 ``forecastTime``、天气现象、
            温度、降水概率、阵风、能见度和紫外线；不要只根据单一温度字段判断活动风险。

        Raises:
            QWeatherException: ``hours`` 超出范围、坐标或语言非法时抛出
                ``QWEATHER_PARAMETER_INVALID``；上游失败时抛出相应 ``QWEATHER_*`` 错误。

        Example:
            ```python
            forecast = await weather_tool.get_hourly_forecast(
                "121.4737,31.2304",
                hours=48,
            )
            hourly_items = forecast["hours"]
            ```
        """

        # 第一步：在请求前限制官方支持的小时范围，避免模型请求把无效参数交给供应商。
        if (
            not isinstance(hours, int)
            or isinstance(hours, bool)
            or not 1 <= hours <= 240
        ):
            raise QWeatherException.invalid_parameter("hours")
        latitude, longitude = _require_coordinate(location)
        query_params = _build_query_params(language, local_time)
        query_params["hours"] = str(hours)
        # 第二步：使用精确坐标小时预报端点，为出行时段和室内备选安排提供依据。
        return await self._request(
            f"/weather/v1/hourly/{latitude}/{longitude}",
            query_params,
        )

    async def get_weather_alerts(
        self,
        location: str,
        *,
        language: WeatherLanguage = "zh",
        local_time: bool = True,
    ) -> dict[str, object]:
        """查询指定坐标当前生效的官方极端天气预警。

        Args:
            location: 必填 ``"经度,纬度"`` 坐标；工具会自动调整为预警接口要求的
                ``纬度/经度`` 路径顺序。
            language: 返回文本语言，当前支持 ``"zh"`` 或 ``"en"``。
            local_time: 是否使用目的地本地时间，默认 ``True``。

        Returns:
            天气预警 JSON。``metadata.zeroResult`` 为 ``True`` 表示没有生效预警；
            否则 Agent 必须检查 ``alerts`` 中的事件类型、严重程度、发布时间和正文。

        Raises:
            QWeatherException: 坐标或语言非法时抛出 ``QWEATHER_PARAMETER_INVALID``；
                上游失败时抛出相应 ``QWEATHER_*`` 错误。

        Example:
            ```python
            alerts = await weather_tool.get_weather_alerts("116.397,39.908")
            has_alert = not alerts["metadata"]["zeroResult"]
            ```
        """

        # 第一步：从“经度,纬度”输入拆分出预警接口规定的“纬度/经度”路径参数。
        latitude, longitude = _require_coordinate(location)
        # 第二步：仅查询当前生效预警，避免将历史或失效事件误写入旅行风险提示。
        return await self._request(
            f"/weatheralert/v1/current/{latitude}/{longitude}",
            _build_query_params(language, local_time),
        )

    async def _request(
        self,
        path: str,
        params: dict[str, str],
    ) -> dict[str, object]:
        """发送和风天气 GET 请求并校验 HTTP 与 JSON 响应契约。"""

        # 第一步：在出站前核对专属 API Host 和 API Key，禁止回退到即将弃用的公共地址。
        operation = _get_operation_name(path)
        missing_settings = [
            setting_name
            for setting_name, value in {
                "QWEATHER_API_HOST": self._api_host,
                "QWEATHER_API_KEY": self._api_key,
            }.items()
            if not value or not value.strip()
        ]
        if missing_settings:
            logger.warning(
                "天气工具配置缺失：operation=%s missing_settings=%s",
                operation,
                ",".join(missing_settings),
            )
            raise QWeatherException.config_missing("、".join(missing_settings))

        try:
            base_url = _normalize_api_host(self._api_host)
        except QWeatherException:
            raise

        try:
            # 第二步：记录不含坐标、Host 和 Key 的调用摘要，供服务日志定位天气能力使用情况。
            logger.info(
                "天气工具开始查询：operation=%s language=%s local_time=%s forecast_size=%s",
                operation,
                params.get("lang"),
                params.get("localTime"),
                params.get("days") or params.get("hours"),
            )
            # 第二步：使用官方推荐的 X-QW-Api-Key 请求头认证，避免 Key 出现在 URL、日志或缓存键中。
            async with httpx.AsyncClient(
                base_url=base_url,
                timeout=self._timeout_seconds,
                transport=self._transport,
                headers={"X-QW-Api-Key": self._api_key},
            ) as client:
                response = await client.get(path, params=params)
                response.raise_for_status()
        except httpx.HTTPStatusError as error:
            # 第三步：只保留安全的 HTTP 状态码，不能记录带认证头的完整请求。
            logger.warning(
                "天气工具请求被拒绝：operation=%s upstream_status_code=%s",
                operation,
                error.response.status_code,
            )
            raise QWeatherException.api_rejected(error.response.status_code) from error
        except httpx.HTTPError as error:
            # 第四步：网络、超时与协议异常交由后续 ToolExecutor 统一决定是否重试。
            logger.warning(
                "天气工具请求不可达：operation=%s error_type=%s",
                operation,
                type(error).__name__,
            )
            raise QWeatherException.api_unreachable() from error

        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            # 第五步：天气响应必须是 JSON 对象，非结构化正文不能进入规划 Agent 上下文。
            logger.warning(
                "天气工具响应非 JSON：operation=%s",
                operation,
            )
            raise QWeatherException.bad_response() from error
        if not isinstance(payload, dict):
            logger.warning(
                "天气工具响应非对象：operation=%s response_type=%s",
                operation,
                type(payload).__name__,
            )
            raise QWeatherException.bad_response()

        # 第六步：新版天气与预警成功响应都包含 metadata；兼容供应商错误对象中的 code 字段。
        metadata = payload.get("metadata")
        if not isinstance(metadata, dict):
            if "code" in payload:
                logger.warning(
                    "天气工具业务响应失败：operation=%s provider_code=%s",
                    operation,
                    payload.get("code"),
                )
                raise QWeatherException.business_rejected(payload.get("code"))
            logger.warning(
                "天气工具响应缺少 metadata：operation=%s",
                operation,
            )
            raise QWeatherException.bad_response()
        # 第七步：记录结果规模和预警空结果状态，便于维护时判断数据是否符合预期。
        logger.info(
            "天气工具查询完成：operation=%s result_count=%s zero_result=%s",
            operation,
            _get_result_count(operation, payload),
            metadata.get("zeroResult"),
        )
        return payload


def _build_query_params(
    language: WeatherLanguage,
    local_time: bool,
) -> dict[str, str]:
    """构造各天气端点共享的语言与本地时间查询参数。"""

    # 第一步：仅接受当前工具公开的语言枚举，避免无效语言参数被错误地视为成功天气数据。
    if language not in {"zh", "en"}:
        raise QWeatherException.invalid_parameter("language")
    # 第二步：显式传递本地时间开关，确保行程日期和小时不会因默认 UTC 发生错位。
    return {
        "lang": language,
        "localTime": str(local_time).lower(),
    }


def _require_coordinate(location: str) -> tuple[str, str]:
    """将高德坐标标准化为和风天气使用的“纬度、经度”路径参数。"""

    # 第一步：接收高德一致的“经度,纬度”文本，拒绝地址、空值和缺少分隔符的输入。
    if not isinstance(location, str):
        raise QWeatherException.invalid_parameter("location")
    parts = [part.strip() for part in location.split(",")]
    if len(parts) != 2 or not all(parts):
        raise QWeatherException.invalid_parameter("location")
    try:
        longitude, latitude = (Decimal(part) for part in parts)
    except InvalidOperation as error:
        raise QWeatherException.invalid_parameter("location") from error

    # 第二步：检查合法经纬度范围；NaN、Infinity 及越界值都不能进入天气服务路径。
    if (
        not longitude.is_finite()
        or not latitude.is_finite()
        or not Decimal("-180") <= longitude <= Decimal("180")
        or not Decimal("-90") <= latitude <= Decimal("90")
    ):
        raise QWeatherException.invalid_parameter("location")
    # 第三步：按官方最多两位小数限制转换坐标，高德更高精度坐标可直接安全传入。
    normalized_longitude = _format_coordinate(
        longitude.quantize(_COORDINATE_PRECISION, rounding=ROUND_HALF_UP)
    )
    normalized_latitude = _format_coordinate(
        latitude.quantize(_COORDINATE_PRECISION, rounding=ROUND_HALF_UP)
    )
    return normalized_latitude, normalized_longitude


def _format_coordinate(value: Decimal) -> str:
    """将已规范化的十进制坐标转换为普通字符串。"""

    # 第一步：禁止科学计数法进入 URL 路径，并移除不会改变数值的末尾零。
    normalized_value = format(value, "f").rstrip("0").rstrip(".")
    return normalized_value if normalized_value else "0"


def _normalize_api_host(api_host: str | None) -> str:
    """将环境变量中的专属 API Host 规范化为 HTTPS 基础地址。"""

    # 第一步：允许配置完整 HTTPS 地址或控制台展示的纯主机名，拒绝非 HTTPS 方案。
    if not isinstance(api_host, str) or not api_host.strip():
        raise QWeatherException.invalid_parameter("QWEATHER_API_HOST")
    normalized_host = api_host.strip().rstrip("/")
    base_url = (
        normalized_host
        if normalized_host.startswith("https://")
        else f"https://{normalized_host}"
    )
    if not base_url.startswith("https://") or "/" in base_url.removeprefix("https://"):
        raise QWeatherException.invalid_parameter("QWEATHER_API_HOST")
    return base_url


def _get_operation_name(path: str) -> str:
    """将内部请求路径映射为不含坐标的日志操作名称。"""

    # 第一步：仅按固定官方路径识别能力类型，日志中绝不保留动态坐标路径段。
    if path.startswith("/weather/v1/current/"):
        return "current"
    if path.startswith("/weather/v1/daily/"):
        return "daily"
    if path.startswith("/weather/v1/hourly/"):
        return "hourly"
    if path.startswith("/weatheralert/v1/current/"):
        return "alerts"
    return "unknown"


def _get_result_count(
    operation: str,
    payload: dict[str, object],
) -> int | None:
    """提取天气响应中可用于维护日志的结果数量。"""

    # 第一步：预报和预警结果按官方列表字段统计，避免打印完整数据正文。
    list_field_by_operation = {
        "daily": "days",
        "hourly": "hours",
        "alerts": "alerts",
    }
    list_field = list_field_by_operation.get(operation)
    if list_field is not None:
        result = payload.get(list_field)
        return len(result) if isinstance(result, list) else 0
    # 第二步：实时天气没有列表结构，仅用 1 或 0 表示是否返回核心天气现象。
    return 1 if isinstance(payload.get("condition"), dict) else 0
