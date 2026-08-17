import asyncio

from app.agents import conversation_entry_agent
from app.schemas import ClientChatMessage


def test_name_chat_uses_llm_reply(monkeypatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_chat_with_llm(
        messages: list[ClientChatMessage],
        **kwargs: object,
    ) -> str:
        calls.append({"messages": messages, **kwargs})
        return (
            '{"intent":"chat","plan_action":null,'
            '"reply":"那我就叫 TripWeave 吧，听起来很适合一起慢慢聊旅行。"}'
        )

    monkeypatch.setattr(
        conversation_entry_agent,
        "chat_with_llm",
        fake_chat_with_llm,
    )

    decision = asyncio.run(
        conversation_entry_agent.analyze_intent(
            [
                ClientChatMessage(role="user", content="那你希望叫我什么呢"),
            ]
        )
    )

    assert decision.intent == "chat"
    assert decision.reply == "那我就叫 TripWeave 吧，听起来很适合一起慢慢聊旅行。"
    assert calls
