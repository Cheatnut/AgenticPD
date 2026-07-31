# AgenticPD Demo 迭代计划

## 项目目的

在原有 `Judge Agent + FP/PL/CTS/RT Stage Agents` 优化框架旁增加一个独立入口，将 Doomed Runs 和 GWTW 接入多候选执行。最终目标是一条能真实运行、能看到 Agent 决策和淘汰/复制效果、能回溯 checkpoint 与 post-route QoR 的 Demo。

平台依赖 ORFS（OpenROAD-flow-scripts）执行 RTL-to-GDS 流程。ORFS 的 logs、reports、results 和 objects 是原始证据；QoR 仅以 post-route 报告为准。Agent 只能提出受限参数 action，不能改写 evaluator、Makefile、Tcl 或设计配置。

## 目标架构

```text
main.py
└─ 原始 Optimizer
   └─ Judge + FP/PL/CTS/RT Agents
      └─ 原有单候选优化循环

multi_agent_gwtw.py
└─ MultiAgentGWTWOrchestrator
   └─ 复用同一个 Judge 与四个 Stage Agents
      ├─ Trial / Tree / Checkpoint / ORFS
      ├─ DoomedPredictor
      ├─ GWTWScheduler
      └─ DecisionTrace
```

- 原始 `main.py` 和 `Optimizer.run()` 保持原行为，新功能不接管默认入口。
- 新入口复用现有 `JudgeAgent`、四个 Stage Agent、LLM client、参数校验、Trial、优化树、checkpoint resolver 和 ORFS runner。
- Judge 与 Stage Agent 决定“探索什么”；Doomed/GWTW 决定“资源给谁”；resolver 决定“实际从哪里执行”。
- Doomed 和 GWTW 是确定性控制模块，不增加新的 LLM Agent。
- 提前停止只标记 pause 并保留 Trial、checkpoint 和原始 artifact；最终结果只采用真实 post-route QoR。

## 阶段 A：冻结 Demo 与最小回归实验

**实现功能**：冻结 legacy demo；固定 smoke、开发和 held-out design；以 YAML 声明平台、设计、预算、seed、参数空间和 evaluator；将既有运行转换为只读 fixture。

**交付物**：实验契约、`environment_manifest.json`、experiment YAML、QoR fixture 与纯 Python 解析测试。

**验收标准**：同一配置可重建一条 trial；fixture 的 QoR 解析稳定；新提交无需启动 ORFS 即通过纯 Python 测试。

## 阶段 B：Trial、Checkpoint 与 Artifact 数据层

**实现功能**：定义 `ExperimentSpec`、`TrialSpec`、`StageResult`、`CheckpointRef`、`DecisionTrace` 与失败分类；记录参数、父 trial、环境 hash、耗时和失败信息；建立 checkpoint manifest 与可恢复性校验。

**交付物**：schema、trial/checkpoint manager、成功与失败 fixture、trial inspection CLI。

**验收标准**：任一 trial 可追溯 parent、参数变化、各阶段耗时、失败类型、可恢复性和最终产物位置；失败阶段耗时不丢失。

## 阶段 C：可靠的分支执行基础（已完成）

**阶段目标**：让任意 Agent 参数候选都能被可靠地记录、验证、从合法 checkpoint 执行并得到真实 QoR。

**已完成能力**：

- Trial、StageResult、CheckpointRef、ExecutionResolution 和优化树来源关联；
- FP/PL/CTS checkpoint manifest、hash 与参数兼容性验证；
- 不兼容 checkpoint 的祖先回退和 full restart；
- ORFS copy → clean → stage → finish 与 post-route QoR；
- 纯 Python 回归、真实 checkpoint fork 验证和审计证据。

Stage C 不再扩展 Slurm、通用 ArtifactStore 或生产级后端。

## 阶段 D：Doomed/GWTW 控制组件（已完成）

**阶段目标**：先用独立入口验证 Doomed 和 GWTW 的控制逻辑、checkpoint child 与证据链。

**已完成能力**：

- PL/CTS 最小 Observation；
- 规则型 `hard_dead`、`soft_bad`、`survivor` 判断；
- `continue`、`pause`、`audit_continue` 和 survivor fork；
- population 补位、合法下游 mutation、checkpoint child 执行；
- append-only decision trace、Trial 引用和真实 ORFS Demo。

Stage D 的 `main.py --stage-d` 入口继续保留，作为组件级回归和故障定位工具；它不是最终的多 Agent Demo 入口。

## 阶段 E：原多 Agent + Doomed/GWTW 集成 Demo

