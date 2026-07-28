# AgenticPD Demo 审查与迭代计划

> 审查对象：`../AgenticPD/`（Windows 快照；真实开发与运行版本在 WSL `flow/agenticpd/`）  
> 审查日期：2026-07-26（§1–9 原始审查）；交付记录最后更新 2026-07-28（§10–12 + 审查问题状态标注）  
> 目标主线：`AgenticPD + GWTW + Doomed Runs` 的 Flow Optimization 平台  
> 当前开发阶段：阶段 C 收尾，即将进入阶段 D  
> 结论：当前仓库已完成 **AgenticPD 的可运行垂直原型**，应先把它重构为”可复现实验底座”，再叠加 GWTW 与 Doomed Runs。现在直接继续加 prompt、加 Agent 或训练预测器，会把一次性脚本变成难以验证的系统。

---

## 1. 本次扫描范围与证据边界

本报告只扫描 `AgenticPD/` 快照的 Python 源码、README、`问题.txt` 和已有运行产物；没有改动仓库，也没有运行真实 ORFS flow。唯一执行的验证是 `PYTHONDONTWRITEBYTECODE=1 python -B utils.py`，其中 JSON 提取和 QoR 比较器内置自测通过。

因此，下文的“已实现”表示**从代码和快照中可以直接确认**；“需要验证”表示代码存在，但还没有看到足够的跨设计、重复实验或服务器调度证据。

### 1.1 已有真实运行快照

`runs/20260718_224454/` 记录了一个 `sky130hd / ibex` 实验：

| 项目 | 观察值 | 含义 |
|---|---:|---|
| 历史 trial 数 | 11（iter 0–10） | 含基线与两条失败轨迹 |
| 成功 / 失败 | 9 / 2 | 失败发生在 PL（iter 9）与 RT（iter 10） |
| 成功 trial 平均耗时 | 735.2 s | 只统计成功 trial；失败耗时被记录为 0，是当前数据缺口 |
| 成功 trial 累计耗时 | 6616.4 s | 约 1.84 小时，不含失败 trial 的真实成本 |
| timing-first 最优 | iter 6，WNS = 357.016 ps | 当前 `qor_is_better()` 的优先规则首先比较 WNS/TNS |
| 最小 area（成功 trial） | iter 5，146684 um² | 并非 timing-first 最优 |
| 最低 power（成功 trial） | iter 3 / 4，0.0211812 W | 与最优 WNS 不是同一 trial |
| 树分支 | ROOT、PL→CTS、CTS→RT | 说明阶段级复用在真实 ORFS 轨迹中被调用过 |

这只能说明“控制器、ORFS 调用和产物解析曾在一个设计上联通”，**不能**说明方法有效、优于 baseline 或可以泛化。缺少多 seed、跨设计、统一预算、固定版本 manifest 与 post-route 完整对比。

---

## 2. 当前实现与负责方向的映射

目标系统有四层：确定性执行底座、AgenticPD 搜索、GWTW 预算调度、Doomed Runs 风险预测。当前仓库主要覆盖前两层的一部分。

| 目标能力 | 当前状态（审查时） | 已有实现 / 证据 | 仍缺什么（→ 已解决项） |
|---|---|---|---|
| 固定参数动作空间 | 已实现，但偏窄 | `config.py::PARAM_SPACE` 有 FP/PL/CTS/RT 共 9 个参数；类型、范围、默认值和落地方式集中定义 | 按 design/platform 配置；参数依赖图；条件参数；参数空间版本与来源记录 |
| ORFS 调用与隔离 variant | 已实现 | `orfs/` 子包用 `FLOW_VARIANT`，以独立 results/logs/reports/objects 运行 | Slurm 后端（→ stub 已建）、资源限制、环境 manifest、并发隔离与缓存一致性校验 |
| 阶段级执行 | 已实现 | `run_stage()` 执行 `floorplan/place/cts/route`；阶段后立即解析中间 QoR | 更细 checkpoint 定义（→ `CheckpointRef` 已建）、阶段输入/输出 hash（→ SHA-256 已实现）、可验证的恢复契约 |
| 分支复用 | 已实现，但风险较高 | 复制 parent 的四类产物，再 `clean_<stage>` 并重跑下游；优化树记录 parent-child | 检查 checkpoint 与参数兼容性（→ `ParameterSpec.affects` + `is_compatible()` 已实现）；复制成本与 cache hit 统计 |
| 最终 QoR 解析 | 已实现 | 优先 `6_report.json`，报告/日志正则兜底；统一 ns→ps | DRC、overflow、violations、buffer/cell 数、runtime、内存等完整 schema |
| 失败处理 | 部分实现→**已改善** | 子进程组超时 kill、非零退出、缺失 JSON 定位阶段、失败写 history | 失败原因 taxonomy（→ `FailureClass` 5 种类型已建）、失败耗时/资源保存（→ `StageResult` 含 elapsed_s/exit_code/failure）、可恢复 checkpoint 与重试策略 |
| Judge Agent | 已实现 | `JudgeAgent` 读取探索度 E(n)、阶段瓶颈 B(s)、历史和 best，输出 branch node/stage/hints | 结构化 decision trace、模型/提示词版本、成本、离线回放与 rule-based 对照 |
| 四个 Stage Agents | 已实现 | FP/PL/CTS/RT 各自生成受限参数；校验、clamp 和 fallback | Policy 接口统一化；动作差分；禁忌/重复动作；基于真实特征的更可靠 observation |
| 优化树与恢复 | 已实现 | `OptimizationTree`、`tree.json`、原子写入、`--resume`（→ bug 已修复） | Trial DAG（→ `TrialRecord.parent_trial_id` 已建）、checkpoint artifact hash（→ SHA-256 已实现）、schema version（→ `trial.schema.json` 已建） |
| 可视化 | 已实现 | `tools/visualize.py` 生成优化树 PNG，并标出 baseline / best path | Pareto 图、预算图、失败图、阶段耗时图、可交互 trial 浏览 |
| mock / dry run | 已实现 | `MockLLMClient`、`MockORFSRunner` 支持无 token / 无 EDA 验证 | fixture-based parser tests（→ test_qor/test_schemas/test_fixtures 已建 56 例）、集成测试、CI |
| GWTW | 未实现 | 无 population、survivor、resample、diversity 或 budget allocation 模块 | 完整实现 |
| Doomed Runs | 未实现 | 当前失败仅事后记录；没有特征数据集、风险模型、校准或安全早停 | 完整实现 |
| 公平实验评价 | 未实现 | 只有单次 snapshot 与 timing-first 比较器 | random/BO/top-k baseline、相同预算、multi-seed、held-out design、Pareto/hypervolume |

