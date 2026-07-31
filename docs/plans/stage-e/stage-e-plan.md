# Stage E：原多 Agent + Doomed/GWTW 集成 Demo 计划

## 1. 目标

Stage E 新增一条独立运行入口，在不改变原始 `main.py` 和 `Optimizer.run()` 行为的前提下，复用同一个 Judge Agent、FP/PL/CTS/RT 四个 Stage Agent，并在 PL、CTS 两个位置接入已完成的 Doomed/GWTW 组件。

```text
Judge + Stage Agents 生成多个候选
                 ↓
             执行到 PL
                 ↓
       Doomed → GWTW → pause/fork
                 ↓
        CTS Agent → 执行到 CTS
                 ↓
       Doomed → GWTW → pause/fork
                 ↓
             RT Agent → finish
                 ↓
             真实 post-route QoR
```

本阶段只证明原多 Agent 决策、Doomed 淘汰、GWTW 资源分配和 checkpoint child 能在同一真实闭环中工作，不要求 QoR 改善。

## 2. 固定边界

### 2.1 原入口保持不变

- 原 `main.py`、`Optimizer`、baseline、`--mock-llm`、`--mock-orfs` 和 resume 行为不得改变。
- 原 Judge/Stage Agent prompt、输出 schema、参数校验和 LLM client 继续作为唯一 Agent 实现。
- Stage D 的 `main.py --stage-d` 入口继续保留，用于组件级回归。
- 新模式不得通过隐藏开关改变原优化循环。

### 2.2 新入口

新增独立薄 CLI：

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

CLI 只负责读取 YAML、选择真实或 mock LLM/ORFS、创建 manager 和启动 `MultiAgentGWTWOrchestrator`，不得承载调度算法。

### 2.3 权限划分

- Judge 决定候选探索节点和给各 Stage Agent 的 hint。
- FP/PL/CTS/RT Agent 只生成自己阶段的参数，继续使用现有 validator。
- Doomed 只根据阶段观测分类，不调用 LLM、不执行 I/O。
- GWTW 决定 survivor 白名单、continue/pause/audit 配额和需要补充的 child 数量。
- Judge 选择 parent 时只能使用 GWTW survivor 白名单；越界选择必须拒绝或使用有 trace 的确定性 fallback。
- GWTW 不生成参数；Agent 不决定 checkpoint 是否能消费。
- checkpoint resolver 是实际 parent、checkpoint 和执行起点的唯一裁决者。

## 3. 最小运行流程

### 3.1 初始化 population

- YAML 固定 `population_size: 4`、seed、PL/CTS survivor 与 audit 配额、trial 上限、platform/design 和 evaluator。
- 为每个候选调用现有 Judge 和当前执行范围内的 Stage Agent，生成合法参数。
- 初始候选至少让 FP、PL Agent 各产生一次真实 proposal，并分别执行到 PL。
- 每个候选在执行前创建 Trial；失败也必须持久化，不得从 cohort 中静默消失。

### 3.2 PL 决策与补位

- 为同层候选构造 `MinimalObservation`，调用现有 DoomedPredictor 和 GWTWScheduler。
- hard-dead 与 pause Trial 不得成为 parent；audit_continue 可继续但默认不作为 fork parent。
- GWTW 给出 survivor 白名单和补位数量；Judge 在白名单内选择探索节点，Stage Agent 生成 checkpoint 下游参数。
- child 必须经过现有 mutation/参数校验和 checkpoint resolver，再执行到 CTS。

### 3.3 CTS 决策与 finish

- CTS 重复 Doomed/GWTW，保留 survivor、audit 和 pause 证据。
- CTS child 只能从合法 CTS checkpoint 继续；实际起点由 resolver 决定。
- RT Agent 生成路由阶段参数，最终存活候选执行到 finish。
- 只有真实 finish 且 QoR 完整的 Trial 进入最终结果。

### 3.4 证据

继续使用 `DecisionTraceWriter`。除 Stage D 条目外，增加或明确记录：

- Judge 的 branch node、branch stage、hint、允许的 survivor 节点集合与 fallback；
- 四个 Stage Agent 的参数 proposal 和 reason；
- Agent proposal 实际对应的 Trial；
- observation、Doomed/GWTW、fork intent、fork、ExecutionResolution；
- finish 状态、QoR 或失败原因。

终端至少能看到 Judge、FP/PL/CTS/RT Agent、PL/CTS Doomed/GWTW、checkpoint fork 和 finish QoR。

## 4. 实现原则

- 优先组合现有模块，不复制 Judge、Stage Agent、Doomed、GWTW、resolver 或 QoR 规则。
- 新增 `multi_agent_gwtw_orchestrator.py` 负责 population barrier 和模块编排。
- 如需共享 copy → clean → stage → finish，可从 Stage D 抽取一个窄的执行 helper；不得修改原 `Optimizer` 的控制流或外部行为。
- parent 白名单校验放在 Agent 输出与 child 创建之间，不能只靠 prompt。
- 所有路径从 YAML、FrameworkConfig 或 session 推导，禁止硬编码用户目录。
- 真实和 mock 模式必须使用同一 orchestration 路径，只替换 LLM/runner。

## 5. 非目标

