"""高德地图工具的离线 HTTP 契约测试。"""

import unittest

import httpx

from app.api.exception.exceptions import AmapException
from app.tools.map_route_tool import AmapMapRouteTool


class AmapMapRouteToolTests(unittest.IsolatedAsyncioTestCase):
    """验证高德工具使用官方路径、参数与成功状态字段。"""

    async def test_search_places_uses_official_v3_text_search_parameters(self) -> None:
        """POI 文本搜索应将分页与城市过滤编码为官方查询参数。"""

        captured_request: httpx.Request | None = None

        def handler(request: httpx.Request) -> httpx.Response:
            """模拟高德 v3 成功响应并记录请求。"""

            nonlocal captured_request
            # 第一步：保存请求，供断言检查路径与查询参数。
            captured_request = request
            return httpx.Response(
                200,
                json={"status": "1", "info": "OK", "pois": []},
            )

        tool = AmapMapRouteTool(
            api_key="test-key",
            base_url="https://restapi.amap.com",
            transport=httpx.MockTransport(handler),
        )

        # 第二步：执行文本搜索，并确认官方 v3 路径和限定参数被正确传递。
        result = await tool.search_places(
            "咖啡馆",
            "上海",
            city_limit=True,
            page_size=10,
            page=2,
            extensions="all",
        )

        self.assertEqual(result["status"], "1")
        self.assertIsNotNone(captured_request)
        assert captured_request is not None
        self.assertEqual(captured_request.url.path, "/v3/place/text")
        self.assertEqual(captured_request.url.params["key"], "test-key")
        self.assertEqual(captured_request.url.params["keywords"], "咖啡馆")
        self.assertEqual(captured_request.url.params["city"], "上海")
        self.assertEqual(captured_request.url.params["citylimit"], "true")
        self.assertEqual(captured_request.url.params["offset"], "10")
        self.assertEqual(captured_request.url.params["page"], "2")
        self.assertEqual(captured_request.url.params["extensions"], "all")

    async def test_bicycling_route_accepts_official_v4_success_shape(self) -> None:
        """骑行路线应使用 v4 端点并识别 errcode 成功字段。"""

        def handler(request: httpx.Request) -> httpx.Response:
            """模拟高德 v4 骑行规划成功响应。"""

            # 第一步：确认调用的是官方 v4 骑行路径。
            self.assertEqual(request.url.path, "/v4/direction/bicycling")
            return httpx.Response(
                200,
                json={"errcode": 0, "errmsg": "OK", "data": {"paths": []}},
            )

        tool = AmapMapRouteTool(
            api_key="test-key",
            base_url="https://restapi.amap.com",
            transport=httpx.MockTransport(handler),
        )

        # 第二步：执行路线规划，确认经度在前的坐标被保留在查询参数中。
        result = await tool.plan_route(
            "bicycling",
            "116.397,39.908",
            "116.407,39.918",
        )

        self.assertEqual(result["errcode"], 0)

    async def test_transit_route_requires_departure_city(self) -> None:
        """公交规划缺少官方必填城市参数时不得发送网络请求。"""

        tool = AmapMapRouteTool(
            api_key="test-key",
            base_url="https://restapi.amap.com",
        )

        # 第一步：执行缺少 city 的公交规划，并确认工具层提前提示参数错误。
        with self.assertRaises(AmapException) as context:
            await tool.plan_route(
                "transit",
                "116.397,39.908",
                "116.407,39.918",
            )

        self.assertEqual(context.exception.code, "AMAP_PARAMETER_INVALID")
        self.assertEqual(context.exception.details, {"parameter": "city"})

    async def test_route_rejects_coordinates_with_more_than_six_decimals(self) -> None:
        """路线接口应拒绝不符合官方精度限制的经纬度。"""

        tool = AmapMapRouteTool(
            api_key="test-key",
            base_url="https://restapi.amap.com",
        )

        # 第一步：传入超过官方 6 位小数限制的起点坐标。
        with self.assertRaises(AmapException) as context:
            await tool.plan_route(
                "walking",
                "116.3971234,39.908",
                "116.407,39.918",
            )

        # 第二步：确认输入在发起网络请求前被拦截为工具参数错误。
        self.assertEqual(context.exception.code, "AMAP_PARAMETER_INVALID")
        self.assertEqual(context.exception.details, {"parameter": "origin"})

    async def test_business_error_is_mapped_without_exposing_request_key(self) -> None:
        """高德业务失败应映射为统一异常且不在详情中回显 Key。"""

        def handler(request: httpx.Request) -> httpx.Response:
            """模拟高德 v3 业务状态失败。"""

            # 第一步：返回官方 v3 失败状态与错误码。
            return httpx.Response(
                200,
                json={
                    "status": "0",
                    "info": "INVALID_USER_KEY",
                    "infocode": "10001",
                },
            )

        tool = AmapMapRouteTool(
            api_key="secret-map-key",
            base_url="https://restapi.amap.com",
            transport=httpx.MockTransport(handler),
        )

        # 第二步：确认错误详情只保留高德业务摘要，不回显请求中的密钥。
        with self.assertRaises(AmapException) as context:
            await tool.geocode("北京市朝阳区阜通东大街6号")

        self.assertEqual(context.exception.code, "AMAP_BUSINESS_REJECTED")
        self.assertEqual(context.exception.details["amap_infocode"], "10001")
        self.assertNotIn("secret-map-key", str(context.exception.details))


if __name__ == "__main__":
    unittest.main()
