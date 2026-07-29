# AgenticPD 八阶段迭代计划

## 项目目的

构建 `AgenticPD + GWTW + Doomed Runs` 的 Flow Optimization 平台：在统一计算预算下，以可追溯 trial、可恢复 checkpoint 和真实 post-route QoR 为基础，比较 Agent、传统搜索和调度策略的优化效果。

平台依赖 ORFS（OpenROAD-flow-scripts）执行 RTL-to-GDS 流程。ORFS 的 logs、reports、results 和 objects 是原始证据；QoR 仅以 post-route 报告为准。Agent 只能提出受限参数 action，不能改写 evaluator、Makefile、Tcl 或设计配置。

## 目标架构

```text
SearchPolicy (AgenticPD / Random / BO)
        ↓ Action
TrialManager ─ ValidationGate ─ CheckpointManager
        ↓                              ↓
ExecutionBackend ───────────────→ ORFS Adapter
        ↓                              ↓
ArtifactStore ← MetricParser ← raw logs / reports / results
        ├─ FeatureExtractor → DoomedPredictor
        ├─ GWTWScheduler
        └─ ParetoArchive / DecisionTrace / experiment reports
```

- `TrialManager` 是创建 trial、恢复 checkpoint 和提交执行的唯一入口。
- `CheckpointManager` 通过参数影响阶段与 artifact manifest 决定复用是否合法。
- `DoomedPredictor` 只输出风险与置信度；`GWTWScheduler` 决定继续、暂停或 fork。
- `ArtifactStore` 保存不可变证据，派生特征必须可追溯到 trial 与原始文件。

## 阶段 A：冻结 Demo 与最小回归实验

**实现功能**：冻结 legacy demo；固定 smoke、开发和 held-out design；以 YAML 声明平台、设计、预算、seed、参数空间和 evaluator；将既有运行转换为只读 fixture。

**交付物**：实验契约、`environment_manifest.json`、experiment YAML、QoR fixture 与纯 Python 解析测试。

**验收标准**：同一配置可重建一条 trial；fixture 的 QoR 解析稳定；新提交无需启动 ORFS 即通过纯 Python 测试。

## 阶段 B：Trial、Checkpoint 与 Artifact 数据层

**实现功能**：定义 `ExperimentSpec`、`TrialSpec`、`StageResult`、`CheckpointRef`、`DecisionTrace` 与失败分类；记录参数、父 trial、环境 hash、耗时和失败信息；建立 checkpoint manifest 与可恢复性校验。

**交付物**：schema、trial/checkpoint manager、成功与失败 fixture、trial inspection CLI。

**验收标准**：任一 trial 可追溯 parent、参数变化、各阶段耗时、失败类型、可恢复性和最终产物位置；失败阶段耗时不丢失。

## 阶段 C：ORFS Adapter 与执行后端

**实现功能**：分离命令构建、报告解析、阶段执行和编排；统一 `StageResult` 的命令、时间、退出码、QoR、报告路径和失败分类；根据 `ParameterSpec.affects` 失效 checkpoint；建立本地后端与 Slurm 接口。

**交付物**：ORFS adapter、execution backend、checkpoint 兼容性测试、stage/artifact/clean-target 映射。

**验收标准**：checkpoint manifest 可证明恢复合法；不兼容参数自动全量重跑或回退；fork 与 full restart 的对照能验证 QoR 一致性和成本收益；日志与持久化记录无绝对用户路径。

## 阶段 D：Observation 与非 Agent 基线

**实现功能**：提取 final/stage timing、面积、功耗、DRC、拥塞、设计规则、runtime 等指标；固定参数空间版本；实现 Default、RandomSearch、TopKGreedy 与 ParetoArchive；统一预算口径。

**交付物**：指标契约、baseline policies、trajectory 数据集、Pareto 与阶段成本报告。

**验收标准**：无 LLM 条件下可公平比较 checkpoint branching 的成本；中间指标与最终 QoR 的关系可量化；失败和超时计入预算。

## 阶段 E：可替换的 AgenticPD 策略

**实现功能**：将 Agent 提炼为 `SearchPolicy.propose(state) -> Action`；建立 rule-based 与 deterministic mock 对照；校验 action，记录建议动作、实际动作、回退和模型调用元数据。

**交付物**：LLM、rule-based、mock policy；`Action` 与 `Observation` schema；同预算消融实验。

**验收标准**：更换模型、prompt 或关闭 LLM 不影响 Runner、Store 与 Evaluator；每项决策可离线回放并验证合法性。

## 阶段 F：GWTW 调度

**实现功能**：维护候选种群、活跃预算、checkpoint fork、resample 与 diversity；以可解释分数综合可行性、Pareto rank、进度、成本、后代数和参数距离；保留探索配额。

**交付物**：GWTW scheduler、population snapshot、预算与多样性统计、等预算基线对比。

**验收标准**：较优 checkpoint 可被有选择地扩展；种群不会坍缩到单一 parent 或相邻参数；所有 continue/pause/fork 决策均有记录。

## 阶段 G：Doomed Runs 第一版

**实现功能**：从 trajectory 构建表格特征；预测最终可行性与 QoR 潜力；按 design 切分训练、开发和 held-out；输出风险、置信度和模型版本；以双阈值辅助暂停并保留随机反事实审计。

**交付物**：feature pipeline、model artifact、dataset manifest、校准与反事实审计报告。

**验收标准**：报告 PR-AUC、Brier score、校准与成本节省；误杀 winner/Pareto candidate 的风险受预设上限约束；不满足风险约束时仅作排序特征。

## 阶段 H：整合与实验比较

**实现功能**：串联 AgenticPD propose、validation、checkpoint 执行、特征提取、Doomed risk 与 GWTW 调度；比较 Default、Random、传统搜索、AgenticPD、AgenticPD+GWTW 和完整系统。

**交付物**：可复现实验入口、图表脚本、实验表、成功与失败 decision trace、最终技术报告。

**验收标准**：每个最终候选可回溯 parent、参数、模型版本、风险判断和真实 post-route QoR；多 seed、同预算、含 held-out 的对照可重复；输出 QoR、Pareto/hypervolume、首次可行时间、计算成本、LLM 成本、误杀率、多样性和 checkpoint cache hit。

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
- GWTW 的 particle、resampling、diversity、队列、并发和恢复。
- 分类/回归、PR-AUC、Brier score、校准、design-level split、数据泄漏与反事实审计。
- 第一版优先表格特征与 Logistic Regression、Random Forest 或 XGBoost；只有跨设计泛化明确不足时再考虑图或序列模型。

### Agent 工程

- 严格分离 observation、action 与 evaluator；以 schema 校验输入输出。
- 追踪 prompt、response、模型、temperature、token、policy version 与 validation outcome。
- 使用 deterministic fake policy 和离线 replay；禁止模型直接输出 shell、Tcl、路径或 evaluator 配置。