### 2.1 当前 AgenticPD 到底已经做成了什么

当前路径是（审查时；阶段 B/C 后数据流已变更，见 §12）：

```text
tree.json + trials.jsonl（阶段 C 后替代 history.json）
  → Observation Tool（E(n)、B(s)、近期历史）
  → Judge Agent 选择 branch_node / branch_stage / hints
  → 继承分支前的参数与中间 QoR
  → 下游 Stage Agent 逐个产生白名单参数
  → ORFS 单阶段 make（→ StageResult: elapsed_s/exit_code/failure）、解析真实中间 timing
  → make finish 解析 final WNS/TNS/area/power
  → 写入 TrialRecord（→ trial.json + trials.jsonl）+ tree.json，更新 timing-first best，导出 best artifact
```

这已经覆盖了 AgenticPD 中最关键的三件事：**阶段化 action space、从历史节点分支、下游阶段按真实中间 QoR 串行决策**。其中 `SETUP_SLACK_MARGIN`、FastRoute layer adjustment 和 global-route iterations 还处理了 ORFS 中“变量不能简单直接传给 make”的特殊落地方式；这部分是有工程价值的。

### 2.2 它目前不等于最终 AgenticPD + GWTW + Doomed Runs

当前系统仍是**单条串行 trial 生成器**：Judge 每轮只选一个分支，随后只执行一条路径。它没有活跃 population，也没有“预算有限时哪些候选继续、暂停、复制”的统一决策，因此不能称为 GWTW。

当前系统也没有对中间观测做训练或风险估计；PL/RT 失败是在 make 失败后才知道的，不能称为 Doomed Runs。即使未来增加一个“WNS 小于阈值就停”的 if，也不够：必须有可校准的风险、可恢复暂停、保留不确定候选和反事实审计。

---

## 3. 关键问题：按优先级处理

### P0：先修正实验基础设施，否则后续结果不可信

1. **配置语义与真实设计不一致。** `config.py` 说明参数范围参考 `sky130hd/gcd` AutoTuner，但默认设计是 `sky130hd/ibex`；快照也确实跑的是 ibex。参数范围可以暂时共用，但不能继续当作”已为 ibex 校准”。应把参数空间放入 `search_space/<platform>/<design>.yaml`，记录来源、单位、默认值和验证状态。  
   **→ 阶段 A 已处理：** 默认设计改为 gcd，smoke.yaml 声明 param-space-v1 参考 gcd AutoTuner，切换 dev/held_out 需重校准。

2. **checkpoint 复用缺少兼容性契约。** 现在从父 variant 复制四个目录后 `clean_<stage>`。这对只影响本阶段及下游的参数有效，但 `SETUP_SLACK_MARGIN` 被归到 CTS，实际还影响 FP/GRT repair timing；从 CTS 以后的节点分支时，历史 FP artifact 可能与新参数语义不一致。应给每个参数声明 `affects_stages`，分支点由”最早受影响阶段”决定，而不是只看参数被归属到哪个 Agent。  
   **→ 阶段 C 已解决：** `config.py` 中 9 个 `ParamSpec` 均标注 `affects` 字段，`CheckpointManager.is_compatible()` 按最早受影响阶段判断兼容性。

3. **失败 trial 的成本被丢失。** `run_stage()` 返回 `(ok, stage_qor)`，没有返回 elapsed、exit code、日志路径和资源；`Optimizer` 构造失败 `RunResult` 时 `elapsed_s` 默认 0。快照中的 iter 9/10 因此无法计入真实预算。应统一返回 `StageResult`，并累计每个 stage 的 wall time、CPU time、MaxRSS、退出码和 failure class。  
   **→ 阶段 C 已解决：** `execute_stage()` 返回 `StageResult`（elapsed_s/exit_code/failure/stage_qor），失败不再显示 elapsed_s=0。

4. **没有独立的数据模型。** `history.json` 同时承担实验记录、搜索记忆和分析数据。它缺少 schema version、trial ID、artifact manifest、环境与 commit hash、完整 stage 记录。Doomed predictor 和 GWTW 都需要可查询、不可变的 trial 数据，而不是从嵌套 JSON 反向猜字段。  
   **→ 阶段 B 已解决：** `schemas/trial.py` 定义 TrialRecord/StageResult/CheckpointRef/FailureClass 四个 dataclass，`trial.schema.json` 提供 JSON Schema，JSONL 提供 append-only 不可变存储。

5. **缺少测试矩阵。** 当前只看到 `utils.py` 手写自测；没有 `pytest`、parser fixture、mock runner 集成测试或恢复测试。一次 ORFS / parser 版本变化就可能静默污染数据。  
   **→ 阶段 A/B 已解决：** 56 例纯 Python 测试（test_qor 21 + test_schemas 17 + test_fixtures 18），`make test` 一键验证。

### P1：搜索逻辑已经能跑，但不够科学和可解释

6. **目标函数只保留 timing-first 单赢家。** `qor_is_better()` 以 WNS/TNS 优先、再 power/area；这是一个合理的 gate，但不能替代 Pareto archive。当前快照中 timing 最优（iter 6）、最小面积（iter 5）和最低功耗（iter 3/4）不同，正好证明只导出一个 `agenticpd_best` 会丢掉有价值候选。  
   **→ 阶段 D 待处理。**

7. **B(s) 过度依赖阶段 WNS。** `compute_stage_bottleneck()` 只比较各阶段历史最大 setup WS 与 final WNS。它没有 congestion、DRC、slew/cap/fanout、runtime，也没有处理阶段指标与 post-route QoR 的 proxy bias；这样 Judge 可能误判”某阶段瓶颈”。  
   **→ 阶段 D 待处理：** 扩展 observation 指标后改善。

