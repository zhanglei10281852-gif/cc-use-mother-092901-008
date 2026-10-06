# 示范运营资源编排

大会示范区的运营资源编排后端：把**任务需求、路线区段、车辆能力、电量窗口、
安全人员资质和充电工位**纳入同一计划，先产出带解释的候选排程，由值班人员
比较、确认并冻结为资源租约；封路或资源失效时只重排受影响且未开始的任务，
高优先级插单可以挤占未确认计划并给出受影响方与替代建议。

## 领域模型与工作流

```
需求 TaskDemand（航段 Leg 粒度占用路线区段）
   │  Planner 联合约束求解（能力/资质/电量/充电窗口/占用/不可用窗口）
   ▼
候选 Plan（score + rationale 依据 + risks 风险，可多方案比较）
   │  值班人员 confirm（并发安全；可选 preempt 插单挤占）
   ▼
已确认/执行中租约 Lease（车辆/安全员/区段/充电工位，SQLite 持久化）
   │  封路/故障 report_unavailable → 仅重排受影响任务
   ▼
运行中（不可移动）→ 完成；支持整单取消与航段/充电的部分取消
```

关键规则：

- **已开始的行程不能被移动或取消**：`RUNNING` 计划是重排硬边界，封路只记录现场处置备注。
- **冻结占用是硬约束**：已确认租约冲突的资源不会再出现在候选中，确认时二次校验，杜绝重复承诺。
- **未确认候选是软占用**：允许重叠；先确认者得，后来者确认时收到冲突明细。
- **插单挤占**：仅当新需求优先级**严格高于**受影响需求时生效，需显式 `preempt=true`，
  返回被挤占需求、冲突资源和替代建议（换车/换安全员/改约充电/改道）。
- **跨午夜**：全部时间使用带时区的半开区间 `[start, end)`，23:00–次日 01:00 的行程
  在两天的占用查询中都可见。
- **重启恢复**：所有台账、需求、计划、租约、不可用窗口落 SQLite，重启后从租约表重建占用视图。

## 代码结构

| 文件 | 职责 |
| --- | --- |
| `src/operation_planning/contracts.py` | 既有领域契约（`PlanState`、`TimeWindow`、`OperationRequest`） |
| `src/operation_planning/models.py` | 资源、需求航段、计划、充电预约、租约 |
| `src/operation_planning/planner.py` | 约束规划器：候选生成、可解释打分、冲突检测、充电排布、挤占评估 |
| `src/operation_planning/store.py` | SQLite 持久化与租约恢复 |
| `src/operation_planning/service.py` | 编排服务：提交/比较/确认/取消/部分取消/封路重排/时点占用 |
| `src/operation_planning/api.py` | 标准库 HTTP 接口（无第三方依赖） |

## HTTP 接口

| 方法与路径 | 说明 |
| --- | --- |
| `POST /api/vehicles` `/officers` `/bays` `/segments` | 资源台账登记 |
| `POST /api/demands` | 提交需求，返回排序后的可解释候选与候选间冲突提示 |
| `GET  /api/plans/compare?ids=A,B` | 比较方案，给出推荐（最高分） |
| `GET  /api/plans/{id}` `/api/requests/{id}` | 查看方案/需求详情 |
| `POST /api/plans/{id}/confirm` | 确认冻结；body `{"preempt": true}` 允许高优先级插单挤占 |
| `POST /api/plans/{id}/cancel` | 整单取消（已开始行程拒绝） |
| `POST /api/plans/{id}/partial-cancel` | 释放单个航段或充电预约，其余租约保留 |
| `POST /api/unavailable` | 封路/车辆故障/安全员缺席，自动只重排受影响任务 |
| `POST /api/replan` | 针对全部已知不可用窗口重新受影响重排 |
| `GET  /api/occupancy?at=...&include_candidates=true` | 查看某一时点资源占用 |

冲突响应为 `409`，body 含 `conflicts`（资源、窗口、受影响计划与替代建议）；
未找到为 `404`。

## 运行

```bash
python3 -m unittest discover -s tests -v     # 22 个测试
python3 -m compileall -q src tests run_cli.py
python3 run_cli.py                            # 契约冒烟
# HTTP 服务：python3 -m operation_planning.api（默认 127.0.0.1:8000，需设置 PYTHONPATH=src）
```

## 测试覆盖

- 跨午夜任务在两天均产生占用，午夜后冲突可检测；
- 并发确认（多线程）恰好一方成功，重复压测稳定；
- 整单取消与航段/充电预约的**部分取消**，已进入航段拒绝释放；
- 重启后从 SQLite 恢复租约，重复承诺仍被拒绝；
- 高优先级插单挤占候选（含受影响方与替代建议）、已确认计划不可被挤占；
- 车辆故障只重排受影响的未开始计划、全段封闭给出不可行原因、运行中行程不移动。
