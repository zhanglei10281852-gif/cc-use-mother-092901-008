# 示范运营资源编排后端

大会示范运营期间，自动驾驶接驳、公开体验与企业测试共享车辆、路线区段、安全员与充电工位。
本服务把**任务需求、路线区段、车辆能力、电量窗口、安全员资质与充电工位**纳入同一计划：
先生成带解释的候选排程，由值班人员持租约确认并冻结；封路或资源失效时只重排受影响任务，
已开始行程不可移动；高优先级插单可以挤占未确认计划，并给出受影响方与替代建议。

## 领域语义

* 时间一律带时区，占用为**半开区间 `[start, end)`**，跨午夜任务与普通任务使用同一套规则。
* 资源统一建模为四类键：车辆 `v`、安全员 `o`、路线区段 `s`、充电工位 `b`；
  一个行程同时占用车辆、安全员与其途经区段，充电同时占用车辆与工位。
* 行程耗电缺省按车辆 `消耗率(kWh/分钟) × 时长` 计算，也可在需求上显式给出。
* 电量不足以覆盖行程时，引擎在出发前的空闲间隙插入**最短充电预约**（受电池容量约束）。
* 占用分两种硬度：
  * **硬占用**：已确认 / 进行中的行程、充电预约、封停窗口，任何方案不可违反；
  * **软占用**：未确认的候选方案，可被**严格更高优先级**的需求挤占，被挤占方立即级联重排；
    重排失败则记录 `preempted_by` 与窗口外最早可行的替代建议。
* 封路（`segment`）、车辆失效（`vehicle`）、安全员缺席（`officer`）登记为封停窗口；
  系统只挑出**时间重叠且资源相关、尚未开始**的任务重排，与已开始行程冲突的封停会被拒绝。

## 模块结构

```
src/operation_planning/
├── contracts.py    # 早期数据契约（PlanState / OperationRequest / TimeWindow）
├── domain.py       # 资源、需求、区间、排程结果的值对象
├── engine.py       # 候选排程引擎：占用表、补能、优先级挤占、级联重排、可解释结论
├── repository.py   # SQLite 持久化：资源、需求、方案、占用、封停、租约
├── service.py      # PlanningService 业务门面（提交/生成/比较/租约/确认/取消/封路/占用查询）
├── errors.py       # NotFound / Validation / Conflict / Lease / State 异常
└── api.py          # 标准库 http.server 实现的零依赖 JSON 接口
```

## 快速开始

```bash
python3 run_cli.py                                   # 端到端冒烟演示
python3 -m unittest discover -s tests -v             # 全部测试
python3 -m compileall -q src tests run_cli.py        # 编译检查
```

启动 HTTP 服务：

```python
from operation_planning import PlanningService
from operation_planning.api import serve_forever

serve_forever(PlanningService("ops.db"), host="127.0.0.1", port=8080)
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/requests` | 提交运营需求 |
| GET | `/requests` | 需求列表 |
| POST | `/plans` | 生成候选方案（`request_ids`、`strategy: priority/early/balanced`） |
| GET | `/plans` / `/plans/{id}` | 方案列表 / 详情（含逐条解释、挤占、未排入、评分） |
| POST | `/plans/{id}/compare` | 比较两个方案（排入数、未排数、补能量、等待时长、推荐结论） |
| POST | `/plans/{id}/lease` | 获取确认租约（`holder`、`ttl_seconds`），返回不透明 `token` |
| DELETE | `/plans/{id}/lease` | 释放租约（请求头 `X-Holder` / `X-Token`） |
| POST | `/plans/{id}/confirm` | 确认并冻结（必须持有有效租约，事务内复检，并发仅一方成功） |
| POST | `/plans/{id}/cancel` | 整单取消；body 带 `request_ids` 时为部分取消；已开始行程拒绝取消 |
| POST | `/blocks` | 封路 / 车辆失效 / 安全员缺席，返回受影响任务与修复方案 |
| GET | `/occupancy?at=...&resource_kind=...&resource_id=...` | 查看某一时点占用 |
| POST | `/tick` | 时间推进（进行中状态迁移、过期租约清理，便于测试） |
| POST | `/recover` | 重启后租约恢复：回收已到期租约、列出仍有效的租约 |

候选方案示例（节选）：

```json
{
  "plan_id": "PLAN-...",
  "status": "candidate",
  "locked_request_ids": ["OP-001"],
  "assignments": [
    {"request_id": "OP-002", "vehicle_id": "V1", "officer_id": "O1",
     "segment_ids": ["S2"], "start": "...", "end": "...",
     "rationale": "车辆 V1 具备能力；安全员 O1 具备资质；区段空闲；先在工位 B1 补能 8.0kWh …"}
  ],
  "charging": [{"vehicle_id": "V1", "bay_id": "B1", "kwh": 8.0, "...": "..."}],
  "unscheduled": [
    {"request_id": "OP-009", "reason_code": "closed",
     "blocked_by": ["需求 OP-001"],
     "alternatives": [{"within_window": false, "start": "...", "note": "窗口外最早可行落位"}]}
  ],
  "displaced": [
    {"request_id": "OP-003", "preempted_by": ["OP-VIP"], "resolved": true}
  ],
  "score": {"assigned": 3.0, "unscheduled": 0.0, "charging_kwh": 8.0,
            "total_slack_minutes": 30.0, "priority_coverage": 205.0}
}
```

## 并发与恢复

* 确认采用「租约 + `BEGIN IMMEDIATE` 事务 + 事务内复检令牌」双重保护，
  两个值班员同时确认同一方案时只有一方成功，另一方得到 423/409。
* 租约带 TTL，持久化在 `leases` 表；进程重启后：
  * 未到期租约继续有效，原持有者可完成确认，他人不可抢占；
  * 已到期租约在服务启动与 `/recover` 时自动回收。
* SQLite 以 WAL 模式打开，时间统一转 UTC 文本存储，字符串比较与时间顺序一致。

## 测试覆盖

`tests/` 下 29+ 个用例，重点验证题目要求的四类场景：

* **跨午夜任务**：行程在日期边界两侧持续占用区段，半开区间结束即释放；
* **并发确认**：多线程同时确认，严格只有一方成功；
* **部分取消**：只释放指定需求，其余确认占用保留；已开始行程不可取消；
* **重启租约恢复**：未到期租约跨进程有效，到期租约重启后被回收并可被接班人获取。

此外覆盖充电预约、候选解释、未排入原因码与替代建议、高优先级挤占候选方案、
已确认行程不可挤占、封路局部重排、车辆故障换车、时点资源占用过滤与 HTTP 全链路。
