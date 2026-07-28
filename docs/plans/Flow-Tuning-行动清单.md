# Flow Tuning 行动清单：AgenticPD + GWTW + Doomed Runs

> 导师给出的最终路线：`AgenticPD + Go-With-The-Winners (GWTW) + Doomed Runs`  
> 计划周期：45 天  
> 当前范围：基于 OpenROAD-flow-scripts（ORFS）的物理设计 flow tuning，不修改 RTL，不修改 OpenROAD C++ 源码  
> 本文是接下来执行工作的主清单；`Agentic-PD_ORFS-Agent项目计划书.md` 作为背景和详细工程规范保留

## 1. 最终要做成什么

最终系统不是一个让 LLM 随意执行 Tcl 的聊天机器人，而是一个受约束、可恢复、可比较的物理设计优化控制器：

```text
初始化一组差异化 ORFS flow 配置
        ↓
运行到阶段 checkpoint
        ↓
AgenticPD：判断从哪个历史节点、哪个阶段继续优化
        ↓
Doomed Runs：估计候选最终失败或严重退化的风险
        ↓
GWTW：暂停高风险候选，把预算分配给有希望且保持多样性的候选
        ↓
从 checkpoint fork 新参数分支
        ↓
所有最终候选使用统一 post-route evaluator
        ↓
保存优化树、模型判断、运行成本和最终 Pareto 结果
```

三部分各自解决不同问题：

| 部分 | 解决的问题 | 在系统中的角色 |
|---|---|---|
| AgenticPD | 下一步应该优化哪个阶段、从哪个历史状态继续、参数如何修改 | 搜索状态表示与决策策略 |
| GWTW | 固定 CPU-hour 或 stage-run 预算应该分给哪些候选 | 群体调度与预算重分配 |
| Doomed Runs | 哪些中间运行大概率无法得到可接受的最终结果 | 风险预测与保守早停 |

不能把三者简单拼接成“LLM 给每个 run 打分”。正确分工是：

- ORFS Runner、Parser、Evaluator 和 Checkpoint Manager 是确定性基础设施；
- Doomed predictor 输出风险、预期最终 QoR 和置信度，不直接删除运行；
- GWTW scheduler 根据风险、当前 QoR、群体多样性和预算决定资源分配；
- AgenticPD controller 选择 branch point、目标阶段和受约束参数动作；
- 最终结论只由固定的 post-route evaluator 给出。

## 2. 45 天结束时的验收标准

### 2.1 必须完成

- [ ] 固定 ORFS、OpenROAD、PDK、benchmark 和容器版本；
- [ ] 至少在 2 个开发设计和 1 个 held-out 设计上稳定运行；
- [ ] 支持完整运行和至少 `placement`、`CTS`、`routing` 三类 checkpoint 恢复；
- [ ] 每个 trial 记录 parent、branch stage、参数、checkpoint、阶段指标和最终指标；
- [ ] 实现 default、random search、top-k greedy 三个基础 baseline；
- [ ] 实现一个不依赖 LLM 的 stage-aware baseline；
- [ ] 实现 AgenticPD 式单 Controller，能选择 branch point 和 stage action；
- [ ] 实现固定大小 population 的 GWTW scheduler；
- [ ] 实现第一版 Doomed predictor，并输出校准后的风险或置信区间；
- [ ] 对被预测为 doomed 的候选只执行可恢复暂停，并保留反事实复跑样本；
- [ ] 所有方法在相同 backend-run、stage-run 或 CPU-hour 预算下比较；
- [ ] 最终统一报告 post-route WNS、TNS、area、power、DRC、wall-clock；
- [ ] 报告首次达标时间、最终 Pareto hypervolume、误杀赢家率和节省的计算量；
- [ ] 从空环境能按 README 复现至少一个小规模实验。

### 2.2 明确不做

- [ ] 不让 Agent 执行任意 shell 或任意 Tcl；
- [ ] 不修改 OpenROAD C++ 源码；
- [ ] 不同时适配 Innovus、iEDA、ECOS Studio 等其他 flow；
- [ ] 不在基础 runner 未稳定前实现多 Agent；
- [ ] 不把中间 WNS 当作最终 ground truth；
- [ ] 不直接按照 Doomed predictor 的单点预测永久终止运行；
- [ ] 不把 Web UI、MCP 或复杂微服务放进关键路径；
- [ ] 不在样本不足时直接训练 GNN + LSTM。

