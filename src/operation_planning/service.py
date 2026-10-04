"""示范运营资源编排服务门面。

职责：
* 维护车辆 / 安全员 / 区段 / 充电工位台账与运营需求；
* 生成可解释候选方案、支持多方案比较；
* 通过租约 + 事务实现并发安全的确认与冻结；
* 支持整单与部分取消；封路 / 资源失效时只重排受影响任务，
  已开始行程保持不动；
* 查询任一时点的资源占用；重启后租约从 SQLite 恢复。
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .domain import (
    Assignment,
    BlockedWindow,
    ChargingBay,
    ChargingSession,
    Interval,
    RouteSegment,
    SafetyOfficer,
    TaskDemand,
    Vehicle,
    from_iso,
    to_iso,
)
from .engine import PlanResult, Problem, Scheduler
from .errors import (
    ConflictError,
    LeaseError,
    NotFoundError,
    StateError,
    ValidationError,
)
from .repository import Repository

DEFAULT_LEASE_SECONDS = 120
FROZEN_STATES = ("confirmed", "running")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _row_to_assignment(row: Any) -> Assignment:
    return Assignment(
        row["plan_id"], row["request_id"], row["vehicle_id"], row["officer_id"],
        Interval(from_iso(row["starts_at"]), from_iso(row["ends_at"])),
        tuple(json.loads(row["segment_ids"])),
    )


def _row_to_charging(row: Any, plan_id: str) -> ChargingSession:
    return ChargingSession(
        plan_id, row["vehicle_id"], row["bay_id"],
        Interval(from_iso(row["starts_at"]), from_iso(row["ends_at"])),
        row["kwh"], row["request_id"],
    )


class PlanningService:
    def __init__(self, repo: Repository | str = ":memory:",
                 lease_ttl_seconds: int = DEFAULT_LEASE_SECONDS,
                 now: datetime | None = None) -> None:
        self.repo = repo if isinstance(repo, Repository) else Repository(repo)
        self.lease_ttl = lease_ttl_seconds
        self._frozen_now = now  # 注入时钟（测试用）；None 表示使用真实 UTC
        # 重启恢复：清理已到期租约，未到期租约继续有效。
        self._startup_purged = self.repo.purge_expired_leases(self.now())

    def now(self) -> datetime:
        return self._frozen_now if self._frozen_now is not None else _utcnow()

    def set_clock(self, now: datetime | None) -> None:
        self._frozen_now = now

    def close(self) -> None:
        self.repo.close()

    # ============================================================== 资源登记
    def register_vehicle(self, vehicle_id: str, capabilities: list[str] | set[str],
                         capacity_kwh: float, initial_charge_kwh: float,
                         consumption_kwh_per_minute: float) -> dict[str, Any]:
        vehicle = Vehicle(
            vehicle_id, frozenset(capabilities), capacity_kwh,
            initial_charge_kwh, consumption_kwh_per_minute,
        )
        self.repo.upsert_vehicle(vehicle)
        return {"vehicle_id": vehicle_id, "status": "registered"}

    def register_officer(self, officer_id: str, qualifications: list[str] | set[str]) -> dict[str, Any]:
        officer = SafetyOfficer(officer_id, frozenset(qualifications))
        self.repo.upsert_officer(officer)
        return {"officer_id": officer_id, "status": "registered"}

    def register_segment(self, segment_id: str) -> dict[str, Any]:
        self.repo.upsert_segment(RouteSegment(segment_id))
        return {"segment_id": segment_id, "status": "registered"}

    def register_bay(self, bay_id: str, power_kw: float) -> dict[str, Any]:
        self.repo.upsert_bay(ChargingBay(bay_id, power_kw))
        return {"bay_id": bay_id, "status": "registered"}

    # ============================================================== 需求提交
    def submit_request(
        self, request_id: str, operation_kind: str,
        starts_at: str | datetime, ends_at: str | datetime,
        duration_minutes: int, priority: int,
        segment_ids: list[str] | None = None,
        energy_kwh: float | None = None,
    ) -> dict[str, Any]:
        if self.repo.demand_exists(request_id):
            raise ConflictError(f"需求 {request_id} 已存在")
        start = from_iso(starts_at) if isinstance(starts_at, str) else starts_at
        end = from_iso(ends_at) if isinstance(ends_at, str) else ends_at
        demand = TaskDemand(
            request_id, operation_kind, Interval(start, end),
            duration_minutes, priority, tuple(segment_ids or ()), energy_kwh,
        )
        for sid in demand.segment_ids:
            if not any(s.segment_id == sid for s in self.repo.load_segments()):
                raise ValidationError(f"路线区段 {sid} 尚未登记")
        self.repo.insert_demand(demand)
        return {
            "request_id": request_id,
            "operation_kind": operation_kind,
            "window": {"start": to_iso(start), "end": to_iso(end)},
            "duration_minutes": duration_minutes,
            "priority": priority,
            "state": "active",
        }

    def list_requests(self, include_cancelled: bool = False) -> list[dict[str, Any]]:
        states = self.repo.demand_states()
        out = []
        for d in self.repo.load_demands(active_only=not include_cancelled):
            out.append({
                "request_id": d.request_id,
                "operation_kind": d.operation_kind,
                "window": d.window.to_dict(),
                "duration_minutes": d.duration_minutes,
                "priority": d.priority,
                "segment_ids": list(d.segment_ids),
                "energy_kwh": d.energy_kwh,
                "state": states.get(d.request_id, "active"),
            })
        return out

    # ============================================================== 方案生成
    def generate_plan(
        self, request_ids: list[str] | None = None, strategy: str = "priority",
        plan_id: str | None = None,
    ) -> dict[str, Any]:
        """为指定需求（缺省为全部活跃需求）生成候选方案。

        高优先级需求可挤占未确认方案；基线为已确认 / 进行中的占用与既有候选。
        """
        return self._generate(request_ids, strategy=strategy, plan_id=plan_id,
                              kind="candidate")

    def _active_demands(self, request_ids: list[str] | None) -> list[TaskDemand]:
        all_demands = self.repo.load_demands(active_only=True)
        if request_ids is None:
            return list(all_demands)
        found = {d.request_id: d for d in all_demands}
        missing = [rid for rid in request_ids if rid not in found]
        if missing:
            raise NotFoundError(f"需求不存在：{', '.join(missing)}")
        return [found[rid] for rid in request_ids]

    def _generate(
        self, request_ids: list[str] | None, *, strategy: str, plan_id: str | None,
        kind: str, reschedule_ids: frozenset[str] | None = None,
        protected_owners: frozenset[str] = frozenset(),
        trigger: str = "manual",
    ) -> dict[str, Any]:
        demands = self._active_demands(request_ids)
        if not demands:
            raise ValidationError("没有可排程的需求")
        schedule_ids = reschedule_ids if reschedule_ids is not None \
            else frozenset(d.request_id for d in demands)
        pid = plan_id or f"PLAN-{uuid.uuid4().hex[:10]}"
        problem = self._build_problem(
            pid, demands, schedule_ids, protected_owners, strategy
        )
        result = Scheduler(problem).solve()
        payload = self._serialize_result(result, trigger=trigger)
        self._persist_candidate(pid, strategy, demands, payload, result)
        return payload

    def _build_problem(
        self, plan_id: str, demands: list[TaskDemand],
        schedule_ids: frozenset[str], protected_owners: frozenset[str],
        strategy: str,
    ) -> Problem:
        occupancy = self.repo.load_occupancy()
        fixed_a: list[Assignment] = []
        fixed_c: list[ChargingSession] = []
        soft_a: list[Assignment] = []
        soft_c: list[ChargingSession] = []
        for row in occupancy:
            if row["state"] not in FROZEN_STATES + ("candidate",):
                continue
            if row["kind"] == "trip":
                a = _row_to_assignment(row)
                # 已确认/进行中一律硬约束；候选作为软基线载入——
                # 即使该需求在本次调度集合中，也要先保留旧落位，
                # 以便更高优先级需求在求解顺序靠前时记录“挤占”，随后再由 _place 收回。
                (fixed_a if row["state"] in FROZEN_STATES else soft_a).append(a)
            elif row["kind"] == "charge":
                c = _row_to_charging(row, row["plan_id"])
                (fixed_c if row["state"] in FROZEN_STATES else soft_c).append(c)
        return Problem(
            plan_id=plan_id,
            vehicles=self.repo.load_vehicles(),
            officers=self.repo.load_officers(),
            segments=self.repo.load_segments(),
            bays=self.repo.load_bays(),
            demands=tuple(self.repo.load_demands(active_only=True)),
            now=self.now(),
            fixed_assignments=fixed_a,
            fixed_charging=fixed_c,
            soft_assignments=soft_a,
            soft_charging=soft_c,
            blocks=self.repo.load_blocks(),
            strategy=strategy,
            schedule_ids=schedule_ids,
            protected_owners=protected_owners,
        )

    def _serialize_result(self, result: PlanResult, *, trigger: str) -> dict[str, Any]:
        locked = set(result.locked_ids)
        return {
            "plan_id": result.plan_id,
            "strategy": result.strategy,
            "status": "candidate",
            "trigger": trigger,
            "generated_at": to_iso(self.now()),
            "locked_request_ids": sorted(locked),
            "assignments": [
                {**a.to_dict(), "plan_id": a.plan_id,
                 "locked": a.request_id in locked,
                 "rationale": result.rationale.get(a.request_id, "")
                 if a.request_id not in locked
                 else "已确认/进行中的行程，不可移动，原样保留"}
                for a in result.assignments
            ],
            "charging": [c.to_dict() for c in result.charging],
            "unscheduled": [u.to_dict() for u in result.unscheduled],
            "displaced": [
                {
                    "request_id": d.request_id,
                    "preempted_by": list(d.preempted_by),
                    "resolved": d.resolved,
                    "new_assignment": d.new_assignment.to_dict()
                    if d.new_assignment is not None else None,
                    "alternative": d.alternative.to_dict()
                    if d.alternative is not None else None,
                }
                for d in result.displaced
            ],
            "score": result.score,
        }

    def _persist_candidate(
        self, plan_id: str, strategy: str, requested: list[TaskDemand],
        payload: dict[str, Any], result: PlanResult,
    ) -> None:
        # 受影响方 = 显式请求 ∪ 实际排入 ∪ 被挤占方 ∪ 未能排入方。
        affected = {d.request_id for d in requested}
        affected |= {a.request_id for a in result.assignments}
        affected |= {d.request_id for d in result.displaced}
        affected |= {u.request_id for u in result.unscheduled}
        affected_ids = sorted(affected)
        self.repo.save_plan(plan_id, strategy, "candidate", affected_ids, payload)
        with self.repo.lock:
            self.repo.begin_immediate()
            try:
                placeholders = ",".join("?" * len(affected_ids))
                conn = self.repo.conn
                conn.execute(
                    f"DELETE FROM occupancy WHERE state='candidate' "
                    f"AND COALESCE(request_id,'') IN ({placeholders})",
                    affected_ids,
                )
                conn.execute(
                    f"UPDATE plans SET status='superseded' WHERE status='candidate' "
                    f"AND plan_id != ? AND plan_id IN ("
                    f"SELECT plan_id FROM plan_requests WHERE request_id IN ({placeholders}))",
                    [plan_id, *affected_ids],
                )
                # 受影响方 = 显式请求 ∪ 实际排入 ∪ 被挤占方 ∪ 未能排入方；
                # 已锁定任务仅在方案视图中展示，不改变其 confirmed 占用。
                locked = set(result.locked_ids)
                for a in result.assignments:
                    if a.request_id in locked:
                        continue
                    self.repo.add_occupancy(
                        request_id=a.request_id, vehicle_id=a.vehicle_id,
                        officer_id=a.officer_id, bay_id=None, kind="trip",
                        window=a.window, segment_ids=a.segment_ids, kwh=0.0,
                        state="candidate", plan_id=plan_id,
                    )
                locked_charge_owners = {
                    c.reason_request_id for c in result.charging
                } & locked
                for c in result.charging:
                    # 已冻结的充电预约保持原占用，不重复登记为候选。
                    if c.reason_request_id in locked_charge_owners:
                        continue
                    self.repo.add_occupancy(
                        request_id=c.reason_request_id, vehicle_id=c.vehicle_id,
                        officer_id=None, bay_id=c.bay_id, kind="charge",
                        window=c.window, segment_ids=(), kwh=c.kwh,
                        state="candidate", plan_id=plan_id,
                    )
                # 本次未能落位的需求，其历史候选占用已随上面的删除而清空。
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self.repo.load_plan(plan_id)
        if plan is None:
            raise NotFoundError(f"方案 {plan_id} 不存在")
        return plan

    def list_plans(self) -> list[dict[str, Any]]:
        return self.repo.list_plans()

    def compare_plans(self, plan_id_a: str, plan_id_b: str) -> dict[str, Any]:
        a, b = self.get_plan(plan_id_a), self.get_plan(plan_id_b)
        return {
            "plans": [
                {"plan_id": a["plan_id"], "strategy": a["strategy"], "score": a["score"]},
                {"plan_id": b["plan_id"], "strategy": b["strategy"], "score": b["score"]},
            ],
            "differences": {
                "assigned_delta": b["score"]["assigned"] - a["score"]["assigned"],
                "unscheduled_delta":
                    b["score"]["unscheduled"] - a["score"]["unscheduled"],
                "charging_kwh_delta":
                    round(b["score"]["charging_kwh"] - a["score"]["charging_kwh"], 3),
                "slack_minutes_delta":
                    b["score"]["total_slack_minutes"] - a["score"]["total_slack_minutes"],
                "priority_coverage_delta":
                    b["score"]["priority_coverage"] - a["score"]["priority_coverage"],
            },
            "only_in_a": self._request_diff(a, b),
            "only_in_b": self._request_diff(b, a),
            "recommendation": self._recommend(a, b),
        }

    @staticmethod
    def _request_diff(x: dict[str, Any], y: dict[str, Any]) -> list[str]:
        x_ids = {a["request_id"] for a in x["assignments"]}
        y_ids = {a["request_id"] for a in y["assignments"]}
        return sorted(x_ids - y_ids)

    @staticmethod
    def _recommend(a: dict[str, Any], b: dict[str, Any]) -> str:
        def key(p: dict[str, Any]) -> tuple[float, float, float, float]:
            s = p["score"]
            return (-s["assigned"], s["unscheduled"],
                    -s["priority_coverage"], s["total_slack_minutes"])
        winner = a["plan_id"] if key(a) <= key(b) else b["plan_id"]
        return f"推荐方案 {winner}：排入需求更多、未排更少、优先级覆盖更高且总等待更短"

    # ============================================================== 租约
    def acquire_lease(
        self, plan_id: str, holder: str, ttl_seconds: int | None = None
    ) -> dict[str, Any]:
        if self.repo.load_plan(plan_id) is None:
            raise NotFoundError(f"方案 {plan_id} 不存在")
        ttl = ttl_seconds or self.lease_ttl
        now = self.now()
        with self.repo.lock:
            self.repo.begin_immediate()
            try:
                existing = self.repo.load_lease(plan_id)
                if existing is not None:
                    expires = from_iso(existing["expires_at"])
                    if expires > now and existing["holder"] != holder:
                        self.repo.rollback()
                        raise LeaseError(
                            f"方案 {plan_id} 正被 {existing['holder']} 持有，"
                            f"租约至 {existing['expires_at']} 到期"
                        )
                token = secrets.token_hex(16)
                self.repo.save_lease(plan_id, holder, token, now,
                                     now + timedelta(seconds=ttl))
                self.repo.commit()
                return {"plan_id": plan_id, "holder": holder, "token": token,
                        "expires_at": to_iso(now + timedelta(seconds=ttl))}
            except Exception:
                self.repo.rollback()
                raise

    def release_lease(self, plan_id: str, holder: str, token: str) -> dict[str, Any]:
        self._check_lease(plan_id, holder, token)
        self.repo.delete_lease(plan_id)
        return {"plan_id": plan_id, "released": True}

    def _check_lease(self, plan_id: str, holder: str, token: str) -> dict[str, Any]:
        lease = self.repo.load_lease(plan_id)
        if lease is None:
            raise LeaseError(f"方案 {plan_id} 没有有效租约，请先获取租约")
        if lease["holder"] != holder:
            raise LeaseError(f"租约由 {lease['holder']} 持有，{holder} 无权操作")
        if lease["token"] != token:
            raise LeaseError("租约令牌不匹配")
        if from_iso(lease["expires_at"]) <= self.now():
            self.repo.delete_lease(plan_id)
            raise LeaseError("租约已过期，请重新获取")
        return lease

    # ============================================================== 确认冻结
    def confirm_plan(
        self, plan_id: str, holder: str, token: str
    ) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        self._check_lease(plan_id, holder, token)
        if plan["status"] == "confirmed":
            raise StateError(f"方案 {plan_id} 已确认")
        if plan["status"] not in ("candidate", "partially_confirmed"):
            raise StateError(f"方案状态为 {plan['status']}，无法确认")
        with self.repo.lock:
            self.repo.begin_immediate()
            try:
                conn = self.repo.conn
                # 事务内复检租约，防止并发确认。
                row = conn.execute(
                    "SELECT * FROM leases WHERE plan_id=?", (plan_id,)
                ).fetchone()
                if row is None or row["token"] != token or row["holder"] != holder:
                    self.repo.rollback()
                    raise LeaseError("租约校验失败")
                if from_iso(row["expires_at"]) <= self.now():
                    self.repo.rollback()
                    raise LeaseError("租约已过期")
                conflicts = self._frozen_conflicts(conn, plan)
                if conflicts:
                    self.repo.rollback()
                    raise ConflictError(
                        "与已确认/进行中的占用冲突：" + "；".join(conflicts[:5])
                    )
                locked_ids = set(plan.get("locked_request_ids", []))
                new_assignments = [a for a in plan["assignments"]
                                   if a["request_id"] not in locked_ids]
                new_charging = [c for c in plan.get("charging", [])
                                if c.get("reason_request_id") not in locked_ids]
                request_ids = [a["request_id"] for a in new_assignments]
                charge_owners = [c.get("reason_request_id") for c in new_charging
                                 if c.get("reason_request_id")]
                replace_ids = sorted(set(request_ids) | set(charge_owners))
                if replace_ids:
                    ph = ",".join("?" * len(replace_ids))
                    # 移除这些需求此前的候选与已确认占用（修复方案会替换旧确认）；
                    # 已锁定（running/confirmed 且未受影响）的占用绝不触碰。
                    conn.execute(
                        f"DELETE FROM occupancy WHERE state IN ('candidate','confirmed') "
                        f"AND COALESCE(request_id,'') IN ({ph})",
                        replace_ids,
                    )
                    # 涉及这些需求的其它候选方案标记为已取代。
                    conn.execute(
                        f"UPDATE plans SET status='superseded' WHERE status='candidate' "
                        f"AND plan_id != ? AND plan_id IN ("
                        f"SELECT plan_id FROM plan_requests WHERE request_id IN ({ph}))",
                        [plan_id, *replace_ids],
                    )
                for a in new_assignments:
                    conn.execute(
                        "INSERT INTO occupancy (request_id,vehicle_id,officer_id,bay_id,"
                        "kind,starts_at,ends_at,segment_ids,kwh,state,plan_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (a["request_id"], a["vehicle_id"], a["officer_id"], None,
                         "trip", a["start"], a["end"],
                         json.dumps(a["segment_ids"], ensure_ascii=False),
                         0.0, "confirmed", plan_id),
                    )
                for c in new_charging:
                    conn.execute(
                        "INSERT INTO occupancy (request_id,vehicle_id,officer_id,bay_id,"
                        "kind,starts_at,ends_at,segment_ids,kwh,state,plan_id) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (c.get("reason_request_id"), c["vehicle_id"], None, c["bay_id"],
                         "charge", c["start"], c["end"], "[]", c["kwh"],
                         "confirmed", plan_id),
                    )
                conn.execute("UPDATE plans SET status='confirmed' WHERE plan_id=?",
                             (plan_id,))
                conn.execute("DELETE FROM leases WHERE plan_id=?", (plan_id,))
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return {
            "plan_id": plan_id,
            "status": "confirmed",
            "confirmed_requests": [a["request_id"] for a in plan["assignments"]
                                   if a["request_id"] not in
                                   set(plan.get("locked_request_ids", []))],
            "locked_request_ids": sorted(set(plan.get("locked_request_ids", []))),
            "unscheduled": plan.get("unscheduled", []),
            "displaced": plan.get("displaced", []),
        }

    def _frozen_conflicts(self, conn: Any, plan: dict[str, Any]) -> list[str]:
        rows = conn.execute(
            "SELECT * FROM occupancy WHERE state IN ('confirmed','running')"
        ).fetchall()

        def frozen_keys(r: Any) -> list[tuple[str, str]]:
            keys = []
            if r["vehicle_id"]:
                keys.append(("v", r["vehicle_id"]))
            if r["officer_id"]:
                keys.append(("o", r["officer_id"]))
            if r["bay_id"]:
                keys.append(("b", r["bay_id"]))
            if r["kind"] == "trip":
                keys += [("s", s) for s in json.loads(r["segment_ids"])]
            return keys

        frozen = [(frozen_keys(r), r) for r in rows]
        conflicts: list[str] = []

        def check(new_keys: list[tuple[str, str]], start: str, end: str,
                  label: str, self_id: str | None) -> None:
            for keys, r in frozen:
                if r["request_id"] == self_id:
                    continue
                shared = set(keys) & set(new_keys)
                if shared and r["starts_at"] < end and start < r["ends_at"]:
                    pretty = "、".join(
                        f"{ {'v':'车辆','o':'安全员','b':'充电工位','s':'区段'}[k]} {v}"
                        for k, v in sorted(shared)
                    )
                    conflicts.append(f"{label} 与 {r['request_id']} 在 {pretty} 上时间重叠")

        for item in plan["assignments"]:
            keys = [("v", item["vehicle_id"]), ("o", item["officer_id"])]
            keys += [("s", s) for s in item["segment_ids"]]
            check(keys, item["start"], item["end"],
                  f"需求 {item['request_id']}", item["request_id"])
        for c in plan.get("charging", []):
            check([("v", c["vehicle_id"]), ("b", c["bay_id"])],
                  c["start"], c["end"],
                  f"充电预约（车辆 {c['vehicle_id']} / 工位 {c['bay_id']}）",
                  c.get("reason_request_id"))
        return conflicts

    # ============================================================== 取消
    def cancel_plan(self, plan_id: str, holder: str, token: str,
                    request_ids: list[str] | None = None) -> dict[str, Any]:
        """取消方案；给定期望取消的需求子集时为部分取消。

        已开始的行程不允许取消；部分取消后方案仍保持其既有状态，
        仅释放被取消需求的占用。
        """
        plan = self.get_plan(plan_id)
        self._check_lease(plan_id, holder, token)
        ids = request_ids if request_ids is not None else \
            [a["request_id"] for a in plan["assignments"]]
        if not ids:
            raise ValidationError("取消列表为空")
        assignments = {a["request_id"]: a for a in plan["assignments"]}
        unknown = [i for i in ids if i not in assignments and
                   not any(u["request_id"] == i for u in plan.get("unscheduled", []))]
        if unknown:
            raise NotFoundError(f"方案内不存在这些需求：{', '.join(unknown)}")
        now = self.now()
        started = [i for i in ids if i in assignments and
                   from_iso(assignments[i]["start"]) <= now]
        if started:
            raise StateError("以下行程已经开始，不能取消：" + "、".join(started))
        with self.repo.lock:
            self.repo.begin_immediate()
            try:
                conn = self.repo.conn
                conn.execute("DELETE FROM occupancy WHERE request_id IN (%s)"
                             % ",".join("?" * len(ids)), ids)
                # 已取消的需求不再参与后续排程。
                conn.execute("UPDATE demands SET state='cancelled' WHERE request_id IN (%s)"
                             % ",".join("?" * len(ids)), ids)
                conn.executemany(
                    "INSERT OR IGNORE INTO plan_requests(plan_id,request_id) VALUES (?,?)",
                    [(plan_id, i) for i in ids],
                )
                if request_ids is None:
                    conn.execute("UPDATE plans SET status='cancelled' WHERE plan_id=?",
                                 (plan_id,))
                    conn.execute("DELETE FROM leases WHERE plan_id=?", (plan_id,))
                else:
                    remaining = conn.execute(
                        "SELECT COUNT(*) AS n FROM occupancy WHERE plan_id=? AND kind='trip'",
                        (plan_id,),
                    ).fetchone()["n"]
                    new_status = "cancelled" if remaining == 0 else "partially_confirmed"
                    conn.execute("UPDATE plans SET status=? WHERE plan_id=?",
                                 (new_status, plan_id))
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return {"plan_id": plan_id, "cancelled_requests": ids,
                "partial": request_ids is not None}

    def cancel_request(self, request_id: str) -> dict[str, Any]:
        """不依赖方案直接取消单个需求（例如需求方撤单）。"""
        demands = self.repo.load_demands([request_id])
        if not demands:
            raise NotFoundError(f"需求 {request_id} 不存在")
        rows = [r for r in self.repo.load_occupancy()
                if r["request_id"] == request_id and r["kind"] == "trip"]
        now = self.now()
        started = [r for r in rows if from_iso(r["starts_at"]) <= now
                   and r["state"] in FROZEN_STATES]
        if started:
            raise StateError(f"需求 {request_id} 的行程已经开始，不能取消")
        with self.repo.lock:
            self.repo.begin_immediate()
            try:
                conn = self.repo.conn
                conn.execute("DELETE FROM occupancy WHERE request_id=?", (request_id,))
                conn.execute("UPDATE demands SET state='cancelled' WHERE request_id=?",
                             (request_id,))
                conn.execute(
                    "UPDATE plans SET status='cancelled' WHERE status='candidate' "
                    "AND plan_id IN (SELECT plan_id FROM plan_requests WHERE request_id=?)",
                    (request_id,),
                )
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        return {"request_id": request_id, "cancelled": True}

    # ============================================================== 封停与重排
    def report_block(
        self, resource_kind: str, resource_id: str,
        starts_at: str | datetime, ends_at: str | datetime,
        reason: str = "",
    ) -> dict[str, Any]:
        """登记封路 / 资源失效，仅重排真正受影响且尚未开始的任务。"""
        start = from_iso(starts_at) if isinstance(starts_at, str) else starts_at
        end = from_iso(ends_at) if isinstance(ends_at, str) else ends_at
        block = BlockedWindow(resource_kind, resource_id, Interval(start, end), reason)
        now = self.now()
        affected, immutable = self._assess_block_impact(block, now)
        if immutable:
            raise StateError(
                "封停与已开始不可移动的行程冲突："
                + "、".join(f"{rid}（{why}）" for rid, why in immutable)
            )
        self.repo.add_block(block)
        if not affected:
            return {
                "block": {"resource_kind": resource_kind, "resource_id": resource_id,
                          "window": {"start": to_iso(start), "end": to_iso(end)},
                          "reason": reason},
                "affected_requests": [],
                "repair_plan": None,
                "note": "没有未开始任务受到影响，无需重排",
            }
        affected_ids = frozenset(affected)
        # 收回受影响需求的既有占用（候选/已确认），交由修复方案重新求解；
        # 未能重新落位的需求将没有占用（状态为 impacted），避免与封停冲突的僵尸占用。
        with self.repo.lock:
            self.repo.begin_immediate()
            try:
                conn = self.repo.conn
                conn.execute(
                    "DELETE FROM occupancy WHERE request_id IN (%s)"
                    % ",".join("?" * len(affected_ids)),
                    list(affected_ids),
                )
                conn.execute(
                    "UPDATE plans SET status='impacted' WHERE status='confirmed' "
                    "AND plan_id IN (SELECT plan_id FROM plan_requests WHERE request_id IN (%s))"
                    % ",".join("?" * len(affected_ids)),
                    list(affected_ids),
                )
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise
        plan = self._generate(
            list(affected_ids), strategy="priority",
            plan_id=f"REPAIR-{uuid.uuid4().hex[:10]}", kind="repair",
            reschedule_ids=affected_ids,
            protected_owners=frozenset(
                d.request_id for d in self.repo.load_demands(active_only=True)
                if d.request_id not in affected_ids
            ),
            trigger=f"block:{resource_kind}:{resource_id}",
        )
        return {
            "block": {"resource_kind": resource_kind, "resource_id": resource_id,
                      "window": {"start": to_iso(start), "end": to_iso(end)},
                      "reason": reason},
            "affected_requests": sorted(affected_ids),
            "repair_plan": plan,
        }

    def _assess_block_impact(
        self, block: BlockedWindow, now: datetime
    ) -> tuple[list[str], list[tuple[str, str]]]:
        affected: set[str] = set()
        immutable: list[tuple[str, str]] = []
        for r in self.repo.load_occupancy():
            window = Interval(from_iso(r["starts_at"]), from_iso(r["ends_at"]))
            if not window.overlaps(block.window):
                continue
            touches = False
            if block.resource_kind == "segment":
                touches = r["kind"] == "trip" and block.resource_id in \
                    set(json.loads(r["segment_ids"]))
            elif block.resource_kind == "vehicle":
                touches = r["vehicle_id"] == block.resource_id
            else:
                touches = r["kind"] == "trip" and r["officer_id"] == block.resource_id
            if not touches or r["request_id"] is None:
                continue
            if r["state"] == "running" or (
                r["state"] == "confirmed" and window.start <= now
            ):
                immutable.append((r["request_id"], "行程已开始"))
                continue
            affected.add(r["request_id"])
        return sorted(affected), immutable

    # ============================================================== 时点占用
    def occupancy_at(
        self, at: str | datetime, resource_kind: str | None = None,
        resource_id: str | None = None, include_candidate: bool = True,
    ) -> dict[str, Any]:
        instant = from_iso(at) if isinstance(at, str) else at
        rows = self.repo.occupancy_at(instant, resource_kind, resource_id)
        if not include_candidate:
            rows = [r for r in rows if r["state"] in FROZEN_STATES]
        self._transition_states(instant)
        return {
            "at": to_iso(instant),
            "resource_kind": resource_kind,
            "resource_id": resource_id,
            "occupancy": sorted(rows, key=lambda r: (r["start"], r["kind"])),
        }

    def _transition_states(self, now: datetime) -> None:
        """已到开始时刻的已确认行程转为进行中。"""
        with self.repo.lock:
            self.repo.begin_immediate()
            try:
                conn = self.repo.conn
                conn.execute(
                    "UPDATE occupancy SET state='running' WHERE state='confirmed' "
                    "AND kind='trip' AND starts_at <= ?",
                    (to_iso(now),),
                )
                self.repo.commit()
            except Exception:
                self.repo.rollback()
                raise

    def tick(self, now: str | datetime | None = None) -> dict[str, Any]:
        if now is not None:
            instant = from_iso(now) if isinstance(now, str) else now
            if self._frozen_now is not None:
                self._frozen_now = instant  # 模拟时钟推进
        else:
            instant = self.now()
        self._transition_states(instant)
        purged = self.repo.purge_expired_leases(instant)
        return {"now": to_iso(instant), "expired_leases": purged}

    # ============================================================== 恢复
    def recover_leases(self) -> dict[str, Any]:
        """重启后调用：返回启动时清理的到期租约与仍然有效的租约。"""
        active = []
        for plan_id in {r["plan_id"] for r in self.repo.load_occupancy()}:
            lease = self.repo.load_lease(plan_id)
            if lease is not None:
                active.append({
                    "plan_id": plan_id,
                    "holder": lease["holder"],
                    "expires_at": lease["expires_at"],
                })
        return {"purged_expired": list(self._startup_purged), "active_leases": active}
