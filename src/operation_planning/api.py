"""基于标准库 http.server 的 JSON 接口。

路由：
* POST   /requests                提交需求
* GET    /requests                需求列表
* POST   /plans                   生成候选方案（body: {"request_ids": [...], "strategy": "..."}）
* GET    /plans                   方案列表
* GET    /plans/{id}              查看方案
* POST   /plans/{id}/compare      body: {"other_plan_id": "..."}，比较两个方案
* POST   /plans/{id}/lease        获取确认租约  body: {"holder": "...", "ttl_seconds": 60}
* DELETE /plans/{id}/lease        释放租约（header: X-Holder / X-Token）
* POST   /plans/{id}/confirm      确认并冻结（header: X-Holder / X-Token）
* POST   /plans/{id}/cancel       取消/部分取消（body 可带 request_ids）
* POST   /blocks                  封路或资源失效，触发局部重排
* GET    /occupancy?at=...&resource_kind=...&resource_id=...
* POST   /tick                    推进时间（测试用：进行中状态迁移、租约过期）
* POST   /recover                 重启后租约恢复
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import (
    ConflictError,
    LeaseError,
    NotFoundError,
    StateError,
    ValidationError,
)
from .service import PlanningService


def _json_response(handler: BaseHTTPRequestHandler, status: int, body: Any) -> None:
    data = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


class PlanningHandler(BaseHTTPRequestHandler):
    server_version = "OperationPlanning/1.0"

    @property
    def service(self) -> PlanningService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默
        return

    # ------------------------------------------------------------------ 工具
    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(body, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return body

    def _holder_token(self, body: dict[str, Any]) -> tuple[str, str]:
        holder = self.headers.get("X-Holder") or body.get("holder")
        token = self.headers.get("X-Token") or body.get("token")
        if not holder or not token:
            raise ValidationError("需要 holder 与 token（请求头 X-Holder/X-Token 或请求体）")
        return holder, token

    def _error(self, exc: Exception) -> None:
        status = {
            NotFoundError: 404,
            ValidationError: 400,
            ValueError: 400,
            ConflictError: 409,
            LeaseError: 423,
            StateError: 409,
        }
        code = next((s for cls, s in status.items() if isinstance(exc, cls)), 500)
        _json_response(self, code, {"error": type(exc).__name__, "message": str(exc)})

    def _handle(self, fn: Callable[[], Any]) -> None:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            self._error(exc)
            return
        _json_response(self, 200, result if result is not None else {"ok": True})

    # ------------------------------------------------------------------ 动词
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        def go() -> Any:
            if path == "/requests":
                return {"requests": self.service.list_requests()}
            if path == "/plans":
                return {"plans": self.service.list_plans()}
            if path.startswith("/plans/"):
                return self.service.get_plan(path.split("/")[2])
            if path == "/occupancy":
                return self.service.occupancy_at(
                    qs["at"][0],
                    resource_kind=qs.get("resource_kind", [None])[0],
                    resource_id=qs.get("resource_id", [None])[0],
                    include_candidate=qs.get("include_candidate", ["true"])[0].lower()
                    != "false",
                )
            raise NotFoundError(f"未知路径：{path}")

        self._handle(go)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        body = self._read_json_safe()
        if isinstance(body, Exception):
            self._error(body)
            return

        def go() -> Any:
            if path == "/requests":
                return self.service.submit_request(**{
                    k: body[k] for k in (
                        "request_id", "operation_kind", "starts_at", "ends_at",
                        "duration_minutes", "priority", "segment_ids", "energy_kwh",
                    ) if k in body
                })
            if path == "/plans":
                return self.service.generate_plan(
                    request_ids=body.get("request_ids"),
                    strategy=body.get("strategy", "priority"),
                    plan_id=body.get("plan_id"),
                )
            if path.startswith("/plans/"):
                parts = path.split("/")
                plan_id = parts[2]
                action = parts[3] if len(parts) > 3 else ""
                if action == "compare":
                    other = body.get("other_plan_id")
                    if not other:
                        raise ValidationError("需要 other_plan_id")
                    return self.service.compare_plans(plan_id, other)
                if action == "lease":
                    return self.service.acquire_lease(
                        plan_id, body.get("holder", "dispatcher"),
                        body.get("ttl_seconds"),
                    )
                if action == "confirm":
                    holder, token = self._holder_token(body)
                    return self.service.confirm_plan(plan_id, holder, token)
                if action == "cancel":
                    holder, token = self._holder_token(body)
                    return self.service.cancel_plan(
                        plan_id, holder, token, body.get("request_ids")
                    )
                raise NotFoundError(f"未知方案操作：{action}")
            if path == "/blocks":
                return self.service.report_block(
                    resource_kind=body["resource_kind"],
                    resource_id=body["resource_id"],
                    starts_at=body["starts_at"],
                    ends_at=body["ends_at"],
                    reason=body.get("reason", ""),
                )
            if path == "/tick":
                return self.service.tick(body.get("now"))
            if path == "/recover":
                return self.service.recover_leases()
            raise NotFoundError(f"未知路径：{path}")

        self._handle(go)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.rstrip("/") or "/"
        body = self._read_json_safe()
        if isinstance(body, Exception):
            self._error(body)
            return

        def go() -> Any:
            if path.startswith("/plans/") and len(path.split("/")) == 4 \
                    and path.split("/")[3] == "lease":
                holder, token = self._holder_token(body)
                return self.service.release_lease(path.split("/")[2], holder, token)
            raise NotFoundError(f"未知路径：{path}")

        self._handle(go)

    def _read_json_safe(self) -> dict[str, Any] | Exception:
        try:
            return self._read_json()
        except Exception as exc:  # noqa: BLE001
            return exc


def create_server(
    service: PlanningService, host: str = "127.0.0.1", port: int = 0
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), PlanningHandler)
    server.service = service  # type: ignore[attr-defined]
    return server


def serve_forever(service: PlanningService, host: str = "127.0.0.1",
                  port: int = 8080) -> None:  # pragma: no cover
    httpd = create_server(service, host, port)
    print(f"示范运营编排接口监听中：http://{host}:{httpd.server_address[1]}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