## 3. 需要 clone 的仓库

代码项目建立后，所有第三方仓库统一放在项目配置指定的 `third_party/` 或工作区外部依赖目录中；不得在 Python 源码中写死绝对路径。clone 后立即记录 commit SHA，实验期间不随意更新。

### 3.1 第一优先级：必须 clone

#### A. OpenROAD-flow-scripts

仓库：<https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts>

用途：

- 完整 RTL-to-GDSII 执行底座；
- 设计与平台配置；
- Make stage targets；
- logs、reports、results 和阶段数据库；
- checkpoint 恢复；
- OpenROAD、Yosys 和 PDK 的受管版本。

建议命令：

```bash
git clone --recursive https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts.git
```

操作清单：

- [ ] 阅读根目录 README 和官方 Flow Tutorial；
- [ ] 跑通官方最小设计；
- [ ] 找到 design `config.mk`、约束文件和阶段 target；
- [ ] 列出 placement、CTS、routing 的输入、输出和 checkpoint；
- [ ] 确认 WNS/TNS、area、power、DRC 分别从哪个报告读取；
- [ ] 记录 ORFS、OpenROAD、Yosys、PDK commit；
- [ ] 验证相同配置重复运行结果是否一致。

注意：第一版不需要单独 clone OpenROAD 和 Yosys。ORFS 已通过子模块或固定依赖管理它们。只有调试 API 或以后进入 Tool-Evolve 时，才把 OpenROAD 作为独立开发仓库。

#### B. MLCAD26 Contest Scripts and Benchmarks

仓库：<https://github.com/ASU-VDA-Lab/MLCAD26-Contest-Scripts-Benchmarks>

用途：

- 学习固定工具版本、benchmark、正确性检查和 evaluator 的边界；
- 参考 `evaluation.tcl`、`parse_log.py`、`compute_score.py`；
- 参考 sample algorithm discovery 工作流；
- 建立“候选不能修改 evaluator”的实验纪律。

建议命令：

```bash
git clone --recursive https://github.com/ASU-VDA-Lab/MLCAD26-Contest-Scripts-Benchmarks.git
```

操作清单：

- [ ] 先复现官方 baseline；
- [ ] 阅读 `evaluation/`；
- [ ] 阅读 `sample_algorithm_discovery_flow_and_resources/`；
- [ ] 理清候选生成、运行、验证、解析、计分和归档边界；
- [ ] 只迁移通用 evaluator/parser 思想，不把竞赛 flow 整体嵌入 ORFS-Agent。

### 3.2 第二优先级：为 Agent harness clone

#### C. RTLScout

仓库：<https://github.com/huawei-csl/rtlscout>

用途：

- 借鉴 Agent 与 evaluator 解耦；
- 借鉴 run directory、模型适配器和 fake-model smoke test；
- 借鉴 multi-run elite pool；
- 借鉴 Pareto front 提取、批量评价和绘图。

建议命令：

```bash
git clone --recursive https://github.com/huawei-csl/rtlscout.git
```

优先阅读：

- [ ] `core/`；
- [ ] `run_benchmark.py`；
- [ ] `run_eval.py`；
- [ ] `run_multirun.py`；
- [ ] `extract_best_designs.py`；
- [ ] `extract_pareto.py`；
- [ ] fake model 与测试目录。

迁移边界：

- 可以迁移实验 harness、模型接口、结果落盘和 Pareto 处理方式；
- 不迁移 RTL mutation、RTL prompt 和 Verilator-specific action space；
- 复制源码前检查许可证，并记录来源和修改。

### 3.3 暂时不要 clone

- `AutoEDA`：只参考 tool schema、session 和 timeout 思想；公开代码入口不稳定，而且商业工具 Tcl 与 ORFS 不兼容；
- `ECOS Studio`：属于替代平台，会扩大适配范围；
- `ArtNet`：等真实样本不足且需要数据增强时再使用；
- `Kepler Formal`：当前只改 flow 参数，不修改 RTL；
- `sv-elab`：ORFS/OpenDB observation 不够时再考虑；
- 独立 `OpenROAD`、`Yosys`：先使用 ORFS 固定版本。

