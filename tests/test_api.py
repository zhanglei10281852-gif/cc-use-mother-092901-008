"""HTTP 接口端到端冒烟测试（标准库 http.server，随机端口）。"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from operation_planning.api import build_server
from operation_planning.service import PlanningService

UTC = timezone.utc
BASE = datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


class HttpApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PlanningService.from_path(":memory:")
        self.server: ThreadingHTTPServer = build_server(
            "127.0.0.1", 0, self.service
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.service.close()

    def call(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_workflow_over_http(self) -> None:
        self.call("POST", "/api/segments", {"segment_id": "S1", "name": "迎宾大道"})
        self.call("POST", "/api/vehicles", {
            "vehicle_id": "V1", "capabilities": ["shuttle"],
            "soc_kwh": 80, "capacity_kwh": 100,
        })
        self.call("POST", "/api/officers", {
            "officer_id": "O1", "qualifications": ["shuttle"],
        })
        self.call("POST", "/api/bays", {"bay_id": "B1", "power_kw": 60})

        end = BASE + timedelta(hours=1)
        status, submitted = self.call("POST", "/api/demands", {
            "request_id": "REQ-1", "kind": "shuttle", "priority": 50,
            "start": BASE.isoformat(), "end": end.isoformat(),
            "legs": [{"segment_id": "S1", "enters_at": BASE.isoformat(),
                      "exits_at": end.isoformat()}],
            "energy_required_kwh": 20,
        })
        self.assertEqual(status, 200)
        plan_id = submitted["options"][0]["plan_id"]
        self.assertTrue(submitted["options"][0]["rationale"])

        status, comparison = self.call(
            "GET", f"/api/plans/compare?ids={plan_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(comparison["recommended"], plan_id)

        status, confirmed = self.call(
            "POST", f"/api/plans/{plan_id}/confirm", {}
        )
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["plan"]["state"], "confirmed")

        # 重复确认同一计划 → 409。
        status, body = self.call(
            "POST", f"/api/plans/{plan_id}/confirm", {}
        )
        self.assertEqual(status, 409)
        self.assertIn("error", body)

        # 某时点资源占用。
        at = (BASE + timedelta(minutes=30)).isoformat()
        status, occupancy = self.call("GET", f"/api/occupancy?at={urllib.parse.quote(at)}")
        self.assertEqual(status, 200)
        self.assertIn("vehicle:V1", occupancy["frozen"])

        # 未知计划 → 404。
        status, _ = self.call("GET", "/api/plans/nope")
        self.assertEqual(status, 404)

    def test_preempt_and_closure_flow_over_http(self) -> None:
        self.call("POST", "/api/segments", {"segment_id": "S1"})
        self.call("POST", "/api/vehicles", {
            "vehicle_id": "V1", "capabilities": ["shuttle"],
            "soc_kwh": 80, "capacity_kwh": 100})
        self.call("POST", "/api/officers", {
            "officer_id": "O1", "qualifications": ["shuttle"]})
        self.call("POST", "/api/bays", {"bay_id": "B1", "power_kw": 60})
        end = BASE + timedelta(hours=1)
        leg = {"segment_id": "S1", "enters_at": BASE.isoformat(),
               "exits_at": end.isoformat()}

        def submit(rid: str, priority: int):
            return self.call("POST", "/api/demands", {
                "request_id": rid, "kind": "shuttle", "priority": priority,
                "start": BASE.isoformat(), "end": end.isoformat(),
                "legs": [leg], "energy_required_kwh": 20,
            })[1]

        low = submit("REQ-LOW", 20)
        vip = submit("REQ-VIP", 99)
        self.assertTrue(vip["contending_candidates"])

        # 不带 preempt 的确认被拒（409，含受影响方与替代建议）。
        status, body = self.call(
            "POST", f"/api/plans/{vip['options'][0]['plan_id']}/confirm", {}
        )
        self.assertEqual(status, 409)
        self.assertTrue(body["conflicts"])

        status, outcome = self.call(
            "POST", f"/api/plans/{vip['options'][0]['plan_id']}/confirm",
            {"preempt": True},
        )
        self.assertEqual(status, 200)
        self.assertEqual(outcome["plan"]["displaced_requests"], ["REQ-LOW"])

        status, cancelled = self.call(
            "GET", f"/api/plans/{low['options'][0]['plan_id']}"
        )
        self.assertEqual(cancelled["state"], "cancelled")
        self.assertTrue(cancelled["amendments"])


if __name__ == "__main__":
    unittest.main()
