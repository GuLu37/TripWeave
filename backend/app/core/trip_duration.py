"""旅行时长的通用日期换算。"""

from math import ceil

from app.schemas import TripDuration


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