## 4. 论文阅读顺序

阅读不是从头到尾平均用力。每篇阅读完成后必须产出一张“实现映射表”：论文概念、系统模块、需要的数据、可验证实验、不能照搬的部分。

### 4.1 第 1 篇：AgenticPD

原文：[AgenticPD.pdf](../papers/Physical-Design-Optimization/AgenticPD.pdf)  
译文：[translated_AgenticPD.md](../02_translations/full_text/translated_AgenticPD.md)  
报告：[rpt_AgenticPD.md](../03_notes/paper_notes/rpt_AgenticPD.md)

阅读目标：

- [ ] 理解 `FP → PL → CTS → RT` 的 stage action space；
- [ ] 理解 optimization tree 的 node、edge、root-to-leaf path；
- [ ] 理解 checkpoint branching 为什么同时节省计算并改变搜索空间；
- [ ] 理解 Judge Agent 与 Stage Agent 的职责分离；
- [ ] 理解 exploration balance 和 stage bottleneck；
- [ ] 理解 timing-primary、area/power guardrail；
- [ ] 整理所有需要复现但论文未充分说明的参数和公式。

读完必须回答：

1. 一个 tree node 具体保存哪些字段？
2. 从 CTS 节点分支时哪些阶段复用、哪些阶段重跑？
3. Judge 输出什么，Stage Agent 输出什么？
4. 如何防止 Agent 输出任意 Tcl？
5. 为什么每个候选仍要使用 post-route evaluator？

第一版实现要求：

- 先用一个 Controller 输出 `branch_from + target_stage + parameter_changes`；
- Judge 和 Stage policy 先做成规则/普通 Python 接口；
- 单 Controller 稳定后再替换成 LLM 或拆成多 Agent。

### 4.2 第 2 篇：Go With The Winners

原文：[Go-With-The-Winners.pdf](../papers/Others/Go-With-The-Winners.pdf)  
译文：[translated_Go-With-The-Winners.md](../02_translations/full_text/translated_Go-With-The-Winners.md)  
报告：[rpt_Go-With-The-Winners.md](../03_notes/paper_notes/rpt_Go-With-The-Winners.md)

阅读目标：

- [ ] 理解 particle、depth、survivor、reproduction 和 budget；
- [ ] 理解为什么复制赢家可以把预算集中到稀有成功路径；
- [ ] 理解论文中的 `κ` 表示什么；
- [ ] 理解理论保证不等于 EDA 中实际有效；
- [ ] 理解 GWTW 与普通 top-k greedy 的区别；
- [ ] 理解为什么必须维持 population diversity。

映射到 ORFS：

| GWTW | ORFS Flow Tuning |
|---|---|
| particle | 一个配置、运行状态及 checkpoint |
| depth | flow stage 或优化轮次 |
| survivor | 仍值得继续执行的候选 |
| leaf | 失败、被暂停或到达 final evaluator 的候选 |
| reproduction | 从 survivor checkpoint fork 新参数 |
| population size `B` | 并行 run 数或活跃候选上限 |

第一版实现要求：

- population size 从 `B=4` 开始；
- 每个 checkpoint 将候选分为 `hard_dead`、`uncertain`、`promising`；
- 只强制暂停 `hard_dead`；
- 至少给 `uncertain` 保留 20% exploration quota；
- fork 时必须改变参数、seed 或 action template；
- 记录 unique configuration 数量，监测 mode collapse。

### 4.3 第 3 篇：Doomed Run Prediction

原文：[Doomed-Run-Prediction.pdf](../papers/Physical-Design-Optimization/Doomed-Run-Prediction.pdf)  
译文：[translated_Doomed-Run-Prediction.md](../02_translations/full_text/translated_Doomed-Run-Prediction.md)  
报告：[rpt_Doomed-Run-Prediction.md](../03_notes/paper_notes/rpt_Doomed-Run-Prediction.md)

阅读目标：

