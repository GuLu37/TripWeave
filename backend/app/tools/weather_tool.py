"""基于和风天气 API 的行程天气预报工具。

规划 Agent 应先从高德地点检索或地理编码结果中取得 ``"经度,纬度"``，再调用本工具：

``get_daily_forecast`` 用于按天安排景点、户外活动和备选日期。

所有公开方法返回已验证的和风天气 JSON。工具同时兼容旧版测试桩中的
``metadata`` 结构，以及当前 v7 接口中的 ``code`` 和 ``daily`` 字段。
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import logging
from typing import Literal

import httpx

from app.api.exception.error_handler import record_error
from app.api.exception.exceptions import QWeatherException
from app.core.settings import get_settings

WeatherLanguage = Literal["zh", "en"]
_COORDINATE_PRECISION = Decimal("0.01")
logger = logging.getLogger(__name__)


class QWeatherTool:
    """封装和风天气的每日预报接口。

    Agent 调用建议：

    - 已有每日行程框架时调用 ``get_daily_forecast``，读取 ``days`` 评估高低温、降水和紫外线。

    Example:
        ```python
        weather_tool = QWeatherTool()
        daily = await weather_tool.get_daily_forecast("120.15507,30.274085", days=3)
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

    async def get_daily_forecast(
        self,
        location: str,
        *,
        days: int = 7,
        language: WeatherLanguage = "zh",
    ) -> dict[str, object]:
        """查询指定坐标未来 1 至 10 天的每日天气预报。

        Args:
            location: 必填 ``"经度,纬度"`` 坐标；工具会收敛到和风天气要求的两位小数。
            days: 预报天数，范围 1 到 10，默认 7。规划一周内行程时建议按实际天数传入。
            language: 返回文本语言，当前支持 ``"zh"`` 或 ``"en"``。

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

        # 第一步：在请求前限制规划层允许的预报范围，避免把无效天数交给供应商。
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 10:
            raise QWeatherException.invalid_parameter("days")
        latitude, longitude = _require_coordinate(location)
        query_params = _build_query_params(language)
        query_params["location"] = f"{longitude},{latitude}"
        # 第二步：将业务所需天数映射为 v7 支持的 3、7、10 天端点。
        endpoint_days = _select_daily_endpoint_days(days)
        payload = await self._request(
            f"/v7/weather/{endpoint_days}d",
            query_params,
        )
        # 第三步：只保留调用方请求的天数，并提供旧版 days 别名供规划层兼容读取。
        return _limit_daily_forecast(payload, days)

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
            error = QWeatherException.config_missing("、".join(missing_settings))
            record_error(
                error,
                component="tool",
                source="weather_tool",
                operation=f"weather.{operation}",
                context={
                    "missing_settings": missing_settings,
                    "degraded": True,
                },
                default_code="QWEATHER_CONFIG_MISSING",
                default_message="天气工具配置不完整，无法查询天气。",
            )
            raise error

        try:
            base_url = _normalize_api_host(self._api_host)
        except QWeatherException as error:
            record_error(
                error,
                component="tool",
                source="weather_tool",
                operation=f"weather.{operation}",
                context={"degraded": True},
                default_code="QWEATHER_PARAMETER_INVALID",
                default_message="天气工具请求参数无效，无法查询天气。",
            )
            raise

        try:
            # 第二步：记录不含坐标、Host 和 Key 的调用摘要，供服务日志定位天气能力使用情况。
            logger.info(
                "天气工具开始查询：operation=%s language=%s local_time=%s forecast_size=%s",
                operation,
                params.get("lang"),
                "provider_local_time",
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
            tool_error = QWeatherException.api_rejected(
                error.response.status_code
            )
            record_error(
                tool_error,
                component="tool",
                source="weather_tool",
                operation=f"weather.{operation}",
                context={
                    "upstream_status_code": error.response.status_code,
                    "degraded": True,
                },
                default_code="QWEATHER_API_REJECTED",
                default_message="天气服务拒绝了本次查询。",
            )
            raise tool_error from error
        except httpx.HTTPError as error:
            # 第四步：网络、超时与协议异常交由后续 ToolExecutor 统一决定是否重试。
            tool_error = QWeatherException.api_unreachable()
            record_error(
                tool_error,
                component="tool",
                source="weather_tool",
                operation=f"weather.{operation}",
                context={"degraded": True},
                default_code="QWEATHER_API_UNREACHABLE",
                default_message="天气服务暂时不可达。",
            )
            raise tool_error from error

        try:
            payload = response.json()
        except (TypeError, ValueError) as error:
            # 第五步：天气响应必须是 JSON 对象，非结构化正文不能进入规划 Agent 上下文。
            tool_error = QWeatherException.bad_response()
            record_error(
                tool_error,
                component="tool",
                source="weather_tool",
                operation=f"weather.{operation}",
                context={"response_shape": "non_json", "degraded": True},
                default_code="QWEATHER_API_BAD_RESPONSE",
                default_message="天气服务返回了无法识别的响应。",
            )
            raise tool_error from error
        if not isinstance(payload, dict):
            tool_error = QWeatherException.bad_response()
            record_error(
                tool_error,
                component="tool",
                source="weather_tool",
                operation=f"weather.{operation}",
                context={
                    "response_shape": type(payload).__name__,
                    "degraded": True,
                },
                default_code="QWEATHER_API_BAD_RESPONSE",
                default_message="天气服务返回了无法识别的响应。",
            )
            raise tool_error

        # 第六步：v7 成功响应通过 code=200 表示成功；旧版测试桩仍可使用 metadata。
        provider_code = payload.get("code")
        if provider_code is not None and str(provider_code) != "200":
            tool_error = QWeatherException.business_rejected(provider_code)
            record_error(
                tool_error,
                component="tool",
                source="weather_tool",
                operation=f"weather.{operation}",
                context={"provider_code": str(provider_code), "degraded": True},
                default_code="QWEATHER_BUSINESS_REJECTED",
                default_message="天气服务未能完成本次查询。",
            )
            raise tool_error
        metadata = payload.get("metadata")
        if provider_code is None and not isinstance(metadata, dict):
            tool_error = QWeatherException.bad_response()
            record_error(
                tool_error,
                component="tool",
                source="weather_tool",
                operation=f"weather.{operation}",
                context={"missing_field": "metadata", "degraded": True},
                default_code="QWEATHER_API_BAD_RESPONSE",
                default_message="天气服务响应缺少必要字段。",
            )
            raise tool_error
        # 第七步：记录结果规模和预警空结果状态，便于维护时判断数据是否符合预期。
        logger.info(
            "天气工具查询完成：operation=%s result_count=%s zero_result=%s",
            operation,
            _get_result_count(operation, payload),
            metadata.get("zeroResult") if isinstance(metadata, dict) else None,
        )
        return payload


def _build_query_params(
    language: WeatherLanguage,
) -> dict[str, str]:
    """构造各天气端点共享的语言查询参数。"""

    # 第一步：仅接受当前工具公开的语言枚举，避免无效语言参数被错误地视为成功天气数据。
    if language not in {"zh", "en"}:
        raise QWeatherException.invalid_parameter("language")
    # 第二步：v7 接口按目的地本地时间返回结果，不再发送旧版 localTime 参数。
    return {"lang": language}


def _require_coordinate(location: str) -> tuple[str, str]:
    """将高德坐标标准化为和风天气查询所需的“纬度、经度”内部表示。"""

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


def _select_daily_endpoint_days(days: int) -> int:
    """将业务预报天数映射为和风天气 v7 支持的每日端点。"""

    # 第一步：v7 不支持任意 N 天路径，使用覆盖请求范围的最小官方端点。
    if days <= 3:
        return 3
    if days <= 7:
        return 7
    return 10


def _limit_daily_forecast(
    payload: dict[str, object],
    days: int,
) -> dict[str, object]:
    """裁剪 v7 每日预报并写入旧版 days 兼容字段。"""

    # 第一步：v7 使用 daily，旧版规划层和测试桩使用 days，优先读取供应商原始列表。
    daily_items = payload.get("daily")
    if not isinstance(daily_items, list):
        daily_items = payload.get("days")
    if not isinstance(daily_items, list):
        return payload
    # 第二步：只传递调用方请求的天数，避免 3/7/10 天端点带来无关未来信息。
    limited_items = daily_items[:days]
    payload["daily"] = limited_items
    payload["days"] = limited_items
    return payload


def _get_operation_name(path: str) -> str:
    """将内部请求路径映射为不含坐标的日志操作名称。"""

    # 第一步：仅按固定官方路径识别能力类型，日志中绝不保留动态坐标或查询参数。
    if path.startswith("/v7/weather/") and path.endswith("d"):
        return "daily"
    return "unknown"


def _get_result_count(
    operation: str,
    payload: dict[str, object],
) -> int | None:
    """提取天气响应中可用于维护日志的结果数量。"""

    # 第一步：预报结果按官方列表字段统计，避免打印完整数据正文。
    if operation != "daily":
        return None
    result = payload.get("days")
    if result is None:
        result = payload.get("daily")
    return len(result) if isinstance(result, list) else 0