- 不实现自然语言 instruction 新接口、对话 Agent 或新的 SearchPolicy。
- 不修改原 `main.py` 默认入口、原 `Optimizer.run()` 或 Judge/Stage Agent 基本职责。
- 不修改参数空间、QoR comparator、ORFS 命令语义或实验契约。
- 不训练 Doomed 模型，不实现 GNN/LSTM、概率校准或 OOD。
- 不实现并发 worker、Slurm、生产级 resume、主动 wall-clock 中断或分布式锁。
- 不做 Random/BO/Pareto 对照、多 seed、统计显著性或性能优势声明。

## 6. 实施步骤

1. **独立入口与契约**：实现 YAML 配置、薄 CLI、survivor 白名单和 Agent proposal 到 Trial 的关联。
2. **多 Agent population**：复用 Judge 与四个 Stage Agent 生成候选，支持执行到 PL/CTS barrier 和从 checkpoint 继续。
3. **接入 Doomed/GWTW**：在 PL/CTS 完成 pause、audit、survivor fork、补位、finish 与完整 trace。
4. **集中验收**：完成纯 Python 回归，再运行真实 LLM + 真实 ORFS Demo。

## 7. 允许修改范围与交付物

允许的最小范围：

- 新增 `multi_agent_gwtw.py`；
- 新增 `multi_agent_gwtw_orchestrator.py` 及必要的纯数据 helper；
- 新增 `configs/experiments/multi-agent-gwtw-demo.yml`；
- 为 Agent 决策 trace 增加最小字段或条目；
- 必要时抽取 Stage D 的共享执行 helper；
- 新增对应纯 Python 测试。

除非验收发现真实接口缺口，否则不修改 `main.py`、`optimizer.py`、`agents.py`、参数空间、QoR comparator 或既有 schema。确需修改时，Claude 必须先在交付报告中说明原因，不得自行扩大范围。

不得修改 `docs/Note.md`、fixtures、`.env`、密钥、实验契约或 CI/CD。

交付物：

- 独立多 Agent + GWTW CLI 和 YAML；
- `MultiAgentGWTWOrchestrator`；
- Judge/四 Stage Agent 与 population 的关联证据；
- PL/CTS Doomed/GWTW、checkpoint child、finish QoR；
- 纯 Python 回归和真实 Demo 证据。

## 8. 纯 Python 验收

测试不得调用网络、真实 LLM、ORFS、OpenROAD、PDK 或已有 `runs/`。必须直接证明：

- 导入或运行原 `main.py` 不进入新 orchestrator，原 Optimizer 回归保持通过；
- 新 CLI 的 `--help` 无副作用，缺失或非法 YAML 明确失败；
- mock 模式实际调用 Judge，以及 FP、PL、CTS、RT 四个 Stage Agent；
- Judge 选择只允许落在 GWTW survivor 白名单，hard-dead/pause 节点被拒绝；
- 相同 seed 和输入的 Agent/GWTW 结果确定；
- PL、CTS 都产生 observation、Doomed/GWTW 和 cohort 完成证据；
- pause Trial 保留 checkpoint、Agent proposal 和 trace ref；
- fork child 的参数来自对应 Stage Agent，parent 来自合法 survivor；
- resolver 的 requested/effective start、checkpoint 与 Trial 一致；
- copy → clean → stage → finish 顺序正确；
- 至少两个 mock 候选完成 finish，final QoR 和树来源可回溯；
- Stage D 和全部既有测试保持通过。

最低命令：

```bash
make test
python3 schemas/trial.py
python3 orchestrator.py
python3 multi_agent_gwtw.py --help
python3 main.py --help
python3 tools/trial_inspect.py --help
python3 tools/trial_reproduce.py --help
python3 tools/clean.py --help
```

## 9. 真实 Demo 验收

配置：

```text
configs/experiments/multi-agent-gwtw-demo.yml
platform/design: sky130hd/gcd
population_size: 4
decision_stages: [PL, CTS]
policy: real LLM
```

命令：

```bash
python3 multi_agent_gwtw.py \
  --config configs/experiments/multi-agent-gwtw-demo.yml
```

通过标准：

- 原 `main.py` 无行为变化；
- 终端或 trace 中能定位一次 Judge 决策及 FP、PL、CTS、RT Agent proposal；
- PL、CTS 均出现 Doomed 分类与 GWTW 动作；
- 至少出现一次 pause、一次 audit_continue 和一次 survivor checkpoint fork；
- hard-dead/pause Trial 未被选作 parent；
- 至少两个 Trial 完成真实 ORFS finish，并保存 WNS、TNS、面积和功耗；
- session 中可从 Agent proposal 回溯 Trial、Doomed/GWTW、parent/child、checkpoint 和原始 ORFS 报告；
- 无 cohort 失败、Traceback、密钥泄露或静默伪造成功。

QoR 没有改善仍可通过；真实 LLM 或 ORFS 工具失败可以证明失败路径，但不能替代完整成功 Demo。

## 10. 后置项与完成定义

后置：并发、Slurm、完整 resume、主动超时、稳定 tree ID、严格预算零超限、路径完全脱敏、训练型 Doomed、多 seed 和性能对照。

Stage E 完成条件：

- 新入口完成多 Agent + Doomed/GWTW 的纯 Python 闭环；
- 原入口与 Stage D 无回归，P0/P1 为零；
- 真实 LLM + 真实 ORFS 满足第 9 节通过标准；
- 验收报告完成；
- 用户授权后 commit、merge；push 单独授权。