- [ ] 理解为什么 PD flow 是阶段序列而非静态样本；
- [ ] 理解 GNN 如何编码不同规模 netlist；
- [ ] 理解 LSTM 如何利用多阶段状态；
- [ ] 区分“预测 post-route TNS”和“安全地停止运行”；
- [ ] 理解 design-level holdout，避免同一设计的数据泄漏；
- [ ] 理解 false-positive pruning 为什么比普通预测误差更危险；
- [ ] 理解不确定度、双阈值和反事实审计。

第一版不要复现 GNN + LSTM。先完成：

```text
ORFS 阶段报告
  → 手工表格特征
  → XGBoost / Random Forest / Logistic Regression
  → predicted success probability + calibration
  → safe-stop / uncertain / must-continue
```

候选特征优先级：

- 当前阶段 WNS、TNS 及其相对 baseline 变化；
- setup/hold violation 数；
- slew/capacitance/fanout violation；
- cell、buffer、inverter 数量；
- utilization、placement density；
- congestion overflow、wirelength；
- area、power；
- 已耗 runtime；
- 参数配置；
- 前几个 checkpoint 的指标变化趋势。

只有满足以下条件才进入 GNN/LSTM：

- [ ] 表格 baseline 已稳定；
- [ ] 至少积累足够的完整 multi-stage trajectory；
- [ ] design-level holdout 已建立；
- [ ] 确认图结构确实带来超出表格特征的收益；
- [ ] 有时间实现 OpenDB graph exporter 和特征一致性检查。

### 4.4 第 4～6 篇：评价与工程补充

#### PDAGENT-BENCH

原文：[PDAGENT-BENCH.pdf](../papers/Evaluation-Benchmarks/PDAGENT-BENCH.pdf)  
报告：[rpt_PDAGENT-BENCH.md](../03_notes/paper_notes/rpt_PDAGENT-BENCH.md)

- [ ] 学习 EDA Agent 能力拆分；
- [ ] 检查工具调用、失败恢复、结果验证如何评价；
- [ ] 避免只按“任务是否执行”评价而忽略最终 PPA。

#### TicTacBench

原文：[TicTacBench.pdf](../papers/Evaluation-Benchmarks/TicTacBench.pdf)  
报告：[rpt_TicTacBench.md](../03_notes/paper_notes/rpt_TicTacBench.md)

- [ ] 学习昂贵 final oracle 与便宜 inner-loop proxy 的分离；
- [ ] 学习 trajectory failure taxonomy；
- [ ] 设计 proxy 与 post-route ground truth 的偏差分析。

#### RTLScout

原文：[RTLScout.pdf](../papers/Agentic-RTL/RTLScout.pdf)  
报告：[rpt_RTLScout.md](../03_notes/paper_notes/rpt_RTLScout.md)

- [ ] 学习 evaluator/agent 解耦；
- [ ] 学习 elite pool、multi-run 和 Pareto archive；
- [ ] 学习 fake model smoke test 和可复现实验目录。

### 4.5 暂缓阅读

以下论文与长期方向有关，但不应阻塞主线：

- `ArtNet`：数据增强与泛化；
- `VeoPlace`：VLM/placer 闭环与多样性历史；
- `InsightAlign`：离线策略迁移和在线修正；
- `Protected-White-Box-DSE`：进入 Tool-Evolve 时再重点阅读；
- `Expert-Level-RL-Placement`：需要 learned reward 或 imitation 时再读；
- `CHIP-MAP`：做 macro-placement vertical slice 时再读；
- `ModuPlace`：研究结构化中间表示和 downstream verification 时再读。

## 5. 需要快速补齐的知识

### 5.1 物理设计基础：优先级最高

目标不是推导全部算法，而是能解释每个阶段的输入、输出、可调参数和失败模式。

- [ ] Synthesis 输出 netlist、SDC 与 library mapping；
- [ ] Floorplan 中 die/core、utilization、aspect ratio、macro、IO、PDN；
- [ ] Global/detailed placement、legalization、density、congestion；
- [ ] STA 基础：arrival/required time、slack、WNS、TNS；
- [ ] setup/hold、slew、capacitance、fanout violation；
- [ ] CTS 中 skew、latency、buffer insertion；
- [ ] Global/detailed routing、RC parasitic、via、DRC；
- [ ] 为什么 placement/CTS 指标不能替代 post-route signoff；
- [ ] area、power、timing、congestion 之间的 trade-off。

