# Stage D：Doomed Runs 与 GWTW 最小闭环计划

## 1. 目标

Stage D 的目标是在不调用 LLM、不训练模型、不引入并行执行基础设施的条件下，打通一次可审计的 Doomed Runs 与 GWTW 控制闭环：

```text
阶段执行 → 最小观测 → 规则风险判断 → continue/pause/audit
                                      ↓
                           survivor checkpoint fork
                                      ↓
                              补位并继续到 finish
```

本阶段只证明控制链、恢复链和证据链成立，不复现论文的 GNN/LSTM，也不声称串行异步实现具有论文 Algorithm 1 的理论保证。最终候选优劣仍只由 ORFS post-route QoR 判断。

开始该闭环前，必须先关闭普通优化循环的 checkpoint 执行决议缺口：任何 fork 都必须验证 manifest、参数兼容性和实际来源，不得按策略请求直接复制 artifact。

## 2. 范围

### 2.1 入口门：checkpoint 执行决议

本项技术归属本应为 Stage C 未闭环验收项，但作为 Stage D 的前置门在本分支最先完成。

- 将 Judge/Policy 的分支结果定义为**请求执行计划**，不能直接等同于实际执行计划。
- 在收到完整候选参数后，验证请求 checkpoint 的 manifest 和兼容性；从所有可用祖先 checkpoint 中选择**最晚的兼容 checkpoint**，以最小化重跑阶段。
- 没有兼容 checkpoint 时执行 full restart，实际起点为 FP；不得复制不兼容父 artifact。
- 将“请求起点、有效起点、使用或拒绝的 checkpoint、manifest 验证、兼容性结果、失效参数、fallback 原因与执行模式”写入不可变 Trial 证据。
- 让优化树节点能够可靠关联来源 trial 或 checkpoint；不得继续以“当前最后一个 trial”代替 Judge 选中的树节点来源。
- 为通用路径创建可消费的 checkpoint，至少覆盖存在下游阶段的 FP、PL、CTS；RT 不作为下游 fork 的来源。

### 2.2 最小数据契约

只增加闭环必需的字段，不提前建设通用数据平台：

| 对象 | 最小字段 |
|---|---|
| `MinimalObservation` | `trial_id`、`stage`、状态、阶段 WNS/TNS、阶段耗时、失败类型、checkpoint、parent |
| `DoomedDecision` | `risk_class`、相对 `risk_score`、`reason_codes`、`rule_version`、输入证据 |
| `GWTWDecision` | `action`、decision stage、rank、parent/child、是否审计放行、scheduler version |
| Trial 生命周期 | 新增主动暂停语义；暂停不得伪装为工具失败，且不要求 final QoR |

`risk_score` 只表示同一 stage cohort 内的规则排序，不是概率或校准置信度。所有路径、checkpoint 和 trial 引用使用相对标识。

### 2.3 规则型 Doomed Predictor

- `hard_dead`：阶段失败、超时、manifest 不可恢复或必要 timing 观测缺失；不得作为 fork parent。
- `soft_bad`：阶段成功但同层 timing proxy 排名靠后；允许暂停，也允许被审计配额放行。
- `survivor`：阶段成功且进入保留集合；可以继续或作为合法 checkpoint parent。
- 排名先比较 WNS，再比较 TNS；缺失值、并列和 trial 顺序必须有确定性规则。
- predictor 只返回判断与证据，不执行暂停、复制、清理或 ORFS 命令。

### 2.4 串行异步 GWTW Scheduler

- 由 YAML 声明固定 `population_size`、decision stages、survivor/audit 配额、seed 和预算。
- 候选在单机上逐个执行，但必须等同一 cohort 到达 PL 或 CTS 后再统一决策。
- 每层至少保留一个 survivor 和一个探索或反事实审计名额；其余 loser 标记为暂停并保留 checkpoint。
- 从 survivor checkpoint fork 补回 population；子候选只能修改 checkpoint 下游有效的参数。
- mutation 必须从 `ParamSpec.affects` 与 `PARAM_SPACE` 推导，禁止维护第二份参数失效表。
- parent 选择设置后代上限；有多个合法 survivor 时优先不同 parent，再做 seed 驱动的确定性参数扰动。
- 全部候选均为 `hard_dead` 时实验明确失败，不得复制失败 artifact 或静默 full restart 冒充 survivor。

