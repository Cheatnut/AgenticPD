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
- `DoomedPredictor` 只输出风险与可追溯证据，训练型版本再输出校准信息；`GWTWScheduler` 决定继续、暂停或 fork。
- `ArtifactStore` 保存不可变证据，派生特征必须可追溯到 trial 与原始文件。
- 阶段 D 先用规则型 predictor 和串行异步 scheduler 打通闭环；稳定接口、训练模型、并行种群和完整数据基础设施后置。
- 提前停止只允许暂停并保留 checkpoint，不删除 trial 或 artifact；最终优劣仍只由真实 post-route QoR 判断。

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

## 阶段 D–H 的 Demo-first 原则

从阶段 D 开始，优先完成“能运行、有效果、可观察”的演示闭环，不再把生产级完备性或论文级实验作为阶段准入条件。每阶段只要求：

1. 有一条明确命令可以启动；
2. 终端或 trace 能看到本阶段新增的关键动作；
3. Trial、决策和最终结果能在 session 中定位；
4. 失败时有可读错误，不静默伪造成功。

严格预算零超限、主动 wall-clock 中断、跨进程完整恢复、稳定节点 ID、路径完全脱敏、并行容错、多 seed 统计显著性、模型校准和性能优势证明统一后置。它们作为后续工程清单维护，不阻塞 Demo，也不得把尚未完成的细节描述为生产能力。

## 阶段 D：Doomed Runs 与 GWTW 可观察闭环

**阶段目标**：在真实 ORFS 小设计上演示一次完整的 Doomed Runs 与 GWTW 控制过程。

**最小实现**：

- 复用 Trial、PL/CTS checkpoint、优化树和现有参数空间；
- 在 PL、CTS 后构造最小观测，以规则将候选分为 `hard_dead`、`soft_bad`、`survivor`；
- 输出 `continue`、`pause`、`audit_continue` 和 fork 计划；
- 从 survivor checkpoint 创建 child，继续执行到 RT/finish；
- 将 observation、decision、fork、execution resolution 和 final QoR 写入 session。

**Demo 命令**：

```bash
python3 main.py --stage-d configs/experiments/stage-d-smoke.yml
```

**交付物**：规则型 predictor、串行 GWTW scheduler、checkpoint child 执行、decision trace、真实 smoke YAML。

**最低验收**：

- 命令正常结束且无 cohort 失败；
- PL、CTS 两层均有可见决策；
- trace 中能看到 pause、audit、fork 和 checkpoint 消费；
- 至少两个 Trial 完成 finish，并保存真实 post-route QoR；
- session 中存在 `trials.jsonl`、`tree.json`、`traces/decisions.jsonl` 和配置快照。

**允许后置**：严格预算边界、CLI resume、主动超时中断、跨进程稳定 tree ID、完整路径脱敏、QoR 改善证明和生产级异常恢复。

## 阶段 E：Agent 指令驱动的参数探索 Demo

**阶段目标**：让用户输入一条自然语言优化指令，Agent 生成受限参数建议，并驱动一次可观察的 ORFS 试验。

**最小实现**：

- 定义简单的 `SearchPolicy.propose(observation) -> action` 接口；
- 支持一个 LLM policy 和一个无需网络的 deterministic mock policy；
- prompt 只包含当前设计、阶段观测、合法参数及范围；
- 对 Agent 输出做字段、类型、范围和阶段影响校验；
- 校验通过后创建一个新 Trial；校验失败则记录原因并回退到 mock 或默认动作；
- 将输入指令、建议参数、校验结果、实际参数和最终 QoR 写入 trace。

**Demo 命令示例**：

```bash
python3 main.py --agent-demo \
  --instruction "优先改善时序，避免明显增加拥塞" \
  --config configs/experiments/stage-e-agent-demo.yml
```

**交付物**：最小 policy 接口、LLM/mock policy、action validator、Agent Demo YAML 和 trace。

**最低验收**：

- 输入指令后能看到 Agent 建议和实际采用的参数；
- Agent 不能输出任意 shell、Tcl、路径或 evaluator 修改；
- 至少一个建议被执行到 finish，或以明确失败原因结束；
- session 能关联 instruction、action、Trial 和 QoR。

**允许后置**：通用版本化 schema、完整 replay、多个 baseline policy、ParetoArchive、token 精确计费、prompt 自动优化和模型热切换。

## 阶段 F：数据型 Doomed Predictor Demo

**阶段目标**：用已有 Trial 数据训练一个简单模型，在不替换规则安全兜底的前提下展示“模型给出 doomed 风险”的效果。

**最小实现**：

- 从现有 Trial 提取 WNS、TNS、runtime、stage、status 等少量表格特征；
- 使用 Logistic Regression、Decision Tree 或其他轻量模型中的一种；
- 生成一个可加载的模型文件和简要 dataset manifest；
- 模型输出 `risk_score` 与模型版本，默认只在 shadow mode 展示；
- 数据不足、字段缺失或模型加载失败时自动回退到阶段 D 的规则 predictor。

