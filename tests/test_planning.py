"""端到端业务测试：候选生成、插单挤占、确认冻结、取消、封路重排、时点占用。"""

import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from operation_planning import (  # noqa: E402
    ConflictError,
    LeaseError,
    PlanningService,
    Repository,
    StateError,
)

UTC = timezone.utc
T = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731


def build_service(path: str = ":memory:",
                  now: datetime | None = None) -> PlanningService:
    svc = PlanningService(path, now=now)
    svc.register_vehicle("V1", ["shuttle", "public-demo"], 80, 80, 0.2)
    svc.register_vehicle("V2", ["shuttle", "enterprise-test"], 60, 60, 0.3)
    svc.register_officer("O1", ["shuttle", "public-demo"])
    svc.register_officer("O2", ["shuttle", "enterprise-test"])
    svc.register_segment("S1")
    svc.register_segment("S2")
    svc.register_segment("S3")
    svc.register_bay("B1", 30)
    svc.register_bay("B2", 20)
    return svc


class CandidateGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()

    def test_candidate_is_explainable_and_covers_all_resources(self):
        self.svc.submit_request(
            "R1", "shuttle", "2026-10-21T08:00:00Z", "2026-10-21T10:00:00Z",
            45, 50, ["S1"],
        )
        plan = self.svc.generate_plan()
        self.assertEqual(plan["status"], "candidate")
        (a,) = plan["assignments"]
        self.assertEqual(a["vehicle_id"], "V1")
        self.assertEqual(a["officer_id"], "O1")
        self.assertIn("区段 S1", a["rationale"])
        self.assertIn("无需补能", a["rationale"])

    def test_charging_session_is_inserted_before_trip(self):
        svc = PlanningService(":memory:")
        svc.register_vehicle("V1", ["shuttle"], 80, 10, 0.2)
        svc.register_officer("O1", ["shuttle"])
        svc.register_segment("S1")
        svc.register_bay("B1", 20)
        svc.submit_request(
            "R1", "shuttle", "2026-10-21T10:00:00Z", "2026-10-21T12:00:00Z",
            60, 50, ["S1"],
        )
        plan = svc.generate_plan()
        (charge,) = plan["charging"]
        self.assertEqual(charge["bay_id"], "B1")
        self.assertLessEqual(charge["end"], plan["assignments"][0]["start"])
        # 12kWh 的行程、初始 10kWh → 至少补 2kWh，且不超过电池容量。
        self.assertGreaterEqual(charge["kwh"], 2 - 1e-6)
        self.assertLessEqual(charge["kwh"], 70)

    def test_unscheduled_request_has_reason_and_alternative(self):
        # 单车辆单安全员单区段，两个互不兼容的长时间需求。
        svc = PlanningService(":memory:")
        svc.register_vehicle("V1", ["shuttle"], 80, 80, 0.2)
        svc.register_officer("O1", ["shuttle"])
        svc.register_segment("S1")
        svc.register_bay("B1", 30)
        svc.submit_request(
            "R1", "shuttle", "2026-10-21T08:00:00Z", "2026-10-21T11:00:00Z",
            120, 50, ["S1"],
        )
        svc.submit_request(
            "R2", "shuttle", "2026-10-21T08:30:00Z", "2026-10-21T10:30:00Z",
            120, 60, ["S1"],
        )
        plan = svc.generate_plan()
        self.assertEqual(len(plan["assignments"]), 1)
        bad = next(u for u in plan["unscheduled"] if u["request_id"] == "R1")
        self.assertIn(bad["reason_code"], {"conflict", "closed"})
        self.assertTrue(bad["blocked_by"])
        self.assertTrue(bad["alternatives"])
        alt = bad["alternatives"][0]
        self.assertFalse(alt["within_window"])
        self.assertGreaterEqual(alt["start"], plan["assignments"][0]["end"])