学习方式：

1. 先完成 ORFS 官方 Flow Tutorial；
2. 用 `open-eda-course` 补 Yosys/OpenROAD 操作；
3. 每学一个阶段，亲自查看一次该阶段的 log、report、database 和 GUI；
4. 建立“参数—直接影响—下游风险—可观察指标”表。

### 5.2 ORFS/OpenROAD 工程知识

- [ ] Linux、Docker、Git submodule；
- [ ] GNU Make target 和变量覆盖；
- [ ] Tcl 基础语法；
- [ ] Python `subprocess`、timeout、signal、日志流式读取；
- [ ] ORFS design config 和平台配置；
- [ ] OpenROAD Tcl/Python API；
- [ ] OpenDB/ODB 的基本对象；
- [ ] checkpoint 保存、恢复和兼容性；
- [ ] commit、环境、配置 hash 的可复现记录。

### 5.3 搜索与多目标优化

- [ ] random/grid search；
- [ ] Bayesian optimization 的基本概念，至少会使用一个库；
- [ ] Pareto dominance、Pareto front、hypervolume；
- [ ] exploitation 与 exploration；
- [ ] population-based search；
- [ ] sequential Monte Carlo / resampling 的直觉；
- [ ] diversity metric 和 mode collapse；
- [ ] 相同 CPU-hour、backend-run、stage-run 预算下的公平比较。

### 5.4 Doomed Runs 所需机器学习知识

- [ ] regression 与 classification 的区别；
- [ ] train/validation/test 按 design 切分；
- [ ] class imbalance；
- [ ] precision、recall、ROC-AUC、PR-AUC；
- [ ] calibration curve、Brier score；
- [ ] false positive/false negative 的成本不对称；
- [ ] prediction interval 或 conformal prediction 的基本思想；
- [ ] out-of-distribution 检测的基本概念；
- [ ] counterfactual audit：让部分被暂停候选继续跑完以获得真值。

### 5.5 Agent 工程

- [ ] JSON Schema 或 Pydantic；
- [ ] tool calling 与 structured output；
- [ ] observation/action/evaluator 分离；
- [ ] prompt 与模型版本记录；
- [ ] deterministic fake policy；
- [ ] budget、retry、rollback 和 failure taxonomy；
- [ ] Agent 决策证据与原 trial 的可追溯关系。

## 6. 分阶段行动清单

## 阶段 0：冻结实验口径（第 1～2 天）

- [ ] 选择一个小设计作为 smoke test；
- [ ] 选择两个开发设计和一个 held-out 设计；
- [ ] 固定一个公开 platform；
- [ ] 固定 ORFS/OpenROAD commit 和 Docker image；
- [ ] 定义最终 ground truth；
- [ ] 定义一次实验的 backend-run、stage-run 和 CPU-hour 预算；
- [ ] 定义失败 trial 是否计入预算；
- [ ] 定义成功条件和 DRC/area/power guardrail；
- [ ] 建立源码项目独立 `AGENTS.md`；
- [ ] 建立参数化目录结构，禁止硬编码本机路径。

验收门：

> 能用一份配置文件唯一确定 design、platform、tool commit、参数、预算和 evaluator。

## 阶段 1：ORFS 基线和确定性 Harness（第 3～9 天）

- [ ] clone ORFS；
- [ ] 跑通一个官方设计；
- [ ] 重复运行并检查结果一致性；
- [ ] 实现 `OrfsRunner`；
- [ ] 实现 timeout 和 failure classifier；
- [ ] 实现 `ReportParser`；
- [ ] 实现 `ExperimentStore`；
- [ ] 保存 raw log/report/result；
- [ ] 保存环境、commit、配置 hash；
- [ ] 编写 parser fixture 和单元测试；
- [ ] 用 fake runner 验证不依赖 EDA 工具的控制逻辑。

最低数据模型：

```text
Experiment
Trial(parent_trial_id, resume_stage, action, status, artifact_dir)
StageMetrics(stage, WNS, TNS, violations, area, power, congestion, runtime)
FinalMetrics(post-route WNS/TNS, area, power, DRC, runtime)
Checkpoint(stage, parent, config_hash, artifact_path)
DecisionTrace(observation, risk, action, reason, policy_version)
```

