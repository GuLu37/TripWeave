"""为聊天请求提供面向前端的执行进度事件。"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import RLock
from typing import Iterator, Literal
from uuid import uuid4

ProgressStatus = Literal["running", "completed", "failed", "unavailable", "rejected"]
_PROGRESS_TTL_SECONDS = 10 * 60


@dataclass
class _ProgressEvent:
    """一条不包含推理正文的用户可读执行事件。"""

    id: str
    sequence: int
    agent: str
    action: str
    tool: str | None
    parent_id: str | None
    status: ProgressStatus
    reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "sequence": self.sequence,
            "agent": self.agent,
            "action": self.action,
            "tool": self.tool,
            "parent_id": self.parent_id,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class _ProgressRecord:
    """一条浏览器请求的事件集合。"""

    created_at: float
    is_complete: bool = False
    events: list[_ProgressEvent] = field(default_factory=list)


@dataclass
class ProgressHandle:
    """允许调用方将已降级处理的外部能力标记为不可用。"""

    status: ProgressStatus = "completed"
    reason: str | None = None

    def mark_failed(self, reason: str | None = None) -> None:
        self.status = "failed"
        self.reason = reason

    def mark_unavailable(self, reason: str | None = None) -> None:
        self.status = "unavailable"
        self.reason = reason

    def mark_rejected(self, reason: str | None = None) -> None:
        self.status = "rejected"
        self.reason = reason


_records: dict[str, _ProgressRecord] = {}
_lock = RLock()
_request_id_context: ContextVar[str | None] = ContextVar(
    "chat_progress_request_id",
    default=None,
)
_parent_event_context: ContextVar[str | None] = ContextVar(
    "chat_progress_parent_event_id",
    default=None,
)


def start_progress(request_id: str) -> None:
    """初始化本轮请求的进度记录，并淘汰过期记录。"""

    with _lock:
        _remove_expired_records_locked()
        _records[request_id] = _ProgressRecord(created_at=time.monotonic())


def finish_progress(request_id: str) -> None:
    """标记请求已结束，并收口未返回完成状态的事件。"""

    with _lock:
        record = _records.get(request_id)
        if record is not None:
            for event in record.events:
                if event.status != "running":
                    continue
                event.status = "failed"
                event.reason = (
                    "请求已结束，但该工具未返回完成结果。"
                    if event.tool is not None
                    else "请求已结束，但该步骤未正常完成。"
                )
            record.is_complete = True


def get_progress(request_id: str) -> dict[str, object]:
    """读取当前请求的安全进度快照。"""

    with _lock:
        _remove_expired_records_locked()
        record = _records.get(request_id)
        if record is None:
            return {
                "found": False,
                "is_complete": False,
                "events": [],
            }
        return {
            "found": True,
            "is_complete": record.is_complete,
            "events": [event.to_dict() for event in record.events],
        }


@contextmanager
def bind_progress(request_id: str) -> Iterator[None]:
    """将当前协程及其子任务关联到指定聊天请求。"""

    token = _request_id_context.set(request_id)
    try:
        yield
    finally:
        _request_id_context.reset(token)


@asynccontextmanager
async def track_progress(
    agent: str,
    action: str,
    *,
    tool: str | None = None,
    parent_agent: str | None = None,
    resume_latest: bool = False,
    resume_action: str | None = None,
):
    """围绕真实节点或工具调用写入开始和结束状态。"""

    request_id = _request_id_context.get()
    parent_id = _parent_event_context.get()
    event_id = _record_event_start(
        request_id,
        agent=agent,
        action=action,
        tool=tool,
        parent_id=parent_id,
        parent_agent=parent_agent,
        resume_latest=resume_latest,
        resume_action=resume_action,
    )
    parent_token = None
    if event_id is not None and tool is None:
        parent_token = _parent_event_context.set(event_id)
    handle = ProgressHandle()
    try:
        yield handle
    except BaseException:
        status = "unavailable" if handle.status == "unavailable" else "failed"
        reason = handle.reason or "执行时发生异常，未能正常完成。"
        _record_event_finish(request_id, event_id, status, reason=reason)
        raise
    else:
        _record_event_finish(
            request_id,
            event_id,
            handle.status,
            reason=handle.reason,
        )
    finally:
        if parent_token is not None:
            _parent_event_context.reset(parent_token)


def _record_event_start(
    request_id: str | None,
    *,
    agent: str,
    action: str,
    tool: str | None,
    parent_id: str | None,
    parent_agent: str | None,
    resume_latest: bool = False,
    resume_action: str | None = None,
) -> str | None:
    """写入运行中事件，或重新激活本轮同一逻辑步骤。"""

    if request_id is None:
        return None
    with _lock:
        record = _records.get(request_id)
        if record is None:
            return None
        if parent_id is None and parent_agent is not None:
            parent_id = next(
                (
                    event.id
                    for event in reversed(record.events)
                    if (
                        event.tool is None
                        and event.parent_id is None
                        and event.agent == parent_agent
                    )
                ),
                None,
            )
        if resume_latest:
            previous_event = next(
                (
                    event
                    for event in reversed(record.events)
                    if event.agent == agent
                    and event.tool == tool
                    and event.parent_id == parent_id
                ),
                None,
            )
            if previous_event is not None:
                previous_event.action = resume_action or action
                previous_event.status = "running"
                previous_event.reason = None
                return previous_event.id
        event_id = uuid4().hex
        record.events.append(
            _ProgressEvent(
                id=event_id,
                sequence=len(record.events) + 1,
                agent=agent,
                action=action,
                tool=tool,
                parent_id=parent_id,
                status="running",
            )
        )
        return event_id


def _record_event_finish(
    request_id: str | None,
    event_id: str | None,
    status: Literal["completed", "failed", "unavailable", "rejected"],
    *,
    reason: str | None = None,
) -> None:
    """将既有事件更新为结束状态。"""

    if request_id is None or event_id is None:
        return
    with _lock:
        record = _records.get(request_id)
        if record is None:
            return
        for event in record.events:
            if event.id == event_id:
                # 子工具未完成时，其直属 Agent 也不能在退出上下文后被覆盖为已完成。
                if event.status in {"failed", "unavailable", "rejected"} and status == "completed":
                    return
                event.status = status
                event.reason = reason
                if event.tool is not None and status == "failed" and event.parent_id:
                    for parent_event in record.events:
                        if parent_event.id == event.parent_id:
                            parent_event.status = "failed"
                            parent_event.reason = reason or "其工具未能正常完成。"
                            break
                return


def _remove_expired_records_locked() -> None:
    """移除已结束且超过保留时间的请求记录。"""

    now = time.monotonic()
    expired_ids = [
        request_id
        for request_id, record in _records.items()
        if record.is_complete and now - record.created_at > _PROGRESS_TTL_SECONDS
    ]
    for request_id in expired_ids:
        del _records[request_id]
