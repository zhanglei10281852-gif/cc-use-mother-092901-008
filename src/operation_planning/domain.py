"""资源、需求与排程结果的领域对象。

时间一律使用带时区的 ``datetime``，区间为半开区间 ``[start, end)``，
因此跨午夜的长任务与普通任务使用完全相同的重叠判定规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def require_aware(dt: datetime, name: str = "时间") -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name}必须携带时区信息")
    return dt


def to_iso(dt: datetime) -> str:
    """统一转成 UTC ISO 文本，便于落库后按字典序比较。"""
    return require_aware(dt).astimezone(timezone.utc).isoformat()


def from_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return require_aware(dt)


@dataclass(frozen=True)
class Interval:
    """半开时间区间，允许跨越午夜。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        require_aware(self.start, "区间开始")
        require_aware(self.end, "区间结束")
        if self.end <= self.start:
            raise ValueError("区间结束时间必须晚于开始时间")

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def contains_instant(self, instant: datetime) -> bool:
        return self.start <= instant < self.end

    def to_dict(self) -> dict[str, Any]:
        return {"start": to_iso(self.start), "end": to_iso(self.end)}


@dataclass(frozen=True)
class Vehicle:
    """自动驾驶车辆。

    capabilities 为可承担的作业类型（如 shuttle / public-demo / enterprise-test）；
    电量按 ``initial_charge_kwh`` 起步，行程按每分钟耗电消耗，可在充电工位补能。
    """

    vehicle_id: str
    capabilities: frozenset[str]
    capacity_kwh: float
    initial_charge_kwh: float
    consumption_kwh_per_minute: float

    def __post_init__(self) -> None:
        if not self.capabilities:
            raise ValueError("车辆至少需要一种作业能力")
        if self.capacity_kwh <= 0:
            raise ValueError("电池容量必须为正")
        if not 0 <= self.initial_charge_kwh <= self.capacity_kwh:
            raise ValueError("初始电量必须位于零到电池容量之间")
        if self.consumption_kwh_per_minute < 0:
            raise ValueError("单位里程耗电不能为负")

    def can_serve(self, operation_kind: str) -> bool:
        return operation_kind in self.capabilities


@dataclass(frozen=True)
class ChargingBay:
    """充电工位，同一时刻只能服务一辆车。"""

    bay_id: str
    power_kw: float

    def __post_init__(self) -> None:
        if self.power_kw <= 0:
            raise ValueError("充电功率必须为正")


@dataclass(frozen=True)
class SafetyOfficer:
    """安全员，qualifications 为可监护的作业类型。"""

    officer_id: str
    qualifications: frozenset[str]

    def __post_init__(self) -> None:
        if not self.qualifications:
            raise ValueError("安全员至少需要一种资质")

    def can_guard(self, operation_kind: str) -> bool:
        return operation_kind in self.qualifications


@dataclass(frozen=True)
class RouteSegment:
    """路线区段，排程时按区段占用，便于表达局部封路。"""

    segment_id: str


@dataclass(frozen=True)
class BlockedWindow:
    """资源不可用窗口：封路（segment）、车辆失效（vehicle）、安全员缺席（officer）。"""

    resource_kind: str  # segment / vehicle / officer
    resource_id: str
    window: Interval
    reason: str = ""

    def __post_init__(self) -> None:
        if self.resource_kind not in {"segment", "vehicle", "officer"}:
            raise ValueError("不支持的资源封停类型")

    @property
    def key(self) -> tuple[str, str]:
        return (self.resource_kind, self.resource_id)


@dataclass(frozen=True)
class TaskDemand:
    """一条运营需求。

    作业可在 ``window`` 内灵活落位，实际占用时长为 ``duration_minutes``；
    ``segment_ids`` 给出该行程必经的路线区段。
    """

    request_id: str
    operation_kind: str
    window: Interval
    duration_minutes: int
    priority: int
    segment_ids: tuple[str, ...] = ()
    energy_kwh: float | None = None  # 缺省时按车辆单位耗电推算

    def __post_init__(self) -> None:
        if not self.request_id:
            raise ValueError("需求编号不能为空")
        if not self.operation_kind:
            raise ValueError("作业类型不能为空")
        if self.duration_minutes <= 0:
            raise ValueError("作业时长必须为正数")
        if self.window.duration_minutes < self.duration_minutes:
            raise ValueError("申请窗口无法容纳作业时长")
        if not 0 <= self.priority <= 100:
            raise ValueError("优先级必须位于零到一百之间")

    def trip_kwh(self, vehicle: Vehicle) -> float:
        if self.energy_kwh is not None:
            return self.energy_kwh
        return vehicle.consumption_kwh_per_minute * self.duration_minutes


@dataclass(frozen=True)
class Assignment:
    """一个任务在某方案中的落位：车辆 + 安全员 + 起止时刻 + 区段。"""

    plan_id: str
    request_id: str
    vehicle_id: str
    officer_id: str
    window: Interval
    segment_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "vehicle_id": self.vehicle_id,
            "officer_id": self.officer_id,
            "segment_ids": list(self.segment_ids),
            **self.window.to_dict(),
        }


@dataclass(frozen=True)
class ChargingSession:
    """方案中的充电预约，占用车辆与充电工位。"""

    plan_id: str
    vehicle_id: str
    bay_id: str
    window: Interval
    kwh: float
    reason_request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "bay_id": self.bay_id,
            "kwh": round(self.kwh, 3),
            "reason_request_id": self.reason_request_id,
            **self.window.to_dict(),
        }


@dataclass(frozen=True)
class Alternative:
    """无法排入时给出的替代建议。"""

    vehicle_id: str
    officer_id: str
    window: Interval
    within_window: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "vehicle_id": self.vehicle_id,
            "officer_id": self.officer_id,
            "within_window": self.within_window,
            "note": self.note,
            **self.window.to_dict(),
        }


@dataclass(frozen=True)
class UnscheduledOutcome:
    """未排入任务的可解释结论：原因、阻挡方与替代建议。"""

    request_id: str
    reason_code: str  # no_resource / closed / resource_down / energy / conflict
    reason: str
    blocked_by: tuple[str, ...] = ()
    preempted_by: tuple[str, ...] = ()
    alternatives: tuple[Alternative, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "blocked_by": list(self.blocked_by),
            "preempted_by": list(self.preempted_by),
            "alternatives": [a.to_dict() for a in self.alternatives],
        }


# 排程策略：按优先级先到先占 / 尽量贴近窗口起点 / 均衡使用资源
STRATEGIES = ("priority", "early", "balanced")