### 2.5 证据与恢复

- 每次 predictor 和 scheduler 决策写入 append-only session JSONL，并在相关 Trial 中保留可定位引用。
- 记录输入观测、排序、阈值/配额、action、parent、checkpoint、mutation、seed、版本和 fallback 原因。
- pause 只停止下游调度；不删除 Trial、checkpoint、ORFS artifact 或原始报告。
- MVP 只要求从已落盘的 stage/checkpoint 重新构建 cohort 状态；并行 worker 的 job 恢复后置到 Stage G。

### 2.6 最小运行入口

- 使用独立的 MVP orchestration 模块协调 cohort、predictor、scheduler、TrialManager、CheckpointManager 和 ORFSRunner。
- CLI 只解析 YAML 和启动 orchestration，不把 predictor、调度或 checkpoint 逻辑堆入 `main.py`。
- mock/fake stage executor 只用于纯 Python 控制流测试；真实验收必须使用 ORFS 和独立实验 YAML。

## 3. 非目标

- 不修改 ORFS 参数空间、QoR comparator 或 ORFS 命令语义；若确有必要，先取得用户授权并更新实验契约。
- 不实现 LLM `SearchPolicy`、prompt、模型元数据或通用 Agent action schema；这些属于 Stage E。
- 不实现 Default、RandomSearch、TopKGreedy、ParetoArchive、完整 trajectory/预算平台或通用离线 replay；这些属于 Stage E。
- 不训练 Logistic Regression、Random Forest、XGBoost、GNN 或 LSTM，不实现校准、OOD 或跨设计学习；这些属于 Stage F。
- 不实现并行 worker、Slurm、生产级 population 恢复或经验 \(\kappa\) 估计；这些属于 Stage G。
- 不以 mock 结果宣称 QoR、checkpoint 加速或策略优越性。
- 不修复已单独记录的 `tools/clean.py` 基线保护风险，除非用户另行授权。

## 4. 核心契约

### 4.1 请求与实际执行分离

```text
Policy/Judge request
  requested parent node + requested start stage + candidate parameters
                         ↓
Checkpoint resolution
  manifest verify + compatibility + ancestor fallback selection
                         ↓
Execution resolution
  effective parent/checkpoint + effective start stage + execution mode
                         ↓
ORFS execution + Trial evidence + budget accounting
```

`requested_start_stage` 说明策略希望从哪里探索；`effective_start_stage` 说明 ORFS 实际从哪里重跑。两者不同是合法且必须记录的结果，不是隐藏的策略失败。

选择规则：在所有祖先 checkpoint 中选择阶段序号最大的、manifest 完整且与完整候选参数兼容的 checkpoint。若 CTS checkpoint 不兼容而 PL checkpoint 兼容，则复用 PL 并从 CTS 重跑；若 FP checkpoint 也不兼容，则 full restart 并从 FP 重跑。

### 4.2 建议的数据模型

新增可序列化的 `ExecutionResolution`，以向后兼容的 optional 字段加入 `TrialRecord`；不得复用 `checkpoint` 字段，因为该字段表示本 Trial **产生**的 checkpoint。

建议最小字段：

| 字段 | 含义 |
|---|---|
| `requested_parent_node_id` | Policy/Judge 选择的优化树节点 |
| `requested_start_stage` | Policy/Judge 请求的重跑起点 |
| `effective_start_stage` | 实际重跑起点 |
| `execution_mode` | `checkpoint_fork` 或 `full_restart` |
| `consumed_checkpoint` | 被复用 checkpoint 的 ID、来源 trial、stage；full restart 时为 null |
| `manifest_verified` / `manifest_errors` | artifact 完整性结论与原因 |
| `compatibility_checked` / `is_compatible` | 参数兼容性结论 |
| `invalidating_parameters` | 使请求 checkpoint 失效的参数及其 `affects` |
| `fallback_reason` | 回退或 full restart 的可读原因 |

