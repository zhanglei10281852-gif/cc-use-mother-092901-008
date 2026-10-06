"""运营编排服务：候选生成 → 值班确认 → 租约冻结的完整工作流。

线程安全：进程内以 :class:`threading.RLock` 串行化读-检-写，落库时使用
``BEGIN IMMEDIATE`` 争取 SQLite 写锁，保证多线程/多进程下并发确认只有
一方成功（租约不会被重复承诺）。
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Optional

from .contracts import PlanState
from .models import (
    ChargingBay,
    Plan,
    RouteSegment,
    SafetyOfficer,
    TaskDemand,
    UnavailableWindow,
    Vehicle,
)
from .planner import (
    PlannedOption,
    Planner,
    PreemptionImpact,
    ResourceLock,
    _FrozenContext,
    overlaps,
)
from .store import SQLiteStore, iso


class PlanningError(Exception):
    """业务规则错误基类。"""


class NotFoundError(PlanningError):
    pass


class ConflictError(PlanningError):
    """确认时与已冻结租约冲突（资源已被先确认的任务承诺）。"""

    def __init__(self, message: str, conflicts: list[dict[str, Any]]) -> None:
        super().__init__(message)
        self.conflicts = conflicts


class InvalidStateError(PlanningError):
    pass


class PlanningService:
    def __init__(self, store: SQLiteStore, *, reserve_soc_kwh: float = 0.0) -> None:
        self.store = store
        self.reserve_soc_kwh = reserve_soc_kwh
        self._lock = threading.RLock()
        self._reload()

    @classmethod
    def from_path(cls, path: str, *, reserve_soc_kwh: float = 0.0) -> "PlanningService":
        return cls(SQLiteStore(path), reserve_soc_kwh=reserve_soc_kwh)

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------
    # 状态装载（重启即恢复）
    # ------------------------------------------------------------------
    def _reload(self) -> None:
        vehicles, officers, bays, segments = self.store.load_catalog()
        self.vehicles = vehicles
        self.officers = officers
        self.bays = bays
        self.segments = segments
        self.demands = self.store.load_demands()
        self.plans = self.store.load_plans()
        rows = self.store.load_leases()
        self.leases: dict[int, dict[str, Any]] = {int(r["lease_id"]): r for r in rows}
        self.unavailable = self.store.load_unavailable()
        self._rebuild_planner()

    def _rebuild_planner(self) -> None:
        self.planner = Planner(
            self.vehicles.values(), self.officers.values(), self.bays.values(),
            self.segments.values(), reserve_soc_kwh=self.reserve_soc_kwh,
        )

    # ------------------------------------------------------------------
    # 资源台账
    # ------------------------------------------------------------------
    def register_vehicle(self, vehicle: Vehicle) -> None:
        with self._lock:
            self.store.upsert_vehicle(vehicle)
            self.store.commit()
            self.vehicles[vehicle.vehicle_id] = vehicle
            self._rebuild_planner()

    def register_officer(self, officer: SafetyOfficer) -> None:
        with self._lock:
            self.store.upsert_officer(officer)
            self.store.commit()
            self.officers[officer.officer_id] = officer
            self._rebuild_planner()

    def register_bay(self, bay: ChargingBay) -> None:
        with self._lock:
            self.store.upsert_bay(bay)
            self.store.commit()
            self.bays[bay.bay_id] = bay
            self._rebuild_planner()

    def register_segment(self, segment: RouteSegment) -> None:
        with self._lock:
            self.store.upsert_segment(segment)
            self.store.commit()
            self.segments[segment.segment_id] = segment
            self._rebuild_planner()

    # ------------------------------------------------------------------
    # 占用视图
    # ------------------------------------------------------------------
    def _frozen_locks(self, ignore_plans: tuple[str, ...] = ()) -> list[ResourceLock]:
        locks: list[ResourceLock] = []
        for row in self.leases.values():
            if row["state"] not in ("confirmed", "running"):
                continue
            if row["plan_id"] in ignore_plans:
                continue
            locks.append(ResourceLock(
                row["resource_type"], row["resource_id"],
                row["starts_at"], row["ends_at"], row["purpose"],
                row["plan_id"], row["request_id"], row["state"],
                row.get("energy_kwh"),
            ))
        return locks

    def _frozen_context(self, ignore_plans: tuple[str, ...] = ()) -> _FrozenContext:
        return _FrozenContext(
            locks=tuple(self._frozen_locks(ignore_plans)),
            unavailable=dict(self.unavailable),
        )

    def _candidate_locks(self, *, exclude_request: Optional[str] = None) -> list[ResourceLock]:
        """未确认候选的“软占用”：不阻止冻结，但用于插单挤占提示。"""

        locks: list[ResourceLock] = []
        for plan in self.plans.values():
            if plan.state != PlanState.CANDIDATE:
                continue
            demand = self.demands.get(plan.request_id)
            if demand is None or plan.request_id == exclude_request:
                continue
            locks.extend(self._plan_locks(plan, demand, "candidate"))
        return locks

    @staticmethod
    def _plan_locks(plan: Plan, demand: TaskDemand, state: str) -> list[ResourceLock]:
        locks = [
            ResourceLock("vehicle", plan.vehicle_id, demand.start, demand.end,
                         "trip", plan.plan_id, demand.request_id, state,
                         energy_kwh=demand.energy_required_kwh),
            ResourceLock("officer", plan.officer_id, demand.start, demand.end,
                         "trip", plan.plan_id, demand.request_id, state),
        ]
        for leg in demand.legs:
            locks.append(ResourceLock(
                "segment", leg.segment_id, leg.enters_at, leg.exits_at,
                "leg", plan.plan_id, demand.request_id, state,
            ))
        if plan.charging:
            booking = plan.charging
            locks.append(ResourceLock(
                "vehicle", plan.vehicle_id, booking.starts_at, booking.ends_at,
                "charge", plan.plan_id, demand.request_id, state,
                energy_kwh=booking.energy_kwh,
            ))
            locks.append(ResourceLock(
                "bay", booking.bay_id, booking.starts_at, booking.ends_at,
                "charge", plan.plan_id, demand.request_id, state,
                energy_kwh=booking.energy_kwh,
            ))
        return locks

    # ------------------------------------------------------------------
    # 提交需求 → 候选排程
    # ------------------------------------------------------------------
    def submit_demand(self, demand: TaskDemand) -> dict[str, Any]:
        with self._lock:
            now = self._now()
            self.demands[demand.request_id] = demand
            self.store.insert_demand(demand, now)
            # 同一需求重新提交：旧候选作废，已确认计划保留。
            for plan in list(self.plans.values()):
                if (plan.request_id == demand.request_id
                        and plan.state == PlanState.CANDIDATE):
                    self.store.delete_plan(plan.plan_id)
                    self.plans.pop(plan.plan_id, None)

            context = self._frozen_context()
            options = self.planner.generate_options(
                demand, context, f"PL-{demand.request_id}", now=now,
            )
            candidate_locks = self._candidate_locks(exclude_request=demand.request_id)
            stored: list[Plan] = []
            contention: list[dict[str, Any]] = []
            for option in options:
                plan = self._persist_option(option, demand, now)
                stored.append(plan)
            if stored:
                best_locks = self._plan_locks(stored[0], demand, "candidate")
                impacts = self.planner.evaluate_preemption(
                    demand, options[0], candidate_locks, context,
                )
                contention = [self._impact_dict(impact) for impact in impacts]
            self.store.commit()
            return {
                "request_id": demand.request_id,
                "options": [self.plan_dict(plan, detailed=True) for plan in stored],
                "infeasible_reasons": (
                    self.planner.explain_infeasibility(demand, context)
                    if not options else []
                ),
                "contending_candidates": contention,
            }

    def _persist_option(
        self, option: PlannedOption, demand: TaskDemand, now: datetime
    ) -> Plan:
        plan = Plan(
            plan_id=option.plan_id,
            request_id=demand.request_id,
            vehicle_id=option.vehicle.vehicle_id,
            officer_id=option.officer.officer_id,
            charging=option.charging,
            score=option.score,
            rationale=list(option.rationale),
            risks=list(option.risks),
        )
        self.store.insert_plan(plan, now)
        self.plans[plan.plan_id] = plan
        return plan

    # ------------------------------------------------------------------
    # 比较方案
    # ------------------------------------------------------------------
    def compare(self, plan_ids: list[str]) -> dict[str, Any]:
        with self._lock:
            plans = [self._require_plan(pid) for pid in plan_ids]
            return {
                "options": [self.plan_dict(plan, detailed=True) for plan in plans],
                "recommended": max(plans, key=lambda p: p.score).plan_id,
            }

    # ------------------------------------------------------------------
    # 确认并冻结
    # ------------------------------------------------------------------
    def confirm(
        self,
        plan_id: str,
        *,
        preempt: bool = False,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        with self._lock:
            now = now or self._now()
            plan = self._require_plan(plan_id)
            demand = self._require_demand(plan.request_id)
            if plan.state != PlanState.CANDIDATE:
                raise InvalidStateError(
                    f"计划 {plan_id} 当前状态为 {plan.state.value}，只有候选可以确认"
                )

            self.store.conn.execute("BEGIN IMMEDIATE")
            try:
                # 值班人员选中一个方案后，同需求的其他候选自动作废。
                for sibling in list(self.plans.values()):
                    if (sibling.request_id == demand.request_id
                            and sibling.state == PlanState.CANDIDATE
                            and sibling.plan_id != plan_id):
                        sibling.state = PlanState.CANCELLED
                        sibling.cancellation_reason = (
                            f"同一需求已选择方案 {plan_id}，该备选作废"
                        )
                        self.store.update_plan_state(
                            sibling.plan_id, PlanState.CANCELLED,
                            cancellation_reason=sibling.cancellation_reason,
                        )

                ignore = (plan.replaces_plan_id,) if plan.replaces_plan_id else ()
                context = self._frozen_context(ignore_plans=ignore)
                new_locks = self._plan_locks(plan, demand, "confirmed")
                conflicts = self.planner.conflicts_for_locks(
                    new_locks, context, ignore_plan=plan.replaces_plan_id,
                )
                if conflicts:
                    self.store.conn.rollback()
                    raise ConflictError(
                        f"计划 {plan_id} 与已冻结租约冲突，资源已被其他任务承诺",
                        [self._lock_dict(lock) for lock in conflicts],
                    )

                preempted: list[dict[str, Any]] = []
                candidate_locks = self._candidate_locks(
                    exclude_request=demand.request_id
                )
                impacts = self.planner.evaluate_preemption(
                    demand, _OptionShim(plan, demand), candidate_locks, context,
                )
                # 仅当本需求优先级严格高于受影响需求时，才构成“插单挤占”：
                # 需要显式 preempt 并整单作废受影响需求的全部未确认候选。
                # 同优先级的候选重叠走“先确认者得”的常规规则。
                affected = {
                    impact.displaced_request_id: impact for impact in impacts
                }
                higher_priority_requests = {
                    rid for rid in affected
                    if demand.priority > self._require_demand(rid).priority
                }
                higher_priority = bool(higher_priority_requests)
                if impacts and higher_priority and not preempt:
                    self.store.conn.rollback()
                    raise ConflictError(
                        f"计划 {plan_id} 将挤占优先级更低的未确认候选；"
                        f"如以优先级 {demand.priority} 插单，"
                        "请显式 preempt=True",
                        [self._impact_dict(impact) for impact in impacts],
                    )
                if higher_priority:
                    for victim in list(self.plans.values()):
                        if (victim.state != PlanState.CANDIDATE
                                or victim.request_id not in higher_priority_requests):
                            continue
                        impact = affected[victim.request_id]
                        victim.cancellation_reason = (
                            f"被高优先级需求 {demand.request_id}"
                            f"（优先级 {demand.priority}）插单挤占"
                        )
                        victim.amendments.append(
                            f"{victim.cancellation_reason}；替代建议：{impact.suggestion}"
                        )
                        self.store.update_plan_state(
                            victim.plan_id, PlanState.CANCELLED,
                            cancellation_reason=victim.cancellation_reason,
                            amendments=victim.amendments,
                        )
                    preempted = [
                        self._impact_dict(impact)
                        for impact in impacts
                        if impact.displaced_request_id in higher_priority_requests
                    ]
                    impacts = [impact for impact in impacts
                               if impact.displaced_request_id in higher_priority_requests]
                else:
                    impacts = []

                # 替换封路重排产生的旧计划：原子切换。
                if plan.replaces_plan_id:
                    old = self.plans.get(plan.replaces_plan_id)
                    if old is not None and old.state in (
                        PlanState.CONFIRMED, PlanState.RUNNING
                    ):
                        if old.state == PlanState.RUNNING:
                            self.store.conn.rollback()
                            raise InvalidStateError(
                                f"旧计划 {old.plan_id} 已开始执行，不能被替换"
                            )
                        old.state = PlanState.CANCELLED
                        old.cancellation_reason = f"被改排计划 {plan.plan_id} 替换"
                        self.store.update_plan_state(
                            old.plan_id, PlanState.CANCELLED,
                            cancellation_reason=old.cancellation_reason,
                        )
                        self.store.replace_leases(old.plan_id, [])

                plan.state = (
                    PlanState.RUNNING if demand.start <= now < demand.end
                    else PlanState.CONFIRMED
                )
                plan.preemptive = bool(impacts)
                plan.displaced_requests = sorted(
                    {impact.displaced_request_id for impact in impacts}
                )
                plan.confirmed_at = now
                lease_state = plan.state.value
                self.store.replace_leases(
                    plan.plan_id,
                    [
                        {
                            "plan_id": lock.plan_id,
                            "request_id": lock.request_id,
                            "resource_type": lock.resource_type,
                            "resource_id": lock.resource_id,
                            "starts_at": lock.starts_at,
                            "ends_at": lock.ends_at,
                            "purpose": lock.purpose,
                            "state": lease_state,
                            "energy": lock.energy_kwh,
                        }
                        for lock in new_locks
                    ],
                )
                self.store.update_plan_state(
                    plan.plan_id, plan.state,
                    preemptive=plan.preemptive,
                    displaced=plan.displaced_requests,
                    confirmed_at=now,
                )
                self.store.commit()
            except Exception:
                self.store.conn.rollback()
                raise

            self._reload()
            return {
                "plan": self.plan_dict(self.plans[plan_id]),
                "preempted": preempted,
            }

    # ------------------------------------------------------------------
    # 取消与部分取消
    # ------------------------------------------------------------------
    def cancel_plan(self, plan_id: str, reason: str, *, now: Optional[datetime] = None) -> dict[str, Any]:
        with self._lock:
            now = now or self._now()
            plan = self._require_plan(plan_id)
            if plan.state == PlanState.CANCELLED:
                raise InvalidStateError(f"计划 {plan_id} 已是取消状态")
            if plan.state == PlanState.RUNNING:
                raise InvalidStateError(
                    f"计划 {plan_id} 已开始执行，行程不能移动或取消"
                )
            demand = self._require_demand(plan.request_id)
            if plan.state == PlanState.CONFIRMED and demand.start <= now:
                raise InvalidStateError(
                    f"计划 {plan_id} 已到发车时刻，不能取消"
                )
            plan.state = PlanState.CANCELLED
            plan.cancellation_reason = reason
            self.store.update_plan_state(
                plan_id, PlanState.CANCELLED, cancellation_reason=reason
            )
            self.store.replace_leases(plan_id, [])
            self.store.commit()
            self._reload()
            return {"plan": self.plan_dict(self.plans[plan_id], detailed=True)}

    def partial_cancel(
        self,
        plan_id: str,
        resource_type: str,
        resource_id: str,
        reason: str,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """释放单个航段租约或充电预约，车辆与安全员行程保留。"""

        with self._lock:
            now = now or self._now()
            plan = self._require_plan(plan_id)
            if plan.state not in (PlanState.CONFIRMED, PlanState.RUNNING):
                raise InvalidStateError("只有已确认/执行中的计划可以部分取消")
            if resource_type not in ("segment", "bay"):
                raise PlanningError(
                    "部分取消仅支持单个路线航段（segment）或充电预约（bay）；"
                    "车辆/安全员的释放请整单取消"
                )
            demand = self._require_demand(plan.request_id)
            if resource_type == "segment":
                leg = next(
                    (leg for leg in demand.legs if leg.segment_id == resource_id),
                    None,
                )
                if leg is None:
                    raise NotFoundError(f"计划 {plan_id} 不经过区段 {resource_id}")
                if leg.enters_at <= now:
                    raise InvalidStateError(
                        f"航段 {resource_id} 已在 {leg.enters_at:%H:%M} 进入，"
                        "已开始的行程不能移动"
                    )
                note = f"航段 {resource_id} 租约释放：{reason}"
            else:
                if not plan.charging or plan.charging.bay_id != resource_id:
                    raise NotFoundError(f"计划 {plan_id} 未预约充电工位 {resource_id}")
                if plan.charging.starts_at <= now < plan.charging.ends_at:
                    raise InvalidStateError("该充电预约已开始，不能释放")
                note = f"充电工位 {resource_id} 预约释放（车辆同步退出充电占用）：{reason}"
            if resource_type == "bay":
                # 充电预约同时占用工位与车辆，二者一并释放。
                self.store.conn.execute(
                    "DELETE FROM leases WHERE plan_id = ? AND purpose = 'charge'",
                    (plan_id,),
                )
                plan.charging = None
                self.store.conn.execute(
                    "UPDATE plans SET charging_json = NULL WHERE plan_id = ?",
                    (plan_id,),
                )
            else:
                self.store.conn.execute(
                    "DELETE FROM leases WHERE plan_id = ? AND resource_type = ? "
                    "AND resource_id = ?",
                    (plan_id, resource_type, resource_id),
                )
            plan.amendments.append(note)
            self.store.update_plan_state(
                plan_id, plan.state, amendments=plan.amendments
            )
            self.store.commit()
            self._reload()
            return {"plan": self.plan_dict(self.plans[plan_id], detailed=True)}

    # ------------------------------------------------------------------
    # 封路 / 资源失效：只重排受影响任务
    # ------------------------------------------------------------------
    def report_unavailable(
        self,
        resource_type: str,
        resource_id: str,
        window: UnavailableWindow,
        *,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        with self._lock:
            now = now or self._now()
            if resource_type not in ("segment", "vehicle", "officer", "bay"):
                raise PlanningError("未知资源类型")
            self.store.add_unavailable(window, resource_type, resource_id)
            self.store.commit()
            self._reload()
            return self.replan_affected(
                resource_type=resource_type, resource_id=resource_id, now=now
            )

    def replan_affected(
        self,
        *,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """找出与不可用窗口重叠的计划，只对未开始的受影响任务重新出候选。"""

        with self._lock:
            now = now or self._now()
            affected_plan_ids: set[str] = set()
            unmovable: list[dict[str, Any]] = []
            for row in self.leases.values():
                if row["state"] not in ("confirmed", "running"):
                    continue
                key = f"{row['resource_type']}:{row['resource_id']}"
                hit = None
                for window in self.unavailable.get(key, ()):
                    if overlaps(row["starts_at"], row["ends_at"],
                                window.starts_at, window.ends_at):
                        hit = window
                        break
                if hit is None:
                    continue
                if (resource_type is not None
                        and (row["resource_type"] != resource_type
                             or row["resource_id"] != resource_id)):
                    continue
                plan = self.plans.get(row["plan_id"])
                if plan is None:
                    continue
                if row["state"] == "running" or (
                    plan.state == PlanState.RUNNING
                ):
                    note = (
                        f"行程已开始，无法移动；{row['resource_type']} "
                        f"{row['resource_id']} 受「{hit.reason}」影响，需现场处置"
                    )
                    if note not in plan.amendments:
                        plan.amendments.append(note)
                        self.store.update_plan_state(
                            plan.plan_id, plan.state, amendments=plan.amendments
                        )
                    unmovable.append({"plan_id": plan.plan_id,
                                      "request_id": plan.request_id, "note": note})
                else:
                    affected_plan_ids.add(plan.plan_id)

            # 受影响计划的旧租约在新计划确认前不参与候选可行性。
            context = self._frozen_context(tuple(affected_plan_ids))
            new_options: dict[str, list[dict[str, Any]]] = {}
            infeasible: dict[str, list[str]] = {}
            for plan_id in sorted(affected_plan_ids):
                old = self.plans[plan_id]
                demand = self._require_demand(old.request_id)
                note = (
                    f"受资源不可用影响，已于 {now:%Y-%m-%d %H:%M} 触发改排，"
                    "待值班人员确认新方案"
                )
                if note not in old.amendments:
                    old.amendments.append(note)
                    self.store.update_plan_state(
                        old.plan_id, old.state, amendments=old.amendments
                    )
                # 作废该需求此前的候选，生成替换候选。
                for p in list(self.plans.values()):
                    if (p.request_id == demand.request_id
                            and p.state == PlanState.CANDIDATE):
                        self.store.delete_plan(p.plan_id)
                        self.plans.pop(p.plan_id, None)
                options = self.planner.generate_options(
                    demand, context, f"PL-{demand.request_id}-R", now=now,
                )
                generated: list[Plan] = []
                for option in options:
                    new_plan = self._persist_option(option, demand, now)
                    new_plan.replaces_plan_id = old.plan_id
                    self.store.insert_plan(new_plan, now)
                    generated.append(new_plan)
                new_options[demand.request_id] = [
                    self.plan_dict(p) for p in generated
                ]
                if not generated:
                    infeasible[demand.request_id] = (
                        self.planner.explain_infeasibility(demand, context)
                    )
            self.store.commit()
            self._reload()
            return {
                "affected_plan_ids": sorted(affected_plan_ids),
                "unmovable": unmovable,
                "options": new_options,
                "infeasible_reasons": infeasible,
            }

    # ------------------------------------------------------------------
    # 时间推进：候选→执行中→完成；某时点占用
    # ------------------------------------------------------------------
    def tick(self, now: datetime) -> list[str]:
        """按当前时间推进计划状态，返回发生变化的计划 ID。"""

        with self._lock:
            changed: list[str] = []
            for plan in list(self.plans.values()):
                demand = self.demands.get(plan.request_id)
                if demand is None:
                    continue
                if plan.state == PlanState.CONFIRMED and demand.start <= now < demand.end:
                    plan.state = PlanState.RUNNING
                    self.store.update_plan_state(plan.plan_id, PlanState.RUNNING)
                    self.store.set_lease_state_for_plan(plan.plan_id, "running")
                    changed.append(plan.plan_id)
                elif plan.state in (PlanState.CONFIRMED, PlanState.RUNNING) \
                        and now >= demand.end:
                    plan.state = PlanState.COMPLETED
                    self.store.update_plan_state(plan.plan_id, PlanState.COMPLETED)
                    self.store.set_lease_state_for_plan(plan.plan_id, "completed")
                    changed.append(plan.plan_id)
            self.store.commit()
            if changed:
                self._reload()
            return changed

    def occupancy(self, at: datetime, *, include_candidates: bool = True) -> dict[str, Any]:
        with self._lock:
            frozen: dict[str, list[dict[str, Any]]] = {}
            for row in self.leases.values():
                if row["state"] not in ("confirmed", "running"):
                    continue
                if not (row["starts_at"] <= at < row["ends_at"]):
                    continue
                key = f"{row['resource_type']}:{row['resource_id']}"
                frozen.setdefault(key, []).append(self._lock_dict(row))
            candidates: dict[str, list[dict[str, Any]]] = {}
            if include_candidates:
                for lock in self._candidate_locks():
                    if lock.starts_at <= at < lock.ends_at:
                        key = f"{lock.resource_type}:{lock.resource_id}"
                        candidates.setdefault(key, []).append(self._lock_dict(lock))
            closures: dict[str, list[dict[str, Any]]] = {}
            for key, windows in self.unavailable.items():
                for window in windows:
                    if window.starts_at <= at < window.ends_at:
                        closures.setdefault(key, []).append({
                            "starts_at": iso(window.starts_at),
                            "ends_at": iso(window.ends_at),
                            "reason": window.reason,
                        })
            return {
                "at": iso(at),
                "frozen": frozen,
                "candidates": candidates,
                "unavailable": closures,
            }

    # ------------------------------------------------------------------
    # 查询与序列化
    # ------------------------------------------------------------------
    def get_plan(self, plan_id: str) -> dict[str, Any]:
        with self._lock:
            return self.plan_dict(self._require_plan(plan_id), detailed=True)

    def get_request(self, request_id: str) -> dict[str, Any]:
        with self._lock:
            demand = self.demands.get(request_id)
            if demand is None:
                raise NotFoundError(f"未知需求 {request_id}")
            plans = [p for p in self.plans.values() if p.request_id == request_id]
            return {
                "request_id": request_id,
                "kind": demand.kind,
                "priority": demand.priority,
                "start": iso(demand.start),
                "end": iso(demand.end),
                "crosses_midnight": demand.crosses_midnight,
                "plans": [self.plan_dict(p) for p in plans],
            }

    def _require_plan(self, plan_id: str) -> Plan:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"未知计划 {plan_id}")
        return plan

    def _require_demand(self, request_id: str) -> TaskDemand:
        demand = self.demands.get(request_id)
        if demand is None:
            raise NotFoundError(f"未知需求 {request_id}")
        return demand

    @staticmethod
    def _now() -> datetime:
        from datetime import timezone
        return datetime.now(timezone.utc)

    @staticmethod
    def _lock_dict(lock: Any) -> dict[str, Any]:
        return {
            "resource_type": lock["resource_type"] if isinstance(lock, dict)
            else lock.resource_type,
            "resource_id": lock["resource_id"] if isinstance(lock, dict)
            else lock.resource_id,
            "starts_at": iso(lock["starts_at"] if isinstance(lock, dict)
                             else lock.starts_at),
            "ends_at": iso(lock["ends_at"] if isinstance(lock, dict)
                           else lock.ends_at),
            "purpose": lock["purpose"] if isinstance(lock, dict) else lock.purpose,
            "plan_id": lock["plan_id"] if isinstance(lock, dict) else lock.plan_id,
            "request_id": lock["request_id"] if isinstance(lock, dict)
            else lock.request_id,
            "state": lock["state"] if isinstance(lock, dict) else lock.state,
        }

    @staticmethod
    def _impact_dict(impact: PreemptionImpact) -> dict[str, Any]:
        return {
            "displaced_plan_id": impact.displaced_plan_id,
            "displaced_request_id": impact.displaced_request_id,
            "resource_type": impact.resource_type,
            "resource_id": impact.resource_id,
            "starts_at": iso(impact.starts_at),
            "ends_at": iso(impact.ends_at),
            "suggestion": impact.suggestion,
        }

    def plan_dict(self, plan: Plan, *, detailed: bool = False) -> dict[str, Any]:
        demand = self.demands.get(plan.request_id)
        charging = None
        if plan.charging:
            charging = {
                "bay_id": plan.charging.bay_id,
                "starts_at": iso(plan.charging.starts_at),
                "ends_at": iso(plan.charging.ends_at),
                "power_kw": plan.charging.power_kw,
                "energy_kwh": plan.charging.energy_kwh,
            }
        data = {
            "plan_id": plan.plan_id,
            "request_id": plan.request_id,
            "state": plan.state.value,
            "vehicle_id": plan.vehicle_id,
            "officer_id": plan.officer_id,
            "charging": charging,
            "score": plan.score,
            "preemptive": plan.preemptive,
            "displaced_requests": plan.displaced_requests,
            "replaces_plan_id": plan.replaces_plan_id,
        }
        if detailed:
            data.update({
                "kind": demand.kind if demand else None,
                "priority": demand.priority if demand else None,
                "start": iso(demand.start) if demand else None,
                "end": iso(demand.end) if demand else None,
                "crosses_midnight": demand.crosses_midnight if demand else None,
                "rationale": plan.rationale,
                "risks": plan.risks,
                "amendments": plan.amendments,
                "cancellation_reason": plan.cancellation_reason,
                "confirmed_at": iso(plan.confirmed_at) if plan.confirmed_at else None,
            })
        return data


class _OptionShim:
    """把已持久化的 Plan 适配成 evaluate_preemption 需要的 option 接口。"""

    def __init__(self, plan: Plan, demand: TaskDemand) -> None:
        self.plan_id = plan.plan_id
        self.request_id = plan.request_id
        self._plan = plan
        self._demand = demand

    def as_lock_tuple(self) -> tuple[ResourceLock, ...]:
        return tuple(PlanningService._plan_locks(self._plan, self._demand, "candidate"))