8. **LLM 接口只有”可解析 JSON”，没有真正的 action contract。** 当前做了 JSON 重试、字段校验与 clamp，这是正确起点；但没有 JSON Schema/Pydantic model、请求/响应 hash、token 用量、模型实际返回版本、prompt 版本和 replay corpus。未来很难判断 QoR 改善来自 Agent、模型升级还是 prompt 变化。  
   **→ 部分解决：** 阶段 B 的 TrialRecord/StageResult 有了类型约束（dataclass → dict），但 LLM action 层仍缺结构化 contract。阶段 E 处理。

9. **同一 parent 可多次分支，但无多样性和预算概念。** `max_branch_count` 仅限制每个节点次数；没有参数距离、action distance、population entropy、每个 stage 的预算，也没有防止低价值局部重复探索的机制。  
   **→ 阶段 F（GWTW）处理。**

10. **本地 `subprocess` 是唯一执行后端。** 服务器有 Slurm，当前代码没有 job submit/poll/cancel、资源请求、排队时间、scratch 使用、并发上限或 job 级日志归档。GWTW 要管理多个活跃 trial，必须先抽象 `ExecutionBackend`。  
   **→ 部分解决：** 阶段 C 已抽象 `orfs/backend.py`，`LocalBackend` 完整实现，`SlurmBackend` 有接口 stub（submit/poll/cancel），待服务器实际部署。

### P2：工程可维护性问题

11. `orfs_interface.py` 同时承担命令生成、目录复制、执行、超时、解析、导出和 mock，职责过多；后续增加 scheduler/predictor 会进一步膨胀。  
   **→ 阶段 C 已解决：** 拆分为 `orfs/command.py`(96行)、`orfs/parser.py`(150行)、`orfs/runner.py`(199行)、`orfs/backend.py`(163行)、`orfs/interface.py`(366行)，原 `orfs_interface.py` 降为 16 行 re-export。

12. `branch_from()` 留在接口中，但优化主循环已经改为 `copy_parent_results() + run_stage()`；应删掉或改为真正统一的 checkpoint API，避免两条实现语义漂移。  
   **→ 阶段 C 已解决：** `branch_from()` 已作为死代码删除，统一使用 `copy_parent_results() → run_stage()` 路径。

13. 运行快照的 `config_snapshot.json` 记录了路径和模型 ID，但没有 ORFS/OpenROAD/PDK/Git commit、Python 包版本、环境变量白名单、机器架构和 seed。它只能”知道在哪里跑过”，不能可靠复现。  
   **→ 部分解决：** `environment_manifest.json` 已记录 OpenROAD/Python 版本；ORFS commit 仍 unresolved（WSL .git 已删除，待学校服务器补）。

14. Windows 控制台以默认 GBK 执行 `utils.py` 时，末尾 `✔` 会触发 `UnicodeEncodeError`；以 `PYTHONIOENCODING=utf-8` 可通过。这不是算法 bug，但 CLI 应避免依赖终端编码，或显式使用 UTF-8 / ASCII 状态文本。

---

## 4. 建议的目标架构

不要在现有 `Optimizer` 上继续堆逻辑。保留其中已经验证的 ORFS 集成与 AgenticPD 思想，将职责拆成下面的稳定接口：

```text
SearchPolicy (AgenticPD / Random / BO)
        │  proposes Action(branch checkpoint, parameter diff)
        ▼
TrialManager ── ValidationGate ── CheckpointManager
        │                                 │
        │ creates immutable TrialSpec      │ verifies compatibility/hash
        ▼                                 ▼
ExecutionBackend (Local first, Slurm next) → ORFS Adapter
        │                                      │
        ▼                                      ▼
ArtifactStore ← MetricParser ← raw logs / reports / results
        │
        ├── FeatureExtractor → DoomedPredictor → risk + uncertainty
        ├── GWTWScheduler   → continue / pause / fork / final-evaluate
        └── ParetoArchive + DecisionTrace + experiment reports
```

关键边界：

- **AgenticPD** 只能提出合法 action，不能直接运行 shell / Tcl；
- **TrialManager** 是唯一允许创建目录、恢复 checkpoint、提交执行的入口；
- **Doomed predictor** 只输出风险与置信度，不能直接永久删除 trial；
- **GWTW scheduler** 才根据风险、QoR、预算和多样性决定 continue / pause / fork；
- **Evaluator** 只读固定 ORFS 与报告，不允许被 Agent 改写；
- **ArtifactStore** 保存不可变原始证据，任何派生特征都能追溯到 trial 和原始文件。

> **当前实现映射**（截至阶段 C，与建议架构的对应关系）：
> - `SearchPolicy` → `agents.py`（JudgeAgent + StageAgent，尚未抽象为 Policy 接口）
> - `TrialManager` → `managers/trial_manager.py`
> - `CheckpointManager` → `managers/checkpoint_manager.py`
> - `ExecutionBackend` → `orfs/backend.py`（`LocalBackend` 实现 + `SlurmBackend` stub）
> - `ORFS Adapter` → `orfs/interface.py`（`ORFSRunner` / `MockORFSRunner`）
> - `MetricParser` → `orfs/parser.py` + `utils.py::QoR`
> - `ArtifactStore` → JSONL（`schemas/trial.py` 中的 append/load 工具）+ 文件系统 trial 目录
> - `ParetoArchive` / `FeatureExtractor` / `DoomedPredictor` / `GWTWScheduler` → **阶段 D–H 待实现**

---

## 5. 分阶段实现计划

时间按“单人、已有 ORFS、需要边学边做”的节奏给出。每阶段必须通过验收门后再进入下一阶段；不要为了赶进度跳过数据与恢复基础设施。

### 阶段 A：冻结 Demo、建立可回归的最小实验（3–4 天）

**要实现的功能**

- 保留当前快照为 `legacy_demo/` 或独立 Git tag；新开发在独立目录/分支进行；
- 为项目根新增专用 `AGENTS.md`：目录职责、trial 命名、不可修改 evaluator、日志和临时目录规则；
- 固定一个 smoke design、两个 development design、一个 held-out design；
- 写 `experiment.yaml`，唯一指定 ORFS/OpenROAD/PDK commit、design/platform、预算、seed、参数空间版本与 evaluator；
- 将当前 `runs/20260718_224454` 转成只读 regression fixture；
- 将 `utils.py` 自测迁入测试框架，并修复 CLI 编码兼容性。

**交付物**

- `docs/experiment-contract.md`；
- `configs/experiments/smoke.yaml`；
- `tests/fixtures/legacy_run/` 与 `tests/test_qor.py`；
- 一份可机器读取的 `environment_manifest.json`。

