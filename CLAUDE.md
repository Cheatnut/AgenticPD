# AgenticPD Executor 规范

AgenticPD 是构建在 OpenROAD-flow-scripts（ORFS）之上的物理设计 QoR 优化实验框架；基线结构与质量分级见 `docs/AgenticPD项目扫描报告.md`，协作流程见 `docs/WORKFLOW.md`。

本规范适用于本目录及子目录。Claude 是执行者：只按 Codex 已确认的 Plan 实现，不擅自改变阶段边界、架构或实验口径。完整协作流程以 `docs/WORKFLOW.md` 为唯一真相源；本文件只补充 Claude 的角色边界和工程约定。

## 常用命令

所有命令在 `flow/` 下执行；测试在 `flow/agenticpd/` 下执行。

```bash
# 纯 Python 测试（无 EDA/LLM/网络依赖）
cd flow/agenticpd && make test

# Smoke test：只跑基线，不调 LLM
cd flow && python3 agenticpd/main.py --baseline-only --design gcd

# 全 mock 调试（零 token / 零 EDA，秒级完成）
cd flow && python3 agenticpd/main.py --mock-llm --mock-orfs --iterations 5

# MockLLM + 真实 ORFS（零 token，端到端验证 make 链路）
cd flow && python3 agenticpd/main.py --mock-llm --iterations 2

# 完整优化（需要 DEEPSEEK_API_KEY）
cd flow && python3 agenticpd/main.py --design gcd --iterations 3

# 断点续跑
cd flow && python3 agenticpd/main.py --resume latest

# 查看 trial
python3 agenticpd/tools/trial_inspect.py --list sky130hd gcd
python3 agenticpd/tools/trial_inspect.py <trial_id> --stages

# 清理产物（base 基线永不删除）
python3 agenticpd/tools/clean.py sky130hd gcd --dry-run
python3 agenticpd/tools/clean.py sky130hd gcd --yes

# 优化树可视化
python3 agenticpd/tools/visualize.py agenticpd/runs/<session>

# 数据模型 / 持久化自检（迁移后在 tests/ 下运行）
python3 -m unittest tests.test_core_models tests.test_storage_trace
```

## 架构总览

### 核心优化循环（`optimizer.py`）

```
baseline (iter #0) → [Observation → Judge → Branch → StageAgent×N → ORFS] × N
```

1. **Baseline**（`run_baseline`）：用 `BASELINE_PARAMS` 跑完整 RTL→GDS 流程，构建优化树的前四层节点（FP/PL/CTS/RT），结果缓存到 `runs/<platform>_<design>/.baseline/trial.json`，跨 session 复用。
2. **Observation**（`build_observation_summary`）：从优化树提取 E(n) 探索平衡 + B(s) 阶段瓶颈，生成自适应摘要文本给 Judge。
3. **Judge**（`JudgeAgent`）：分析摘要 + 历史，选择 branch_node（从哪个历史节点分支）和 branch_stage（从哪个阶段开始重跑），为下游 StageAgent 生成调参提示。
4. **Branch 执行**：Bef(branch_stage) 阶段参数和 QoR 从树祖先继承（零成本），只对 {branch_stage} ∪ Aft(branch_stage) 调用 LLM 生成新参数并重跑 ORFS。
5. **Per-stage pipeline**：StageAgent 按序执行 — 每个 StageAgent 收到上游真实 QoR 后生成参数 → `make <stage>` → 解析中间 QoR → 传给下一个 StageAgent。

### 分支机制（论文 §3）

- 优化树 `OptimizationTree`：每个节点 = 某个 iteration 在某个 stage 的执行快照（参数 + 中间 QoR）。
- `branchable_nodes()`：stage 不是 root/RT 且 branch_count < max_branch_count 的节点可作为分支源。
- 一致性约束：branch_node 的 stage 唯一决定 branch_stage（root→FP, FP节点→PL, PL节点→CTS, CTS节点→RT）。Judge 输出不一致时系统自动纠正。
- `copy_parent_results()`：从父 variant 复制 results/logs/reports/objects 四目录到新 variant，使 make 只需增量重跑下游 stage。

### 模块职责

| 模块 | 职责 | 依赖 |
|------|------|------|
| `main.py` | CLI 入口，参数解析，run_dir 创建，模式分发 | config, optimizer, orfs |
| `config.py` | **唯一真相源**：9 个 ParamSpec、PARAM_SPACE、BASELINE_PARAMS、FrameworkConfig、路径常量 | 无 |
| `config.py` | **唯一真相源**：9 个 ParamSpec、PARAM_SPACE、BASELINE_PARAMS、FrameworkConfig、路径常量 | 无 |
| `core/` | 数据模型（models/decisions）、QoR 比较（qor）、通用工具（utils） | config |
| `storage/` | TrialManager、CheckpointManager、DecisionTraceWriter/TraceIO | core |
| `agents/` | JudgeAgent + 4×StageAgent + LLMClient/MockLLMClient + 观测摘要 | config, core |
| `search/` | Optimizer 主循环 + stage_pipeline + OptimizationTree | agents, core, storage, gwtw.resolver, orfs |
| `gwtw/` | Doomed/GWTW 统一编排（orchestrator/population/cohort_*/execution/doom/scheduler/mutation/resolver） | agents, core, storage, orfs |
| `orfs/` | ORFS 适配层：command/runner/parser/interface/backend | config, core, storage |
| `tools/` | CLI 工具：clean、visualize、trial_inspect、trial_reproduce、checkpoint_fork_*、session_visualize/ | config, core, storage, orfs |

