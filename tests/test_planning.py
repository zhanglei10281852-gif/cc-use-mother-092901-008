"""示范运营编排的集成测试。

覆盖：跨午夜任务、并发确认互斥、部分取消、重启后租约恢复，
以及高优先级插单挤占、封路只重排受影响任务、已开始行程不可移动。
"""

import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from operation_planning import (
    ChargingBay,
    ConflictError,
    InvalidStateError,
    Leg,
    NotFoundError,
    PlanState,
    PlanningService,
    RouteSegment,
    SafetyOfficer,
    TaskDemand,
    UnavailableWindow,
    Vehicle,
)

UTC = timezone.utc
BASE = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)


def at(hour: int, minute: int = 0, day: int = 21) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def build_service(path: str = ":memory:") -> PlanningService:
    service = PlanningService.from_path(path)
    service.register_segment(RouteSegment("S1", "示范区迎宾大道"))
    service.register_segment(RouteSegment("S2", "环湖展示环线"))
    service.register_segment(RouteSegment("S3", "企业测试专用道"))
    for vehicle in (
        Vehicle("V1", "星环一号", frozenset({"shuttle", "public-demo"}), 80, 100),
        Vehicle("V2", "星环二号", frozenset({"shuttle", "enterprise-test"}), 30, 100),
        Vehicle("V3", "星环三号", frozenset({"shuttle", "public-demo",
                                             "enterprise-test"}), 90, 100),
    ):
        service.register_vehicle(vehicle)
    for officer in (
        SafetyOfficer("O1", "安岚", frozenset({"shuttle", "public-demo"})),
        SafetyOfficer("O2", "石敢当", frozenset({"shuttle", "enterprise-test"})),
        SafetyOfficer("O3", "高照", frozenset({"shuttle", "public-demo",
                                               "enterprise-test"})),
    ):
        service.register_officer(officer)
    service.register_bay(ChargingBay("B1", "一号快充", 60))
    service.register_bay(ChargingBay("B2", "二号快充", 30))
    return service


def demand(
    request_id: str,
    kind: str = "shuttle",
    *,
    start: datetime = BASE,
    end: datetime = BASE + timedelta(hours=1),
    segments=("S1",),
    priority: int = 50,
    energy: float = 20.0,
) -> TaskDemand:
    span = (end - start) / len(segments)
    legs = tuple(
        Leg(seg, start + span * i, start + span * (i + 1))
        for i, seg in enumerate(segments)
    )
    return TaskDemand(request_id, kind, priority, start, end, legs, energy)


def first_plan(result: dict) -> str:
    return result["options"][0]["plan_id"]


class CandidateAndExplainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_submit_returns_explained_candidates_ranked(self) -> None:
        result = self.service.submit_demand(demand("REQ-1"))
        self.assertTrue(result["options"])
        plan = result["options"][0]
        self.assertEqual(plan["state"], "candidate")
        detailed = self.service.get_plan(plan["plan_id"])
        self.assertTrue(any("能力" in r for r in detailed["rationale"]))
        scores = [o["score"] for o in result["options"]]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_compare_recommends_highest_score(self) -> None:
        result = self.service.submit_demand(demand("REQ-1"))
        ids = [o["plan_id"] for o in result["options"][:2]]
        comparison = self.service.compare(ids)
        self.assertEqual(comparison["recommended"], ids[0])

    def test_infeasibility_explains_missing_capability_and_closure(self) -> None:
        result = self.service.submit_demand(
            demand("REQ-X", "rocket-launch", segments=("S1",))
        )
        self.assertEqual(result["options"], [])
        self.assertTrue(
            any("rocket-launch" in r for r in result["infeasible_reasons"])
        )

    def test_low_soc_schedules_charging_before_trip(self) -> None:
        d = demand("REQ-C", "enterprise-test",
                   start=at(14), end=at(15), segments=("S3",), energy=80)
        result = self.service.submit_demand(d)
        self.assertTrue(result["options"])
        # V2 电量仅 30kWh，它的候选必须带任务前充电预约。
        low_soc = self.service.get_plan(
            next(o["plan_id"] for o in result["options"]
                 if o["vehicle_id"] == "V2")
        )
        self.assertIsNotNone(low_soc["charging"])
        self.assertLessEqual(
            datetime.fromisoformat(low_soc["charging"]["ends_at"]), at(14)
        )
        self.assertTrue(any("补能" in r for r in low_soc["rationale"]))


class ConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_confirm_freezes_leases_and_blocks_double_booking(self) -> None:
        # 两个重叠需求在任何确认之前同时提交，候选允许重叠（软占用）。
        a = self.service.submit_demand(demand("REQ-A", start=at(9), end=at(10)))
        b = self.service.submit_demand(demand("REQ-B", start=at(9, 30), end=at(10, 30)))
        plan_a = a["options"][0]
        same_vehicle = next(
            o for o in b["options"]
            if o["vehicle_id"] == plan_a["vehicle_id"]
        )
        self.service.confirm(plan_a["plan_id"], now=at(6))

        # A 已冻结后，B 的同车方案确认必须失败，避免重复承诺。
        with self.assertRaises(ConflictError) as ctx:
            self.service.confirm(same_vehicle["plan_id"], now=at(6))
        self.assertTrue(
            {c["resource_type"] for c in ctx.exception.conflicts}
            <= {"vehicle", "officer", "segment"}
        )

    def test_concurrent_confirmation_only_one_wins(self) -> None:
        """两线程同时确认同一车辆同一时段：恰有一方成功，租约不重复承诺。"""

        # 先在单线程内生成两份重叠候选，再并发确认制造竞态。
        option_x = self.service.submit_demand(
            demand("REQ-X", start=at(16), end=at(17))
        )["options"][0]
        option_y = self.service.submit_demand(
            demand("REQ-Y", start=at(16), end=at(17))
        )["options"][0]
        self.assertEqual(option_x["vehicle_id"], option_y["vehicle_id"])
        ids = [option_x["plan_id"], option_y["plan_id"]]
        results: dict[str, str] = {}

        def confirm(plan_id: str, tag: str) -> None:
            try:
                self.service.confirm(plan_id, now=at(12))
                results[tag] = "ok"
            except ConflictError:
                results[tag] = "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(confirm, ids[0], "X"),
                       pool.submit(confirm, ids[1], "Y")]
            for future in futures:
                future.result()

        self.assertEqual(sorted(results.values()), ["conflict", "ok"])
        occ = self.service.occupancy(at(16, 30), include_candidates=False)
        vehicle_key = f"vehicle:{option_x['vehicle_id']}"
        self.assertEqual(len(occ["frozen"].get(vehicle_key, [])), 1)

    def test_lease_recovery_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "ops.db")
            service = build_service(db)
            a = service.submit_demand(demand("REQ-A", start=at(9), end=at(10)))
            chosen = a["options"][0]
            vehicle_id = chosen["vehicle_id"]
            self.service = service  # 仅用于统一 tearDown 关闭
            service.confirm(chosen["plan_id"], now=at(6))

            revived = PlanningService.from_path(db)
            self.service = revived
            occ = revived.occupancy(at(9, 30), include_candidates=False)
            self.assertIn(f"vehicle:{vehicle_id}", occ["frozen"])
            self.assertIn("segment:S1", occ["frozen"])

            # 重启后 S1 的时段租约依旧生效：同区段新需求无可行候选。
            b = revived.submit_demand(demand("REQ-B", start=at(9), end=at(10)))
            self.assertEqual(b["options"], [])
            self.assertTrue(
                any("S1" in r for r in b["infeasible_reasons"])
            )

    def tearDown(self) -> None:
        self.service.close()


class CrossMidnightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_cross_midnight_trip_occupies_both_days(self) -> None:
        d = demand(
            "REQ-NIGHT", "shuttle",
            start=at(23, day=21), end=at(1, day=22),
            segments=("S1", "S2"), energy=60,
        )
        self.assertTrue(d.crosses_midnight)
        result = self.service.submit_demand(d)
        self.assertTrue(result["options"], result["infeasible_reasons"])
        chosen = result["options"][0]
        vehicle_id = chosen["vehicle_id"]
        self.service.confirm(chosen["plan_id"], now=at(12))

        before_midnight = self.service.occupancy(
            datetime(2026, 10, 21, 23, 30, tzinfo=UTC),
            include_candidates=False,
        )
        after_midnight = self.service.occupancy(
            datetime(2026, 10, 22, 0, 30, tzinfo=UTC),
            include_candidates=False,
        )
        for snapshot in (before_midnight, after_midnight):
            self.assertIn(f"vehicle:{vehicle_id}", snapshot["frozen"])

        # 午夜后行程仍在 S2 上（00:00–01:00），同区段新任务无可行候选。
        clash = self.service.submit_demand(
            demand("REQ-AFTER", start=at(0, 30, day=22), end=at(1, 30, day=22),
                   segments=("S2",))
        )
        self.assertEqual(clash["options"], [])
        self.assertTrue(
            any("S2" in r for r in clash["infeasible_reasons"])
        )

    def tearDown(self) -> None:
        self.service.close()


class PreemptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_high_priority_insertion_preempts_only_candidates(self) -> None:
        low = self.service.submit_demand(
            demand("REQ-LOW", "shuttle", priority=20, start=at(10), end=at(11))
        )
        low_plan = low["options"][0]
        # 低优先级仍是候选（未确认）。
        self.assertEqual(low_plan["state"], "candidate")

        urgent = self.service.submit_demand(
            demand("REQ-VIP", "shuttle", priority=99,
                   start=at(10), end=at(11))
        )
        self.assertTrue(urgent["contending_candidates"])
        impact = urgent["contending_candidates"][0]
        self.assertEqual(impact["displaced_request_id"], "REQ-LOW")
        self.assertTrue(impact["suggestion"])

        vip_plan = urgent["options"][0]
        self.assertEqual(vip_plan["vehicle_id"], low_plan["vehicle_id"])
        # 未声明 preempt 时，确认必须被拒绝并给出受影响方。
        with self.assertRaises(ConflictError):
            self.service.confirm(vip_plan["plan_id"], now=at(6))

        outcome = self.service.confirm(
            vip_plan["plan_id"], preempt=True, now=at(6)
        )
        self.assertEqual(
            outcome["plan"]["displaced_requests"], ["REQ-LOW"]
        )
        low_after = self.service.get_plan(low_plan["plan_id"])
        self.assertEqual(low_after["state"], "cancelled")
        self.assertIn("REQ-VIP", low_after["cancellation_reason"])
        self.assertTrue(low_after["amendments"])

    def test_confirmed_plan_cannot_be_preempted(self) -> None:
        a = self.service.submit_demand(demand("REQ-A", priority=20,
                                              start=at(10), end=at(11)))
        chosen = a["options"][0]
        frozen_vehicle = chosen["vehicle_id"]
        self.service.confirm(chosen["plan_id"], now=at(6))
        # 高优先级新需求走另一条区段：候选必须绕开已冻结的车辆与安全员。
        b = self.service.submit_demand(
            demand("REQ-B", priority=99, start=at(10), end=at(11),
                   segments=("S2",))
        )
        self.assertTrue(b["options"])
        self.assertFalse(
            any(o["vehicle_id"] == frozen_vehicle for o in b["options"])
        )
        # 改派其他资源后可以正常确认，已确认计划不受影响。
        self.service.confirm(b["options"][0]["plan_id"], now=at(6))
        self.assertEqual(
            self.service.get_plan(chosen["plan_id"])["state"],
            "confirmed",
        )

    def tearDown(self) -> None:
        self.service.close()


class ClosureAndReplanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()

    def test_vehicle_failure_replans_only_affected_unstarted_plan(self) -> None:
        a = self.service.submit_demand(demand("REQ-A", start=at(9), end=at(10)))
        plan_a = a["options"][0]
        frozen_vehicle = plan_a["vehicle_id"]
        self.service.confirm(plan_a["plan_id"], now=at(6))

        unaffected = self.service.submit_demand(
            demand("REQ-B", start=at(14), end=at(15), segments=("S2",))
        )
        plan_b = first_plan(unaffected)
        self.service.confirm(plan_b, now=at(6))

        outcome = self.service.report_unavailable(
            "vehicle", frozen_vehicle,
            UnavailableWindow(at(8, 30), at(12), "车辆 V 系列通讯模块故障"),
            now=at(6),
        )
        self.assertEqual(outcome["affected_plan_ids"], [plan_a["plan_id"]])
        self.assertNotIn(plan_b, outcome["affected_plan_ids"])
        # 改排候选必须避开故障车辆，且记录了待确认的替换关系。
        new_options = outcome["options"]["REQ-A"]
        self.assertTrue(new_options)
        self.assertTrue(all(o["vehicle_id"] != frozen_vehicle for o in new_options))
        replacement_id = new_options[0]["plan_id"]
        self.assertEqual(
            self.service.get_plan(replacement_id)["replaces_plan_id"],
            plan_a["plan_id"],
        )

        # 确认替换方案：旧计划原子取消，新租约生效，故障车辆不再被占用。
        self.service.confirm(replacement_id, now=at(6))
        self.assertEqual(
            self.service.get_plan(plan_a["plan_id"])["state"], "cancelled"
        )
        self.assertEqual(
            self.service.get_plan(replacement_id)["state"], "confirmed"
        )
        occ = self.service.occupancy(at(9, 30), include_candidates=False)
        self.assertNotIn(f"vehicle:{frozen_vehicle}", occ["frozen"])

    def test_full_segment_closure_is_reported_infeasible_until_rerouted(self) -> None:
        a = self.service.submit_demand(demand("REQ-A", start=at(9), end=at(10)))
        self.service.confirm(first_plan(a), now=at(6))
        outcome = self.service.report_unavailable(
            "segment", "S1",
            UnavailableWindow(at(8, 30), at(12), "大会开幕式临时封闭"),
            now=at(6),
        )
        # 需求仍必须走 S1，无路可改：值班人员应看到明确原因而非空结果。
        self.assertIn("REQ-A", outcome["infeasible_reasons"])
        self.assertTrue(outcome["infeasible_reasons"]["REQ-A"])
        self.assertTrue(
            any("S1" in r for r in outcome["infeasible_reasons"]["REQ-A"])
        )
        # 旧租约在新方案确认前保持有效（不会凭空释放承诺）。
        occ = self.service.occupancy(at(9, 30), include_candidates=False)
        self.assertIn("segment:S1", occ["frozen"])

    def test_running_trip_is_never_moved(self) -> None:
        a = self.service.submit_demand(demand("REQ-A", start=at(9), end=at(10)))
        plan_a = first_plan(a)
        self.service.confirm(plan_a, now=at(6))
        # 时间推进到行程中。
        self.service.tick(at(9, 30))
        self.assertEqual(self.service.get_plan(plan_a)["state"], "running")

        outcome = self.service.report_unavailable(
            "segment", "S1",
            UnavailableWindow(at(9, 45), at(11), "突发封路"),
            now=at(9, 30),
        )
        self.assertEqual(outcome["affected_plan_ids"], [])
        self.assertEqual(len(outcome["unmovable"]), 1)
        self.assertEqual(outcome["unmovable"][0]["plan_id"], plan_a)
        self.assertEqual(self.service.get_plan(plan_a)["state"], "running")
        with self.assertRaises(InvalidStateError):
            self.service.cancel_plan(plan_a, "尝试取消进行中行程", now=at(9, 30))


class CancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = build_service()
        result = self.service.submit_demand(
            demand("REQ-A", start=at(9), end=at(11),
                   segments=("S1", "S2"), energy=30)
        )
        self.vehicle_id = result["options"][0]["vehicle_id"]
        self.plan_id = first_plan(result)
        self.service.confirm(self.plan_id, now=at(6))

    def test_full_cancel_releases_all_leases(self) -> None:
        self.service.cancel_plan(self.plan_id, "演示取消", now=at(6))
        occ = self.service.occupancy(at(10), include_candidates=False)
        self.assertNotIn(f"vehicle:{self.vehicle_id}", occ["frozen"])
        self.assertNotIn("segment:S1", occ["frozen"])
        # 释放后同一资源可以被新需求重新承诺。
        follow = self.service.submit_demand(demand("REQ-B", start=at(9), end=at(10)))
        self.service.confirm(first_plan(follow), now=at(6))

    def test_partial_cancel_releases_single_segment_lease(self) -> None:
        detail = self.service.partial_cancel(
            self.plan_id, "segment", "S2", "S2 末端临时管制",
        )
        self.assertTrue(
            any("S2" in note for note in detail["plan"]["amendments"])
        )
        occ = self.service.occupancy(
            at(10, 30), include_candidates=False  # 位于 S2 航段窗口
        )
        self.assertNotIn("segment:S2", occ["frozen"])
        # 车辆与 S1 行程租约保留。
        self.assertIn(f"vehicle:{self.vehicle_id}", occ["frozen"])
        self.assertIn("segment:S1", self.service.occupancy(
            at(9, 15), include_candidates=False)["frozen"])

    def test_partial_cancel_charging_releases_bay_and_vehicle_charge(self) -> None:
        # 电量不足的 V2 必须带充电预约：energy=80，soc=30。
        result = self.service.submit_demand(
            demand("REQ-CHG", "enterprise-test", start=at(14), end=at(15),
                   segments=("S3",), energy=80)
        )
        option = next(o for o in result["options"] if o["vehicle_id"] == "V2")
        self.service.confirm(option["plan_id"], now=at(6))
        bay_id = option["charging"]["bay_id"]

        detail = self.service.partial_cancel(
            option["plan_id"], "bay", bay_id, "充电工位临时检修", now=at(6)
        )
        self.assertIsNone(detail["plan"]["charging"])
        # 工位与车辆的充电占用均已释放，但行程租约保留。
        charge_start = datetime.fromisoformat(option["charging"]["starts_at"])
        occ = self.service.occupancy(charge_start,
                                     include_candidates=False)
        self.assertNotIn(f"bay:{bay_id}", occ["frozen"])
        self.assertNotIn("vehicle:V2", occ["frozen"])
        trip_occ = self.service.occupancy(at(14, 30),
                                          include_candidates=False)["frozen"]
        self.assertIn("vehicle:V2", trip_occ)
        # 重载后状态保持（charging_json 已清空）。
        self.assertIsNone(self.service.get_plan(option["plan_id"])["charging"])

    def test_partial_cancel_unknown_resource_raises(self) -> None:
        with self.assertRaises(NotFoundError):
            self.service.partial_cancel(
                self.plan_id, "segment", "S9", "不存在的区段"
            )

    def test_partial_cancel_refuses_started_leg(self) -> None:
        with self.assertRaises(InvalidStateError):
            self.service.partial_cancel(
                self.plan_id, "segment", "S1", "行程已进入 S1", now=at(9, 30)
            )

    def tearDown(self) -> None:
        self.service.close()


if __name__ == "__main__":
    unittest.main()