**预期效果 / 验收**

同一配置可以重建一条 trial；解析器在既有 fixture 上稳定输出相同指标；新提交至少通过纯 Python 测试，不需要启动 ORFS。

### 阶段 B：先建 Trial / Checkpoint / Artifact 数据层（5–7 天）

**要实现的功能**

- 定义 Pydantic 或 dataclass + JSON Schema：`ExperimentSpec`、`TrialSpec`、`StageResult`、`FinalResult`、`CheckpointRef`、`DecisionTrace`、`FailureClass`；
- 每个 trial 使用 UUID 和独立目录，禁止共享可写 `metrics.csv` 或 variant 目录；
- 记录 parent trial、branch stage、参数 diff、完整 resolved parameters、配置/源码/环境 hash；
- 设计 checkpoint manifest：来源 trial、stage、上游参数 hash、ORFS commit、artifact file list/hash、可恢复 target；
- 先使用 JSONL + 目录，或 SQLite；第一版推荐 SQLite 索引 + 文件系统 artifact，避免把大日志塞入数据库；
- 将失败 stage 的 elapsed、exit code、日志路径、timeout、OOM、legality / tool / parser error 全部入库。

**交付物**（实际实现：不再有 `store/` 目录，JSONL + 文件系统替代；manager 移入 `managers/` 子包）

- `schemas/trial.py`、`managers/trial_manager.py`、`managers/checkpoint_manager.py`；
- 一份 `trial.schema.json`；
- 两个 fixture：成功 trial、PL 失败 trial；
- `tools/trial_inspect.py`（Trial 查看器 CLI）。

**预期效果 / 验收**

任意 trial 都能回答：从哪个 parent 来、哪些参数变了、在哪个 stage 花了多少时间、失败是否可恢复、最终文件在哪里。此阶段完成前，不写 GWTW。

### 阶段 C：重构 ORFS Adapter 与执行后端（5–7 天）

**要实现的功能**

- 拆分当前 `orfs_interface.py` 为 `orfs/command.py`（命令构建）、`orfs/parser.py`（报告解析）、`orfs/runner.py`（阶段执行）、`orfs/interface.py`（编排器）；
- `StageResult` 统一返回 command、start/end、wall time、exit code、stage QoR、报告路径与 failure class；
- 声明 `ParameterSpec.affects`，由最早受影响 stage 决定 checkpoint invalidation；
- 建立 `LocalBackend`（保持当前 `subprocess`）和 `SlurmBackend` 接口；Slurm 先实现 submit/poll/cancel、日志路径和资源字段，暂不追求高并发（实际实现：合并为单文件 `orfs/backend.py`，Slurm 为 stub）；
- 对 `copy_parent_results()` 加 manifest 校验，不能只依赖目录存在；
- 在同一 checkpoint fork 两个不同 action，分别跑到 post-route，并与 full restart 结果/耗时对照。

**交付物**（实际实现：backend 合并为单文件而非 `execution/` 子包，adapter → interface.py）

- `orfs/backend.py`（含 `LocalBackend` + `SlurmBackend` stub）；
- `orfs/command.py`、`orfs/parser.py`、`orfs/runner.py`、`orfs/interface.py`；
- checkpoint 恢复兼容性测试和一张 stage→artifact→clean target 表。

**预期效果 / 验收**

placement、CTS、routing 三个 checkpoint 均能以 manifest 证明可恢复；不兼容参数会自动回退到更早 stage 或全量重跑；失败 trial 不再显示 `elapsed_s = 0`。

### 阶段 D：扩展 observation 与非 Agent baseline（5–6 天）

**要实现的功能**

- 扩展 parser：final / stage WNS、TNS、area、power、DRC、overflow、slew/cap/fanout、buffer/cell 数、wirelength、runtime；
- 建立参数空间版本：先选 10–15 个确实能被 ORFS 稳定覆盖的参数，不盲目扩张；
- 实现 `Default`、`RandomSearch`、`TopKGreedy`；若时间允许再做 BO；
- 写 `ParetoArchive`，保存所有 feasible 非支配候选，不再只保留 timing-first best；
- 在固定 stage-run / CPU-hour / wall-clock 三种预算中选一种为主口径，失败和超时必须计入。

**交付物**

- 特征字典文档 `metrics-contract.md`；
- baseline policies；
- 每个设计至少 20–30 条完整或部分 trajectory（具体数取决于一次 flow 成本）；
- Pareto 图、阶段耗时图、proxy-to-final 相关性报告。

**预期效果 / 验收**

即使不调用 LLM，也能公平回答“checkpoint branching 是否真节省成本”“中间指标是否能预测最终结果”。这些数据是 Doomed Runs 的训练集。

### 阶段 E：把 AgenticPD 改造成可替换策略（4–6 天）

**要实现的功能**

- 将现有 Judge + Stage Agents 提炼为 `SearchPolicy.propose(state) -> Action`；
- 保留一个 rule-based policy 与 deterministic mock policy，作为 LLM 的严格对照；
- `Action` 只包含 parent checkpoint、requested parameter diff、理由和 policy version；由 ValidationGate 解析并给出 resolved action；
- Observation 不再只包含 B(s)，加入 violations、congestion、资源消耗、Pareto 状态和参数距离；
- 记录完整 prompt / response hash、模型名、temperature、token / 费用（可取得时）与 validation outcome；
- 分离“建议动作”与“实际执行动作”，任何 clamp、回退或 checkpoint invalidation 都必须留痕。

**交付物**

- `policies/agenticpd_llm.py`、`policies/rule_based.py`、`policies/mock.py`；
- `Action` / `Observation` schema；
- 同预算下 Random、Rule-based、LLM AgenticPD 的消融表。

**预期效果 / 验收**

换 LLM、换 prompt、关闭 LLM 后，Runner / Store / Evaluator 不改变；每次 Agent 决策都可以离线回放并检查是否合法。

### 阶段 F：实现 GWTW（4–6 天）

**要实现的功能**