验收门：

> 不调用 LLM，也能批量执行配置、解析指标、分类失败并重放实验。

## 阶段 2：Checkpoint DAG 与动作白名单（第 10～14 天）

- [ ] 列出可稳定恢复的 stage boundary；
- [ ] 验证 placement、CTS、routing checkpoint 的恢复方式；
- [ ] 检查 upstream config hash 兼容性；
- [ ] 实现 `CheckpointManager`；
- [ ] 实现 parent-child trial DAG；
- [ ] 定义第一版 10～20 个参数；
- [ ] 每个参数记录 type、range、stage、default、constraint；
- [ ] 实现 JSON Schema/Pydantic 校验；
- [ ] 拒绝任意 shell、任意 Tcl 和 evaluator 修改；
- [ ] 从同一 checkpoint fork 两个不同参数并运行到 post-route。

验收门：

> 能从历史 checkpoint 产生两个可追溯分支，而且不会错误复用不兼容的上游状态。

## 阶段 3：非 Agent 搜索基线与数据采集（第 15～20 天）

- [ ] default ORFS；
- [ ] random search；
- [ ] 小规模 grid search；
- [ ] top-k greedy；
- [ ] 一个 Bayesian optimization baseline；
- [ ] 统一运行预算和 evaluator；
- [ ] 在 placement、CTS、global route 保存中间特征；
- [ ] 每个候选尽量运行到 post-route，建立 early-to-final 数据集；
- [ ] 检查参数、stage metric 和 final metric 的缺失值及单位；
- [ ] 输出 baseline Pareto front；
- [ ] 输出不同阶段 proxy 与最终 QoR 的相关性。

验收门：

> 已拥有可训练 Doomed predictor 的完整 trajectory 数据，并能公平比较传统搜索方法。

## 阶段 4：AgenticPD 最小版本（第 21～26 天）

- [ ] 先实现 rule-based branch selector；
- [ ] 定义 tree node profile；
- [ ] 定义 stage bottleneck；
- [ ] 定义 exploration count/balance；
- [ ] 实现 `branch_from + target_stage + parameter_changes` 动作；
- [ ] 实现 timing-primary、area/power guardrail；
- [ ] 实现 best-so-far 和 Pareto archive；
- [ ] 实现 deterministic fake agent；
- [ ] 接入一个 LLM policy；
- [ ] 保存完整 prompt、observation、action、校验结果和 token；
- [ ] 对比 full restart 与 checkpoint branching 的 stage-run、wall-clock；
- [ ] 对比随机选 stage 与 rule-based/LLM stage selection。

验收门：

> Agent 能在受约束 action space 中选择历史节点并产生分支，且系统无需信任自然语言输出。

## 阶段 5：GWTW Population Scheduler（第 27～31 天）

- [ ] population size 先固定为 `B=4`；
- [ ] 初始化差异化参数配置；
- [ ] 定义 `hard_dead`、`uncertain`、`promising`；
- [ ] 定义 survivor score，但保留原始多目标指标；
- [ ] 从 promising checkpoint fork 新候选；
- [ ] 设置至少 20% exploration quota；
- [ ] 加入配置距离或 action distance，维持多样性；
- [ ] 防止同一 parent 无限复制；
- [ ] 记录每轮 survivor、暂停、fork 和预算变化；
- [ ] 与 independent random restart 和 top-k greedy 比较；
- [ ] 检查 unique configuration 数和 population entropy。

验收门：

> 在相同预算下，GWTW 能重新分配资源且不会快速坍缩成完全相同的候选。

## 阶段 6：Doomed Runs 第一版（第 32～37 天）

- [ ] 定义 doomed label，不能仅凭单个 WNS 阈值；
- [ ] 按 design 切分训练和测试；
- [ ] 建立 Logistic Regression baseline；
- [ ] 建立 Random Forest 或 XGBoost baseline；
- [ ] 比较单 checkpoint 与多 checkpoint 特征；
- [ ] 输出 final TNS regression 和 success classification 两类结果；
- [ ] 做 probability calibration；
- [ ] 定义 `safe_stop` 和 `must_continue` 双阈值；
- [ ] 中间不确定区域继续运行；
- [ ] 被暂停候选保留 checkpoint；
- [ ] 随机抽取 10%～20% 暂停候选继续跑完；
- [ ] 统计误杀赢家率；
- [ ] 统计节省 CPU-hour 与 Pareto 损失；
- [ ] 记录 OOD 或低置信输入；
- [ ] 暂不训练 GNN/LSTM，除非表格 baseline 和数据规模已经充分。

