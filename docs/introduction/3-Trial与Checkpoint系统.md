# 3-Trial 与 Checkpoint 系统

## 目标

Trial 是“一次候选执行”的可审计记录；checkpoint 是“已完成到某阶段的一组可验证产物”的引用。前者让人知道试过什么、结果如何，后者让后续候选能在合法时少跑上游阶段。

## 数据模型

数据模型位于 `core/models.py` 与 `core/decisions.py`，持久化由 `storage/` 负责。

| 对象 | 它记录什么 |
|---|---|
| `StageResult` | 一个阶段的状态、耗时、退出码、命令、日志/报告相对路径、中间 QoR、失败信息 |
| `TrialRecord` | 一个候选的身份、父 trial、完整参数、参数差异、所有阶段结果、最终 QoR、状态和 checkpoint |
| `CheckpointRef` | checkpoint 的来源 trial、截止阶段、上游参数 hash、产物清单及每个文件的 SHA-256 |
| `FailureClass` | 可机器识别的失败分类，例如超时、工具崩溃、QoR 不完整 |

trial ID 是随机八位十六进制字符串；`iter-N-<trial_id>` 只是便于人阅读的目录名，不是唯一身份。所有持久化路径应相对 session 或 flow 根保存，避免暴露用户绝对路径。

## Trial 生命周期

`TrialManager.create()` 一开始就建立 `trial.json`，状态为 `running`。这样即使进程中断，也会留下“曾经启动过”的证据。执行过程不断把 `StageResult` 加入 Trial，结束时更新为 `ok` 或 `failed`。

同一 session 有两个记录入口：

```text
trials.jsonl                 每次状态更新追加一行，便于扫描历史
iter-N-<trial_id>/trial.json 该 trial 的当前完整快照，原子替换写入
```

读取 JSONL 时会按 `trial_id` 合并同一 trial 的多次更新，保留最新状态。Trial 的 `elapsed_s` 优先累加各阶段的实际耗时，而不是仅依赖开始和结束时间的差。

一个合格完成的 Trial 至少需要：状态为 `ok`、完整 final QoR，并保存用于解释结果的参数和阶段证据。失败 Trial 同样有价值：它应记录失败阶段、失败类别和错误说明，不能悄悄从实验成本中消失。

## Checkpoint 生命周期

checkpoint 通常在某一上游阶段完成后创建。例如 CTS checkpoint 表示：这份 trial 的 FP、PL、CTS 产物可以作为后续 RT-only 改动的起点。

创建时 `CheckpointManager` 会：

1. 找到该阶段所需的 ORFS artifact；
2. 为每个文件记录相对路径、大小和 SHA-256；
3. 对该 checkpoint 上游涉及的参数计算 hash；
4. 把 `CheckpointRef` 写入 trial 的记录目录。

复用前不能只看“文件还在”。系统先验证 manifest 中每个文件是否存在、大小与 hash 是否一致，再检查候选参数相对来源参数的变化。只有变更变量的 `affects` 全部严格位于 checkpoint 之后，才允许 fork；否则必须从更早阶段或 full restart 开始。

例如 `GRT_CONGESTION_ITERATIONS` 仅影响 RT，因此 CTS checkpoint 兼容；`FASTROUTE_LAYER_ADJUSTMENT` 在 FP 已被读取，尽管它按管理分类位于 RT，也会使 CTS checkpoint 不兼容。

## 一致性与安全

checkpoint 复用不是零成本：仍需要复制父 variant 的结果、对象、日志和报告，并清理、执行下游阶段。它只是减少上游 EDA 工作，绝不是跳过验证。

Trial 的参数字段保存的是**继承后的完整参数**，`param_diff` 才保存相对父 trial 的变化。两者缺一不可：前者用于复现，后者用于理解这次搜索真正改了什么。

`trial_reproduce.py` 可以读取成功 trial 的持久化参数，以新的 variant 重新运行并比较 QoR。重新运行会启动真实 ORFS，因此它是验证手段，也会消耗计算资源；不能把旧记录直接当作新的复现实验结果。

## 约束与风险

- checkpoint 的完整性证明只覆盖 manifest 列出的 artifact；若 ORFS 的依赖语义变化，应重新审查 manifest 规则。
- 不兼容 checkpoint 被拒绝是正确保护，不是普通执行失败；实验报告应单独说明。
- `trials.jsonl` 是追加索引，不能手工编辑来“修正结果”；应通过结构化更新保留演变证据。
- 真实 QoR 仍以 ORFS finish 后报告为准，checkpoint 和中间 stage QoR 都不能代替它。