class CrossMidnightTests(unittest.TestCase):
    def test_trip_spanning_midnight_occupies_segment_across_date_boundary(self):
        svc = build_service()
        svc.submit_request(
            "NIGHT-1", "shuttle", "2026-10-21T23:00:00Z", "2026-10-22T02:00:00Z",
            150, 50, ["S1"],
        )
        plan = svc.generate_plan()
        (a,) = plan["assignments"]
        self.assertEqual(a["start"], "2026-10-21T23:00:00+00:00")
        self.assertEqual(a["end"], "2026-10-22T01:30:00+00:00")
        # 午夜前后两个时点都应被占用。
        before = svc.occupancy_at("2026-10-21T23:30:00Z", "segment", "S1")
        after = svc.occupancy_at("2026-10-22T00:30:00Z", "segment", "S1")
        self.assertEqual(len(before["occupancy"]), 1)
        self.assertEqual(len(after["occupancy"]), 1)
        # 半开区间：结束时点不再占用。
        ended = svc.occupancy_at("2026-10-22T01:30:00Z", "segment", "S1")
        self.assertEqual(ended["occupancy"], [])

    def test_two_cross_midnight_trips_use_disjoint_resources(self):
        svc = build_service()
        svc.submit_request(
            "N1", "shuttle", "2026-10-21T23:30:00Z", "2026-10-22T01:30:00Z",
            90, 50, ["S1"],
        )
        svc.submit_request(
            "N2", "shuttle", "2026-10-21T23:30:00Z", "2026-10-22T01:30:00Z",
            90, 50, ["S2"],
        )
        plan = svc.generate_plan()
        self.assertEqual(len(plan["assignments"]), 2)
        vehicles = {a["vehicle_id"] for a in plan["assignments"]}
        officers = {a["officer_id"] for a in plan["assignments"]}
        self.assertEqual(len(vehicles), 2)
        self.assertEqual(len(officers), 2)


class PriorityInsertionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.svc.submit_request(
            "LOW", "shuttle", "2026-10-21T09:00:00Z", "2026-10-21T12:00:00Z",
            60, 30, ["S1"],
        )
        self.first = self.svc.generate_plan()
        lease = self.svc.acquire_lease(self.first["plan_id"], "duty")
        self.svc.confirm_plan(self.first["plan_id"], "duty", lease["token"])

    def test_high_priority_insertion_reports_affected_party_and_suggestion(self):
        # 已确认的 LOW 不可挤占；插单时间窗与之错开时不会互相影响。
        self.svc.submit_request(
            "VIP", "shuttle", "2026-10-21T11:00:00Z", "2026-10-21T13:00:00Z",
            60, 95, ["S1"],
        )
        plan = self.svc.generate_plan()
        vip = next(a for a in plan["assignments"] if a["request_id"] == "VIP")
        # 已确认 LOW 为硬约束，VIP 只能排在其结束之后。
        self.assertGreaterEqual(vip["start"], "2026-10-21T10:00:00+00:00")
        # 已确认行程仍然保留。
        occ = self.svc.occupancy_at("2026-10-21T09:30:00Z", include_candidate=False)
        self.assertEqual([o["request_id"] for o in occ["occupancy"]], ["LOW"])

    def test_preemption_only_targets_unconfirmed_plans(self):
        svc = build_service()
        svc.submit_request(
            "LOW", "shuttle", "2026-10-21T09:00:00Z", "2026-10-21T12:00:00Z",
            30, 20, ["S1"],
        )
        svc.generate_plan()  # 不确认 → 候选占用可被挤占
        svc.submit_request(
            "VIP", "shuttle", "2026-10-21T09:00:00Z", "2026-10-21T10:00:00Z",
            30, 99, ["S1"],
        )
        plan = svc.generate_plan()
        displaced = {d["request_id"]: d for d in plan["displaced"]}
        self.assertIn("LOW", displaced)
        self.assertEqual(displaced["LOW"]["preempted_by"], ["VIP"])
        # LOW 被级联重排到 VIP 之后。
        self.assertTrue(displaced["LOW"]["resolved"])
        order = [a["request_id"] for a in sorted(
            plan["assignments"], key=lambda a: a["start"])]
        self.assertEqual(order, ["VIP", "LOW"])

    def test_high_priority_cannot_displace_confirmed_trip(self):
        # LOW 已确认且与 VIP 同窗、单车单安全员单区段 → VIP 无法落位，
        # 必须返回阻挡方（LOW）与窗口外替代建议，且 LOW 不受影响。
        self.svc.submit_request(
            "VIP", "shuttle", "2026-10-21T09:30:00Z", "2026-10-21T10:00:00Z",
            30, 99, ["S1"],
        )
        plan = self.svc.generate_plan()
        vip = next(u for u in plan["unscheduled"] if u["request_id"] == "VIP")
        self.assertIn(vip["reason_code"], {"conflict", "closed", "resource_down"})
        self.assertTrue(any("LOW" in b for b in vip["blocked_by"]))
        self.assertTrue(vip["alternatives"])
        # 已确认的 LOW 原封不动。
        occ = self.svc.occupancy_at("2026-10-21T09:45:00Z", include_candidate=False)
        self.assertEqual([o["request_id"] for o in occ["occupancy"]], ["LOW"])
        self.assertEqual(plan["displaced"], [])

    def test_preempted_request_without_slots_is_reported_with_alternative(self):
        svc = build_service()
        svc.submit_request(
            "LOW", "shuttle", "2026-10-21T09:00:00Z", "2026-10-21T10:00:00Z",
            60, 20, ["S1"],
        )
        svc.generate_plan()  # 仅候选，未确认
        svc.submit_request(
            "VIP", "shuttle", "2026-10-21T09:00:00Z", "2026-10-21T12:00:00Z",
            60, 99, ["S1"],
        )
        plan = svc.generate_plan()
        # VIP 占 09:00–10:00；LOW 的窗口 09:00–10:00 已无 60 分钟空位 → 无法重排。
        displaced = {d["request_id"]: d for d in plan["displaced"]}
        self.assertIn("LOW", displaced)
        self.assertFalse(displaced["LOW"]["resolved"])
        self.assertEqual(displaced["LOW"]["preempted_by"], ["VIP"])
        self.assertIsNotNone(displaced["LOW"]["alternative"])
        bad = next(u for u in plan["unscheduled"] if u["request_id"] == "LOW")
        self.assertEqual(bad["preempted_by"], ["VIP"])


class ConfirmationLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service()
        self.svc.submit_request(
            "R1", "shuttle", "2026-10-21T08:00:00Z", "2026-10-21T10:00:00Z",
            45, 50, ["S1"],
        )
        self.plan = self.svc.generate_plan()

    def test_confirm_requires_lease(self):
        with self.assertRaises(LeaseError):
            self.svc.confirm_plan(self.plan["plan_id"], "duty", "bogus-token")

    def test_second_holder_is_blocked_until_lease_expires(self):
        self.svc.acquire_lease(self.plan["plan_id"], "甲", ttl_seconds=60)
        with self.assertRaises(LeaseError):
            self.svc.acquire_lease(self.plan["plan_id"], "乙")
        # 租约到期后乙可接手。
        self.svc.tick("2026-10-22T00:00:00Z")
        lease = self.svc.acquire_lease(self.plan["plan_id"], "乙")
        result = self.svc.confirm_plan(self.plan["plan_id"], "乙", lease["token"])
        self.assertEqual(result["status"], "confirmed")

    def test_concurrent_confirmation_only_one_wins(self):
        outcomes: list[str] = []
        lock = threading.Lock()
        lease = self.svc.acquire_lease(self.plan["plan_id"], "值班台",
                                       ttl_seconds=300)
        barrier = threading.Barrier(2)

        def attempt(who: str) -> None:
            barrier.wait(timeout=5)  # 两线程同时进入确认
            try:
                self.svc.confirm_plan(
                    self.plan["plan_id"], "值班台", lease["token"]
                )
                result = "ok"
            except Exception as exc:  # noqa: BLE001
                result = type(exc).__name__
            with lock:
                outcomes.append(f"{who}:{result}")

        t1 = threading.Thread(target=attempt, args=("甲",))
        t2 = threading.Thread(target=attempt, args=("乙",))
        t1.start(); t2.start(); t1.join(); t2.join()
        oks = [o for o in outcomes if o.endswith(":ok")]
        failures = [o for o in outcomes if not o.endswith(":ok")]
        self.assertEqual(len(oks), 1, outcomes)
        self.assertTrue(all(f.endswith("LeaseError") or f.endswith("StateError")
                            for f in failures), outcomes)

    def test_lease_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ops.db"
            base_now = T("2026-10-21T07:00:00Z")
            svc = PlanningService(str(db), lease_ttl_seconds=600, now=base_now)
            svc.register_vehicle("V1", ["shuttle"], 80, 80, 0.2)
            svc.register_officer("O1", ["shuttle"])
            svc.register_segment("S1")
            svc.submit_request(
                "R1", "shuttle", "2026-10-21T08:00:00Z", "2026-10-21T10:00:00Z",
                45, 50, ["S1"],
            )
            plan = svc.generate_plan()
            svc.acquire_lease(plan["plan_id"], "night-dispatcher")
            svc.close()

            # 重启：租约未到期，应可凭原持有者身份继续；他人不可抢占。
            rebooted = PlanningService(str(db), lease_ttl_seconds=600, now=base_now)
            info = rebooted.recover_leases()
            self.assertEqual(
                [l["plan_id"] for l in info["active_leases"]], [plan["plan_id"]]
            )
            with self.assertRaises(LeaseError):
                rebooted.acquire_lease(plan["plan_id"], "relief-dispatcher")
            rebooted.close()

            # 过期租约在重启清理时被回收，接班人可以获取。
            future = PlanningService(str(db), now=T("2026-10-22T00:00:00Z"))
            recovered = future.recover_leases()
            self.assertIn(plan["plan_id"], recovered["purged_expired"])
            lease = future.acquire_lease(plan["plan_id"], "relief-dispatcher")
            self.assertEqual(lease["holder"], "relief-dispatcher")
            future.close()