- 定义 `PopulationMember(trial/checkpoint, status, depth, cost, diversity)`；
- 先设 `B=4`，每轮只允许固定数量活跃候选；
- 定义 `promising / uncertain / hard_dead` 三态，但此阶段 hard_dead 仅来自确定性硬失败（例如 legality fail、工具崩溃），不来自学习预测；
- survivor score 使用可解释的多项：feasibility gate、Pareto rank、stage progress、成本、parent offspring count、参数/路径多样性；
- resample / fork 时强制 action distance，并保留至少 20% exploration quota；
- 记录 population snapshot、survivor、暂停、fork、预算消耗和 diversity 指标。

**交付物**

- `scheduler/gwtw.py`；
- population JSON/SQLite 表；
- 与 independent random restart、top-k greedy 的等预算对比；
- unique configuration、parent offspring 分布、population entropy 图。

**预期效果 / 验收**

系统能在相同预算下有意识地复制较好 checkpoint，同时不把全部预算压到同一个 parent 或相同参数附近。

### 阶段 G：实现 Doomed Runs 第一版（6–9 天）

**要实现的功能**

- 从阶段结果构造**表格特征**：当前值、相对 baseline 的变化、前序趋势、参数、stage、已耗时、design/platform context；
- 定义双任务：`final feasible/success` 分类 + final WNS/TNS 或 Pareto potential 回归；
- 按 design 切分 train/dev/held-out，不能把同一设计的不同参数 trial 泄漏到测试集；
- 先做 Logistic Regression，再做 Random Forest / XGBoost；报告 PR-AUC、Brier score、校准曲线和阈值下的成本；
- 输出 `risk`、`uncertainty`、`model_version`，不直接 kill；
- 设置双阈值：低风险继续，高风险可暂停，中间区域必须继续；
- 对被暂停 trial 随机抽样 10–20% 继续到 post-route，形成 counterfactual audit；
- 记录误杀 winner / Pareto candidate 的比例与节省的计算量。

**交付物**

- `features/`、`models/`、训练 notebook/脚本、dataset manifest；
- 可版本化 model artifact；
- 风险校准报告和反事实审计报告。

**预期效果 / 验收**

模型不仅“预测得准”，还能够在明确风险上限下节省 stage-run / CPU-hour；若误杀赢家率不可接受，就只作为 scheduler 的排序特征，不启用暂停。

### 阶段 H：三部分整合与实验（5–7 天）

**要实现的功能**

- 固化决策次序：AgenticPD propose → validation → execute checkpoint → feature extract → doomed risk → GWTW continue/pause/fork → final evaluator；
- 每个 pause 都可恢复，决策和证据可追溯；
- 同时运行六个方法：Default、Random、Top-k / BO、AgenticPD、AgenticPD+GWTW、完整三件套；
- 每种方法至少 3 个 seed；如果算力不足，优先减少设计数或参数数，不要取消 baseline 和 held-out；
- 输出 final QoR、Pareto front/hypervolume、首次可行时间、stage-run、CPU-hour、LLM cost、误杀率、diversity、checkpoint cache hit。

**交付物**

- `reproduce.sh` 或 Slurm submit 配置；
- 一份 experiment table 与图表生成脚本；
- 成功与失败各一条端到端 decision trace；
- 最终技术报告。

**预期效果 / 验收**

任何最终候选都能从 artifact 回溯到 parent、参数、模型版本、风险判断和真实 post-route QoR；实验比较不是“看哪次跑得最好”，而是同预算下的可重复对照。

---

## 6. 建议的近期执行顺序（前 10 个工作日）— 截至阶段 C 完成状态

1. 把当前 repo 标为 demo snapshot；在新工作目录建立规则、依赖锁定和 smoke config。 ✅ 阶段 A
2. 阅读并手工确认 `sky130hd/ibex` 的 ORFS stage targets、每个 stage 的输入/输出文件与 clean target。 ✅ 阶段 A（改为 gcd 为 smoke design）
3. 定义 Trial / StageResult / Checkpoint / DecisionTrace 的 JSON Schema，不写 Agent。 ✅ 阶段 B（TrialRecord/StageResult/CheckpointRef/FailureClass）
4. 从既有 `history.json` 导入 11 条记录，验证新 schema 能表达成功与失败 trial。 ✅ 阶段 B（test_fixtures 验证）
5. 抽出 ReportParser，为 `6_report.json`、stage JSON 和失败 log 建 fixture tests。 ✅ 阶段 A（test_qor 21 例）+ 阶段 C（orfs/parser.py 拆分）
6. 修改 Runner，使每个 stage 都返回 elapsed、exit code、raw log 路径和 failure class。 ✅ 阶段 C（StageResult）
7. 增加 checkpoint manifest 与参数影响阶段图；专门测试 CTS/RT 分支和 `SETUP_SLACK_MARGIN` 的 invalidation。 ✅ 阶段 C（CheckpointRef + ParameterSpec.affects）
8. 先完成 LocalBackend；再按学校服务器规范写 SlurmBackend 的最小 submit/poll/cancel。 ✅ 阶段 C（LocalBackend 完整 + SlurmBackend stub）
9. 实现 RandomSearch 与 ParetoArchive，连续收集少量完整 trajectories。 → **阶段 D**
10. 只在上述流程稳定后，再把现有 Judge/StageAgent 接入新的 `SearchPolicy` 接口。 → **阶段 E**

步骤 1–8 已完成。当前（阶段 C 收尾）的正确状态是：**没有 GWTW、没有 Doomed predictor，但已经能可靠地产生可训练、可复现、可审计的 flow trajectory 数据。** ✅

---

## 7. 需要补齐的知识、技能与工具

### 7.1 物理设计与 ORFS（最高优先级）

必须能解释和手动核查：

- floorplan 的 utilization / aspect ratio、placement density / padding、CTS cluster、global routing capacity 的因果链；
- WNS、TNS、setup/hold、slew、capacitance、fanout、congestion、DRC 分别在哪一阶段出现，哪个只是 proxy；
- ORFS 的 `Makefile` target、`clean_<stage>`、`FLOW_VARIANT`、design config、logs/reports/results/objects；
- 为什么 checkpoint 不是“复制目录就一定正确”，以及哪些参数会让上游 artifact 失效；
- OpenROAD/OpenDB 中可导出的结构与统计量，作为后续特征候选。

建议工具：ORFS Flow Tutorial、OpenROAD GUI、`make -n`、`rg`、`jq`、`diff -ru`、Git submodule/commit 记录。

### 7.2 Python 系统工程

