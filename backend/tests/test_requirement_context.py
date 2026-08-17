from app.agents.conversation_entry_agent import _merge_requirements
from app.schemas import TripRequirements


def test_destination_change_drops_old_place_preferences() -> None:
    known = TripRequirements(
        destination="广州",
        accommodation_preferences=["北京路附近酒店"],
        dining_preferences=["粤菜"],
        attraction_preferences=["中山纪念堂"],
        fixed_schedule=["参观中山纪念堂"],
        general_preferences=["节奏轻松"],
    )
    current = TripRequirements(destination="乌鲁木齐")

    merged = _merge_requirements(known, current)

    assert merged.destination == "乌鲁木齐"
    assert merged.accommodation_preferences == []
    assert merged.dining_preferences == []
    assert merged.attraction_preferences == []
    assert merged.fixed_schedule == []
    assert merged.general_preferences == ["节奏轻松"]


def test_destination_change_keeps_new_place_preferences() -> None:
    known = TripRequirements(
        destination="广州",
        dining_preferences=["粤菜"],
        attraction_preferences=["中山纪念堂"],
    )
    current = TripRequirements(
        destination="乌鲁木齐",
        dining_preferences=["新疆菜"],
    )

    merged = _merge_requirements(known, current)

    assert merged.destination == "乌鲁木齐"
    assert merged.dining_preferences == ["新疆菜"]
    assert merged.attraction_preferences == []