优化树节点还须保存来源 `trial_id` 或等价、可持久化的 trial 关联。普通循环的 parent Trial 不得再从 `self._current_trial` 推断。

### 4.3 MVP 闭环

```text
YAML + seed
    ↓
初始化差异化 population
    ↓
逐个执行到 PL checkpoint
    ↓
构造同层 MinimalObservation cohort
    ↓
DoomedPredictor：hard_dead / soft_bad / survivor
    ↓
GWTWScheduler：continue / pause / audit_continue / fork
    ↓
验证 survivor checkpoint + 生成合法下游 mutation + 补位
    ↓
CTS 重复一次 → RT/finish → 真实 post-route QoR
```

决策顺序固定：

1. 先排除 `hard_dead` 和不可恢复 checkpoint。
2. 对剩余候选按阶段 WNS、TNS 和稳定 tie-break 排名。
3. 分配 survivor 与 audit 配额。
4. 暂停其余候选并持久化证据。
5. 从合法 survivor fork 补位；resolver 决定实际 checkpoint 与执行起点。
6. 只有执行到 finish 且 final QoR 完整的 Trial 参加最终比较。

## 5. 实施步骤

1. **验收入口修复**：完成 `ExecutionResolution`、source trial 关联、祖先 checkpoint fallback、普通循环接入及其向后兼容测试。
2. **冻结 MVP 契约**：定义最小 Observation、Doomed/GWTW decision、pause 生命周期和 JSON/schema；旧 Trial 必须继续加载。
3. **实现纯 predictor**：输入单个 observation 与 cohort，输出稳定、可解释、无副作用的风险判断。
4. **实现纯 scheduler**：输入 cohort、predictor 结果、配置与 seed，输出动作、parent 分配和 mutation 请求；不直接执行 I/O。
5. **接入串行 orchestration**：复用现有 manager、runner 和 resolver，在 PL/CTS 建 cohort、执行决策、暂停 loser、审计放行并 fork 补位。
6. **持久化证据**：落盘 decision JSONL、Trial 引用、实际 ExecutionResolution 和最终 QoR；验证中断后可从已完成 stage 重建 cohort。
7. **补齐测试与实验声明**：恢复既有核心回归覆盖，添加 MVP 纯 Python 测试和真实 `gcd` 实验 YAML。
8. **真实闭环验收**：用户运行真实 ORFS 实验，Codex 检查原始报告、checkpoint、Trial、decision trace、日志脱敏和通过标准。

## 6. 交付物

- `ExecutionResolution` 数据模型、向后兼容 schema 与 Trial/JSONL 证据。
- checkpoint resolver、祖先 fallback 与普通优化循环接入。
- 通用路径 FP/PL/CTS checkpoint 创建与来源 trial 关联。
- 最小 Observation、Doomed/GWTW decision、pause 生命周期与 schema。
- 规则型 predictor、串行异步 scheduler、合法 mutation 和 MVP orchestration。
- append-only decision trace 与 Trial/ExecutionResolution 关联。
- Stage D 纯 Python 回归测试、真实 MVP 实验 YAML 与验收报告。

## 7. 测试与验收

### 7.1 必须的纯 Python 回归