class CancelTests(unittest.TestCase):
    def setUp(self) -> None:
        # 时钟固定在首班行程开始之前，避免真实时间干扰。
        self.svc = build_service(now=T("2026-10-21T07:00:00Z"))
        self.svc.submit_request(
            "R1", "shuttle", "2026-10-21T08:00:00Z", "2026-10-21T10:00:00Z",
            60, 50, ["S1"],
        )
        self.svc.submit_request(
            "R2", "shuttle", "2026-10-21T10:00:00Z", "2026-10-21T12:00:00Z",
            60, 50, ["S1"],
        )
        self.svc.submit_request(
            "R3", "public-demo", "2026-10-21T08:00:00Z", "2026-10-21T12:00:00Z",
            60, 50, ["S2"],
        )
        self.plan = self.svc.generate_plan()
        lease = self.svc.acquire_lease(self.plan["plan_id"], "duty")
        self.svc.confirm_plan(self.plan["plan_id"], "duty", lease["token"])

    def _lease(self) -> str:
        return self.svc.acquire_lease(self.plan["plan_id"], "duty")["token"]

    def test_partial_cancel_releases_only_named_requests(self):
        token = self._lease()
        result = self.svc.cancel_plan(
            self.plan["plan_id"], "duty", token, ["R2"]
        )
        self.assertTrue(result["partial"])
        self.assertEqual(result["cancelled_requests"], ["R2"])
        occ = self.svc.occupancy_at("2026-10-21T08:30:00Z", include_candidate=False)
        self.assertEqual({o["request_id"] for o in occ["occupancy"]}, {"R1"})
        # R3 在 09:00–10:00 由 V1/O1 执行（先让行早班 R1）。
        occ_later = self.svc.occupancy_at("2026-10-21T09:30:00Z", include_candidate=False)
        self.assertEqual({o["request_id"] for o in occ_later["occupancy"]}, {"R3"})
        # R2 的时间窗内不再有任何承诺。
        occ2 = self.svc.occupancy_at("2026-10-21T10:30:00Z", include_candidate=False)
        self.assertNotIn("R2", {o["request_id"] for o in occ2["occupancy"]})

    def test_full_cancel_removes_everything(self):
        token = self._lease()
        self.svc.cancel_plan(self.plan["plan_id"], "duty", token)
        occ = self.svc.occupancy_at("2026-10-21T08:30:00Z", include_candidate=False)
        self.assertEqual(occ["occupancy"], [])
        self.assertEqual(self.svc.get_plan(self.plan["plan_id"])["status"], "cancelled")

    def test_running_trip_cannot_be_cancelled(self):
        self.svc.tick("2026-10-21T08:30:00Z")  # R1 / R3 已进入进行中
        token = self._lease()
        with self.assertRaises(StateError):
            self.svc.cancel_plan(self.plan["plan_id"], "duty", token, ["R1"])
        # R2（10:00 才开始）尚可取消。
        self.svc.cancel_plan(self.plan["plan_id"], "duty", token, ["R2"])


class RoadClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = build_service(now=T("2026-10-21T07:00:00Z"))
        self.svc.register_vehicle("V3", ["shuttle", "enterprise-test"], 60, 60, 0.3)
        self.svc.submit_request(
            "EARLY", "shuttle", "2026-10-21T08:00:00Z", "2026-10-21T10:00:00Z",
            60, 50, ["S1"],
        )
        self.svc.submit_request(
            "LATE", "enterprise-test", "2026-10-21T13:00:00Z",
            "2026-10-21T16:00:00Z", 60, 50, ["S2"],
        )
        plan = self.svc.generate_plan()
        lease = self.svc.acquire_lease(plan["plan_id"], "duty")
        self.svc.confirm_plan(plan["plan_id"], "duty", lease["token"])
        self.plan_id = plan["plan_id"]
        late = next(a for a in plan["assignments"] if a["request_id"] == "LATE")
        self.assertEqual(late["vehicle_id"], "V2")  # V2 先于 V3 被选中
        self.svc.tick("2026-10-21T08:30:00Z")  # EARLY 已开始

    def test_closure_reschedules_only_unstarted_affected_tasks(self):
        result = self.svc.report_block(
            "segment", "S2",
            "2026-10-21T12:00:00Z", "2026-10-21T18:00:00Z",
            reason="临时道路管制",
        )
        self.assertEqual(result["affected_requests"], ["LATE"])
        repair = result["repair_plan"]
        # LATE 在 S2 全天封闭时无法落位 → 给出窗口外替代建议。
        bad = next(u for u in repair["unscheduled"] if u["request_id"] == "LATE")
        self.assertEqual(bad["reason_code"], "closed")
        self.assertTrue(bad["alternatives"])
        self.assertIn("道路管制", bad["reason"])
        # 已经开始的 EARLY 不受影响，仍在运行。
        occ = self.svc.occupancy_at("2026-10-21T08:45:00Z", include_candidate=False)
        self.assertEqual([o["request_id"] for o in occ["occupancy"]], ["EARLY"])
        self.assertEqual(occ["occupancy"][0]["state"], "running")

    def test_closure_conflicting_with_running_trip_is_rejected(self):
        with self.assertRaises(StateError):
            self.svc.report_block(
                "segment", "S1",
                "2026-10-21T08:00:00Z", "2026-10-21T09:30:00Z",
                reason="事故封路",
            )

    def test_vehicle_failure_reroutes_to_alternative_vehicle(self):
        result = self.svc.report_block(
            "vehicle", "V2",
            "2026-10-21T12:00:00Z", "2026-10-21T18:00:00Z",
            reason="车辆故障",
        )
        repair = result["repair_plan"]
        late = next(a for a in repair["assignments"] if a["request_id"] == "LATE")
        self.assertNotEqual(late["vehicle_id"], "V2")

    def test_unrelated_closure_triggers_no_reschedule(self):
        result = self.svc.report_block(
            "segment", "S3",
            "2026-10-21T00:00:00Z", "2026-10-21T23:59:00Z",
        )
        self.assertEqual(result["affected_requests"], [])
        self.assertIsNone(result["repair_plan"])


