"""旅行时长与地点推荐数量的通用换算。"""

from datetime import date
from math import ceil

from app.schemas import TripDuration, TripRequirements

MIN_RECOMMENDED_POI_COUNT = 2
MAX_RECOMMENDED_POI_COUNT = 12


def duration_to_days(duration: TripDuration | None) -> int | None:
    """将旅行时长向上换算为至少一天的自然日数量。"""

    # 小时、天、周和月统一换算为自然日，避免酒店和天气模块各自维护一份映射。
    if duration is None:
        return None
    unit_to_days = {
        "hour": 1 / 24,
        "day": 1,
        "week": 7,
        "month": 30,
    }
    return max(1, ceil(duration.amount * unit_to_days[duration.unit]))


def requirements_to_days(requirements: TripRequirements) -> int:
    """从已确认时长或往返日期推导旅行天数。"""

    duration_days = duration_to_days(requirements.trip_duration)
    if duration_days is not None:
        return duration_days
    if requirements.departure_date and requirements.return_date:
        try:
            departure = date.fromisoformat(requirements.departure_date)
            return_date = date.fromisoformat(requirements.return_date)
        except ValueError:
            return 1
        return max(1, (return_date - departure).days)
    return 1


def recommended_poi_limit(requirements: TripRequirements) -> int:
    """按旅行天数确定景点与餐饮合计推荐数量。"""

    return min(
        MAX_RECOMMENDED_POI_COUNT,
        max(MIN_RECOMMENDED_POI_COUNT, requirements_to_days(requirements) * 2),
    )
