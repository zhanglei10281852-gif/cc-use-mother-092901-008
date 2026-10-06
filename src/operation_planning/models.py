"""示范运营的资源与任务模型。

四类可竞争资源：路线区段、自动驾驶车辆、充电工位、安全人员。
任务需求 :class:`TaskDemand` 以航段（leg）粒度占用路线区段，
车辆占用按整体行程窗口计算，从而支持跨午夜任务与按航段的部分取消。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .contracts import PlanState


def require_aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 必须携带时区信息")
    return value


@dataclass(frozen=True)
class Leg:
    """车辆在某一路线区段内的占用窗口。"""

    segment_id: str
    enters_at: datetime
    exits_at: datetime

    def __post_init__(self) -> None:
        require_aware(self.enters_at, "enters_at")
        require_aware(self.exits_at, "exits_at")
        if self.exits_at <= self.enters_at:
            raise ValueError("航段离开时间必须晚于进入时间")


@dataclass(frozen=True)
class TaskDemand:
    """运营任务需求：接驳、公开体验或企业测试等。"""

    request_id: str
    kind: str
    priority: int
    start: datetime
    end: datetime
    legs: tuple[Leg, ...]
    energy_required_kwh: float = 0.0

    def __post_init__(self) -> None:
        require_aware(self.start, "start")
        require_aware(self.end, "end")
        if self.end <= self.start:
            raise ValueError("任务结束时间必须晚于开始时间（跨午夜请使用次日时间）")
        if not 0 <= self.priority <= 100:
            raise ValueError("优先级必须位于零到一百之间")
        if not self.legs:
            raise ValueError("任务至少需要一个路线航段")
        for leg in self.legs:
            if not (self.start <= leg.enters_at < leg.exits_at <= self.end):
                raise ValueError(f"航段 {leg.segment_id} 的窗口必须落在任务窗口内")
        if self.energy_required_kwh < 0:
            raise ValueError("任务能耗不能为负")

    @property
    def crosses_midnight(self) -> bool:
        return self.start.date() != self.end.date()


@dataclass(frozen=True)
class RouteSegment:
    segment_id: str
    name: str


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    name: str
    capabilities: frozenset[str]
    soc_kwh: float
    capacity_kwh: float

    def __post_init__(self) -> None:
        if self.soc_kwh < 0 or self.capacity_kwh <= 0:
            raise ValueError("电量参数非法")
        if self.soc_kwh > self.capacity_kwh:
            raise ValueError("当前电量不能超过电池容量")


@dataclass(frozen=True)
class ChargingBay:
    bay_id: str
    name: str
    power_kw: float

    def __post_init__(self) -> None:
        if self.power_kw <= 0:
            raise ValueError("充电功率必须为正")


@dataclass(frozen=True)
class SafetyOfficer:
    officer_id: str
    name: str
    qualifications: frozenset[str]


@dataclass(frozen=True)
class UnavailableWindow:
    """资源不可用窗口：封路、车辆故障、安全员缺席等。"""

    starts_at: datetime
    ends_at: datetime
    reason: str


@dataclass(frozen=True)
class ChargingBooking:
    bay_id: str
    starts_at: datetime
    ends_at: datetime
    power_kw: float
    energy_kwh: float


@dataclass
class Lease:
    """确认后冻结的资源租约。"""

    lease_id: str
    plan_id: str
    resource_type: str  # vehicle / officer / segment / bay
    resource_id: str
    starts_at: datetime
    ends_at: datetime
    purpose: str  # trip / leg / charge
    energy_kwh: Optional[float] = None


@dataclass
class Plan:
    """可解释的候选排程，确认后冻结为资源租约。"""

    plan_id: str
    request_id: str
    vehicle_id: str
    officer_id: str
    charging: Optional[ChargingBooking]
    score: int
    rationale: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    state: PlanState = PlanState.CANDIDATE
    preemptive: bool = False
    displaced_requests: list[str] = field(default_factory=list)
    replaces_plan_id: Optional[str] = None
    cancellation_reason: Optional[str] = None
    amendments: list[str] = field(default_factory=list)
    created_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None

    def resource_signature(self) -> tuple:
        return (
            self.vehicle_id,
            self.officer_id,
            self.charging.bay_id if self.charging else None,
        )
