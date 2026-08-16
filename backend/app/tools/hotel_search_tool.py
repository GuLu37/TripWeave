"""酒店价格估算工具。

该工具只生成稳定的本地估算结果，不访问网页或真实酒店 API。
结果用于规划阶段提供价格量级参考，不能视为实时房价、库存或可预订状态。
"""

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timezone
from math import ceil


CITY_HOTEL_DATA: dict[str, list[dict[str, object]]] = {
    "北京": [
        {
            "name": "北京舒适四星酒店",
            "star_rating": 4.0,
            "base_price": 520,
            "amenities": ["WiFi", "早餐", "健身房"],
        },
        {
            "name": "北京中心精品酒店",
            "star_rating": 4.5,
            "base_price": 760,
            "amenities": ["WiFi", "餐厅", "地铁接驳"],
        },
        {
            "name": "北京经济连锁酒店",
            "star_rating": 3.0,
            "base_price": 260,
            "amenities": ["WiFi", "空调"],
        },
        {
            "name": "北京青年旅舍",
            "star_rating": 2.0,
            "base_price": 140,
            "amenities": ["WiFi", "公共厨房", "洗衣房"],
        },
    ],
    "广州": [
        {
            "name": "广州商务四星酒店",
            "star_rating": 4.0,
            "base_price": 460,
            "amenities": ["WiFi", "早餐", "健身房"],
        },
        {
            "name": "广州中心精品酒店",
            "star_rating": 4.5,
            "base_price": 680,
            "amenities": ["WiFi", "餐厅", "地铁接驳"],
        },
        {
            "name": "广州经济连锁酒店",
            "star_rating": 3.0,
            "base_price": 220,
            "amenities": ["WiFi", "空调"],
        },
        {
            "name": "广州青年旅舍",
            "star_rating": 2.0,
            "base_price": 120,
            "amenities": ["WiFi", "公共厨房"],
        },
    ],
    "东京": [
        {
            "name": "东京帝国酒店",
            "star_rating": 5.0,
            "base_price": 1500,
            "amenities": ["WiFi", "温泉", "米其林餐厅", "管家服务"],
        },
        {
            "name": "新宿华盛顿酒店",
            "star_rating": 4.0,
            "base_price": 650,
            "amenities": ["WiFi", "早餐", "健身房"],
        },
        {
            "name": "东京胶囊旅馆",
            "star_rating": 2.0,
            "base_price": 120,
            "amenities": ["WiFi", "公共浴室"],
        },
    ],
    "default": [
        {
            "name": "城市豪华五星酒店",
            "star_rating": 5.0,
            "base_price": 1000,
            "amenities": ["WiFi", "SPA", "泳池", "管家服务"],
        },
        {
            "name": "城市舒适四星酒店",
            "star_rating": 4.0,
            "base_price": 500,
            "amenities": ["WiFi", "早餐", "健身房"],
        },
        {
            "name": "城市经济连锁酒店",
            "star_rating": 3.0,
            "base_price": 200,
            "amenities": ["WiFi", "空调"],
        },
        {
            "name": "城市青年旅舍",
            "star_rating": 2.0,
            "base_price": 80,
            "amenities": ["WiFi", "公共厨房"],
        },
    ],
}

STYLE_MULTIPLIERS = {
    "budget": 0.7,
    "comfort": 1.0,
    "luxury": 1.5,
    "adventure": 0.6,
    "cultural": 0.9,
    "relaxation": 1.2,
}


async def search_hotels(
    city: str,
    check_in: str,
    check_out: str,
    travelers: int = 1,
    style: str = "comfort",
) -> dict[str, object]:
    """按城市、日期和旅行风格生成酒店价格估算。"""

    normalized_city = city.strip() if isinstance(city, str) else ""
    if not normalized_city or not _valid_positive_int(travelers):
        return _unavailable("缺少有效城市或出行人数，暂时无法生成酒店估算。")

    try:
        nights = (date.fromisoformat(check_out) - date.fromisoformat(check_in)).days
    except (TypeError, ValueError):
        return _unavailable("入住或退房日期格式无效，暂时无法生成酒店估算。")
    if nights <= 0:
        return _unavailable("退房日期必须晚于入住日期，暂时无法生成酒店估算。")

    normalized_style = style if style in STYLE_MULTIPLIERS else "comfort"
    seed_text = "|".join(
        (normalized_city, check_in, check_out, str(travelers), normalized_style)
    )
    rng = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16))
    room_count = ceil(travelers / 2)
    offers: list[dict[str, object]] = []

    for template in CITY_HOTEL_DATA.get(normalized_city, CITY_HOTEL_DATA["default"]):
        nightly_price = round(
            float(template["base_price"])
            * STYLE_MULTIPLIERS[normalized_style]
            * rng.uniform(0.85, 1.15)
        )
        offers.append(
            {
                "name": template["name"],
                "city": normalized_city,
                "address": f"{normalized_city}市中心",
                "room_type": "家庭房" if travelers > 2 else "大床房/双床房",
                "star_rating": template["star_rating"],
                "user_rating": round(rng.uniform(7.6, 9.5), 1),
                "price": nightly_price,
                "price_per_night": nightly_price,
                "total_price": nightly_price * nights * room_count,
                "currency": "CNY",
                "check_in": check_in,
                "check_out": check_out,
                "nights": nights,
                "rooms": room_count,
                "travelers": travelers,
                "amenities": template["amenities"],
                "distance_to_center_km": round(rng.uniform(0.3, 5.0), 1),
                "availability": "estimated",
                "is_estimate": True,
                "source": "mock_hotel_search_tool",
            }
        )

    return {
        "status": "estimated",
        "is_estimate": True,
        "price_type": "estimated",
        "message": "后端尚未接入酒店实时 API，以下仅提供价格参考，不代表实时房价、库存或可预订状态。",
        "offers": sorted(offers, key=lambda item: item["price_per_night"]),
        "sources": [],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def _valid_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _unavailable(message: str) -> dict[str, object]:
    return {
        "status": "unavailable",
        "reason": "requirements_incomplete",
        "message": message,
        "offers": [],
        "sources": [],
        "is_estimate": True,
        "price_type": "estimated",
    }