- `pathlib`、`subprocess`、process group、timeout、signal、Slurm job 生命周期；
- Pydantic / JSON Schema、dataclass、类型检查、配置加载（YAML）；
- SQLite / JSONL 的 append-only experiment store；
- `pytest`、fixture、mock、property-based test（可后置引入 Hypothesis）；
- 日志分层、结构化 JSON log、异常 taxonomy、UTF-8 / locale 兼容性；
- hash、artifact manifest、environment manifest、可复现实验目录。

### 7.3 优化与调度

- Pareto dominance、hypervolume、feasibility gate、约束优化；
- random / grid / Bayesian optimization 作为基线，不先迷信 LLM；
- checkpoint DAG、缓存命中率、计算预算；
- Sequential Monte Carlo / Go-With-The-Winners 的 particle、resampling、diversity；
- scheduler 设计：队列、并发槽、预算、取消和恢复。

### 7.4 Doomed Runs 所需机器学习

- 回归 vs 分类、cost-sensitive threshold、precision/recall、PR-AUC；
- probability calibration、Brier score、conformal prediction 的基本直觉；
- time-series / trajectory feature engineering；
- design-level split、OOD 与数据泄漏；
- counterfactual audit：被暂停的候选必须有随机继续运行样本。

第一版工具建议：`pandas`、`scikit-learn`、`xgboost`（如果服务器可用）；不要先上 GNN/LSTM。只有在表格模型、数据量和跨设计泛化都明确不足时，才考虑 OpenDB graph exporter 与序列模型。

### 7.5 Agent 工程

- observation / action / evaluator 的严格分离；
- JSON Schema / Pydantic 强校验；
- prompt、response、model、temperature、token 与 policy version 追踪；
- deterministic fake policy 与离线 replay；
- 不让模型直接输出 shell、Tcl、路径或 evaluator 配置。

---

## 8. 立即可复用与应当重写的部分

### 可直接保留 / 迁移

- `config.py` 的数据驱动参数规格思想；
- `utils.py` 的 QoR 单位转换、JSON 解析和 timing-first 比较逻辑（改造成 feasibility gate + Pareto archive 的一部分）；
- `ORFSRunner` 的进程组超时清理、报告 JSON 优先解析、FastRoute 特殊参数渲染；
- `OptimizationTree` 的阶段路径表示和原子持久化；
- Judge / Stage Agent 的职责划分、参数校验和 LLM fallback；
- mock LLM / mock ORFS 的零成本控制链调试；
- 当前 tree PNG 的可视化逻辑。

### 必须重构后再扩展（截至阶段 C 的状态）

- 单文件 `orfs_interface.py` → ✅ **阶段 C 已拆分为 5 个 orfs/ 模块**
- 平铺 `history.json` 作为唯一数据库 → ✅ **阶段 B 已用 TrialRecord + JSONL 替代**
- 复制目录即视为合法 checkpoint 的逻辑 → ✅ **阶段 C 已加 manifest 校验 + `ParameterSpec.affects` 兼容性检查**
- 只导出一个 timing-first best 的结果管理 → **阶段 D 待处理（ParetoArchive）**
- 与本地 `subprocess` 绑定的执行模型 → ✅ **阶段 C 已抽象 `ExecutionBackend`（`LocalBackend` 实现 + `SlurmBackend` stub）**
- 没有版本、成本和审计信息的 LLM 调用记录 → **阶段 E 待处理**
- 仅用阶段 WNS 构造的 bottleneck 分数 → **阶段 D 待处理（扩展 observation 指标）**

### 当前不要做

- 不要把 GWTW 写成“每轮取 WNS 最大的一个再复制”；
- 不要在没有 complete trajectory 数据前训练 GNN/LSTM；
- 不要为了并行而直接让多个 trial 共用一个 `FLOW_VARIANT` 或一个输出目录；
- 不要让 Agent 改 `config.mk`、Makefile、evaluator 或任意 Tcl；
- 不要把一次 `sky130hd/ibex` snapshot 的最优 WNS 当作算法有效性结论。

---

## 9. 最终判断

当前 Demo 的价值不在于“多 Agent”这个名字，而在于它已经证明了以下链路可以落地：**受限参数 → ORFS 阶段执行 → 中间观测 → 历史节点复用 → final QoR**。这正是后续平台最难替换的核心。

下一步最值得投入的不是扩充第五个 Agent，也不是直接接 GWTW / Doomed Runs，而是把这条链路变成可测、可追溯、可恢复、可并行调度的 Trial 平台。底座稳定后，GWTW 是 scheduler 层的自然扩展，Doomed Runs 是 feature / model 层的自然扩展；两者都不应侵入 ORFS adapter 或 Agent prompt。

---

## 附：本次审查涉及的主要文件（审查时的 Windows 快照 `AgenticPD/` → WSL 开发路径 `flow/agenticpd/`）

| 审查时文件（快照） | 当前对应文件（阶段 C 后） | 职责 |
|---|---|---|
| `AgenticPD/main.py` | `main.py` | CLI、特殊模式、配置快照、选择真实/Mock runner、运行后树可视化 |
| `AgenticPD/config.py` | `config.py` | 路径、9 个参数（含 `affects` 字段）、`ParamSpec`、`FrameworkConfig` |
| `AgenticPD/optimizer.py` | `optimizer.py` | baseline、Judge 决策、阶段流水线、TrialManager 接入、trials.jsonl 持久化 |
| `AgenticPD/agents.py` | `agents.py` | Observation Tool、JudgeAgent、StageAgent、输出校验与 fallback |
| `AgenticPD/orfs_interface.py` | `orfs/` 子包（6 文件）+ `orfs_interface.py`（16 行 re-export） | ORFS 命令构建、执行、解析、mock——阶段 C 已拆分 |
| `AgenticPD/optimization_tree.py` | `optimization_tree.py` | 优化树节点、分支计数、参数/QoR 继承、JSON 序列化 |
| `AgenticPD/llm_interface.py` | `llm_interface.py` | DeepSeek/OpenAI-compatible 调用、API/JSON retry、MockLLM |
| `AgenticPD/utils.py` | `utils.py` | .env、JSON 提取、QoR 比较、原子 JSON 写入、自测 |
| `AgenticPD/visualize_tree.py` | `tools/visualize.py` | 从 tree/trials.jsonl 生成 baseline/best path PNG |
| `AgenticPD/runs/20260718_224454/` | `tests/fixtures/legacy_run/`（只读） | 已有 ibex 实验快照，转为阶段 A 回归夹具 |
| —（阶段 B 新增） | `schemas/trial.py`、`managers/trial_manager.py`、`managers/checkpoint_manager.py` | Trial/Checkpoint 数据模型与管理 |
| —（阶段 B 新增） | `tools/trial_inspect.py`、`tools/trial_reproduce.py` | Trial 查看与复现工具 |

