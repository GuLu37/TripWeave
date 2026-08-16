"""飞机与火车价格估算工具。

该工具只生成本地模拟班次，不访问 12306、携程、去哪儿或其他网页。
所有价格、时间和可售状态均为估算值，不能视为实时票价或余票。
"""

from __future__ import annotations

import hashlib
import random
from datetime import datetime, timezone


ROUTE_DATA: dict[tuple[str, str], dict[str, object]] = {
    ("广州", "北京"): {
        "flight_price": 1150,
        "train_price": 900,
        "flight_duration": "3小时20分",
        "train_duration": "8小时30分",
        "flight_services": ("CZ3101", "CA1352"),
        "train_services": ("G80", "G70"),
    },
    ("广州白云机场", "北京"): {
        "flight_price": 1150,
        "train_price": 900,
        "flight_duration": "3小时20分",
        "train_duration": "8小时30分",
        "flight_services": ("CZ3101", "CA1352"),
        "train_services": ("G80", "G70"),
    },
}


async def search_traffic(
    origin: str,
    destination: str,
    departure_date: str,
    travelers: int = 1,
    preferences: list[str] | None = None,
) -> dict[str, object]:
    """生成飞机和高铁的估算班次与价格。"""

    normalized_origin = origin.strip() if isinstance(origin, str) else ""
    normalized_destination = (
        destination.strip() if isinstance(destination, str) else ""
    )
    if (
        not normalized_origin
        or not normalized_destination
        or not isinstance(departure_date, str)
        or not departure_date.strip()
        or not _valid_positive_int(travelers)
    ):
        return _unavailable("缺少有效出发地、目的地、出发日期或人数，暂时无法生成交通估算。")

    try:
        datetime.fromisoformat(departure_date)
    except ValueError:
        return _unavailable("出发日期格式无效，暂时无法生成交通估算。")

    route = ROUTE_DATA.get(
        (normalized_origin, normalized_destination),
        {
            "flight_price": 1000,
            "train_price": 750,
            "flight_duration": "约3小时",
            "train_duration": "约8小时",
            "flight_services": ("MU0001", "CA0002"),
            "train_services": ("G0001", "G0002"),
        },
    )
    seed_text = "|".join(
        (normalized_origin, normalized_destination, departure_date, str(travelers))
    )
    rng = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16))
    fetched_at = datetime.now(timezone.utc).isoformat()
    offers: list[dict[str, object]] = []

    for index, service_no in enumerate(route["flight_services"]):
        price = round(float(route["flight_price"]) * rng.uniform(0.9, 1.1))
        departure_time = ("08:00", "14:30")[index]
        offers.append(
            _build_offer(
                mode="flight",
                operator="中国南方航空/中国国际航空",
                service_no=service_no,
                origin=normalized_origin,
                destination=normalized_destination,
                departure_date=departure_date,
                departure_time=departure_time,
                arrival_time=("11:20", "17:50")[index],
                duration=route["flight_duration"],
                price=price,
                travelers=travelers,
                fetched_at=fetched_at,
            )
        )

    for index, service_no in enumerate(route["train_services"]):
        price = round(float(route["train_price"]) * rng.uniform(0.9, 1.1))
        departure_time = ("07:00", "10:00")[index]
        offers.append(
            _build_offer(
                mode="high_speed_train",
                operator="中国铁路",
                service_no=service_no,
                origin=normalized_origin.replace("白云机场", ""),
                destination=normalized_destination,
                departure_date=departure_date,
                departure_time=departure_time,
                arrival_time=("15:30", "18:30")[index],
                duration=route["train_duration"],
                price=price,
                travelers=travelers,
                fetched_at=fetched_at,
            )
        )

    return {
        "status": "estimated",
        "is_estimate": True,
        "price_type": "estimated",
        "message": "后端尚未接入飞机和火车实时 API，以下仅提供价格参考，不代表实时票价、余票或已确认班次。",
        "offers": offers,
        "sources": [],
        "fetched_at": fetched_at,
        "preferences": preferences or [],
    }


def _build_offer(
    *,
    mode: str,
    operator: str,
    service_no: str,
    origin: str,
    destination: str,
    departure_date: str,
    departure_time: str,
    arrival_time: str,
    duration: str,
    price: int,
    travelers: int,
    fetched_at: str,
) -> dict[str, object]:
    return {
        "mode": mode,
        "operator": operator,
        "service_no": service_no,
        "origin": origin,
        "destination": destination,
        "departure_time": f"{departure_date} {departure_time}",
        "arrival_time": f"{departure_date} {arrival_time}",
        "duration": duration,
        "price": price,
        "price_per_person": price,
        "total_price": price * travelers,
        "currency": "CNY",
        "availability": "estimated",
        "is_estimate": True,
        "source": "mock_traffic_search_tool",
        "fetched_at": fetched_at,
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