**Demo 命令示例**：

```bash
python3 tools/doomed_demo.py \
  --runs-dir runs/sky130hd_gcd \
  --stage PL
```

**交付物**：特征提取脚本、轻量模型、模型推理入口、预测结果 JSON。

**最低验收**：

- 能从真实 Trial 构建一个小数据集并完成训练或加载；
- 对至少一个 cohort 输出每个 Trial 的风险排序；
- 输出中能区分 rule risk 与 model risk；
- 模型失败不影响阶段 D 规则闭环继续运行。

**允许后置**：held-out design、PR-AUC/Brier、概率校准、OOD、反事实审计统计、图模型、序列模型和自动暂停权限。

## 阶段 G：种群调度与并发展示 Demo

**阶段目标**：把串行候选池包装成一个可观察的 bounded population Demo，并可选展示两个本地 worker 并发执行。

**最小实现**：

- 展示当前 population、active、paused、finished 和待补位数量；
- 复用阶段 D 的 survivor fork 与 audit 配额；
- 增加简单的 parent 后代上限，避免所有 child 都来自同一 parent；
- 默认继续支持串行执行；本地环境允许时增加 `workers: 2`；
- 每次调度后输出 population snapshot 或 summary JSON。

**Demo 命令示例**：

```bash
python3 main.py \
  --stage-g configs/experiments/stage-g-population-demo.yml
```

**交付物**：population summary、可选本地 worker、调度状态 CLI 或简易可视化。

**最低验收**：

- 能看到候选进入、暂停、继续、fork、finish 的状态变化；
- population 不超过 YAML 声明的演示上限；
- 至少两个不同 parent 或不同参数 child 可被观察；
- 串行模式始终可用，并发模式失败时可回退串行。

**允许后置**：Slurm、生产队列、严格预算预留、worker 崩溃恢复、任务抢占、分布式锁、经验 \(\kappa\)、多样性理论指标和等 CPU-hour 比较。

## 阶段 H：一键整合展示与简单对比

**阶段目标**：提供一个面向演示的一键入口，串联 Agent 指令、参数校验、checkpoint 执行、Doomed 判断、GWTW 调度和真实 QoR。

**最小实现**：

- 使用一个固定小设计和一个集成 YAML；
- 支持 `mock policy` 与真实 LLM policy 二选一；
- 运行结束后生成 session summary，列出 Trial lineage、关键决策、finish QoR 和失败原因；
- 只做一次简单对比：默认参数 vs AgenticPD+Doomed+GWTW；
- 提供一个 Markdown/JSON 汇总和一张简单表格或流程图。

**Demo 命令示例**：

```bash
python3 main.py \
  --demo configs/experiments/stage-h-integrated-demo.yml
```

**交付物**：一键 Demo YAML、汇总脚本、结果 JSON/Markdown、演示说明。

**最低验收**：

- 一条命令能启动完整流程；
- 终端能看到 Agent action、Doomed risk、GWTW action、checkpoint fork 和 finish QoR；
- 至少一个默认 Trial 和一个调度后的候选完成真实 finish；
- 结果汇总可从 Trial 回溯 parent、参数、决策和原始 QoR；
- 即使没有得到更优 QoR，只要闭环真实执行且证据完整，Demo 仍视为通过。

**允许后置**：多 design、多 seed、同预算学术对照、Pareto/hypervolume、LLM 成本分析、误杀率、checkpoint cache hit 统计、技术论文和生产部署。

## D–H 后置工程清单

以下事项不再阻塞八阶段 Demo，但在准备长期实验、论文或生产使用前必须逐项关闭：

- child 创建前的精确预算预留与零超限保证；
- Stage D CLI resume、部分 cohort 恢复和跨进程稳定 tree ID；
- 主动 wall-clock 中断、worker 生命周期和异常清理；
- JSON、日志和配置快照的相对路径与脱敏；
- 完整 evaluator 版本、参数空间快照和环境版本记录；
- 数据集切分、模型校准、OOD、误杀率和反事实审计；
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
- GWTW 的 particle、resampling、diversity、队列、并发和恢复；阶段 D 先掌握串行异步近似，不把它表述为论文 Algorithm 1 的严格复现。
- 分类/回归、PR-AUC、Brier score、校准、design-level split、数据泄漏与反事实审计。
- 阶段 D 不训练模型；阶段 F 优先表格特征与 Logistic Regression、Random Forest 或 XGBoost，只有跨设计泛化明确不足时再考虑图或序列模型。

### Agent 工程

- 严格分离 observation、action 与 evaluator；以 schema 校验输入输出。
- 追踪 prompt、response、模型、temperature、token、policy version 与 validation outcome。
- 使用 deterministic fake policy 和离线 replay；禁止模型直接输出 shell、Tcl、路径或 evaluator 配置。
