"""HTTP 接口：提交需求、比较方案、确认、取消、占用查询、封路上报。

仅依赖标准库，使用 :class:`http.server.ThreadingHTTPServer`；业务串行化
由 :class:`PlanningService` 内部锁与 SQLite 事务保证。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from .models import (
    ChargingBay,
    Leg,
    RouteSegment,
    SafetyOfficer,
    TaskDemand,
    UnavailableWindow,
    Vehicle,
)
from .service import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    PlanningError,
    PlanningService,
)


def parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("时间必须携带时区偏移，例如 2026-10-21T20:00:00+00:00")
    return parsed


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (frozenset, set)):
        return sorted(value)
    raise TypeError(f"不可序列化的类型 {type(value)!r}")


def dumps(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")


class PlanningHTTPHandler(BaseHTTPRequestHandler):
    service: PlanningService  # 由工厂函数注入到类属性

    server_version = "DemoOpsOrchestrator/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    def _send(self, status: int, payload: Any) -> None:
        body = dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlanningError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise PlanningError("请求体必须是 JSON 对象")
        return data

    def _error_status(self, exc: Exception) -> int:
        if isinstance(exc, NotFoundError):
            return HTTPStatus.NOT_FOUND
        if isinstance(exc, (ConflictError, InvalidStateError)):
            return HTTPStatus.CONFLICT
        if isinstance(exc, (ValueError, PlanningError)):
            return HTTPStatus.BAD_REQUEST
        return HTTPStatus.INTERNAL_SERVER_ERROR

    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            route = self._match_get(parsed.path, query)
            if route is None:
                self._send(HTTPStatus.NOT_FOUND, {"error": "未知接口", "path": parsed.path})
                return
            self._send(HTTPStatus.OK, route())
        except Exception as exc:  # noqa: BLE001
            self._send(self._error_status(exc), self._error_body(exc))

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            route = self._match_post(parsed.path)
            if route is None:
                self._send(HTTPStatus.NOT_FOUND, {"error": "未知接口", "path": parsed.path})
                return
            self._send(HTTPStatus.OK, route(self._read_json()))
        except Exception as exc:  # noqa: BLE001
            self._send(self._error_status(exc), self._error_body(exc))

    def _error_body(self, exc: Exception) -> dict[str, Any]:
        body: dict[str, Any] = {"error": str(exc)}
        if isinstance(exc, ConflictError):
            body["conflicts"] = exc.conflicts
        return body

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def _match_get(self, path: str, query: dict[str, list[str]]) -> Optional[Callable[[], Any]]:
        if path == "/health":
            return lambda: {"status": "ok"}

        if path == "/api/plans/compare":
            ids = query.get("ids", [""])[0].split(",")
            ids = [item.strip() for item in ids if item.strip()]
            if not ids:
                raise PlanningError("compare 需要 ids 查询参数，逗号分隔")
            return lambda: self.service.compare(ids)
        if path == "/api/occupancy":
            at_raw = query.get("at", [None])[0]
            at = parse_dt(at_raw) if at_raw else datetime.now().astimezone()
            include = query.get("include_candidates", ["true"])[0].lower() != "false"
            return lambda: self.service.occupancy(at, include_candidates=include)
        match = re.fullmatch(r"/api/plans/([^/]+)", path)
        if match:
            return lambda: self.service.get_plan(match.group(1))
        match = re.fullmatch(r"/api/requests/([^/]+)", path)
        if match:
            return lambda: self.service.get_request(match.group(1))
        return None

    def _match_post(self, path: str) -> Optional[Callable[[dict[str, Any]], Any]]:
        routes = {
            "/api/vehicles": self._register_vehicle,
            "/api/officers": self._register_officer,
            "/api/bays": self._register_bay,
            "/api/segments": self._register_segment,
            "/api/demands": self._submit_demand,
            "/api/unavailable": self._report_unavailable,
            "/api/replan": lambda body: self.service.replan_affected(),
        }
        if path in routes:
            return routes[path]
        match = re.fullmatch(r"/api/plans/([^/]+)/confirm", path)
        if match:
            plan_id = match.group(1)
            return lambda body: self.service.confirm(
                plan_id, preempt=bool(body.get("preempt", False))
            )
        match = re.fullmatch(r"/api/plans/([^/]+)/cancel", path)
        if match:
            plan_id = match.group(1)
            return lambda body: self.service.cancel_plan(
                plan_id, body.get("reason", "值班人员取消")
            )
        match = re.fullmatch(r"/api/plans/([^/]+)/partial-cancel", path)
        if match:
            plan_id = match.group(1)
            return self._partial_cancel(plan_id)
        return None

    # ------------------------------------------------------------------
    # 请求体解析
    # ------------------------------------------------------------------
    def _register_vehicle(self, body: dict[str, Any]) -> Any:
        vehicle = Vehicle(
            body["vehicle_id"], body.get("name", body["vehicle_id"]),
            frozenset(body.get("capabilities", [])),
            float(body["soc_kwh"]), float(body["capacity_kwh"]),
        )
        self.service.register_vehicle(vehicle)
        return {"registered": vehicle.vehicle_id}

    def _register_officer(self, body: dict[str, Any]) -> Any:
        officer = SafetyOfficer(
            body["officer_id"], body.get("name", body["officer_id"]),
            frozenset(body.get("qualifications", [])),
        )
        self.service.register_officer(officer)
        return {"registered": officer.officer_id}

    def _register_bay(self, body: dict[str, Any]) -> Any:
        bay = ChargingBay(
            body["bay_id"], body.get("name", body["bay_id"]),
            float(body["power_kw"]),
        )
        self.service.register_bay(bay)
        return {"registered": bay.bay_id}

    def _register_segment(self, body: dict[str, Any]) -> Any:
        segment = RouteSegment(body["segment_id"], body.get("name", body["segment_id"]))
        self.service.register_segment(segment)
        return {"registered": segment.segment_id}

    def _submit_demand(self, body: dict[str, Any]) -> Any:
        legs = tuple(
            Leg(item["segment_id"], parse_dt(item["enters_at"]),
                parse_dt(item["exits_at"]))
            for item in body.get("legs", [])
        )
        demand = TaskDemand(
            body["request_id"], body["kind"], int(body.get("priority", 50)),
            parse_dt(body["start"]), parse_dt(body["end"]), legs,
            float(body.get("energy_required_kwh", 0.0)),
        )
        return self.service.submit_demand(demand)

    def _report_unavailable(self, body: dict[str, Any]) -> Any:
        window = UnavailableWindow(
            parse_dt(body["starts_at"]), parse_dt(body["ends_at"]),
            body.get("reason", "资源不可用"),
        )
        return self.service.report_unavailable(
            body["resource_type"], body["resource_id"], window
        )

    def _partial_cancel(self, plan_id: str) -> Callable[[dict[str, Any]], Any]:
        def handle(body: dict[str, Any]) -> Any:
            return self.service.partial_cancel(
                plan_id,
                body["resource_type"],
                body["resource_id"],
                body.get("reason", "部分取消"),
            )
        return handle


def build_server(host: str, port: int, service: PlanningService) -> ThreadingHTTPServer:
    handler = type(
        "BoundPlanningHTTPHandler",
        (PlanningHTTPHandler,),
        {"service": service},
    )
    server = ThreadingHTTPServer((host, port), handler)
    return server


def serve(host: str = "127.0.0.1", port: int = 8000,
          db_path: str = ":memory:") -> None:  # pragma: no cover
    service = PlanningService.from_path(db_path)
    server = build_server(host, port, service)
    print(f"示范运营编排服务已启动：http://{host}:{port}")
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="示范运营资源编排 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=":memory:", help="SQLite 路径，默认内存库")
    args = parser.parse_args()
    serve(args.host, args.port, args.db)


if __name__ == "__main__":  # pragma: no cover
    main()
