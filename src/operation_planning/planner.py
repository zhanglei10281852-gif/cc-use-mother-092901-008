"""约束规划器：在同一计划内联合考虑车辆、路线、安全员、电量与充电工位。

输出的每个候选 :class:`PlannedOption` 都携带评分依据（rationale）与风险
（risks），供值班人员比较。规划器自身无状态，所有“已冻结占用”通过
:class="_FrozenContext` 注入，便于只重排受影响任务。
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from .models import (
    ChargingBooking,
    Leg,
    SafetyOfficer,
    ChargingBay,
    TaskDemand,
    UnavailableWindow,
    Vehicle,
)


def overlaps(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """半开区间 [start, end) 重叠判定，端点相接不算冲突（支持背靠背行程）。"""

    return start_a < end_b and start_b < end_a


@dataclass(frozen=True)
class ResourceLock:
    resource_type: str
    resource_id: str
    starts_at: datetime
    ends_at: datetime
    purpose: str
    plan_id: str
    request_id: str
    state: str
    energy_kwh: Optional[float] = None


@dataclass
class PlannedOption:
    plan_id: str
    request_id: str
    vehicle: Vehicle
    officer: SafetyOfficer
    charging: Optional[ChargingBooking]
    score: int
    demand: TaskDemand
    rationale: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)

    def as_lock_tuple(self) -> tuple[ResourceLock, ...]:
        demand = self.demand
        locks = [
            ResourceLock("vehicle", self.vehicle.vehicle_id, demand.start, demand.end,
                         "trip", self.plan_id, self.request_id, "candidate",
                         energy_kwh=demand.energy_required_kwh),
            ResourceLock("officer", self.officer.officer_id, demand.start, demand.end,
                         "trip", self.plan_id, self.request_id, "candidate"),
        ]
        for leg in demand.legs:
            locks.append(
                ResourceLock("segment", leg.segment_id, leg.enters_at, leg.exits_at,
                             "leg", self.plan_id, self.request_id, "candidate")
            )
        if self.charging:
            booking = self.charging
            locks.append(
                ResourceLock("vehicle", self.vehicle.vehicle_id,
                             booking.starts_at, booking.ends_at,
                             "charge", self.plan_id, self.request_id, "candidate",
                             energy_kwh=booking.energy_kwh)
            )
            locks.append(
                ResourceLock("bay", booking.bay_id, booking.starts_at, booking.ends_at,
                             "charge", self.plan_id, self.request_id, "candidate",
                             energy_kwh=booking.energy_kwh)
            )
        return tuple(locks)


@dataclass
class _FrozenContext:
    """已确认/进行中任务形成的资源占用视图。"""

    locks: tuple[ResourceLock, ...] = ()
    unavailable: dict[str, list[UnavailableWindow]] = field(default_factory=dict)

    def hard_blocked(
        self,
        resource_type: str,
        resource_id: str,
        start: datetime,
        end: datetime,
        *,
        ignore_plan: Optional[str] = None,
        include_cancelled: bool = False,
    ) -> Optional[ResourceLock]:
        """返回与给定占用窗口冲突的第一条已冻结租约。"""

        active_states = {"confirmed", "running"}
        for lock in self.locks:
            if lock.resource_type != resource_type or lock.resource_id != resource_id:
                continue
            if lock.state not in active_states and not include_cancelled:
                continue
            if ignore_plan and lock.plan_id == ignore_plan:
                continue
            if overlaps(start, end, lock.starts_at, lock.ends_at):
                return lock
        for window in self.unavailable.get(f"{resource_type}:{resource_id}", ()):
            if overlaps(start, end, window.starts_at, window.ends_at):
                return ResourceLock(
                    resource_type, resource_id, window.starts_at, window.ends_at,
                    "unavailable", "resource-unavailable", "-", "confirmed",
                )
        return None


@dataclass
class PreemptionImpact:
    """高优先级插单对未确认（候选）计划的挤占影响。"""

    displaced_plan_id: str
    displaced_request_id: str
    resource_type: str
    resource_id: str
    starts_at: datetime
    ends_at: datetime
    suggestion: str


@dataclass
class PlanningOutcome:
    options: list[PlannedOption]
    preemptions: list[PreemptionImpact] = field(default_factory=list)
    infeasible_reasons: list[str] = field(default_factory=list)


class Planner:
    """贪心可解释规划器。

    对每辆车 × 每名资质合格的安全员组合做可行性检查：能力、电量、车辆/
    安全员/区段占用、封路与故障；电量不足时尝试在任务开始前插入充电。
    """

    def __init__(
        self,
        vehicles: Iterable[Vehicle],
        officers: Iterable[SafetyOfficer],
        bays: Iterable[ChargingBay],
        segments: Iterable = (),
        *,
        reserve_soc_kwh: float = 0.0,
        charge_buffer: timedelta = timedelta(minutes=10),
        max_chase: timedelta = timedelta(hours=12),
    ) -> None:
        self.vehicles = {v.vehicle_id: v for v in vehicles}
        self.officers = {o.officer_id: o for o in officers}
        self.bays = {b.bay_id: b for b in bays}
        self.segments = {s.segment_id: s for s in segments}
        self.reserve_soc_kwh = reserve_soc_kwh
        self.charge_buffer = charge_buffer
        self.max_chase = max_chase

    # ------------------------------------------------------------------
    # 候选生成
    # ------------------------------------------------------------------
    def generate_options(
        self,
        demand: TaskDemand,
        context: _FrozenContext,
        plan_id_prefix: str,
        *,
        candidate_locks: Iterable[ResourceLock] = (),
        now: Optional[datetime] = None,
    ) -> list[PlannedOption]:
        full_context = self._with_locks(context, candidate_locks)
        options: list[PlannedOption] = []
        serial = 0
        for vehicle in self.vehicles.values():
            if demand.kind not in vehicle.capabilities:
                continue
            hard_stop = self._vehicle_hard_block(vehicle, demand, full_context)
            if hard_stop:
                continue
            for officer in self.officers.values():
                if demand.kind not in officer.qualifications:
                    continue
                officer_lock = full_context.hard_blocked(
                    "officer", officer.officer_id, demand.start, demand.end
                )
                if officer_lock:
                    continue
                segment_lock = self._segment_hard_block(demand, full_context)
                if segment_lock:
                    continue
                serial += 1
                option = self._build_option(
                    f"{plan_id_prefix}-{serial}", demand, vehicle, officer,
                    full_context, now,
                )
                if option is not None:
                    options.append(option)
        options.sort(key=lambda item: (-item.score, item.vehicle.vehicle_id))
        return options

    def _build_option(
        self,
        plan_id: str,
        demand: TaskDemand,
        vehicle: Vehicle,
        officer: SafetyOfficer,
        context: _FrozenContext,
        now: Optional[datetime],
    ) -> Optional[PlannedOption]:
        rationale: list[str] = [
            f"车辆 {vehicle.name} 具备 {demand.kind} 能力",
            f"安全员 {officer.name} 持有 {demand.kind} 资质",
        ]
        risks: list[str] = []
        score = 0

        needed = demand.energy_required_kwh + self.reserve_soc_kwh
        booking: Optional[ChargingBooking] = None
        if vehicle.soc_kwh + 1e-9 >= needed:
            rationale.append(
                f"当前电量 {vehicle.soc_kwh:.1f}kWh 满足任务能耗 "
                f"{demand.energy_required_kwh:.1f}kWh（含余量 {self.reserve_soc_kwh:.1f}）"
            )
            score += 40
        else:
            booking = self._find_charging(vehicle, demand, context, now)
            if booking is None:
                rationale.append(
                    f"电量 {vehicle.soc_kwh:.1f}kWh 不足且任务开始前无可用充电窗口"
                )
                return None
            deficit = needed - vehicle.soc_kwh
            rationale.append(
                f"任务开始前在 {booking.bay_id} 补能 {booking.energy_kwh:.1f}kWh"
            )
            if booking.starts_at > demand.start - timedelta(minutes=30):
                risks.append("充电结束距发车不足 30 分钟，延误风险高")
                score -= 20
            score += 10
            score -= int(max(0.0, deficit) // 5)

        # 电量裕度越大越好（跨午夜长任务的安全感）。
        headroom = vehicle.soc_kwh - needed
        score += int(min(headroom, 30))
        if demand.crosses_midnight and headroom < 10:
            risks.append("跨午夜任务电量裕度偏低")

        # 能力匹配的冗余度：恰好满足 vs 多能力车辆。
        score += len(vehicle.capabilities)

        option = PlannedOption(
            plan_id=plan_id,
            request_id=demand.request_id,
            vehicle=vehicle,
            officer=officer,
            charging=booking,
            score=score,
            demand=demand,
            rationale=rationale,
            risks=risks,
        )
        return option

    def _find_charging(
        self,
        vehicle: Vehicle,
        demand: TaskDemand,
        context: _FrozenContext,
        now: Optional[datetime],
    ) -> Optional[ChargingBooking]:
        needed = demand.energy_required_kwh + self.reserve_soc_kwh
        deficit = max(0.0, needed - vehicle.soc_kwh)
        if deficit <= 0:
            return None
        no_earlier_than = (now or demand.start) - self.max_chase
        if now is not None and no_earlier_than < now:
            no_earlier_than = now
        for bay in self.bays.values():
            charge_minutes = deficit / bay.power_kw * 60
            duration = timedelta(minutes=charge_minutes) + self.charge_buffer
            # 最晚开始：任务前充完；从最晚窗口向前回退寻找空闲。
            latest_start = demand.start - duration
            cursor = latest_start
            while cursor >= max(demand.start - self.max_chase, no_earlier_than):
                end = cursor + duration
                bay_lock = context.hard_blocked("bay", bay.bay_id, cursor, end)
                veh_lock = context.hard_blocked("vehicle", vehicle.vehicle_id, cursor, end)
                if bay_lock is None and veh_lock is None:
                    return ChargingBooking(
                        bay_id=bay.bay_id,
                        starts_at=cursor,
                        ends_at=end,
                        power_kw=bay.power_kw,
                        energy_kwh=deficit,
                    )
                # 落到冲突窗口之前继续尝试。
                cursor = bay_lock.starts_at - duration if bay_lock else veh_lock.starts_at - duration  # type: ignore[union-attr]
        return None

    # ------------------------------------------------------------------
    # 冲突检测
    # ------------------------------------------------------------------
    def _vehicle_hard_block(
        self, vehicle: Vehicle, demand: TaskDemand, context: _FrozenContext
    ) -> Optional[ResourceLock]:
        return context.hard_blocked(
            "vehicle", vehicle.vehicle_id, demand.start, demand.end
        )

    def _segment_hard_block(
        self, demand: TaskDemand, context: _FrozenContext
    ) -> Optional[ResourceLock]:
        for leg in demand.legs:
            lock = context.hard_blocked("segment", leg.segment_id, leg.enters_at, leg.exits_at)
            if lock:
                return lock
        return None

    def conflicts_for_locks(
        self,
        locks: Iterable[ResourceLock],
        context: _FrozenContext,
        *,
        ignore_plan: Optional[str] = None,
    ) -> list[ResourceLock]:
        """返回给定占用与已冻结占用之间的全部冲突。"""

        found: list[ResourceLock] = []
        for lock in locks:
            clash = context.hard_blocked(
                lock.resource_type, lock.resource_id, lock.starts_at, lock.ends_at,
                ignore_plan=ignore_plan,
            )
            if clash:
                found.append(clash)
        return found

    # ------------------------------------------------------------------
    # 高优先级插单：只能挤占候选计划
    # ------------------------------------------------------------------
    def evaluate_preemption(
        self,
        demand: TaskDemand,
        chosen: PlannedOption,
        candidate_locks: list[ResourceLock],
        context: _FrozenContext,
    ) -> list[PreemptionImpact]:
        impacts: list[PreemptionImpact] = []
        new_locks = chosen.as_lock_tuple()
        for new_lock in new_locks:
            for existing in candidate_locks:
                if existing.request_id == demand.request_id:
                    continue
                if existing.resource_type != new_lock.resource_type:
                    continue
                if existing.resource_id != new_lock.resource_id:
                    continue
                if not overlaps(
                    new_lock.starts_at, new_lock.ends_at,
                    existing.starts_at, existing.ends_at,
                ):
                    continue
                impacts.append(
                    PreemptionImpact(
                        displaced_plan_id=existing.plan_id,
                        displaced_request_id=existing.request_id,
                        resource_type=existing.resource_type,
                        resource_id=existing.resource_id,
                        starts_at=existing.starts_at,
                        ends_at=existing.ends_at,
                        suggestion=self._alternative_suggestion(existing, context),
                    )
                )
        # 同一被挤占计划只提示一次（保留首个冲突资源）。
        deduped: dict[str, PreemptionImpact] = {}
        for impact in impacts:
            deduped.setdefault(impact.displaced_plan_id, impact)
        return list(deduped.values())

    def _alternative_suggestion(
        self, lock: ResourceLock, context: _FrozenContext
    ) -> str:
        if lock.resource_type == "vehicle":
            alternatives = [
                v_id for v_id in self.vehicles
                if not context.hard_blocked("vehicle", v_id, lock.starts_at, lock.ends_at)
            ]
            if alternatives:
                return f"可改用车辆 {'、'.join(sorted(alternatives)[:2])}，或平移至相邻时段"
        elif lock.resource_type == "officer":
            alternatives = [
                o_id for o_id in self.officers
                if not context.hard_blocked("officer", o_id, lock.starts_at, lock.ends_at)
            ]
            if alternatives:
                return f"可改派安全员 {'、'.join(sorted(alternatives)[:2])}"
        elif lock.resource_type == "bay":
            alternatives = [
                b_id for b_id in self.bays
                if not context.hard_blocked("bay", b_id, lock.starts_at, lock.ends_at)
            ]
            if alternatives:
                return f"可改约充电工位 {'、'.join(sorted(alternatives)[:2])}"
        elif lock.resource_type == "segment":
            return "该路段封闭/占用，建议改道或调整发车时刻避开封闭窗口"
        return "建议改期或等待值班人员协调替代资源"

    def explain_infeasibility(
        self, demand: TaskDemand, context: _FrozenContext
    ) -> list[str]:
        """无候选时给出可读原因，避免值班人员面对空列表。"""

        reasons: list[str] = []
        capable_vehicles = [v for v in self.vehicles.values()
                            if demand.kind in v.capabilities]
        if not capable_vehicles:
            reasons.append(f"台账中没有具备 {demand.kind} 能力的车辆")
            return reasons
        for vehicle in capable_vehicles:
            lock = context.hard_blocked(
                "vehicle", vehicle.vehicle_id, demand.start, demand.end
            )
            if lock:
                reasons.append(
                    f"车辆 {vehicle.vehicle_id} 在任务窗口被占用或不可用"
                )
                continue
            needed = demand.energy_required_kwh + self.reserve_soc_kwh
            if vehicle.soc_kwh + 1e-9 < needed:
                booking = self._find_charging(vehicle, demand, context, None)
                if booking is None:
                    reasons.append(
                        f"车辆 {vehicle.vehicle_id} 电量不足且任务前无充电窗口"
                    )
        qualified = [o for o in self.officers.values()
                     if demand.kind in o.qualifications]
        if not qualified:
            reasons.append(f"没有持有 {demand.kind} 资质的安全员")
        else:
            free = [
                o.officer_id for o in qualified
                if context.hard_blocked("officer", o.officer_id,
                                        demand.start, demand.end) is None
            ]
            if not free:
                reasons.append("资质合格的安全员在该窗口均已排班或缺席")
        for leg in demand.legs:
            lock = context.hard_blocked(
                "segment", leg.segment_id, leg.enters_at, leg.exits_at
            )
            if lock:
                reasons.append(
                    f"区段 {leg.segment_id} 在 "
                    f"{leg.enters_at:%Y-%m-%d %H:%M}–{leg.exits_at:%H:%M} "
                    f"封闭或被占用"
                )
        return reasons or ["无满足全部约束的车辆与安全员组合"]

    @staticmethod
    def _with_locks(
        context: _FrozenContext, extra: Iterable[ResourceLock]
    ) -> _FrozenContext:
        extra = tuple(extra)
        if not extra:
            return context
        return _FrozenContext(locks=context.locks + extra, unavailable=context.unavailable)
