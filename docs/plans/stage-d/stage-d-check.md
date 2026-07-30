# 阶段 D 验收报告

## 1. 验收结论

通过。阶段 D 已按用户确认的 Demo-first 标准完成真实 ORFS 闭环：

```text
YAML → PL/CTS 阶段观测 → Doomed 风险分类
     → GWTW continue/pause/audit/fork
     → checkpoint child 执行 → finish QoR
```

本次验收证明控制链、checkpoint 复用链和证据链可以运行并被观察，不证明 QoR 改善、成本节省或生产级可靠性。

## 2. 验收依据与范围调整

- 验收日期：2026-07-31。
- 阶段总纲：`docs/AgenticPD八阶段迭代计划.md` 的“阶段 D–H 的 Demo-first 原则”。
- 阶段实现参考：`docs/plans/stage-d/stage-d-plan.md`。
- 用户已明确将阶段 D 的完成定义调整为“输入命令后有效果、可观察、完成最小闭环”。
- 原详细 Plan 中超出 Demo 所需的严格预算、完整恢复、路径完全脱敏和生产级异常处理，经用户批准移入后置工程清单，不再阻塞本阶段。

## 3. 范围与交付物

- [x] 最小 Observation、DoomedDecision、GWTWDecision 和 pause 生命周期已落盘。
- [x] 规则型 DoomedPredictor 可输出 `hard_dead`、`soft_bad`、`survivor` 及原因。
- [x] 串行 GWTWScheduler 可输出 `continue`、`pause`、`audit_continue` 和 fork。
- [x] checkpoint resolver 可记录实际消费的 checkpoint 和有效执行起点。
- [x] PL、CTS 两层 cohort 可完成决策、补位和下游执行。
- [x] Trial、优化树、decision trace 和真实 finish QoR 可定位。
- [x] `configs/experiments/stage-d-smoke.yml` 可直接启动真实 Demo。
- [x] 未引入阶段 E 的 Agent 指令、阶段 F 的训练模型或阶段 G 的生产并行基础设施。

## 4. 纯 Python 验证

| 命令 | 实际结果 |
|---|---|
| `make test` | 437/437 通过 |
| `python3 schemas/trial.py` | 87/87 通过 |
| `python3 managers/checkpoint_manager.py` | 31/31 通过 |
| `python3 managers/trial_manager.py` | 25/25 通过 |
| `python3 orchestrator.py` | 17/17 通过 |
| `python3 main.py --help` | 退出码 0，包含 `--stage-d` |
| `python3 tools/trial_inspect.py --help` | 退出码 0 |
| `python3 tools/trial_reproduce.py --help` | 退出码 0 |
| `python3 tools/clean.py --help` | 退出码 0 |
| `python3 tools/checkpoint_fork_verify.py --help` | 退出码 0 |

验证未调用 LLM、网络、OpenROAD、ORFS、PDK 或既有运行产物。真实 ORFS 结果单独列于下一节。

## 5. 真实 ORFS Demo

### 5.1 配置与命令

- 配置：`configs/experiments/stage-d-smoke.yml`
- 平台与设计：`sky130hd/gcd`
- seed：42
- population：4
- `max_trials`：20

```bash
python3 main.py \
  --stage-d configs/experiments/stage-d-smoke.yml \
  --log-level INFO
```

### 5.2 Session 与结果

Session：

```text
runs/sky130hd_gcd/stage-d-smoke-gcd_20260731_041742/
```

终端结果：

```text
[MAIN] Stage D complete: total_trials=8 budget_remaining=12 errors=[] resumed=False
```

独立复核结果：

- 8 个唯一 Trial：4 个 `ok`，4 个 `paused`；
- 4 个 child Trial，全部具有 `checkpoint_fork` ExecutionResolution；
- 两个 PL child 消费 PL checkpoint，有效起点为 CTS；
- 两个 CTS child 消费 CTS checkpoint，有效起点为 RT；
- decision trace 共 38 条，包含 PL、CTS 两个 `cohort_complete`；
- GWTW 动作包含 3 次 `continue`、4 次 `pause`、1 次 `audit_continue`；
- trace 包含 4 条 `fork_intent`、4 条 `fork` 和 4 条 `execution_resolution`；
- 8 份 checkpoint manifest 均通过文件存在性与 hash 验证；
- 4 个 Trial 完成真实 finish，均有完整 WNS、TNS、面积和功耗；
- `agenticpd.log` 和 make 日志未发现 Traceback、cohort 失败或 checkpoint 失败。

### 5.3 真实 QoR

4 个 finish Trial 的 post-route QoR 相同：

```text
WNS   = -1460.26 ps
TNS   = -61747.6 ps
Area  = 5400.18 µm²
Power = 0.00937965 W
```

权威原始报告位于：

```text
flow/logs/sky130hd/gcd/agenticpd_sd_<trial-id>/6_report.json
flow/reports/sky130hd/gcd/agenticpd_sd_<trial-id>/6_finish.rpt
```

QoR 相同不影响 Demo 闭环验收，但本结果不得用于宣称优化策略优于默认配置。

## 6. 证据路径

```text
runs/sky130hd_gcd/stage-d-smoke-gcd_20260731_041742/config_snapshot.json
runs/sky130hd_gcd/stage-d-smoke-gcd_20260731_041742/trials.jsonl
runs/sky130hd_gcd/stage-d-smoke-gcd_20260731_041742/tree.json
runs/sky130hd_gcd/stage-d-smoke-gcd_20260731_041742/traces/decisions.jsonl
runs/sky130hd_gcd/stage-d-smoke-gcd_20260731_041742/iter-*/trial.json
```

## 7. 后置工程项

以下事项已获用户批准后置，不阻塞 Demo 验收：

- child 创建前的精确预算预留与所有边界下的零超限保证；
- Stage D CLI resume、部分 cohort 恢复和恢复过程的精确计费；
- 主动 wall-clock 中断；
- 跨进程确定且稳定的优化树节点 ID；
- JSON、日志和配置快照的完整相对路径与脱敏；
- Trial finish `report_path` 与实际 `logs/.../6_report.json` 位置统一；
- 差异化初始 population、QoR 改善验证、多 seed 和统计对照；
- 生产级容错、并行 worker 和 Slurm。

按 Demo-first 口径无阻断项；按长期实验或生产口径，上述事项必须在相应后续阶段关闭。

## 8. Git 收尾

- 阶段分支：`agenticpd-stage-d`
- 阶段 commit：待创建
- main merge commit：待创建
- push：未授权、未执行

## 9. 最终结论

阶段 D 功能与真实 Demo 验收通过，可以进入 Git 收尾。完成 commit 和 merge 前不得开始阶段 E；push 仍需用户单独授权。
