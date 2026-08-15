"""和风天气工具的离线 HTTP 契约测试。"""

import unittest

import httpx

from app.api.exception.exceptions import QWeatherException
from app.tools.weather_tool import QWeatherTool


class QWeatherToolTests(unittest.IsolatedAsyncioTestCase):
    """验证天气工具使用专属 Host、API Key 请求头与 v7 官方端点。"""

    async def test_daily_forecast_uses_v7_location_query_and_api_key_header(
        self,
    ) -> None:
        """每日预报应将高德坐标转为 location 查询参数。"""

        captured_request: httpx.Request | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            """模拟每日预报成功响应并保存请求。"""

            nonlocal captured_request
            # 第一步：记录出站请求，验证路径、参数和 API Key 请求头。
            captured_request = request
            return httpx.Response(
                200,
                json={"code": "200", "daily": []},
            )

        tool = QWeatherTool(
            api_key="test-weather-key",
            api_host="demo.qweatherapi.com",
            transport=httpx.MockTransport(handler),
        )

        # 第二步：使用高德格式坐标查询三天预报，并捕获后端安全摘要日志。
        with self.assertLogs("app.tools.weather_tool", level="INFO") as logs:
            result = await tool.get_daily_forecast(
                "116.407,39.918",
                days=3,
            )

        self.assertEqual(result["daily"], [])
        self.assertEqual(result["days"], [])
        output = "\n".join(logs.output)
        self.assertIn("天气工具开始查询：operation=daily", output)
        self.assertIn("天气工具查询完成：operation=daily result_count=0", output)
        self.assertNotIn("test-weather-key", output)
        self.assertNotIn("116.407", output)
        self.assertIsNotNone(captured_request)
        assert captured_request is not None
        self.assertEqual(
            captured_request.url.path,
            "/v7/weather/3d",
        )
        self.assertEqual(captured_request.url.params["location"], "116.41,39.92")
        self.assertEqual(captured_request.url.params["lang"], "zh")
        self.assertEqual(
            captured_request.headers["X-QW-Api-Key"],
            "test-weather-key",
        )
        self.assertNotIn("key", captured_request.url.params)

    async def test_daily_forecast_maps_requested_days_to_supported_v7_endpoint(
        self,
    ) -> None:
        """每日预报应使用覆盖请求范围的最小 v7 天数端点。"""

        captured_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            """记录不同业务天数对应的 v7 路径。"""

            # 第一步：收集请求路径，确认 1/4/8 天分别映射到 3/7/10 天端点。
            captured_paths.append(request.url.path)
            return httpx.Response(200, json={"code": "200", "daily": []})

        tool = QWeatherTool(
            api_key="test-weather-key",
            api_host="demo.qweatherapi.com",
            transport=httpx.MockTransport(handler),
        )

        # 第二步：逐一验证规划层常用的三个日期档位。
        await tool.get_daily_forecast("116.407,39.918", days=1)
        await tool.get_daily_forecast("116.407,39.918", days=4)
        await tool.get_daily_forecast("116.407,39.918", days=8)

        self.assertEqual(
            captured_paths,
            ["/v7/weather/3d", "/v7/weather/7d", "/v7/weather/10d"],
        )

    async def test_hourly_forecast_validates_requested_hour_range(self) -> None:
        """逐小时预报应在请求前拒绝超过官方上限的小时数。"""

        tool = QWeatherTool(
            api_key="test-weather-key",
            api_host="demo.qweatherapi.com",
        )

        # 第一步：请求超过 240 小时上限的预报。
        with self.assertRaises(QWeatherException) as context:
            await tool.get_hourly_forecast(
                "116.407,39.918",
                hours=241,
            )

        # 第二步：确认工具在发起网络请求前返回统一参数异常。
        self.assertEqual(context.exception.code, "QWEATHER_PARAMETER_INVALID")
        self.assertEqual(context.exception.details, {"parameter": "hours"})

    async def test_hourly_forecast_maps_requested_hours_to_supported_v7_endpoint(
        self,
    ) -> None:
        """逐小时预报应使用覆盖请求范围的最小 v7 小时端点。"""

        captured_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            """记录不同业务小时数对应的 v7 路径。"""

            # 第一步：收集请求路径，确认 25/73/169 小时分别映射到 72/168/240 小时。
            captured_paths.append(request.url.path)
            return httpx.Response(200, json={"code": "200", "hourly": []})

        tool = QWeatherTool(
            api_key="test-weather-key",
            api_host="demo.qweatherapi.com",
            transport=httpx.MockTransport(handler),
        )

        # 第二步：逐一验证 v7 逐小时预报的固定端点。
        await tool.get_hourly_forecast("116.407,39.918", hours=25)
        await tool.get_hourly_forecast("116.407,39.918", hours=73)
        await tool.get_hourly_forecast("116.407,39.918", hours=169)

        self.assertEqual(
            captured_paths,
            ["/v7/weather/72h", "/v7/weather/168h", "/v7/weather/240h"],
        )

    async def test_alerts_reorder_coordinates_for_official_path(self) -> None:
        """天气预警接口应将高德坐标转换为 v7 location 查询参数。"""

        def handler(request: httpx.Request) -> httpx.Response:
            """模拟无生效预警的官方响应。"""

            # 第一步：确认路径按官方预警接口的纬度、经度顺序构造。
            self.assertEqual(
                request.url.path,
                "/v7/warning/now",
            )
            self.assertEqual(request.url.params["location"], "116.41,39.92")
            return httpx.Response(200, json={"code": "200", "warning": []})

        tool = QWeatherTool(
            api_key="test-weather-key",
            api_host="https://demo.qweatherapi.com",
            transport=httpx.MockTransport(handler),
        )

        # 第二步：调用预警查询并确认成功结果不被误判为空响应。
        result = await tool.get_weather_alerts("116.407,39.918")
        self.assertEqual(result["warning"], [])

    async def test_missing_metadata_with_provider_code_is_mapped_to_business_error(
        self,
    ) -> None:
        """供应商业务错误对象应转为统一异常且不回显 API Key。"""

        def handler(request: httpx.Request) -> httpx.Response:
            """模拟携带供应商业务状态码的 JSON 错误对象。"""

            # 第一步：返回不包含成功 metadata 的业务错误响应。
            return httpx.Response(200, json={"code": "401"})

        tool = QWeatherTool(
            api_key="secret-weather-key",
            api_host="demo.qweatherapi.com",
            transport=httpx.MockTransport(handler),
        )

        # 第二步：确认错误详情只携带供应商状态码，不包含 Key。
        with self.assertRaises(QWeatherException) as context:
            await tool.get_current_weather("116.407,39.918")

        self.assertEqual(context.exception.code, "QWEATHER_BUSINESS_REJECTED")
        self.assertEqual(context.exception.details["qweather_code"], "401")
        self.assertNotIn("secret-weather-key", str(context.exception.details))

    async def test_invalid_coordinates_are_rejected_before_network_request(self) -> None:
        """超出合法范围的坐标不得触发第三方天气请求。"""

        tool = QWeatherTool(
            api_key="test-weather-key",
            api_host="demo.qweatherapi.com",
        )

        # 第一步：传入非法纬度。
        with self.assertRaises(QWeatherException) as context:
            await tool.get_current_weather("116.407,91")

        # 第二步：确认调用方能根据异常参数名修正输入。
        self.assertEqual(context.exception.code, "QWEATHER_PARAMETER_INVALID")
        self.assertEqual(context.exception.details, {"parameter": "location"})


if __name__ == "__main__":
    unittest.main()
