import asyncio

from app.services.chat_progress import (
    _record_event_start,
    bind_progress,
    finish_progress,
    get_progress,
    start_progress,
    track_progress,
)


def test_completed_progress_event_is_not_left_running() -> None:
    request_id = "completed-progress-event"
    start_progress(request_id)

    async def run() -> None:
        async with track_progress("规划 Agent", "生成行程方案"):
            pass

    with bind_progress(request_id):
        asyncio.run(run())

    event = get_progress(request_id)["events"][0]
    assert event["status"] == "completed"


def test_finish_progress_closes_unfinished_events_with_reason() -> None:
    request_id = "unfinished-progress-event"
    start_progress(request_id)
    _record_event_start(
        request_id,
        agent="执行 Agent",
        action="收集目的地信息",
        tool=None,
        parent_id=None,
        parent_agent=None,
    )

    finish_progress(request_id)

    progress = get_progress(request_id)
    event = progress["events"][0]
    assert progress["is_complete"] is True
    assert event["status"] == "failed"
    assert event["reason"] == "请求已结束，但该步骤未正常完成。"


def test_failed_tool_marks_its_agent_as_unfinished() -> None:
    request_id = "failed-tool-progress-event"
    start_progress(request_id)

    async def run() -> None:
        async with track_progress("城际交通查询 Agent", "收集城际交通信息"):
            async with track_progress(
                "城际交通查询 Agent",
                "缺少必要查询条件",
                tool="交通价格估算",
            ) as progress:
                progress.mark_failed("缺少出发地、目的地或出发时间。")

    with bind_progress(request_id):
        asyncio.run(run())

    events = get_progress(request_id)["events"]
    assert events[0]["status"] == "failed"
    assert events[0]["reason"] == "缺少出发地、目的地或出发时间。"
    assert events[1]["status"] == "failed"
    assert events[1]["reason"] == "缺少出发地、目的地或出发时间。"


def test_resume_reuses_the_existing_agent_event() -> None:
    request_id = "resumed-agent-progress-event"
    start_progress(request_id)

    async def run() -> None:
        async with track_progress("规划 Agent", "拆分旅差信息收集任务"):
            pass
        async with track_progress(
            "规划 Agent",
            "拆分旅差信息收集任务",
            resume_latest=True,
            resume_action="根据审批反馈重新拆分旅差信息收集任务",
        ):
            pass

    with bind_progress(request_id):
        asyncio.run(run())

    events = get_progress(request_id)["events"]
    assert len(events) == 1
    assert events[0]["sequence"] == 1
    assert events[0]["status"] == "completed"
    assert events[0]["action"] == "根据审批反馈重新拆分旅差信息收集任务"


def test_only_parallel_agents_are_nested_under_planning_dispatch() -> None:
    request_id = "top-level-parent-progress-event"
    start_progress(request_id)

    async def run() -> None:
        async with track_progress("规划 Agent", "拆分旅差信息收集任务"):
            pass
        async with track_progress(
            "执行 Agent",
            "收集地点信息",
            parent_agent="规划 Agent",
        ):
            pass
        async with track_progress(
            "规划 Agent",
            "合并子 Agent 结果并生成具体方案",
        ):
            pass

    with bind_progress(request_id):
        asyncio.run(run())

    root, execution_step, merge_step = get_progress(request_id)["events"]
    assert merge_step["parent_id"] is None
    assert execution_step["parent_id"] == root["id"]