**阶段目标**：不改变原始 `main.py`/`Optimizer` 行为，通过独立入口复用同一个 Judge 和四个 Stage Agent，完成多候选的淘汰、审计、复制和真实 finish。

**独立入口**：

```bash
python3 multi_agent_gwtw.py \
  --config configs/experiments/multi-agent-gwtw-demo.yml
```

离线控制流：

```bash
python3 multi_agent_gwtw.py \
  --config configs/experiments/multi-agent-gwtw-demo.yml \
  --mock-llm \
  --mock-orfs
```

**最小闭环**：

1. 创建原有 `JudgeAgent` 和 FP/PL/CTS/RT Stage Agents，生成四个候选并执行到 PL。
2. 在 PL 调用 Doomed/GWTW，暂停 loser、保留 audit、从 survivor checkpoint 补位。
3. 由 Judge 在 survivor 白名单内选择探索节点，CTS/RT Agent 生成下游参数；在 CTS 再做一次 Doomed/GWTW。
4. 最终存活候选运行到 finish，持久化 Agent 决策、pause/fork、checkpoint 和真实 QoR。

**权限边界**：

- Judge 和 Stage Agent 决定探索节点、hint 与阶段参数；
- GWTW 决定 survivor 白名单、继续/暂停、audit 配额和补位数量；
- Judge 不能选择 hard-dead 或已暂停 Trial；违反白名单时拒绝或记录确定性 fallback；
- Doomed/GWTW 不生成参数，Agent 不绕过 resolver 或直接复制 artifact；
- 新入口只编排新模式，不接管或改变原 `Optimizer.run()`。

**最低验收**：

- 原始 `main.py`、baseline、mock LLM 和既有测试行为不变；
- 新入口的终端和 trace 能看到 Judge、四个 Stage Agent、PL/CTS Doomed/GWTW；
- population 固定为 4，至少出现 pause、audit 和 checkpoint fork；
- 至少两个候选完成真实 ORFS finish 并保存完整 QoR；
- session 可回溯 Agent proposal → Trial → Doomed/GWTW → parent/child → ExecutionResolution → QoR；
- 不要求 QoR 优于默认配置，不要求并发或论文算法的严格理论复现。

Stage E 完成后，本轮 Demo 路线结束，不再设置阶段 F–H。

## 后置工程清单

以下事项不阻塞 Demo；只有准备长期实验、论文或生产使用时再拆分新计划：

- child 创建前的精确预算预留与零超限保证；
- Stage D CLI resume、部分 cohort 恢复和跨进程稳定 tree ID；
- 主动 wall-clock 中断、worker 生命周期和异常清理；
- JSON、日志和配置快照的相对路径与脱敏；
- 完整 evaluator 版本、参数空间快照和环境版本记录；
- 训练型 Doomed 模型、数据集切分、模型校准、OOD、误杀率和反事实审计；
- 并行容错、Slurm、分布式状态与等 CPU-hour 比较；
- 多 seed、held-out design、统计显著性和性能优势结论。

## 建议补齐的知识

### 物理设计与 ORFS

- floorplan、placement、CTS、routing 的依赖关系，以及 utilization、density、congestion、DRC 的因果链。
- WNS、TNS、setup/hold、slew、capacitance、fanout 的阶段语义，区分 proxy 与最终 QoR。
- ORFS 的 target、`clean_<stage>`、`FLOW_VARIANT`、design config、artifact 目录和 checkpoint 失效条件。

### Python 系统工程

- `pathlib`、`subprocess`、timeout、process group、Slurm 生命周期。
- dataclass/Schema、YAML、JSONL/SQLite、fixture、mock、结构化日志、hash 与 manifest。

### 优化、调度与机器学习

- 约束优化、Pareto dominance、hypervolume、随机/贝叶斯搜索、计算预算与 checkpoint DAG。
- GWTW 的 particle、resampling、diversity、队列、并发和恢复；当前 Demo 只实现串行 barrier 近似，不把它表述为论文 Algorithm 1 的严格复现。
- 分类/回归、PR-AUC、Brier score、校准、design-level split、数据泄漏与反事实审计。
- 当前路线不训练模型；需要训练型 Doomed 时另开计划，优先表格特征和轻量模型。

### Agent 工程

- 严格分离 observation、action 与 evaluator；以 schema 校验输入输出。
- 追踪 prompt、response、模型、temperature、token、policy version 与 validation outcome。
- 使用 deterministic fake policy 和离线 replay；禁止模型直接输出 shell、Tcl、路径或 evaluator 配置。
