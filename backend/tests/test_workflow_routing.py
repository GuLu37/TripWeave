from app.schemas import ConversationAnalysis, TripRequirements
from app.workflows.trip_conversation_graph import _route_after_search_requirement_analysis


def test_completed_direct_queries_share_one_graph_node() -> None:
    for intent in ("accommodation_search", "intercity_transport_search"):
        analysis = ConversationAnalysis(
            intent=intent,
            reply="正在查询。",
            requirements=TripRequirements(destination="杭州"),
            is_complete=True,
        )

        assert _route_after_search_requirement_analysis({"analysis": analysis}) == "direct_search"


def test_incomplete_direct_query_ends_with_follow_up() -> None:
    analysis = ConversationAnalysis(
        intent="accommodation_search",
        reply="请问计划哪天入住？",
        requirements=TripRequirements(destination="杭州"),
        is_complete=False,
    )

    assert _route_after_search_requirement_analysis({"analysis": analysis}) == "end"