| 场景 | 预期结果 |
|---|---|
| CTS 请求 + RT-only 参数 | 复用 CTS checkpoint；有效起点 RT |
| CTS 请求 + CTS/RT 参数 | 回退到 PL checkpoint；有效起点 CTS |
| CTS 请求 + PL 影响参数 | 回退到 FP checkpoint；有效起点 PL |
| CTS 请求 + FP 影响参数 | 无兼容 checkpoint；full restart；有效起点 FP |
| manifest 文件缺失或 hash 不匹配 | 不消费该 checkpoint；尝试更早祖先或 full restart；原因被记录 |
| 未知参数 | 保守视为不兼容；不得 fork |
| 旧 Trial JSON | 可反序列化，`execution_resolution` 为 null |
| 请求起点与有效起点不同 | Trial 同时保留二者及 fallback 原因 |
| 相同 seed 和 cohort | risk、rank、audit、parent 与 mutation 完全一致 |
| hard-dead 与 soft-bad 混合 | hard-dead 不作为 parent；soft-bad 可被审计放行 |
| timing 并列或缺失 | tie-break 稳定；必要观测缺失进入明确风险类别 |
| pause Trial 序列化 | 无 final QoR 仍合法；checkpoint 和 decision 引用保留 |
| survivor checkpoint 不兼容 | resolver 回退或拒绝；不得复制错误 artifact |
| 合法下游 mutation | 只产生 `ParamSpec.affects` 位于 checkpoint 之后的参数 |
| population 补位 | 不超过配置预算和 parent 后代上限；数量符合配置 |
| 全部 hard-dead | 实验明确失败，不生成伪 survivor |
| 中断后重建 cohort | 已完成 stage 不重复计费，未完成动作可识别 |

最低命令：

```bash
make test
python3 schemas/trial.py
python3 main.py --help
python3 tools/trial_inspect.py --help
python3 tools/trial_reproduce.py --help
python3 tools/clean.py --help
```

### 7.2 真实实验判定

**需要用户真实实验。** 原因：纯 Python 测试不能证明阶段观测来自真实 ORFS、暂停后 artifact 仍可恢复、survivor fork 的实际执行起点正确，或子候选能够完成 post-route。

真实验收使用独立 YAML，固定 `sky130hd/gcd`、环境 manifest、参数空间、evaluator、seed、`population_size: 4`、PL/CTS decision stage、survivor/audit 配额和 trial/wall-clock 上限。通过标准：

- 至少形成一个含四个候选的真实同层 cohort；
- 至少出现一次可解释 pause、一次 `audit_continue` 和一次 survivor checkpoint fork；
- 被暂停 Trial 保留 checkpoint、原始阶段报告和 decision trace；
- fork 的 mutation 与 checkpoint 兼容，实际 ExecutionResolution 可追溯；
- 至少两个候选完成 finish，final QoR 来自真实 `6_report.json`；
- 原始 ORFS 报告、manifest、Trial 和日志不存在绝对用户路径或密钥。

本实验只验收闭环，不用于宣称节省 CPU-hour、降低误杀率或优于 baseline。若 cohort 的自然排序未触发所需动作，应调整实验 YAML 中预先声明的规则阈值后重新完整运行，不得事后篡改 decision trace。

## 8. 风险与依赖

- 实际 fallback 会增加 runtime；比较策略时必须以有效执行成本而不是 Judge 请求成本计费。
- 当前中间观测主要是 WNS/TNS，不能证明候选最终 doomed；因此 soft-bad 必须保留审计配额。
- CTS 后可合法变化的参数较少，mutation 多样性受 `ParamSpec.affects` 限制；不得为增加多样性错误复用 checkpoint。
- 串行 cohort 会延长 wall-clock，且不代表生产级并行 GWTW；并行化留到 Stage G。
- `environment_manifest.json` 中存在 unresolved revision；真实实验前需按实验契约刷新并说明缺失项。
- 所有新增文档、实验口径或架构范围在实现前须经用户确认。

## 9. 准入条件

- 本计划经用户确认后，Claude 才能继续 Stage D 实现；现有未提交代码不因计划重写自动视为已验收。
- checkpoint 执行决议的 P0/P1、既有核心测试删除和文档/实现冲突关闭前，不得开始步骤 2–8。
- Stage D 完成前必须使用 `docs/阶段验收门模板.md` 形成 `stage-d-check.md`，且第 7.1 节纯 Python 验证和第 7.2 节真实实验均通过。
- 未完成 Stage D 验收报告不得 merge 到 `main` 或开始 Stage E。
