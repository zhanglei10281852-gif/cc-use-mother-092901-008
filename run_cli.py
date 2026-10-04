"""命令行冒烟：完整演示一次"生成候选 → 租约确认 → 插单挤占 → 封路重排"流程。

运行：python run_cli.py
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from operation_planning import PlanningService  # noqa: E420

DAY = "2026-10-21"


def main() -> None:
    svc = PlanningService(now=datetime(2026, 10, 21, 7, tzinfo=timezone.utc))

    # ---- 资源台账 ----------------------------------------------------------
    svc.register_vehicle("V1", ["shuttle", "public-demo"], 80, 80, 0.2)
    svc.register_vehicle("V2", ["shuttle", "enterprise-test"], 60, 40, 0.3)
    svc.register_officer("O1", ["shuttle", "public-demo"])
    svc.register_officer("O2", ["shuttle", "enterprise-test"])
    svc.register_segment("S1")
    svc.register_segment("S2")
    svc.register_bay("B1", 30)

    # ---- 提交需求（含跨午夜任务）------------------------------------------
    svc.submit_request(
        "OP-SHUTTLE", "shuttle", f"{DAY}T23:00:00Z", "2026-10-22T02:00:00Z",
        120, 50, ["S1"],
    )
    svc.submit_request(
        "OP-DEMO", "public-demo", f"{DAY}T09:00:00Z", f"{DAY}T11:00:00Z",
        45, 60, ["S2"],
    )
    plan = svc.generate_plan(strategy="priority")
    print("== 候选方案 ==")
    print(json.dumps(
        [{"request": a["request_id"], "vehicle": a["vehicle_id"],
          "officer": a["officer_id"], "start": a["start"], "end": a["end"]}
         for a in plan["assignments"]],
        ensure_ascii=False, indent=2,
    ))
    print("解释示例：", plan["assignments"][0]["rationale"])

    # ---- 值班员租约确认 ----------------------------------------------------
    lease = svc.acquire_lease(plan["plan_id"], "dispatcher-wang")
    svc.confirm_plan(plan["plan_id"], "dispatcher-wang", lease["token"])
    print("\n== 已确认 ==", plan["plan_id"])

    # ---- 高优先级插单：先有未确认低优先级方案，再被 VIP 挤占 ---------------
    svc.submit_request(
        "OP-LOW", "enterprise-test", f"{DAY}T23:30:00Z", "2026-10-22T01:30:00Z",
        30, 20, ["S2"],
    )
    svc.generate_plan(strategy="priority")  # 低优先级候选，暂不确认
    svc.submit_request(
        "OP-VIP", "enterprise-test", f"{DAY}T23:30:00Z", "2026-10-22T01:30:00Z",
        30, 99, ["S2"],
    )
    new_plan = svc.generate_plan(strategy="priority")
    print("\n== 插单后的方案 ==")
    print("排入：", [(a["request_id"], a["vehicle_id"], a["start"])
                    for a in new_plan["assignments"]])
    print("挤占：", [(d["request_id"], d["preempted_by"], d["resolved"])
                    for d in new_plan["displaced"]])
    print("未排入：", [(u["request_id"], u["reason_code"])
                      for u in new_plan["unscheduled"]])
    if new_plan["displaced"]:
        d = new_plan["displaced"][0]
        print("受影响方说明：", d["request_id"], "被", d["preempted_by"],
              "挤占；替代建议：",
              (d["alternative"]["note"] if d["alternative"] and not d["resolved"]
               else "已自动改排"))

    # ---- 封路：只重排受影响任务 -------------------------------------------
    result = svc.report_block(
        "segment", "S2", f"{DAY}T08:30:00Z", f"{DAY}T12:00:00Z",
        reason="大会临时管制",
    )
    print("\n== 封路影响 ==")
    print("受影响：", result["affected_requests"])
    if result["repair_plan"]:
        print("修复方案：",
              [(u["request_id"], u["reason_code"])
               for u in result["repair_plan"]["unscheduled"]]
              or "全部重新落位")

    # ---- 时点占用 ----------------------------------------------------------
    occ = svc.occupancy_at(f"{DAY}T23:45:00Z")
    print("\n== 23:45 占用 ==")
    print([(o["request_id"], o["kind"], o["state"]) for o in occ["occupancy"]])


if __name__ == "__main__":
    main()
