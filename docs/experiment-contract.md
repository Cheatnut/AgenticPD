# 实验契约（Experiment Contract）

> 版本 1 · 阶段 A · 2026-07-27
> 本文档定义 AgenticPD 实验的固定口径。修改本文档前必须先更新测试和 fixture。

---

## 1. 设计层次

| 角色 | Platform | Design | 用途 |
|------|----------|--------|------|
| smoke | sky130hd | gcd | 快速验证链路（~500 cells，基线 ~2 min） |
| development | sky130hd | aes | 调 prompt / 阈值 / 策略（~20k cells） |
| development | sky130hd | ibex | 调 prompt / 阈值 / 策略（~15k cells） |
| held_out | sky130hd | jpeg | 最终评价，全程冻结（~500k cells） |

## 2. 环境版本

当前 WSL 工作树非 Git checkout，无法可靠获取 commit SHA。
在学校服务器上运行前必须填入 `git rev-parse HEAD` 的 40 位 SHA。

| 组件 | 版本 / Commit | 备注 |
|------|-------------|------|
| ORFS | unresolved | 需 `git -C <orfs_dir> rev-parse HEAD` |
| OpenROAD | unresolved | 需 `openroad -version` |
| PDK (sky130hd) | unresolved | 需确认 PDK 来源和版本 |
| Python | 3.10+ | `python3 --version` |

## 3. QoR 数据来源

### 正式来源（唯一权威）

`logs/<platform>/<design>/<variant>/6_report.json`

| 字段 | ORFS key | 单位 | 说明 |
|------|---------|------|------|
| WNS | `finish__timing__setup__ws` | ns -> x1000 = ps | 最差负时序裕量；负值 = violation |
| TNS | `finish__timing__setup__tns` | ns -> x1000 = ps | 总负时序裕量 |
| Area | `finish__design__instance__area` | um2 | 标准单元总面积（重复键取后值） |
| Power | `finish__power__total` | W | 总功耗 |

### 兜底来源（仅 JSON 缺失时使用）

- 时序：`reports/.../6_finish.rpt`（正则提取 worst slack / tns，精度约 1-5 ps 误差）
- 面积：`logs/.../6_report.log`（正则提取 "Design area ... um^2"，精度约 1 um2 误差）
- 功耗：`reports/.../6_finish.rpt`（正则提取 Total 行第 4 列，精度约 0.01 mW 误差）

> **规则**：凡有 JSON 时必须用 JSON。fallback 只在 JSON 文件缺失时启用，
> 且必须在 trial record 中标记 `qor_source: fallback`。

## 4. 评价函数

### 可行性门（Feasibility Gate）

- 流程成功运行到 finish（退出码 0）
- `6_report.json` 存在且四项指标齐全
- placement legalization 通过（无 illegals）

### 时序优先比较

`qor_is_better(new, old, wns_tol=10ps, tns_tol=50ps)`：

1. 双方 WNS >= 0（均收敛）-> 跳过时序，直接比功耗/面积
2. abs(delta_WNS) > 10 ps -> WNS 大者胜
3. abs(delta_TNS) > 50 ps -> TNS 大者胜
4. 功耗小者胜 -> 面积小者胜 -> 完全打平判不优（保留旧最佳）

### 强制规则

- **中间 stage 指标只用于决策，最终胜负只看 post-route**
- **DRC 失败不能被加权 score 掩盖**
- **失败 trial 不计入 QoR 比较**
- **所有 lambda 和容差在实验开始前固定，不允许看完结果再改**

## 5. Trial 记录

每个 trial 必须记录：

```yaml
trial_id: <experiment>-<platform>-<design>-s<seed>-<seq>
parent_trial_id: null | <trial_id>
branch_stage: FP | PL | CTS | RT | null
params: {stage: {param: value}}
status: ok | failed
failed_stage: null | FP | PL | CTS | RT | finish
qor: {wns_ps, tns_ps, area_um2, power_w}
stage_qor: {stage: {metric: value}}
elapsed_s: float
make_log_path: str
qor_source: json | fallback
```

## 6. 预算

| 类型 | 定义 |
|------|------|
| backend-run | 从 synth 到 finish 的一次完整 make all |
| stage-run | 单次 make <stage>（FP/PL/CTS/RT/finish） |
| CPU-hour | wall-clock x 实际使用的 CPU 数 |
| LLM token | 每次 LLM 调用的 prompt + completion tokens |

失败和超时按预先规定计入预算。Agent 决策的 token 消耗单独记录。

## 7. 实验公平性约束

- 所有方法（default / random / BO / AgenticPD / AgenticPD+GWTW）使用相同参数空间和 evaluator
- held_out design（jpeg）不参与 prompt、阈值和模型调参
- 不看结果后修改 score 权重或容差
- 归一化参数只用 training set 统计
- 至少 3 个 seed；算力不足时必须明确声明
