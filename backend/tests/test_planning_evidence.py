import asyncio

from app.agents.planning_evidence import (
    _fallback_to_generic_city_poi_search,
    _select_poi_candidates,
)
from app.core.trip_duration import recommended_poi_limit
from app.schemas import TripDuration, TripRequirements


def test_empty_preference_search_falls_back_to_generic_city_search() -> None:
    async def generic_search() -> dict[str, object]:
        return {"pois": [{"name": "红山公园"}]}

    result = asyncio.run(
        _fallback_to_generic_city_poi_search(
            {"pois": []},
            ["中山纪念堂"],
            "景点",
            generic_search,
        )
    )

    assert result == {"pois": [{"name": "红山公园"}]}


def test_default_keyword_does_not_issue_duplicate_fallback_search() -> None:
    called = False

    async def generic_search() -> dict[str, object]:
        nonlocal called
        called = True
        return {"pois": [{"name": "红山公园"}]}

    result = asyncio.run(
        _fallback_to_generic_city_poi_search(
            {"pois": []},
            ["景点"],
            "景点",
            generic_search,
        )
    )

    assert result == {"pois": []}
    assert called is False


def test_poi_recommendation_limit_scales_with_trip_duration() -> None:
    def requirements(days: int) -> TripRequirements:
        return TripRequirements(
            destination="杭州",
            departure_date="2026-08-19",
            traveler_count=1,
            trip_duration=TripDuration(
                raw_text=f"{days}天",
                amount=days,
                unit="day",
            ),
        )

    assert recommended_poi_limit(requirements(1)) == 2
    assert recommended_poi_limit(requirements(3)) == 6
    assert recommended_poi_limit(requirements(7)) == 12


def test_poi_candidates_transfer_unused_category_quota() -> None:
    attractions = [{"name": f"景点{index}"} for index in range(1, 13)]
    foods = [{"name": "餐厅1"}]

    selected_attractions, selected_foods = _select_poi_candidates(
        attractions,
        foods,
        limit=6,
    )

    assert len(selected_attractions) == 5
    assert len(selected_foods) == 1
    assert len(selected_attractions) + len(selected_foods) == 6
