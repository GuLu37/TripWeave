from app.services.confirmed_trip_service import (
    _build_map_points,
    _select_evidence_route_targets,
)
from app.schemas import ConfirmedTripDetails, TripRoute


def test_confirmed_route_targets_follow_duration_specific_limit() -> None:
    attraction_candidates = [
        {
            "name": f"景点{index}",
            "location": f"120.{index},30.{index}",
            "address": f"地址{index}",
        }
        for index in range(1, 7)
    ]
    food_candidates = [
        {
            "name": f"餐厅{index}",
            "location": f"121.{index},31.{index}",
            "address": f"地址{index}",
        }
        for index in range(1, 7)
    ]
    proposal = "\n".join(
        [candidate["name"] for candidate in attraction_candidates + food_candidates]
    )

    targets = _select_evidence_route_targets(
        proposal,
        {
            "attraction_candidates": attraction_candidates,
            "food_candidates": food_candidates,
        },
        limit=12,
    )
    points = _build_map_points(targets, limit=12)

    assert len(targets) == 12
    assert len(points) == 12
    assert [point.sequence for point in points] == list(range(1, 13))


def test_confirmed_details_allow_routes_between_twelve_points() -> None:
    details = ConfirmedTripDetails(
        routes=[
            TripRoute(
                category="food",
                origin=f"地点{index}",
                destination=f"地点{index + 1}",
            )
            for index in range(1, 12)
        ]
    )

    assert len(details.routes) == 11