class FrozenProtectionTests(unittest.TestCase):
    def test_regenerating_plan_keeps_confirmed_trip_immobile(self):
        svc = build_service(now=T("2026-10-21T07:00:00Z"))
        svc.submit_request(
            "LOCKED", "shuttle", "2026-10-21T09:00:00Z", "2026-10-21T12:00:00Z",
            60, 30, ["S1"],
        )
        p1 = svc.generate_plan()
        a1 = next(a for a in p1["assignments"] if a["request_id"] == "LOCKED")
        lease = svc.acquire_lease(p1["plan_id"], "duty")
        svc.confirm_plan(p1["plan_id"], "duty", lease["token"])

        # 再提一个同窗高优先级需求并重新生成全量方案。
        svc.submit_request(
            "NEW", "shuttle", "2026-10-21T09:00:00Z", "2026-10-21T12:00:00Z",
            60, 99, ["S2"],
        )
        p2 = svc.generate_plan()
        locked = next(a for a in p2["assignments"] if a["request_id"] == "LOCKED")
        self.assertTrue(locked.get("locked"))
        self.assertEqual(locked["start"], a1["start"])
        self.assertEqual(locked["vehicle_id"], a1["vehicle_id"])
        self.assertIn("LOCKED", p2["locked_request_ids"])
        # 已确认占用状态保持 confirmed，未被移动。
        occ = svc.occupancy_at("2026-10-21T09:30:00Z", include_candidate=False)
        self.assertEqual(
            [(o["request_id"], o["state"]) for o in occ["occupancy"]],
            [("LOCKED", "confirmed")],
        )


class PlanComparisonTests(unittest.TestCase):
    def test_compare_highlights_assigned_and_slack_differences(self):
        svc = build_service()
        svc.submit_request(
            "R1", "shuttle", "2026-10-21T08:00:00Z", "2026-10-21T12:00:00Z",
            45, 50, ["S1"],
        )
        by_priority = svc.generate_plan(strategy="priority")
        early = svc.generate_plan(strategy="early")
        diff = svc.compare_plans(by_priority["plan_id"], early["plan_id"])
        self.assertEqual(diff["plans"][0]["plan_id"], by_priority["plan_id"])
        self.assertIn("recommendation", diff)


class OccupancyAtPointTests(unittest.TestCase):
    def test_filters_by_resource_kind_and_id(self):
        svc = build_service()
        svc.submit_request(
            "R1", "shuttle", "2026-10-21T08:00:00Z", "2026-10-21T10:00:00Z",
            45, 50, ["S1", "S2"],
        )
        svc.generate_plan()
        v = svc.occupancy_at("2026-10-21T08:30:00Z", "vehicle", "V1")
        self.assertEqual(len(v["occupancy"]), 1)
        none = svc.occupancy_at("2026-10-21T08:30:00Z", "vehicle", "V2")
        self.assertEqual(none["occupancy"], [])
        seg = svc.occupancy_at("2026-10-21T08:30:00Z", "segment", "S2")
        self.assertEqual(seg["occupancy"][0]["request_id"], "R1")


if __name__ == "__main__":
    unittest.main()
