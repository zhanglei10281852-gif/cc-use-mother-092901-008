"""HTTP JSON 接口测试。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from operation_planning import PlanningService  # noqa: E402
from operation_planning.api import create_server  # noqa: E402


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base = base_url

    def call(self, method: str, path: str, body: dict | None = None,
             headers: dict | None = None):
        data = None
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, headers=hdrs,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        svc = PlanningService(":memory:")
        svc.register_vehicle("V1", ["shuttle", "public-demo"], 80, 80, 0.2)
        svc.register_officer("O1", ["shuttle", "public-demo"])
        svc.register_segment("S1")
        svc.register_bay("B1", 30)
        cls.svc = svc
        cls.server = create_server(svc)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient(f"http://127.0.0.1:{cls.server.server_address[1]}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_01_full_workflow_over_http(self):
        status, body = self.api.call("POST", "/requests", {
            "request_id": "HTTP-1", "operation_kind": "shuttle",
            "starts_at": "2026-10-21T08:00:00Z", "ends_at": "2026-10-21T10:00:00Z",
            "duration_minutes": 45, "priority": 50, "segment_ids": ["S1"],
        })
        self.assertEqual(status, 200, body)

        status, plan = self.api.call("POST", "/plans", {"strategy": "priority"})
        self.assertEqual(status, 200, plan)
        plan_id = plan["plan_id"]
        self.assertEqual(plan["assignments"][0]["vehicle_id"], "V1")

        status, fetched = self.api.call("GET", f"/plans/{plan_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["plan_id"], plan_id)

        # 无租约确认 → 423。
        status, err = self.api.call("POST", f"/plans/{plan_id}/confirm",
                                    {"holder": "op-a", "token": "x"})
        self.assertEqual(status, 423)
        self.assertEqual(err["error"], "LeaseError")

        status, lease = self.api.call("POST", f"/plans/{plan_id}/lease",
                                      {"holder": "op-a"})
        self.assertEqual(status, 200)

        status, confirmed = self.api.call(
            "POST", f"/plans/{plan_id}/confirm", {},
            {"X-Holder": "op-a", "X-Token": lease["token"]},
        )
        self.assertEqual(status, 200, confirmed)
        self.assertEqual(confirmed["status"], "confirmed")

    def test_02_validation_error_returns_400(self):
        status, body = self.api.call("POST", "/requests", {
            "request_id": "BAD", "operation_kind": "shuttle",
            "starts_at": "2026-10-21T09:00:00Z", "ends_at": "2026-10-21T08:00:00Z",
            "duration_minutes": 45, "priority": 50,
        })
        self.assertEqual(status, 400)
        self.assertIn("message", body)

    def test_03_occupancy_query(self):
        status, body = self.api.call(
            "GET", "/occupancy?at=2026-10-21T08:30:00Z&resource_kind=segment&resource_id=S1"
        )
        self.assertEqual(status, 200)
        self.assertTrue(any(o["request_id"] == "HTTP-1" for o in body["occupancy"]))

    def test_04_block_triggers_repair(self):
        # 新增一个未来任务并生成候选，再封路。
        status, _ = self.api.call("POST", "/requests", {
            "request_id": "HTTP-2", "operation_kind": "shuttle",
            "starts_at": "2026-10-21T13:00:00Z", "ends_at": "2026-10-21T15:00:00Z",
            "duration_minutes": 60, "priority": 50, "segment_ids": ["S1"],
        })
        self.assertEqual(status, 200)
        status, plan2 = self.api.call("POST", "/plans", {"strategy": "priority"})
        self.assertEqual(status, 200, plan2)
        status, body = self.api.call("POST", "/blocks", {
            "resource_kind": "segment", "resource_id": "S1",
            "starts_at": "2026-10-21T12:00:00Z", "ends_at": "2026-10-21T18:00:00Z",
            "reason": "管制",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["affected_requests"], ["HTTP-2"])
        self.assertIsNotNone(body["repair_plan"])


if __name__ == "__main__":
    unittest.main()