验收门：

> 预测器不仅报告 RMSE/AUC，还报告误杀赢家率、校准误差、节省时间和反事实审计结果。

## 阶段 7：三部分整合（第 38～41 天）

整合决策顺序：

```text
AgenticPD 提出 branch 和 stage action
        ↓
Guard 校验并创建 trial
        ↓
ORFS 执行到 checkpoint
        ↓
Parser 生成结构化 observation
        ↓
Doomed predictor 输出 risk + confidence
        ↓
GWTW scheduler 决定 continue / pause / fork / deprioritize
        ↓
最终候选运行到 post-route evaluator
        ↓
更新 optimization tree、population 和 Pareto archive
```

- [ ] Doomed predictor 只提供证据，不直接 kill；
- [ ] GWTW 统一管理 population 和预算；
- [ ] AgenticPD 只能操作经过 scheduler 分配的候选；
- [ ] 所有暂停操作可恢复；
- [ ] 每个 fork 都能追溯 parent 和决策原因；
- [ ] 避免同一指标在 Agent、predictor、scheduler 中被不同方式重复解释；
- [ ] evaluator 与搜索策略完全隔离；
- [ ] 完成一次端到端小预算实验。

验收门：

> 任意一个最终候选都能沿 DAG 回溯到 baseline，重建每次参数变化、风险判断、预算决策和真实 QoR。

## 阶段 8：最终实验与报告（第 42～45 天）

至少比较：

1. Default ORFS；
2. Random search；
3. Bayesian optimization；
4. AgenticPD only；
5. AgenticPD + GWTW；
6. AgenticPD + GWTW + Doomed Runs。

公平性要求：

- [ ] 相同 design/platform/tool commit；
- [ ] 相同参数空间；
- [ ] 相同 post-route evaluator；
- [ ] 相同 CPU-hour 或 stage-run 预算；
- [ ] 至少 3 个 seed，若算力不足必须明确说明；
- [ ] held-out design 不参与 prompt、阈值和模型调参；
- [ ] 失败和超时按预先规定计入预算；
- [ ] 不看结果后修改 score 权重。

必须报告：

- [ ] 最终 WNS、TNS、area、power、DRC；
- [ ] Pareto front 与 hypervolume；
- [ ] 首次达到 timing/DRC gate 的时间；
- [ ] 总 backend trial；
- [ ] 各 stage 实际执行次数；
- [ ] checkpoint cache hit；
- [ ] wall-clock、CPU-hour、峰值内存；
- [ ] LLM token 和费用；
- [ ] Doomed predictor precision/recall/calibration；
- [ ] 误杀赢家率；
- [ ] counterfactual audit；
- [ ] population diversity；
- [ ] full restart 与 branching 的成本差异；
- [ ] 成功案例和失败案例各一条完整 trajectory。

## 7. 第一周每天做什么

### Day 1

- [ ] 精读 AgenticPD 报告第 3、4、10、11 节；
- [ ] 画出自己的 node、trial、checkpoint 数据结构；
- [ ] clone ORFS；
- [ ] 记录当前 commit。

### Day 2

- [ ] 完成 ORFS Flow Tutorial；
- [ ] 跑通一个小设计；
- [ ] 找到各阶段 log、report、result、database；
- [ ] 手工提取一次 WNS、TNS、area、power、DRC。

### Day 3

- [ ] 阅读 GWTW 报告第 8～10 节；
- [ ] 把 particle/survivor/reproduction 映射成 ORFS 对象；
- [ ] 写出 `B=4` 的伪代码；
- [ ] 明确 exploration quota 和 diversity 条件。

### Day 4

- [ ] 阅读 Doomed Run 报告第 5～7 节；
- [ ] 定义第一版 early feature 表；
- [ ] 定义 success/doomed label；
- [ ] 定义 false-positive pruning 的审计方式。

