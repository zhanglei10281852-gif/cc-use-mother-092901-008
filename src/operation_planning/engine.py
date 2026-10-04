"""候选排程引擎。

输入资源台账、需求、已冻结占用与封停窗口，输出一个可解释的候选排程：

* 车辆 / 安全员 / 路线区段 / 充电工位统一按半开时间区间占用；
* 电量不足时在行程开始前自动插入最短充电预约；
* 高优先级需求可以挤占低优先级的未落锤占用，被挤占方立即级联重排，
  重排失败则记录受影响方与窗口外替代建议；
* 已确认 / 进行中的占用与封停窗口为硬约束，任何方案都不能违反；
* 无法落位的需求返回原因代码、阻挡方与最早可行的窗口外方案。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .domain import (
    Alternative,
    Assignment,
    BlockedWindow,
    ChargingBay,
    ChargingSession,
    Interval,
    RouteSegment,
    SafetyOfficer,
    TaskDemand,
    UnscheduledOutcome,
    Vehicle,
)

MINUTE = timedelta(minutes=1)
RELAXED_HORIZON_HOURS = 24
EPS = 1e-6


@dataclass
class _Entry:
    interval: Interval
    owner: str | None  # 需求编号；封停为 None
    frozen: bool  # 已确认/进行中/封停，任何方案不可违反
    kind: str  # trip / charge / block
    label: str  # 面向解释的阻挡方标签
    kwh: float = 0.0
    block_resource: str | None = None  # segment / vehicle / officer（仅封停）
    session: ChargingSession | None = None


@dataclass
class Displacement:
    """被挤占方及其去向。"""

    request_id: str
    preempted_by: tuple[str, ...]
    resolved: bool
    new_assignment: Assignment | None = None
    alternative: Alternative | None = None


@dataclass
class PlanResult:
    plan_id: str
    strategy: str
    assignments: tuple[Assignment, ...]
    charging: tuple[ChargingSession, ...]
    unscheduled: tuple[UnscheduledOutcome, ...]
    displaced: tuple[Displacement, ...]
    rationale: dict[str, str]
    score: dict[str, float]
    locked_ids: tuple[str, ...] = ()  # 已确认/进行中，方案中原样保留的需求


@dataclass
class Problem:
    plan_id: str
    vehicles: tuple[Vehicle, ...]
    officers: tuple[SafetyOfficer, ...]
    segments: tuple[RouteSegment, ...]
    bays: tuple[ChargingBay, ...]
    demands: tuple[TaskDemand, ...]
    now: datetime
    fixed_assignments: Iterable[Assignment] = ()
    fixed_charging: Iterable[ChargingSession] = ()
    soft_assignments: Iterable[Assignment] = ()
    soft_charging: Iterable[ChargingSession] = ()
    blocks: Iterable[BlockedWindow] = ()
    strategy: str = "priority"
    schedule_ids: frozenset[str] | None = None  # None 表示调度全部需求
    protected_owners: frozenset[str] = frozenset()  # 修复重排时不可挤占的需求


class _Timetable:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], list[_Entry]] = {}
        self._owners: dict[str, list[_Entry]] = {}

    def add(
        self,
        keys: Iterable[tuple[str, str]],
        interval: Interval,
        owner: str | None,
        frozen: bool,
        kind: str,
        label: str,
        block_resource: str | None = None,
    ) -> list[_Entry]:
        added: list[_Entry] = []
        for key in keys:
            entry = _Entry(interval, owner, frozen, kind, label, 0.0, block_resource)
            self._entries.setdefault(key, []).append(entry)
            added.append(entry)
            if owner is not None:
                self._owners.setdefault(owner, []).append(entry)
        return added

    def entries_on(self, key: tuple[str, str]) -> list[_Entry]:
        return self._entries.get(key, [])

    def overlaps(self, key: tuple[str, str], window: Interval) -> list[_Entry]:
        return [e for e in self.entries_on(key) if e.interval.overlaps(window)]

    def intervals(self, key: tuple[str, str], frozen_only: bool = False) -> list[Interval]:
        return [e.interval for e in self.entries_on(key) if not frozen_only or e.frozen]

    def remove_owner(self, owner: str) -> None:
        entries = self._owners.pop(owner, [])
        for entry in entries:
            for key_entries in self._entries.values():
                if entry in key_entries:
                    key_entries.remove(entry)

    def charge_at(
        self, vehicle_id: str, at: datetime, initial_kwh: float,
        ignore_owners: frozenset[str] = frozenset(),
    ) -> float:
        """按事件时刻回放电量：行程在开始时扣减，充电在结束时到账。"""
        changes: list[tuple[datetime, float]] = []
        for e in self.entries_on(("v", vehicle_id)):
            if e.owner in ignore_owners:
                continue
            if e.kind == "trip" and e.interval.start <= at:
                changes.append((e.interval.start, -e.kwh))
            elif e.kind == "charge" and e.interval.end <= at:
                changes.append((e.interval.end, e.kwh))
        charge = initial_kwh
        for _, delta in sorted(changes, key=lambda c: c[0]):
            charge += delta
        return max(charge, 0.0)


def _merge(intervals: list[Interval]) -> list[Interval]:
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda i: i.start)
    merged = [ordered[0]]
    for nxt in ordered[1:]:
        last = merged[-1]
        if nxt.start <= last.end:
            merged[-1] = Interval(min(last.start, nxt.start), max(last.end, nxt.end))
        else:
            merged.append(nxt)
    return merged


def _latest_free_gap(bounds: Interval, busy: list[Interval], minutes_needed: int) -> Interval | None:
    """在 bounds 内寻找能容纳 minutes_needed 的空闲片段，返回最晚开始的窗口（紧贴占用前）。"""
    cursor = bounds.start
    gap: Interval | None = None
    for iv in _merge(busy):
        if iv.start > cursor and (iv.start - cursor).total_seconds() / 60 + EPS >= minutes_needed:
            gap = Interval(iv.start - minutes_needed * MINUTE, iv.start)
        cursor = max(cursor, iv.end)
        if cursor >= bounds.end:
            break
    if cursor < bounds.end and (bounds.end - cursor).total_seconds() / 60 + EPS >= minutes_needed:
        gap = Interval(bounds.end - minutes_needed * MINUTE, bounds.end)
    return gap


class Scheduler:
    def __init__(self, problem: Problem) -> None:
        self.p = problem
        self.tt = _Timetable()
        self.priority_by_owner: dict[str, int] = {
            d.request_id: d.priority for d in problem.demands
        }
        self.assignments: dict[str, Assignment] = {}
        self.committed_charging: list[ChargingSession] = []
        self.displaced: dict[str, Displacement] = {}
        self.handled: set[str] = set()
        self.unscheduled: list[UnscheduledOutcome] = []
        self.locked_assignments: dict[str, Assignment] = {}
        self.locked_charging: list[ChargingSession] = []
        self._rationales: dict[str, str] = {}
        self.schedule_ids = problem.schedule_ids
        self.protected_owners = problem.protected_owners
        self._load_base()

    # ------------------------------------------------------------------ 装载
    def _load_base(self) -> None:
        for block in self.p.blocks:
            if block.resource_kind == "segment":
                keys = [("s", block.resource_id)]
            elif block.resource_kind == "vehicle":
                keys = [("v", block.resource_id)]
            else:
                keys = [("o", block.resource_id)]
            self.tt.add(
                keys, block.window, None, True, "block",
                f"封停:{block.resource_id}" + (f"（{block.reason}）" if block.reason else ""),
                block.resource_kind,
            )
        for a in self.p.fixed_assignments:
            self._add_trip(a, frozen=True)
            self.locked_assignments[a.request_id] = a
        for c in self.p.fixed_charging:
            self._add_charge(c, frozen=True)
            self.locked_charging.append(c)
        for a in self.p.soft_assignments:
            self._add_trip(a, frozen=False)
        for c in self.p.soft_charging:
            self._add_charge(c, frozen=False)

    def _add_trip(self, a: Assignment, frozen: bool) -> None:
        demand = self._demand(a.request_id)
        vehicle = self._vehicle(a.vehicle_id)
        kwh = demand.trip_kwh(vehicle) if demand else 0.0
        keys = [("v", a.vehicle_id), ("o", a.officer_id)] + [("s", s) for s in a.segment_ids]
        for e in self.tt.add(keys, a.window, a.request_id, frozen, "trip", f"需求 {a.request_id}"):
            e.kwh = kwh

    def _add_charge(self, c: ChargingSession, frozen: bool) -> None:
        entries = self.tt.add(
            [("v", c.vehicle_id), ("b", c.bay_id)], c.window, c.reason_request_id,
            frozen, "charge", f"充电（{c.bay_id}）",
        )
        for e in entries:
            e.kwh = c.kwh
            e.session = c
        if not frozen:
            self.committed_charging = [
                s for s in self.committed_charging
                if not (s.reason_request_id == c.reason_request_id and s.vehicle_id == c.vehicle_id)
            ]
            self.committed_charging.append(c)

    def _vehicle(self, vehicle_id: str) -> Vehicle:
        return next(v for v in self.p.vehicles if v.vehicle_id == vehicle_id)

    def _demand(self, request_id: str) -> TaskDemand | None:
        return next((d for d in self.p.demands if d.request_id == request_id), None)

    # ------------------------------------------------------------------ 排序
    def _ordered_demands(self) -> list[TaskDemand]:
        demands = list(self.p.demands)
        if self.p.strategy == "early":
            return sorted(demands, key=lambda d: (d.window.start, -d.priority, d.request_id))
        return sorted(demands, key=lambda d: (-d.priority, d.window.start, d.request_id))

    def _ordered_vehicles(self) -> list[Vehicle]:
        vehicles = list(self.p.vehicles)
        if self.p.strategy == "balanced":
            def load(v: Vehicle) -> int:
                return len([e for e in self.tt.entries_on(("v", v.vehicle_id)) if e.kind == "trip"])
            return sorted(vehicles, key=lambda v: (load(v), v.vehicle_id))
        return sorted(vehicles, key=lambda v: v.vehicle_id)

    # ------------------------------------------------------------------ 冲突
    def _is_protected_owner(self, owner: str) -> bool:
        """局部重排时，受影响集合之外的需求一律视为不可挤占。"""
        if self.schedule_ids is None:
            return False
        return owner not in self.schedule_ids

    def _blocking_intervals(self, keys: list[tuple[str, str]], priority: int,
                            self_owner: str) -> list[Interval]:
        """不可穿越的占用区间：封停/已冻结，以及同级或更高优先级的方案占用。"""
        out: list[Interval] = []
        for key in keys:
            for e in self.tt.entries_on(key):
                if e.owner == self_owner:
                    continue
                if e.frozen:
                    out.append(e.interval)
                elif e.owner is not None:
                    if self._is_protected_owner(e.owner):
                        out.append(e.interval)
                        continue
                    owner_priority = self.priority_by_owner.get(e.owner)
                    if owner_priority is None or owner_priority >= priority:
                        out.append(e.interval)
        return out

    def _evictable_owners(self, keys: list[tuple[str, str]], window: Interval,
                          priority: int, self_owner: str) -> list[str]:
        out: list[str] = []
        for key in keys:
            for e in self.tt.overlaps(key, window):
                if e.frozen or e.owner is None or e.owner == self_owner:
                    continue
                if self._is_protected_owner(e.owner):
                    continue
                owner_priority = self.priority_by_owner.get(e.owner)
                if owner_priority is not None and owner_priority < priority and e.owner not in out:
                    out.append(e.owner)
        return out

    def _plan_charging(
        self, demand: TaskDemand, vehicle: Vehicle, trip_start: datetime,
        need_kwh: float, extra_evicts: list[str],
    ) -> tuple[ChargingSession | None, str | None]:
        """在行程开始前找一段最短补能；与低优先级占用重叠时将其一并挤占。"""
        ignore = frozenset(extra_evicts + [demand.request_id])
        charge_now = self.tt.charge_at(
            vehicle.vehicle_id, trip_start, vehicle.initial_charge_kwh, ignore
        )
        deficit = need_kwh - charge_now
        if deficit <= EPS:
            return None, None
        floor = max(self.p.now, trip_start - timedelta(hours=RELAXED_HORIZON_HOURS))
        bounds = Interval(floor, trip_start)
        keys_base = [("v", vehicle.vehicle_id)]
        for bay in sorted(self.p.bays, key=lambda b: b.bay_id):
            # kWh ÷ kW 得到小时数，换算成分钟并向上取整。
            minutes_needed = math.ceil(deficit / bay.power_kw * 60 - EPS)
            busy = _merge(self._blocking_intervals(keys_base + [("b", bay.bay_id)],
                                                   demand.priority, demand.request_id))
            window = _latest_free_gap(bounds, busy, minutes_needed)
            if window is None:
                continue
            charge_evicts = self._evictable_owners(
                keys_base + [("b", bay.bay_id)], window, demand.priority, demand.request_id
            )
            ignore2 = frozenset(set(charge_evicts) | set(extra_evicts) | {demand.request_id})
            charge_before = self.tt.charge_at(
                vehicle.vehicle_id, window.start, vehicle.initial_charge_kwh, ignore2
            )
            headroom = vehicle.capacity_kwh - charge_before
            delivered = bay.power_kw * minutes_needed / 60
            added = min(delivered, headroom)
            if added + EPS < deficit or added <= 0:
                continue
            session = ChargingSession(
                self.p.plan_id, vehicle.vehicle_id, bay.bay_id, window, added,
                demand.request_id,
            )
            for owner in charge_evicts:
                if owner not in extra_evicts:
                    extra_evicts.append(owner)
            return session, None
        return None, "energy"

    def _try_place(
        self, demand: TaskDemand, relaxed: bool = False
    ) -> tuple[Assignment | None, ChargingSession | None, list[str]]:
        """搜索单个需求的最早落位；relaxed 时允许窗口外方案（替代建议）。

        可行起点只可能出现在窗口起点或某段不可穿越占用结束之后，
        因此无需逐分钟扫描，只需检查占用区间边界时刻。
        """
        start_min = max(demand.window.start, self.p.now)
        end_max = demand.window.end
        if relaxed:
            end_max = demand.window.end + timedelta(hours=RELAXED_HORIZON_HOURS)
        latest_start = end_max - demand.duration_minutes * MINUTE
        if latest_start < start_min:
            return None, None, []
        officers = sorted(
            (o for o in self.p.officers if o.can_guard(demand.operation_kind)),
            key=lambda o: o.officer_id,
        )
        for vehicle in self._ordered_vehicles():
            if not vehicle.can_serve(demand.operation_kind):
                continue
            for officer in officers:
                keys = [("v", vehicle.vehicle_id), ("o", officer.officer_id)] + \
                       [("s", s) for s in demand.segment_ids]
                blocking = self._blocking_intervals(
                    keys, demand.priority, demand.request_id
                )
                candidates = {start_min}
                for iv in blocking:
                    if start_min <= iv.end <= latest_start:
                        candidates.add(iv.end)
                for t in sorted(candidates):
                    window = Interval(t, t + demand.duration_minutes * MINUTE)
                    if window.end > end_max:
                        continue
                    if any(iv.overlaps(window) for iv in _merge(blocking)):
                        continue
                    evicts = self._evictable_owners(
                        keys, window, demand.priority, demand.request_id
                    )
                    charge, energy_fail = self._plan_charging(
                        demand, vehicle, t, demand.trip_kwh(vehicle), evicts
                    )
                    if energy_fail:
                        continue
                    assignment = Assignment(
                        self.p.plan_id, demand.request_id, vehicle.vehicle_id,
                        officer.officer_id, window, tuple(demand.segment_ids),
                    )
                    return assignment, charge, evicts
        return None, None, []

    # ------------------------------------------------------------------ 解释
    def _explain_failure(self, demand: TaskDemand) -> UnscheduledOutcome:
        capable = [v for v in self.p.vehicles if v.can_serve(demand.operation_kind)]
        qualified = [o for o in self.p.officers if o.can_guard(demand.operation_kind)]
        if not capable or not qualified:
            return UnscheduledOutcome(
                demand.request_id, "no_resource",
                "没有同时具备该作业能力的车辆与具备资质的安全员",
            )
        blockers: list[str] = []
        road_blocks: list[str] = []
        down_blocks: list[str] = []
        keys = [("v", v.vehicle_id) for v in capable] + \
               [("o", o.officer_id) for o in qualified] + \
               [("s", s) for s in demand.segment_ids]
        for key in keys:
            for e in self.tt.overlaps(key, demand.window):
                if e.kind == "block":
                    if e.block_resource == "segment":
                        road_blocks.append(e.label)
                    else:
                        down_blocks.append(e.label)
                elif e.label not in blockers:
                    blockers.append(e.label)
        alt_assignment, alt_charge, _ = self._try_place(demand, relaxed=True)
        alternative: Alternative | None = None
        if alt_assignment is not None:
            note = "申请窗口之外的最早可行落位"
            if alt_charge is not None:
                note += f"；需先在工位 {alt_charge.bay_id} 补能 {alt_charge.kwh:.1f}kWh"
            alternative = Alternative(
                alt_assignment.vehicle_id, alt_assignment.officer_id,
                alt_assignment.window, False, note,
            )
        preempted = tuple(self.displaced[demand.request_id].preempted_by) \
            if demand.request_id in self.displaced else ()
        if road_blocks:
            code = "closed"
            reason = "作业窗口内路线区段封闭：" + "、".join(sorted(set(road_blocks)))
        elif down_blocks:
            code = "resource_down"
            reason = "作业窗口内车辆或安全员不可用：" + "、".join(sorted(set(down_blocks)))
        elif blockers:
            code = "conflict"
            reason = "窗口内车辆、安全员或区段均被占用，阻挡方：" + "、".join(blockers[:6])
        else:
            code = "energy"
            reason = "电量与可用充电工位无法在窗口内支撑该行程"
        alts = (alternative,) if alternative else ()
        return UnscheduledOutcome(
            demand.request_id, code, reason, tuple(blockers), preempted, alts
        )

    # ------------------------------------------------------------------ 主流程
    def _release(self, owner: str) -> None:
        self.tt.remove_owner(owner)
        self.committed_charging = [
            c for c in self.committed_charging if c.reason_request_id != owner
        ]
        self.assignments.pop(owner, None)

    def _place(self, demand: TaskDemand, preempted_by: tuple[str, ...]) -> UnscheduledOutcome | None:
        """落位一个需求；若挤占了更低优先级需求，随后级联重排。"""
        prior = tuple(self.displaced.get(demand.request_id).preempted_by) \
            if demand.request_id in self.displaced else ()
        all_preempted_by = tuple(dict.fromkeys(prior + preempted_by))
        self._release(demand.request_id)
        assignment, charge, evicts = self._try_place(demand)
        if assignment is None:
            outcome = self._explain_failure(demand)
            self.handled.add(demand.request_id)
            if all_preempted_by:
                self.displaced[demand.request_id] = Displacement(
                    demand.request_id, all_preempted_by, False, None,
                    outcome.alternatives[0] if outcome.alternatives else None,
                )
            self.unscheduled.append(outcome)
            return outcome
        for owner in evicts:
            self._release(owner)
            prior_owner = tuple(self.displaced.get(owner).preempted_by) \
                if owner in self.displaced else ()
            self.displaced[owner] = Displacement(
                owner, tuple(dict.fromkeys(prior_owner + (demand.request_id,))),
                False, None, None,
            )
        self._add_trip(assignment, frozen=False)
        if charge is not None:
            self._add_charge(charge, frozen=False)
        self.assignments[demand.request_id] = assignment
        self.handled.add(demand.request_id)
        self._rationales[demand.request_id] = self._rationale(
            demand, assignment, charge, evicts, all_preempted_by
        )
        # 级联重排被挤占方（优先级更低，无法再挤占当前需求）。
        for owner in evicts:
            victim = self._demand(owner)
            if victim is None:
                continue
            self._place(victim, (demand.request_id,))
            entry = self.displaced[owner]
            if owner in self.assignments:
                self.displaced[owner] = Displacement(
                    owner, entry.preempted_by, True, self.assignments[owner], None,
                )
        return None

    def solve(self) -> PlanResult:
        locked_ids = set(self.locked_assignments)
        for demand in self._ordered_demands():
            if demand.request_id in locked_ids:
                # 已确认 / 进行中的行程不可移动，原样保留。
                self.handled.add(demand.request_id)
                continue
            if self.schedule_ids is not None and demand.request_id not in self.schedule_ids:
                continue
            if demand.request_id in self.handled:
                continue
            self._place(demand, ())
        assignments = tuple(sorted(
            list(self.locked_assignments.values()) + list(self.assignments.values()),
            key=lambda x: (x.window.start, x.request_id),
        ))
        charging = tuple(sorted(
            self.locked_charging + self.committed_charging,
            key=lambda c: (c.window.start, c.vehicle_id, c.bay_id),
        ))
        displaced = tuple(self.displaced[k] for k in sorted(self.displaced))
        score = self._score(assignments, charging)
        return PlanResult(
            self.p.plan_id, self.p.strategy, assignments, charging,
            tuple(self.unscheduled), displaced, dict(self._rationales), score,
            locked_ids=tuple(sorted(locked_ids)),
        )

    def _rationale(
        self, demand: TaskDemand, a: Assignment, charge: ChargingSession | None,
        evicts: list[str], preempted_by: tuple[str, ...],
    ) -> str:
        parts = [
            f"车辆 {a.vehicle_id} 具备 {demand.operation_kind} 作业能力",
            f"安全员 {a.officer_id} 具备对应监护资质",
            f"区段 {'/'.join(a.segment_ids) or '—'} 在 {a.window.start:%m-%d %H:%M}–{a.window.end:%H:%M} 空闲",
        ]
        need = demand.trip_kwh(self._vehicle(a.vehicle_id))
        if charge is None:
            parts.append(f"出发电量可覆盖行程耗电 {need:.1f}kWh，无需补能")
        else:
            parts.append(
                f"先在工位 {charge.bay_id} 于 {charge.window.start:%m-%d %H:%M}–{charge.window.end:%H:%M} "
                f"补能 {charge.kwh:.1f}kWh，随后执行行程（耗电 {need:.1f}kWh）"
            )
        text = "；".join(parts)
        if evicts:
            text = f"依高优先级挤占未确认方案中的 {', '.join(sorted(set(evicts)))}；" + text
        if preempted_by:
            text = f"被 {', '.join(preempted_by)} 挤占，已改排至此；" + text
        return text

    def _score(
        self, assignments: tuple[Assignment, ...], charging: tuple[ChargingSession, ...]
    ) -> dict[str, float]:
        demands_by_id = {d.request_id: d for d in self.p.demands}
        slack = 0.0
        priority_cover = 0.0
        for a in assignments:
            d = demands_by_id.get(a.request_id)
            if d:
                slack += (a.window.start - d.window.start).total_seconds() / 60
                priority_cover += d.priority
        return {
            "assigned": float(len(assignments)),
            "unscheduled": float(len(self.unscheduled)),
            "charging_kwh": round(sum(c.kwh for c in charging), 3),
            "total_slack_minutes": slack,
            "priority_coverage": priority_cover,
        }


def solve(problem: Problem) -> PlanResult:
    return Scheduler(problem).solve()
