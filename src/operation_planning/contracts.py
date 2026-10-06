"""运营需求与时间窗口的数据契约。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PlanState(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    RUNNING = "running"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


@dataclass(frozen=True)
class TimeWindow:
    starts_at: datetime
    ends_at: datetime

    def __post_init__(self) -> None:
        if self.ends_at <= self.starts_at:
            raise ValueError("任务结束时间必须晚于开始时间")


@dataclass(frozen=True)
class OperationRequest:
    request_id: str
    operation_kind: str
    window: TimeWindow
    priority: int
    state: PlanState = PlanState.CANDIDATE

    def __post_init__(self) -> None:
        if not 0 <= self.priority <= 100:
            raise ValueError("优先级必须位于零到一百之间")
