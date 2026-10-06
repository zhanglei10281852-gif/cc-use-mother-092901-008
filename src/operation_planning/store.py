"""SQLite 持久化：资源台账、需求、计划、租约与不可用窗口。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable, Optional

from .contracts import PlanState
from .models import (
    ChargingBay,
    ChargingBooking,
    Leg,
    Plan,
    RouteSegment,
    SafetyOfficer,
    TaskDemand,
    UnavailableWindow,
    Vehicle,
)

ISO = "%Y-%m-%dT%H:%M:%S.%f%z"


def iso(value: datetime) -> str:
    return value.strftime(ISO)


def parse(value: str) -> datetime:
    return datetime.strptime(value, ISO)


SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    soc_kwh REAL NOT NULL,
    capacity_kwh REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS officers (
    officer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    qualifications_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bays (
    bay_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    power_kw REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    segment_id TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS demands (
    request_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    priority INTEGER NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    energy REAL NOT NULL,
    legs_json TEXT NOT NULL,
    created_ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    vehicle_id TEXT NOT NULL,
    officer_id TEXT NOT NULL,
    charging_json TEXT,
    score INTEGER NOT NULL,
    rationale_json TEXT NOT NULL,
    risks_json TEXT NOT NULL,
    state TEXT NOT NULL,
    preemptive INTEGER NOT NULL DEFAULT 0,
    displaced_json TEXT NOT NULL DEFAULT '[]',
    replaces_plan_id TEXT,
    cancel_reason TEXT,
    amendments_json TEXT NOT NULL DEFAULT '[]',
    created_ts TEXT NOT NULL,
    confirmed_ts TEXT
);
CREATE TABLE IF NOT EXISTS leases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    purpose TEXT NOT NULL,
    state TEXT NOT NULL,
    energy REAL
);
CREATE TABLE IF NOT EXISTS unavailable_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    start_ts TEXT NOT NULL,
    end_ts TEXT NOT NULL,
    reason TEXT NOT NULL
);
"""


class SQLiteStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------
    # 资源台账
    # ------------------------------------------------------------------
    def upsert_vehicle(self, vehicle: Vehicle) -> None:
        self.conn.execute(
            "INSERT INTO vehicles VALUES (?,?,?,?,?) "
            "ON CONFLICT(vehicle_id) DO UPDATE SET "
            "name=excluded.name, capabilities_json=excluded.capabilities_json, "
            "soc_kwh=excluded.soc_kwh, capacity_kwh=excluded.capacity_kwh",
            (vehicle.vehicle_id, vehicle.name, json.dumps(sorted(vehicle.capabilities)),
             vehicle.soc_kwh, vehicle.capacity_kwh),
        )

    def upsert_officer(self, officer: SafetyOfficer) -> None:
        self.conn.execute(
            "INSERT INTO officers VALUES (?,?,?) "
            "ON CONFLICT(officer_id) DO UPDATE SET "
            "name=excluded.name, qualifications_json=excluded.qualifications_json",
            (officer.officer_id, officer.name, json.dumps(sorted(officer.qualifications))),
        )

    def upsert_bay(self, bay: ChargingBay) -> None:
        self.conn.execute(
            "INSERT INTO bays VALUES (?,?,?) "
            "ON CONFLICT(bay_id) DO UPDATE SET "
            "name=excluded.name, power_kw=excluded.power_kw",
            (bay.bay_id, bay.name, bay.power_kw),
        )

    def upsert_segment(self, segment: RouteSegment) -> None:
        self.conn.execute(
            "INSERT INTO segments VALUES (?,?) "
            "ON CONFLICT(segment_id) DO UPDATE SET name=excluded.name",
            (segment.segment_id, segment.name),
        )

    def load_catalog(
        self,
    ) -> tuple[dict[str, Vehicle], dict[str, SafetyOfficer],
               dict[str, ChargingBay], dict[str, RouteSegment]]:
        vehicles = {
            row["vehicle_id"]: Vehicle(
                row["vehicle_id"], row["name"],
                frozenset(json.loads(row["capabilities_json"])),
                row["soc_kwh"], row["capacity_kwh"],
            )
            for row in self.conn.execute("SELECT * FROM vehicles")
        }
        officers = {
            row["officer_id"]: SafetyOfficer(
                row["officer_id"], row["name"],
                frozenset(json.loads(row["qualifications_json"])),
            )
            for row in self.conn.execute("SELECT * FROM officers")
        }
        bays = {
            row["bay_id"]: ChargingBay(row["bay_id"], row["name"], row["power_kw"])
            for row in self.conn.execute("SELECT * FROM bays")
        }
        segments = {
            row["segment_id"]: RouteSegment(row["segment_id"], row["name"])
            for row in self.conn.execute("SELECT * FROM segments")
        }
        return vehicles, officers, bays, segments

    # ------------------------------------------------------------------
    # 需求
    # ------------------------------------------------------------------
    def insert_demand(self, demand: TaskDemand, created_at: datetime) -> None:
        legs = [
            {"segment_id": leg.segment_id, "enters_at": iso(leg.enters_at),
             "exits_at": iso(leg.exits_at)}
            for leg in demand.legs
        ]
        self.conn.execute(
            "INSERT OR REPLACE INTO demands VALUES (?,?,?,?,?,?,?,?)",
            (demand.request_id, demand.kind, demand.priority, iso(demand.start),
             iso(demand.end), demand.energy_required_kwh, json.dumps(legs),
             iso(created_at)),
        )

    def load_demands(self) -> dict[str, TaskDemand]:
        result: dict[str, TaskDemand] = {}
        for row in self.conn.execute("SELECT * FROM demands"):
            legs = tuple(
                Leg(item["segment_id"], parse(item["enters_at"]), parse(item["exits_at"]))
                for item in json.loads(row["legs_json"])
            )
            result[row["request_id"]] = TaskDemand(
                row["request_id"], row["kind"], row["priority"],
                parse(row["start_ts"]), parse(row["end_ts"]),
                legs, row["energy"],
            )
        return result

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------
    def insert_plan(self, plan: Plan, created_at: datetime) -> None:
        charging = None
        if plan.charging:
            charging = json.dumps({
                "bay_id": plan.charging.bay_id,
                "starts_at": iso(plan.charging.starts_at),
                "ends_at": iso(plan.charging.ends_at),
                "power_kw": plan.charging.power_kw,
                "energy_kwh": plan.charging.energy_kwh,
            })
        self.conn.execute(
            "INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan.plan_id, plan.request_id, plan.vehicle_id, plan.officer_id, charging,
             plan.score, json.dumps(plan.rationale, ensure_ascii=False),
             json.dumps(plan.risks, ensure_ascii=False), plan.state.value,
             int(plan.preemptive), json.dumps(plan.displaced_requests),
             plan.replaces_plan_id, plan.cancellation_reason,
             json.dumps(plan.amendments, ensure_ascii=False), iso(created_at),
             iso(plan.confirmed_at) if getattr(plan, "confirmed_at", None) else None),
        )

    def update_plan_state(
        self,
        plan_id: str,
        state: PlanState,
        *,
        cancellation_reason: Optional[str] = None,
        preemptive: Optional[bool] = None,
        displaced: Optional[list[str]] = None,
        amendments: Optional[list[str]] = None,
        confirmed_at: Optional[datetime] = None,
    ) -> None:
        fields = ["state = ?", "cancel_reason = COALESCE(?, cancel_reason)"]
        params: list[Any] = [state.value, cancellation_reason]
        if preemptive is not None:
            fields.append("preemptive = ?")
            params.append(int(preemptive))
        if displaced is not None:
            fields.append("displaced_json = ?")
            params.append(json.dumps(displaced))
        if amendments is not None:
            fields.append("amendments_json = ?")
            params.append(json.dumps(amendments, ensure_ascii=False))
        if confirmed_at is not None:
            fields.append("confirmed_ts = ?")
            params.append(iso(confirmed_at))
        params.append(plan_id)
        self.conn.execute(f"UPDATE plans SET {', '.join(fields)} WHERE plan_id = ?", params)

    def load_plans(self) -> dict[str, Plan]:
        result: dict[str, Plan] = {}
        for row in self.conn.execute("SELECT * FROM plans"):
            charging = None
            if row["charging_json"]:
                data = json.loads(row["charging_json"])
                charging = ChargingBooking(
                    data["bay_id"], parse(data["starts_at"]), parse(data["ends_at"]),
                    data["power_kw"], data["energy_kwh"],
                )
            plan = Plan(
                plan_id=row["plan_id"],
                request_id=row["request_id"],
                vehicle_id=row["vehicle_id"],
                officer_id=row["officer_id"],
                charging=charging,
                score=row["score"],
                rationale=json.loads(row["rationale_json"]),
                risks=json.loads(row["risks_json"]),
                state=PlanState(row["state"]),
                preemptive=bool(row["preemptive"]),
                displaced_requests=json.loads(row["displaced_json"]),
                replaces_plan_id=row["replaces_plan_id"],
                cancellation_reason=row["cancel_reason"],
                amendments=json.loads(row["amendments_json"]),
                created_at=parse(row["created_ts"]),
            )
            setattr(plan, "confirmed_at",
                    parse(row["confirmed_ts"]) if row["confirmed_ts"] else None)
            result[plan.plan_id] = plan
        return result

    def delete_plan(self, plan_id: str) -> None:
        self.conn.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
        self.conn.execute("DELETE FROM leases WHERE plan_id = ?", (plan_id,))

    # ------------------------------------------------------------------
    # 租约
    # ------------------------------------------------------------------
    def replace_leases(self, plan_id: str, leases: Iterable[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM leases WHERE plan_id = ?", (plan_id,))
        self.conn.executemany(
            "INSERT INTO leases (plan_id, request_id, resource_type, resource_id, "
            "start_ts, end_ts, purpose, state, energy) VALUES (?,?,?,?,?,?,?,?,?)",
            [(item["plan_id"], item["request_id"], item["resource_type"],
              item["resource_id"], iso(item["starts_at"]), iso(item["ends_at"]),
              item["purpose"], item["state"], item.get("energy")) for item in leases],
        )

    def load_leases(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM leases ORDER BY id").fetchall()
        return [
            {
                "lease_id": str(row["id"]),
                "plan_id": row["plan_id"],
                "request_id": row["request_id"],
                "resource_type": row["resource_type"],
                "resource_id": row["resource_id"],
                "starts_at": parse(row["start_ts"]),
                "ends_at": parse(row["end_ts"]),
                "purpose": row["purpose"],
                "state": row["state"],
                "energy_kwh": row["energy"],
            }
            for row in rows
        ]

    def set_lease_state_for_plan(
        self, plan_id: str, state: str, *, purposes: Optional[Iterable[str]] = None,
        resource_ids: Optional[Iterable[str]] = None,
    ) -> None:
        sql = "UPDATE leases SET state = ? WHERE plan_id = ?"
        params: list[Any] = [state, plan_id]
        if purposes is not None:
            sql += f" AND purpose IN ({','.join('?' for _ in purposes)})"
            params.extend(purposes)
        if resource_ids is not None:
            ids = tuple(resource_ids)
            sql += f" AND resource_id IN ({','.join('?' for _ in ids)})"
            params.extend(ids)
        self.conn.execute(sql, params)

    # ------------------------------------------------------------------
    # 不可用窗口
    # ------------------------------------------------------------------
    def add_unavailable(self, window: UnavailableWindow, resource_type: str,
                        resource_id: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO unavailable_windows "
            "(resource_type, resource_id, start_ts, end_ts, reason) VALUES (?,?,?,?,?)",
            (resource_type, resource_id, iso(window.starts_at), iso(window.ends_at),
             window.reason),
        )
        return int(cur.lastrowid)

    def load_unavailable(self) -> dict[str, list[UnavailableWindow]]:
        result: dict[str, list[UnavailableWindow]] = {}
        for row in self.conn.execute("SELECT * FROM unavailable_windows ORDER BY id"):
            key = f"{row['resource_type']}:{row['resource_id']}"
            result.setdefault(key, []).append(
                UnavailableWindow(
                    parse(row["start_ts"]), parse(row["end_ts"]), row["reason"]
                )
            )
        return result

    def commit(self) -> None:
        self.conn.commit()