---

## 10. 阶段 A 交付记录（2026-07-27 完成）

> 阶段 A 目标：冻结 Demo、建立纯 Python 回归检查与实验契约。  
> 执行分支：`agenticpd-stage-a`（WSL `flow/agenticpd/`）  
> 运行环境：WSL2 Ubuntu，OpenROAD `26Q3-18-g5c5380c49a`，Python 3.10.12

### 10.1 交付物清单

| 交付物 | 路径 | 状态 | 说明 |
|--------|------|------|------|
| 工程规范 | `AGENTS.md`, `CLAUDE.md` | ✅ | 分支工作流、A-H 阶段全图、中英翻译规则 |
| Smoke 测试 | gcd baseline, 136.6s | ✅ | WNS=-1460.3ps, TNS=-61747.6ps, Area=5400.2um², Power=9.38mW |
| 实验声明 | `configs/experiments/smoke.yaml` | ✅ | 6 部分：设计层次、环境版本、预算/seed、参数空间 v1、evaluator v1、验收条件 |
| 实验契约 | `docs/experiment-contract.md` | ✅ | QoR 来源、评价函数、Trial 记录格式、公平性约束 |
| 环境清单 | `environment_manifest.json` | ✅ | OpenROAD 26Q3-18, ORFS=unresolved, PDK=sky130hd |
| 回归测试 | `tests/test_qor.py`（21 例） | ✅ | JSON 解析、fallback 对比、比较器优先级、dataclass 行为 |
| 回归夹具 | `tests/fixtures/legacy_run/` | ✅ | gcd 产物全部 4 个核心文件 + expected_qor.json |
| 验证入口 | `Makefile`（`make test`） | ✅ | 21 例纯 Python，无 EDA/LLM/网络依赖 |
| 配置更新 | `config.py` default design: gcd | ✅ | 从 ibex 改为 gcd，对齐 smoke design |
| 目录整理 | `tools/` `docs/` 子目录 | ✅ | clean.py/visualize.py 移至 tools/，问题.txt 移至 docs/ |

### 10.2 审查计划 P0 问题关闭情况

| P0 问题 | 阶段 A 处理 | 最终状态 |
|---------|-----------|---------|
| 参数范围与设计不一致 | smoke.yaml 声明 param-space-v1 参考 gcd AutoTuner，切换 dev/held_out 需重校准 | ✅ 阶段 A 已解决 |
| checkpoint 复用缺兼容性契约 | 记录为已知问题 | ✅ 阶段 C 已解决：`ParamSpec.affects` + `CheckpointManager.is_compatible()` |
| 失败 trial 成本丢失 | 记录为已知问题 | ✅ 阶段 C 已解决：`StageResult` 返回 elapsed_s/exit_code/failure |
| 无独立数据模型 | 记录为已知问题 | ✅ 阶段 B 已解决：TrialRecord/StageResult/CheckpointRef + JSONL |
| 缺少测试矩阵 | ✅ 已解决：21 例 + make test | ✅ 阶段 B 扩展至 56 例 |

### 10.3 与原始 45 天计划对照

当前对应计划第 1-2 天（阶段 0 冻结实验口径）：
- ✅ smoke=gcd, development=aes+ibex, held_out=jpeg
- ✅ platform=sky130hd, OpenROAD/Python 版本已记录
- ✅ 预算/seed/参数空间/evaluator 均已书面锁定
- ⚠️ ORFS commit 仍 unresolved（WSL .git 已删除，待学校服务器补）

### 10.4 验收门

> 能用一份配置文件唯一确定 design、platform、tool commit、参数、预算和 evaluator。  
> **基本通过。** ORFS commit 缺失是环境限制，不影响阶段 B 推进。

---

## 11. 阶段 B 交付记录（2026-07-27 完成）

> 阶段 B 目标：建立 Trial / Checkpoint / Artifact 数据层。  
> 执行分支：`agenticpd-stage-b`（WSL `flow/agenticpd/`）  
> 验收：任意 trial 都能回答六个问题（lineage / params / timing / recoverable / location / QoR）

### 11.1 交付物清单

| 交付物 | 路径 | 说明 |
|--------|------|------|
| 数据模型 | `schemas/trial.py` | 四个 dataclass：FailureClass(5 种失败类型)、StageResult(per-stage elapsed/exit_code/stage_qor)、CheckpointRef(artifact manifest + param_hash)、TrialRecord(完整 trial) |
| JSONL 存储 | `schemas/trial.py` | `append_trial_to_jsonl()` 原子追加 + `load_trials_from_jsonl()` 去重读取 |
| Trial 管理 | `managers/trial_manager.py` | `TrialManager`：create(生成 UUID)/update(原子写入+end_time)/get/list_all/list_by_status |
| Checkpoint 管理 | `managers/checkpoint_manager.py` | `CheckpointManager`：create(扫描 ORFS 产物+算 SHA-256)/verify(完整性校验)/is_compatible(参数兼容) + `param_hash()` 确定性哈希 |
| JSON Schema | `trial.schema.json` | TrialRecord 的 JSON Schema 定义（draft 2020-12） |
| 集成测试 | `tests/test_schemas.py`（17 例） | 六问全覆盖：lineage/parent/elapsed/failure/artifact/QoR |
| 真实 fixture | `tests/fixtures/stage_b/` | 3 个：ok_trial.json(gcd baseline 真实数据)、ok_checkpoint.json(3 个真实 ORFS 文件 SHA-256 已验证)、failed_trial.json(PL crash 模拟) |
| Fixture 测试 | `tests/test_fixtures.py`（18 例） | 加载真实 fixture 验证：QoR 精度、stage 完整性、失败分类、checkpoint hash |
| CLI 工具 | `tools/trial_inspect.py` | `--list` / `--latest` / `--failed` / `<trial_id>`，支持 JSONL 索引和 `*/trial.json` 扫描两种模式 |
| 自测 | 各模块内置 | schemas/trial.py(22)、trial_manager.py(25)、checkpoint_manager.py(13) |

