"""SQLite 持久化层。

所有时间以 ISO-8601 文本落库；占用与租约在同一事务内更新，
保证确认动作的原子性，并支持进程重启后的租约恢复。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .domain import (
    BlockedWindow,
    ChargingBay,
    Interval,
    RouteSegment,
    SafetyOfficer,
    TaskDemand,
    Vehicle,
    to_iso,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    capabilities TEXT NOT NULL,
    capacity_kwh REAL NOT NULL,
    initial_charge_kwh REAL NOT NULL,
    consumption_kwh_per_minute REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS officers (
    officer_id TEXT PRIMARY KEY,
    qualifications TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    segment_id TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS bays (
    bay_id TEXT PRIMARY KEY,
    power_kw REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS demands (
    request_id TEXT PRIMARY KEY,
    operation_kind TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    segment_ids TEXT NOT NULL,
    energy_kwh TEXT,
    state TEXT NOT NULL DEFAULT 'active'
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS plan_requests (
    plan_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    PRIMARY KEY (plan_id, request_id)
);
CREATE TABLE IF NOT EXISTS occupancy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT,
    vehicle_id TEXT,
    officer_id TEXT,
    bay_id TEXT,
    kind TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    segment_ids TEXT NOT NULL DEFAULT '[]',
    kwh REAL NOT NULL DEFAULT 0,
    state TEXT NOT NULL,
    plan_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS leases (
    plan_id TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    token TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class Repository:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self._path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(demands)")}
        if "state" not in cols:
            self.conn.execute("ALTER TABLE demands ADD COLUMN state TEXT NOT NULL DEFAULT 'active'")

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------------ 资源
    def upsert_vehicle(self, v: Vehicle) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO vehicles VALUES (?,?,?,?,?) "
                "ON CONFLICT(vehicle_id) DO UPDATE SET "
                "capabilities=excluded.capabilities, capacity_kwh=excluded.capacity_kwh, "
                "initial_charge_kwh=excluded.initial_charge_kwh, "
                "consumption_kwh_per_minute=excluded.consumption_kwh_per_minute",
                (v.vehicle_id, json.dumps(sorted(v.capabilities), ensure_ascii=False),
                 v.capacity_kwh, v.initial_charge_kwh, v.consumption_kwh_per_minute),
            )
            self.conn.commit()

    def upsert_officer(self, o: SafetyOfficer) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO officers VALUES (?,?) ON CONFLICT(officer_id) DO UPDATE SET "
                "qualifications=excluded.qualifications",
                (o.officer_id, json.dumps(sorted(o.qualifications), ensure_ascii=False)),
            )
            self.conn.commit()

    def upsert_segment(self, s: RouteSegment) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO segments VALUES (?) ON CONFLICT(segment_id) DO NOTHING",
                (s.segment_id,),
            )
            self.conn.commit()

    def upsert_bay(self, b: ChargingBay) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO bays VALUES (?,?) ON CONFLICT(bay_id) DO UPDATE SET "
                "power_kw=excluded.power_kw",
                (b.bay_id, b.power_kw),
            )
            self.conn.commit()

    def load_vehicles(self) -> tuple[Vehicle, ...]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM vehicles ORDER BY vehicle_id").fetchall()
        return tuple(
            Vehicle(r["vehicle_id"], frozenset(json.loads(r["capabilities"])),
                    r["capacity_kwh"], r["initial_charge_kwh"],
                    r["consumption_kwh_per_minute"])
            for r in rows
        )

    def load_officers(self) -> tuple[SafetyOfficer, ...]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM officers ORDER BY officer_id").fetchall()
        return tuple(
            SafetyOfficer(r["officer_id"], frozenset(json.loads(r["qualifications"])))
            for r in rows
        )

    def load_segments(self) -> tuple[RouteSegment, ...]:
        with self.lock:
            rows = self.conn.execute("SELECT segment_id FROM segments ORDER BY segment_id").fetchall()
        return tuple(RouteSegment(r["segment_id"]) for r in rows)

    def load_bays(self) -> tuple[ChargingBay, ...]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM bays ORDER BY bay_id").fetchall()
        return tuple(ChargingBay(r["bay_id"], r["power_kw"]) for r in rows)

    # ------------------------------------------------------------------ 需求
    def insert_demand(self, d: TaskDemand) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO demands (request_id,operation_kind,starts_at,ends_at,"
                "duration_minutes,priority,segment_ids,energy_kwh,state) "
                "VALUES (?,?,?,?,?,?,?,?, 'active')",
                (d.request_id, d.operation_kind, to_iso(d.window.start), to_iso(d.window.end),
                 d.duration_minutes, d.priority,
                 json.dumps(list(d.segment_ids), ensure_ascii=False),
                 None if d.energy_kwh is None else str(d.energy_kwh)),
            )
            self.conn.commit()

    def demand_exists(self, request_id: str) -> bool:
        with self.lock:
            row = self.conn.execute(
                "SELECT 1 FROM demands WHERE request_id=?", (request_id,)
            ).fetchone()
        return row is not None

    def demand_states(self) -> dict[str, str]:
        with self.lock:
            rows = self.conn.execute("SELECT request_id, state FROM demands").fetchall()
        return {r["request_id"]: r["state"] for r in rows}

    def load_demands(self, request_ids: Iterable[str] | None = None,
                     active_only: bool = False) -> tuple[TaskDemand, ...]:
        where = " WHERE state='active'" if active_only else ""
        with self.lock:
            if request_ids is None:
                rows = self.conn.execute(
                    f"SELECT * FROM demands{where} ORDER BY request_id"
                ).fetchall()
            else:
                ids = list(request_ids)
                if not ids:
                    return ()
                placeholders = ",".join("?" * len(ids))
                extra = " AND state='active'" if active_only else ""
                rows = self.conn.execute(
                    f"SELECT * FROM demands WHERE request_id IN ({placeholders}){extra} "
                    f"ORDER BY request_id",
                    ids,
                ).fetchall()
        out = []
        for r in rows:
            out.append(TaskDemand(
                r["request_id"], r["operation_kind"],
                Interval(datetime.fromisoformat(r["starts_at"]),
                         datetime.fromisoformat(r["ends_at"])),
                r["duration_minutes"], r["priority"],
                tuple(json.loads(r["segment_ids"])),
                None if r["energy_kwh"] is None else float(r["energy_kwh"]),
            ))
        return tuple(out)

    # ------------------------------------------------------------------ 方案
    def save_plan(self, plan_id: str, strategy: str, status: str,
                  request_ids: Iterable[str], payload: dict[str, Any]) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?)",
                (plan_id, strategy, status,
                 to_iso(datetime.now(timezone.utc)),
                 json.dumps(payload, ensure_ascii=False)),
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO plan_requests VALUES (?,?)",
                [(plan_id, rid) for rid in request_ids],
            )
            self.conn.commit()

    def set_plan_status(self, plan_id: str, status: str) -> None:
        with self.lock:
            self.conn.execute("UPDATE plans SET status=? WHERE plan_id=?",
                              (status, plan_id))
            self.conn.commit()

    def load_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        payload["plan_id"] = row["plan_id"]
        payload["strategy"] = row["strategy"]
        payload["status"] = row["status"]
        payload["created_at"] = row["created_at"]
        return payload

    def list_plans(self) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT plan_id FROM plans ORDER BY created_at DESC"
            ).fetchall()
        return [p for p in (self.load_plan(r["plan_id"]) for r in rows) if p]

    def latest_candidate_plan_for(self, request_id: str) -> dict[str, Any] | None:
        """该需求最新的候选方案（用于生成时的软占用基线）。"""
        with self.lock:
            row = self.conn.execute(
                """
                SELECT p.plan_id FROM plans p
                JOIN plan_requests pr ON pr.plan_id = p.plan_id
                WHERE pr.request_id=? AND p.status='candidate'
                ORDER BY p.created_at DESC LIMIT 1
                """,
                (request_id,),
            ).fetchone()
        return self.load_plan(row["plan_id"]) if row else None

    # ------------------------------------------------------------------ 占用
    def add_occupancy(self, *, request_id: str | None, vehicle_id: str | None,
                      officer_id: str | None, bay_id: str | None, kind: str,
                      window: Interval, segment_ids: tuple[str, ...], kwh: float,
                      state: str, plan_id: str) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO occupancy (request_id,vehicle_id,officer_id,bay_id,kind,"
                "starts_at,ends_at,segment_ids,kwh,state,plan_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (request_id, vehicle_id, officer_id, bay_id, kind,
                 to_iso(window.start), to_iso(window.end),
                 json.dumps(list(segment_ids), ensure_ascii=False), kwh, state, plan_id),
            )

    def load_occupancy(self) -> tuple[sqlite3.Row, ...]:
        with self.lock:
            return tuple(self.conn.execute("SELECT * FROM occupancy").fetchall())

    def occupancy_at(self, at: datetime, resource_kind: str | None,
                     resource_id: str | None) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM occupancy").fetchall()
        iso = to_iso(at)
        out = []
        kind_col = {"segment": None, "vehicle": "vehicle_id",
                    "officer": "officer_id", "bay": "bay_id"}.get(resource_kind or "", "")
        for r in rows:
            if not (r["starts_at"] <= iso < r["ends_at"]):
                continue
            if resource_kind == "segment":
                if resource_id and resource_id not in json.loads(r["segment_ids"]):
                    continue
            elif kind_col:
                if resource_id and r[kind_col] != resource_id:
                    continue
            out.append({
                "request_id": r["request_id"],
                "kind": r["kind"],
                "vehicle_id": r["vehicle_id"],
                "officer_id": r["officer_id"],
                "bay_id": r["bay_id"],
                "segment_ids": json.loads(r["segment_ids"]),
                "start": r["starts_at"],
                "end": r["ends_at"],
                "state": r["state"],
                "plan_id": r["plan_id"],
                "kwh": r["kwh"],
            })
        return out

    def delete_occupancy_for_request(self, request_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM occupancy WHERE request_id=?", (request_id,))
            self.conn.commit()

    def begin_immediate(self) -> None:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        with self.lock:
            self.conn.commit()

    def rollback(self) -> None:
        with self.lock:
            self.conn.rollback()

    # ------------------------------------------------------------------ 封停
    def add_block(self, block: BlockedWindow) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO blocks (resource_kind,resource_id,starts_at,ends_at,reason) "
                "VALUES (?,?,?,?,?)",
                (block.resource_kind, block.resource_id,
                 to_iso(block.window.start), to_iso(block.window.end), block.reason),
            )
            self.conn.commit()

    def load_blocks(self) -> tuple[BlockedWindow, ...]:
        with self.lock:
            rows = self.conn.execute("SELECT * FROM blocks").fetchall()
        return tuple(
            BlockedWindow(
                r["resource_kind"], r["resource_id"],
                Interval(datetime.fromisoformat(r["starts_at"]),
                         datetime.fromisoformat(r["ends_at"])),
                r["reason"],
            )
            for r in rows
        )

    # ------------------------------------------------------------------ 租约
    def save_lease(self, plan_id: str, holder: str, token: str,
                   acquired_at: datetime, expires_at: datetime) -> None:
        with self.lock:
            self.conn.execute(
                "INSERT INTO leases VALUES (?,?,?,?,?) "
                "ON CONFLICT(plan_id) DO UPDATE SET holder=excluded.holder, "
                "token=excluded.token, acquired_at=excluded.acquired_at, "
                "expires_at=excluded.expires_at",
                (plan_id, holder, token, to_iso(acquired_at), to_iso(expires_at)),
            )
            self.conn.commit()

    def load_lease(self, plan_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT * FROM leases WHERE plan_id=?", (plan_id,)
            ).fetchone()
        return dict(row) if row else None

    def delete_lease(self, plan_id: str) -> None:
        with self.lock:
            self.conn.execute("DELETE FROM leases WHERE plan_id=?", (plan_id,))
            self.conn.commit()

    def purge_expired_leases(self, now: datetime) -> list[str]:
        """删除已到期租约（重启恢复时也调用），返回被清理的方案编号。"""
        with self.lock:
            rows = self.conn.execute(
                "SELECT plan_id FROM leases WHERE expires_at <= ?", (to_iso(now),)
            ).fetchall()
            ids = [r["plan_id"] for r in rows]
            if ids:
                self.conn.executemany("DELETE FROM leases WHERE plan_id=?",
                                      [(i,) for i in ids])
                self.conn.commit()
        return ids