### Day 5

- [ ] 设计 Experiment、Trial、Metrics、Checkpoint、DecisionTrace schema；
- [ ] 确认所有文件路径都从配置解析；
- [ ] 设计 run artifact 目录；
- [ ] 设计 environment manifest。

### Day 6

- [ ] 开始实现或设计 `OrfsRunner`；
- [ ] 支持 timeout、退出码、stdout/stderr 和 artifact 保存；
- [ ] 先用 fake command 做单元测试。

### Day 7

- [ ] 实现第一个 parser；
- [ ] 用固定 log fixture 验证单位、缺失值和异常格式；
- [ ] 复盘第一周是否已满足“无 LLM 可运行、可解析、可记录”；
- [ ] 若没有满足，不得进入 Agent prompt 开发。

## 8. 关键风险检查表

### 8.1 数据泄漏

- [ ] 同一 design 的不同配置不能同时出现在 Doomed predictor 的训练集和 held-out test；
- [ ] 归一化参数只用训练集统计；
- [ ] Agent memory 不得检索 held-out design 的最终结果；
- [ ] evaluator 数据不能进入搜索策略的隐藏调参。

### 8.2 早停误杀

- [ ] 不使用不可恢复 kill；
- [ ] 保存 checkpoint；
- [ ] 双阈值而非单阈值；
- [ ] 保留 exploration quota；
- [ ] 定期反事实复跑；
- [ ] 单独报告被误杀的最终 top-k/Pareto 候选。

### 8.3 群体坍缩

- [ ] 限制同一 parent 的 offspring 数；
- [ ] 参数和 action 保持最小距离；
- [ ] 保留随机探索槽位；
- [ ] 记录 population entropy/unique configs；
- [ ] 不只按一个 scalar score 复制。

### 8.4 Proxy 偏差

- [ ] 中间 WNS/TNS 只作为 observation；
- [ ] 分析每个阶段 proxy 与 post-route 指标的相关性；
- [ ] 所有论文结论使用 final evaluator；
- [ ] 报告 proxy 选错候选的案例。

### 8.5 不可复现

- [ ] 保存 commit、容器、PDK、seed、模型和 prompt 版本；
- [ ] 保存完整参数与配置 hash；
- [ ] 保存 raw artifact；
- [ ] 失败 trial 也入库；
- [ ] 不在实验结束后覆盖原始记录；
- [ ] 每个图表都能追溯生成脚本和数据。

## 9. 遇到时间不足时的裁剪顺序

按以下顺序裁剪，不得反向牺牲基础设施：

1. 取消 Web UI；
2. 取消 MCP；
3. 取消 Judge + 四个 Stage Agents，保留单 Controller；
4. 取消 GNN/LSTM，保留表格 Doomed predictor；
5. 将 population 从 8 降为 4；
6. 减少参数数量和 benchmark 规模；
7. 保留至少一个 held-out design 和三个核心消融。

无论时间多紧，都不能裁掉：

- 固定 evaluator；
- parser 和原始 artifact；
- checkpoint 可恢复性；
- traditional baseline；
- 同预算比较；
- Doomed false-positive audit；
- post-route 最终验证。

## 10. 当前最直接的下一步

按顺序执行，不并行扩展范围：

1. [ ] 精读 `rpt_AgenticPD.md` 的方法与落地章节；
2. [ ] clone 并跑通 ORFS；
3. [ ] 手工定位三个 checkpoint 和最终 QoR 报告；
4. [ ] 固定 Experiment/Trial/Checkpoint/Metrics schema；
5. [ ] 实现无 LLM 的 Runner、Parser、Store；
6. [ ] 从 checkpoint fork 两条分支；
7. [ ] clone MLCAD26，复现 evaluator baseline；
8. [ ] 采集 early-stage 到 post-route 的完整 trajectory；
9. [ ] 实现 random、top-k 和 BO baseline；
10. [ ] 实现 AgenticPD 单 Controller；
11. [ ] 实现 `B=4` 的 GWTW；
12. [ ] 实现表格特征 Doomed predictor；
13. [ ] 加入双阈值、可恢复暂停和反事实审计；
14. [ ] 完成三部分整合与消融实验。

