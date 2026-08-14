"""和风天气工具的离线 HTTP 契约测试。"""

import unittest

import httpx

from app.api.exception.exceptions import QWeatherException
from app.tools.weather_tool import QWeatherTool


class QWeatherToolTests(unittest.IsolatedAsyncioTestCase):
    """验证天气工具使用专属 Host、API Key 请求头与官方端点。"""

    async def test_daily_forecast_uses_coordinate_endpoint_and_api_key_header(
        self,
    ) -> None:
        """每日预报应将高德坐标转为纬度在前的官方路径。"""

        captured_request: httpx.Request | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            """模拟每日预报成功响应并保存请求。"""

            nonlocal captured_request
            # 第一步：记录出站请求，验证路径、参数和 API Key 请求头。
            captured_request = request
            return httpx.Response(
                200,
                json={"metadata": {"attributions": []}, "days": []},
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
            "/weather/v1/daily/39.92/116.41",
        )
        self.assertEqual(captured_request.url.params["days"], "3")
        self.assertEqual(captured_request.url.params["lang"], "zh")
        self.assertEqual(captured_request.url.params["localTime"], "true")
        self.assertEqual(
            captured_request.headers["X-QW-Api-Key"],
            "test-weather-key",
        )
        self.assertNotIn("key", captured_request.url.params)

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

    async def test_alerts_reorder_coordinates_for_official_path(self) -> None:
        """天气预警接口应将输入经纬度重排为纬度、经度路径参数。"""

        def handler(request: httpx.Request) -> httpx.Response:
            """模拟无生效预警的官方响应。"""

            # 第一步：确认路径按官方预警接口的纬度、经度顺序构造。
            self.assertEqual(
                request.url.path,
                "/weatheralert/v1/current/39.92/116.41",
            )
            return httpx.Response(
                200,
                json={"metadata": {"zeroResult": True, "attributions": []}},
            )

        tool = QWeatherTool(
            api_key="test-weather-key",
            api_host="https://demo.qweatherapi.com",
            transport=httpx.MockTransport(handler),
        )

        # 第二步：调用预警查询并确认成功结果不被误判为空响应。
        result = await tool.get_weather_alerts("116.407,39.918")
        self.assertTrue(result["metadata"]["zeroResult"])

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