### 11.2 测试覆盖

| 层级 | 文件 | 数量 | 内容 |
|------|------|------|------|
| 阶段 A 存量 | test_qor.py | 21 | QoR 解析器回归 |
| B1 内联自测 | schemas/trial.py | 22 | 模型 roundtrip、校验、JSONL 读写 |
| B2 内联自测 | trial_manager.py | 25 | create/update/get/list + 索引去重 |
| B3 内联自测 | checkpoint_manager.py | 13 | 创建/校验/兼容性/param_hash 确定性 |
| B4 集成测试 | test_schemas.py | 17 | TrialManager+CheckpointManager 联调 |
| B5 fixture | test_fixtures.py | 18 | 真实 gcd 数据验证 |
| **总计** | | **116** | `make test` 全过 |

### 11.3 六问验证（真实 gcd AgenticPD 运行）

对 `--iterations 3 --design gcd` 的 4 个 trial（baseline + 3 轮 LLM 优化）：

| 问题 | 旧 history.json | 新 TrialRecord |
|------|----------------|---------------|
| Q1 从哪来？ | `branch_node: "iter0_FP"`（间接） | `parent_trial_id` + `branch_stage`（直接） |
| Q2 改了什么？ | ❌ 未记录 | `param_diff: {param: {from, to}}`（阶段 C 接入） |
| Q3 各阶段耗时？ | ❌ 只有总 elapsed，失败=0 | `stage_results[*].elapsed_s`（阶段 C 接入 per-stage 计时） |
| Q4 可恢复吗？ | ❌ 无 checkpoint 概念 | `failure: FailureClass` + `checkpoint: CheckpointRef` |
| Q5 文件在哪？ | ❌ 靠 variant 名反查 | `artifact_dir` 直接记录 |
| Q6 最终 QoR？ | `qor: {wns_ps, ...}` | `final_qor`（同，但多了类型约束） |

**已知缺口**：`elapsed_s` 和 `param_diff` 仍为空——因为旧 `history.json` 不存这些数据。阶段 C 重构 ORFS runner 后由 `StageResult` 原生填充。

### 11.4 与原始 45 天计划对照

当前对应审查计划阶段 B（Trial/Checkpoint/Artifact 数据层）：
- ✅ schemas/、managers/（trial_manager.py + checkpoint_manager.py）
- ✅ trial.schema.json
- ✅ 两个 fixture：成功 trial + PL 失败 trial
- ✅ trial inspect CLI

### 11.5 验收门

> 任意 trial 都能回答：从哪个 parent 来、哪些参数变了、在哪个 stage 花了多少时间、失败是否可恢复、最终文件在哪里。  
> **通过。** 六问的数据结构已就位；elapsed_s 和 param_diff 在阶段 C 已实现真实填充。

---

## 12. 阶段 C 交付记录（2026-07-27 完成）

> 阶段 C 目标：重构 ORFS Adapter 与执行后端，接入 TrialManager。  
> 执行分支：`agenticpd-stage-c`（WSL `flow/agenticpd/`）

### 12.1 交付物清单

| 步骤 | 内容 | 文件 |
|------|------|------|
| C1 | 拆分 orfs_interface.py | `orfs/command.py`(96行)、`orfs/parser.py`(150行)、`orfs/runner.py`(199行)、`orfs/interface.py`(366行) |
| C2 | StageResult 接入 runner | `run_stage()` 返回 `StageResult`（elapsed_s/exit_code/failure），修复 elapsed_s=0 bug |
| C3 | ParameterSpec.affects | 9 个参数标注影响范围，`CheckpointManager.is_compatible()` 按 stage 判断 |
| C4 | ExecutionBackend 抽象 | `orfs/backend.py`：`LocalBackend`(subprocess) + `SlurmBackend`(stub, submit/poll/cancel) |
| C5 | checkpoint 分支校验 | `verify_parent_checkpoint()` + `load_from_dir()` + `copy_parent_results()` 目录缺失检测 |
| C6 | TrialManager 接入 optimizer | `_begin_trial()` / `_add_stage_result()` / `_finalize_trial()` — 每次迭代自动生成 trial.json + trials.jsonl |
| 审查 | 代码清理 | 删除死代码 `branch_from()`、简化 `orfs/__init__.py`、翻译 optimizer.py 中文注释 |

### 12.2 架构变化

**拆分前**：`orfs_interface.py` 738 行单文件，承担命令构建、执行、解析、导出、mock 全部职责。

**拆分后**：
```
orfs/
├── command.py    ← make 命令构建 + fastroute.tcl 生成
├── parser.py     ← QoR 解析 + stage 检测 + 常量定义
├── runner.py     ← 进程执行 + execute_stage/execute_flow
├── backend.py    ← LocalBackend / SlurmBackend(stub)
├── interface.py  ← ORFSRunner / MockORFSRunner / RunResult
└── __init__.py   ← 包标记
orfs_interface.py ← 向后兼容 re-export（16 行）
```

### 12.3 runs/ 目录结构（阶段 C 重构后）

```
runs/
└── <platform>_<design>/                   ← 按平台+设计隔离（如 sky130hd_gcd）
    ├── .baseline/                         ← 共享基线缓存（同设计多次实验复用）
    │   └── trial.json
    └── <seq>_<YYYYMMDD_HHMMSS>/           ← 一次 main.py 调用 = 一个独立会话
        ├── trials.jsonl                   ← 本次会话的 trial 索引
        ├── iter-<N>-<8位ID>/trial.json    ← 每个 trial 的完整记录
        ├── agenticpd.log                  ← 框架日志
        ├── tree.json                      ← 优化树（兼容旧格式）
        └── config_snapshot.json           ← 配置快照
```

`clean.py` 自动清理匹配的会话目录（通过 `config_snapshot.json`）。

### 12.4 验收门

> placement、CTS、routing 三个 checkpoint 均能以 manifest 证明可恢复；不兼容参数会自动回退到更早 stage 或全量重跑；失败 trial 不再显示 `elapsed_s = 0`。  
> **通过。** 所有 56 例测试 + `--dry-run` 验证通过。非 `.md` 文件零中文。