### 数据流

```
main.py → FrameworkConfig → Optimizer
  ├─ llm: LLMClient (or MockLLMClient)
  ├─ runner: ORFSRunner (or MockORFSRunner)
  ├─ tree: OptimizationTree → tree.json
  ├─ trial_mgr: TrialManager → trials.jsonl + iter-{N}-{trial_id}/trial.json
  └─ history: List[dict] (内存，resume 时从 trials.jsonl 重建)
```

- **参数流**：`config.PARAM_SPACE` → StageAgent 生成 → `stage_params: dict[stage, dict[name, value]]` → `orfs.command.build_make_cmd()` → make 命令行
- **QoR 流**：ORFS `6_report.json` → `QoR.from_report_json()` → `qor_is_better()` 比较 → history + tree 更新
- **Trial 流**：`TrialManager.create()` → populate stage_results → `update()` 原子写 `trial.json` + append `trials.jsonl`

### 关键设计决策

- **Timing 单位**：ORFS JSON metrics 中 timing 为 ns，框架内部统一用 ps（`TIMING_UNIT_TO_PS = 1000.0`）。
- **QoR 比较优先级**：WNS → TNS → Power → Area。当双方 WNS ≥ 0（timing 都收敛）时跳过 WNS/TNS 直接比 power/area。
- **JSON metrics 解析依赖 CPython dict 的 "last-wins" 行为**：`finish__design__instance__area` 在 `6_report.json` 中出现两次，后者才是正确的标准单元面积。不能换成报错或取首个值的 parser。
- **基线缓存**：`.baseline/trial.json` 跨 session 共享，永不删除（`clean.py` 和 `wipe_all_variants()` 都保护它）。
- **原子写**：tree.json、trial.json、trials.jsonl 全部先写 `.tmp` 再 `os.replace()`，crash 不 corrupt。

## 执行与阶段

- 开始前阅读 `docs/WORKFLOW.md`、`docs/HANDOVER.md` 与 `docs/AgenticPD项目扫描报告.md`；歧义、冲突、范围变化或方案缺失先反馈 Codex。
- 不得创建、修改或补写 Plan、验收报告或后续阶段内容；不得提前实现后续阶段。
- 以一次性交付包为实现单位，集中完成其中全部 P0/P1 和必要回归；不得只修点名行而忽略相邻接口，也不得顺带重构或修改无关文件。
- 完成后报告改动、测试、遗留风险和计划偏离，等待 Codex 审查；不自行 commit、merge 或 push。
- 交付和返工格式严格遵守 `docs/WORKFLOW.md`，测试名称必须与关键断言实际证明的行为一致。
- 功能交付在 `main` 上直接开发或在用户指定的功能分支上进行，不得混入无关改动；删除、重写历史、`.env`/密钥/CI/CD、数据库迁移、全局依赖或系统配置先获用户授权。

## 文档与语言

- 除用户明确要求外，不得创建、修改、移动或删除 `docs/`；不得以同步文档或实现需要为由擅自改动。
- `docs/Note.md` 是用户笔记，禁止修改、移动或删除；`HANDOVER.md` 是唯一例外，每日结束时覆盖当前阶段、待修复项、验证状态和下一步，次日先加载。
- 根目录保留 `HANDOVER.md` 与 `Note.md`（只读）；CLI 在 `docs/usage/`，系统介绍在 `docs/introduction/`，基线扫描在 `docs/AgenticPD项目扫描报告.md`。
- 所有 `.md` 使用中文；其他文件内容、代码注释、docstring、配置说明、CLI 输出和测试描述使用英文。
- 修改参数空间、QoR comparator 或 ORFS 命令语义前，先获用户授权更新 `docs/introduction/实验契约.md`。

## 编程纪律

- 禁止硬编码用户目录、环境路径、预算、参数或 magic number；由项目根、配置、函数参数或具名常量推导。
- 复用 `core/`、`storage/`、`orfs/` 边界；新职责放对应分层包，不得堆入 `main.py` 或超大编排文件。
- 行为 bug 先添加失败回归测试再修复；不注释报错、不加绕过标记，应定位根因。
- 注释和 docstring 解释 EDA 语义、约束和设计原因，不重复显而易见的 Python 语法。
- `tests/fixtures/` 只读；测试用临时目录，禁止回写；`runs/` 不能作为唯一实验记录。
- Agent 只能提出合法、受限参数 action，不得改 evaluator、Makefile、Tcl、设计配置或执行任意 shell。

## 实验、安全与交接

- 当前 Trial ID 为随机 8 位十六进制标识；`agenticpd_iter<N>` 仅作 legacy variant 证据；QoR 权威来源是 ORFS 报告。
- 真实实验必须有 YAML，记录参数空间、evaluator、预算、seed 和 design 角色；真实运行前刷新 `environment_manifest.json`。
- 不读取、打印、提交或复制 `.env`、token、密钥；日志不得含密钥、完整请求头或绝对用户路径；清理产物必须显式指定 trial。
- 每次实现后运行 `make test`；测试不得依赖网络、LLM、OpenROAD、PDK 或既有 runs；真实 smoke run 仅作补充。
- 收尾时说明改动是否影响真实 ORFS、checkpoint、报告解析或 QoR；由 Codex 决定是否需真实实验，Claude 不得把 mock/单测写成真实实验结论。
- 向 Codex 提交：改动范围、`make test` 与自检结果、真实实验状态、P0/P1/P2 遗留、配置/报告/原始 QoR/checkpoint/trial/对照判据的证据路径，以及用户下一步命令。
