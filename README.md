# AgenticPD — LLM 多智能体驱动的物理设计 QoR 优化框架

复现论文 *"AgenticPD: Stage-Aware Agentic Framework for Physical Design QoR
Optimization"* 的原型实现：1 个法官智能体（JudgeAgent）+ 4 个阶段智能体
（FP/PL/CTS/RT），迭代调整 OpenROAD Flow Scripts（ORFS）的流程参数，
优化 WNS / TNS / Area / Power 四项 QoR 指标。支持 nangate45、sky130hd 等
ORFS 兼容的任意工艺/设计组合。

核心机制：**优化树 + 分支复用**（Bef 阶段零成本继承）→ **观测工具**（探索平衡度
E(n) + 阶段瓶颈 B(s)）→ **逐阶段流水线**（StageAgent 调 LLM 生成参数 → make 单阶段
→ 获取真实中间 QoR → 传给下一个 StageAgent）→ **自动树可视化** → **一键清理**。

## AgenticPD 核心机制形式化整理

### 1. 物理设计流程的形式化

#### 1.1 阶段与动作空间
设物理设计流程为有序阶段序列：

$$
\mathcal{S} = (\text{FP},\ \text{PL},\ \text{CTS},\ \text{RT})
$$

每个阶段 $s \in \mathcal{S}$ 拥有自己的参数空间（动作空间）$\Theta_s$，则完整流程的动作空间为笛卡尔积：

$$
\Theta_{\mathrm{PD}} = \Theta_{\text{FP}} \times \Theta_{\text{PL}} \times \Theta_{\text{CTS}} \times \Theta_{\text{RT}}
$$

一次完整流程由一个动作元组唯一确定：

$$
\mathbf{a} = (a_{\text{FP}},\ a_{\text{PL}},\ a_{\text{CTS}},\ a_{\text{RT}}) \in \Theta_{\mathrm{PD}}
$$

其中 $a_s \in \Theta_s$ 表示在阶段 $s$ 选取的具体参数值。

#### 1.2 分支与前后继关系
由于阶段顺序固定，任意选定阶段 $b \in \mathcal{S}$ 可将流程划分为：

- **前置阶段**：$\mathrm{Bef}(b) = \{s \in \mathcal{S} \mid s \text{ 在 } b \text{ 之前}\}$
- **后置阶段**：$\mathrm{Aft}(b) = \{s \in \mathcal{S} \mid s \text{ 在 } b \text{ 之后}\}$

**示例**：若 $b = \text{CTS}$，则 $\mathrm{Bef(CTS)} = \{\text{FP},\text{PL}\}$，$\mathrm{Aft(CTS)} = \{\text{RT}\}$。

#### 1.3 QoR 度量
执行完整流程 $\mathbf{a}$ 后，获得后布线（post‑route）签核指标元组：

$$
Q(\mathbf{a}) = ( \text{WNS},\ \text{TNS},\ \text{Area},\ \text{Power} )
$$

其中 WNS（最差负时序裕量）和 TNS（总负时序裕量）为时序指标（越高越好），面积和功耗越低越好。所有优化反馈均基于该真实后布线结果，**不使用任何中间阶段代理指标**。

### 2. 优化目标与迭代过程

给定迭代预算 $N$，优化器依次产生 $N$ 个完整流程动作 $\mathbf{a}_1, \mathbf{a}_2, \ldots, \mathbf{a}_N$，目标是最大化最优后布线 QoR（时序优先）：

$$
\max_{k \in \{1,\ldots,N\}} Q(\mathbf{a}_k)
$$

最终报告结果为历史最优候选 $\mathbf{a}^* = \arg\max_k Q(\mathbf{a}_k)$，其 QoR 已在迭代中测得，无需额外评估。

### 3. 优化树与分支机制

#### 3.1 树结构定义
所有历史执行结果组织为一棵有根树 $\mathcal{T}$。根节点 $n_0$ 代表综合后的网表（PD 输入）。每次执行阶段 $s$ 时，创建一个节点：

$$
n_k^s = \big( a_k(s),\ Q_k(s) \big)
$$

其中 $a_k(s)$ 是该阶段采取的动作，$Q_k(s)$ 是该阶段执行后观测到的阶段性 QoR。  
从根到叶的每条完整路径（依次经过 FP、PL、CTS、RT）对应一个完整的动作元组 $\mathbf{a}_k$。

#### 3.2 分支操作
在迭代 $k$ 中，优化器选择一个已存在的中间节点 $\hat{n}$（位于某个阶段 $b \in \mathcal{S}$），从该节点出发启动新分支。则新分支：

- **复用** $\mathrm{Bef}(b)$ 阶段的所有结果（即继承祖先路径上的动作与 QoR），**成本为零**；
- **重新执行** $\{b\} \cup \mathrm{Aft}(b)$ 阶段，产生新的动作和新的节点。

新节点作为子树挂载到 $\hat{n}$ 下：

$$
\mathcal{T}_k = \mathcal{T}_{k-1} \ \cup \ \{ n_k^s \mid s \in \{b_k\} \cup \mathrm{Aft}(b_k) \}
$$

其中 $b_k$ 为第 $k$ 次迭代选定的分支阶段。特别地，若 $b_k = \text{FP}$，则从根节点分支，等价于从头运行全新流程。

> **说明**：分支机制避免了每次迭代都重复运行所有阶段，能将宝贵预算集中在有提升空间的后期阶段，实现"增量式"优化。

### 4. 法官智能体（Judge Agent）

法官智能体由通用 LLM 引擎 $\mathcal{L}$、Prompt $\mathcal{P}_J$ 和Harness Skills $\mathcal{U}_J$ 构成：

$$
\text{Judge} = (\mathcal{L},\ \mathcal{P}_J,\ \mathcal{U}_J)
$$

#### 4.1 输入：优化历史
在迭代 $k$ 开始时，Harness向法官提供历史记录 $\mathcal{H}_k$：

$$
\mathcal{H}_k = \{\ (\hat{n}_i,\ b_i,\ \{Q_i(s)\}_{s \ge b_i})\ \}_{i=1}^{k-1}
$$

每个历史条目包含：第 $i$ 次迭代的分支起始节点 $\hat{n}_i$、分支阶段 $b_i$，以及该分支执行后各阶段（从 $b_i$ 到 RT）的 QoR。

#### 4.2 观测工具（Observation Tool）
Harness $\mathcal{U}_J$ 内置的观测工具根据 $\mathcal{H}_k$ 和当前树 $\mathcal{T}_k$，计算自适应概要 $\mathcal{A}_k$，包含两个关键信号：

- **探索平衡度** $E(n)$：每个节点 $n$ 被选为分支起点的次数，用于识别过探索/欠探索区域。
- **阶段瓶颈** $B(s)$：每个阶段的当前 QoR 与历史最优的差距，用于定位当前最薄弱的环节。

观测工具将这两个信号连同树结构快照组装成搜索状态概要(search state profile)，作为法官的观测输入（而非原始历史转储，以控制 token 开销）。

#### 4.3 决策输出
基于概要，法官产生决策：

$$
\mathcal{D}_k = (\hat{n}_k,\ b_k,\ \{ \text{hint}_s \}_{s \in \{b_k\}\cup \mathrm{Aft}(b_k)} )
$$

- $\hat{n}_k$：选定的分支节点（平衡探索与利用，由 $E(n)$ 引导）；
- $b_k$：选定的分支阶段（通常选择瓶颈最大的阶段 $B(s)$）；
- 为每个将要执行的下游阶段提供一条文本提示（hint），指导该阶段智能体如何调整参数。


### 5. 阶段智能体（Stage Agent）

每个阶段 $s$ 拥有一个专属的阶段智能体：

$$
\text{StageAgent}_s = (\mathcal{L},\ \mathcal{P}_s,\ \mathcal{U}_s)
$$

其中 $\mathcal{P}_s$ 是该阶段的系统提示（描述职责、参数范围、优化目标等），$\mathcal{U}_s$ 是该阶段的 **PD 技能**，负责与后端工具交互（执行阶段、返回 QoR）。

#### 5.1 执行上下文
在法官选定分支 $b_k$ 后，依次执行 $s \in \{b_k\} \cup \mathrm{Aft}(b_k)$。对于每个阶段 $s$，Harness为它构建上下文：

$$
\text{ctx}_s = \big( \{Q_k(i)\}_{i \in \mathrm{Bef}(s)},\ e_s,\ \text{hint}_s \big)
$$

- $\{Q_k(i)\}_{i \in \mathrm{Bef}(s)}$：当前分支中上游阶段（已完成）的 QoR 结果；
- $e_s$：该阶段跨迭代的历史经验（如之前尝试过的参数及结果）；
- $\text{hint}_s$：法官专门给该阶段的提示。

#### 5.2 动作生成与执行
阶段智能体根据 $\text{ctx}_s$ 推理，输出一个具体动作 $a_k(s) \in \Theta_s$。然后Harness调用其 PD 技能执行该阶段：

$$
a_k(s) = \pi_s(\text{ctx}_s), \quad Q_k(s) = \text{Execute}(s,\ a_k(s))
$$

执行完成后，$Q_k(s)$ 被记录，并作为上游 QoR 传递给下一个阶段。

> **文字说明**：每个阶段智能体只关注本阶段的参数调整，无需了解全局树结构，降低了单个智能体的决策复杂度。法官负责全局导航，阶段智能体负责局部优化，形成清晰的职责分离。



### 6. 整体优化循环（伪代码）

```
输入：设计 D，迭代预算 N，初始动作 a0
输出：最优动作 a* 及其后布线 QoR Q*

1. 运行初始完整流程 (a0)，记录所有阶段 QoR，初始化树 T 和历史 H
2. Q* = Q(a0), a* = a0
3. for k = 1 to N do
4.     A_k = ObservationTool(T, H)          // 生成自适应概要
5.     (n_hat, b, hints) = Judge(H, A_k)    // 法官决策
6.     复用 Bef(b) 的结果（从节点 n_hat 继承）
7.     for s in {b} ∪ Aft(b) do             // 按顺序执行
8.         ctx = BuildContext(s, n_hat, hints[s])  // 构建上下文
9.         a_k(s) = StageAgent_s(ctx)               // 阶段智能体生成动作
10.        Q_k(s) = ExecuteStage(s, a_k(s))          // 执行并获取 QoR
11.    end for
12.    更新 T 和 H（加入新节点）
13.    如果当前候选的时序指标优于 Q* 且满足面积/功耗约束，则更新 (a*, Q*)
14. end for
15. 返回 (a*, Q*)
```

### 7. 关键设计要点总结

| 组件 | 功能 | 输入 | 输出 |
|------|------|------|------|
| **Judge Agnet** | 全局导航：选择分支节点和分支阶段，生成 hint | 历史记录 + 自适应概要 | 分支决策 + 各阶段 hint |
| **Observation Tool** | 压缩历史为探索平衡度和阶段瓶颈 | 树 T + 历史 H | 自适应概要 A |
| **Stage Agent** | 局部优化：为所属阶段生成具体参数 | 上游 QoR + 跨迭代经验 + hint | 该阶段动作 |
| **Optimization Tree** | 存储所有尝试及其 QoR，支持分支复用 | - | 搜索空间结构 |
| **Harness** | 协调智能体调度、上下文组装、工具执行 | - | 完整的迭代闭环 |

---

## 目录结构

### 根目录 — 入口与规范

| 文件 | 职责 |
|------|------|
| `AGENTS.md` | Codex 工程规范（与 CLAUDE.md 内容完全一致） |
| `CLAUDE.md` | Claude 工程规范（与 AGENTS.md 内容完全一致） |
| `README.md` | 本文件 —— 项目说明、使用指南、架构文档 |
| `Makefile` | 验证入口：`make test`（纯 Python，无 EDA/LLM 依赖） |
| `.env` | API key（不提交，本地环境变量） |
| `.env.example` | `.env` 模板（不含真实 key，供参考） |
| `.gitignore` | 忽略 runs/、.env、pycache 等 |
| `requirements.txt` | Python 依赖（目前仅 `openai`） |
| `environment_manifest.json` | 环境版本快照（OpenROAD 版本、Python 版本等） |
| `trial.schema.json` | TrialRecord 的 JSON Schema 定义（draft 2020-12） |

### 核心模块（根目录 `.py`）

| 文件 | 行数 | 职责 |
|------|------|------|
| `main.py` | 208 | CLI 入口：`--baseline-only` / `--dry-run` / `--resume` / `--iterations N`，初始化 LLM/Runner/Optimizer，启动优化循环 |
| `config.py` | 361 | **全局配置唯一来源**。定义 9 个可调参数（`ParamSpec`，含类型/范围/默认值/影响阶段）、`FrameworkConfig`（路径/超参/LLM 设置）、全局路径常量 |
| `optimizer.py` | 501 | **优化主循环**。实现论文 §6 伪代码：建树 → ObservationTool → Judge 决策 → 分支 → 逐阶段流水线 → 记录历史。阶段 C 接入 `TrialManager` |
| `agents.py` | 573 | **Agent 层**。`JudgeAgent`（全局导航，选分支节点和阶段）、`StageAgent ×4`（FP/PL/CTS/RT 各阶段参数生成）、`ObservationTool`（计算 E(n) 探索平衡度 + B(s) 阶段瓶颈） |
| `llm_interface.py` | 184 | **LLM 客户端**。DeepSeek/OpenAI-compatible API 调用，含指数退避重试、JSON 回喂重问、`MockLLMClient`（零 token 调试） |
| `utils.py` | 396 | **工具集**。`QoR` 数据类（从 6_report.json / rpt 解析）、`qor_is_better()` 时序优先比较器、原子 JSON 写入、日志配置 |
| `optimization_tree.py` | 266 | **优化树 T**。有根树数据结构：节点增删查、E(n) 分支计数、Bef 阶段参数/QoR 继承、JSON 序列化/反序列化 |

### orfs/ — ORFS 适配层（阶段 C 拆分）

| 文件 | 行数 | 职责 |
|------|------|------|
| `orfs/command.py` | 96 | **make 命令构建**。将 `{stage: {param: value}}` 转为 `make DESIGN_CONFIG=... FLOW_VARIANT=... NAME=value ...` 命令行。处理三类参数：普通 make 变量、FastRoute TCL 生成、GRT 参数渲染 |
| `orfs/parser.py` | 150 | **报告解析**。`parse_qor()` 从 6_report.json（优先）或 rpt/log 正则兜底提取 WNS/TNS/Area/Power；`parse_stage_qor()` 提取各阶段中间时序；`detect_failed_stage()` 定位崩溃阶段。定义 `STEP_JSON_SEQUENCE`、`STAGE_QOR_SOURCES` 等 ORFS 常量 |
| `orfs/runner.py` | 199 | **阶段执行**。`execute_stage()` 单阶段 make（clean → make → 解析，返回 `StageResult`）；`execute_flow()` 完整 RTL→GDS；`run_make()` 委托给 `ExecutionBackend` |
| `orfs/backend.py` | 163 | **执行后端抽象**。`LocalBackend`（subprocess + 进程组超时清理）；`SlurmBackend`（stub，接口 sbatch/squeue/scancel，待服务器部署） |
| `orfs/interface.py` | 366 | **ORFS 编排器**。`ORFSRunner`（绑定 `FrameworkConfig`，提供 `run_flow/run_stage/run_finish/copy_parent_results/export_best`）；`MockORFSRunner`（零 EDA 调试）；`RunResult` 数据类 |
| `orfs/__init__.py` | 4 | 包标记 |
| `orfs_interface.py` | 16 | **向后兼容重导出层**。`from orfs_interface import ORFSRunner, RunResult` 保持旧 import 路径有效 |

### schemas/ — 数据模型（阶段 B）

| 文件 | 行数 | 职责 |
|------|------|------|
| `schemas/trial.py` | 465 | **四个核心数据类**。`FailureClass`（5 种失败类型枚举）、`StageResult`（单阶段执行记录，elapsed/exit_code/stage_qor/failure）、`CheckpointRef`（可恢复存档点，artifact manifest + SHA-256）、`TrialRecord`（一次完整 RTL→GDS 运行，lineage/QoR/stage_results/checkpoint）。含 JSONL 追加/读取工具 |
| `schemas/__init__.py` | 13 | 包导出 |

### 管理层（阶段 B）

| 文件 | 行数 | 职责 |
|------|------|------|
| `trial_manager.py` | 263 | **Trial 生命周期管理**。`TrialManager`：`create()` 生成 UUID + 写入 `runs/<id>/trial.json`；`update()` 覆盖 + 追加 `trials.jsonl` 索引；`get/list_all/list_by_status/latest` 查询。原子写入（.tmp → os.replace） |
| `checkpoint_manager.py` | 392 | **Checkpoint 生命周期管理**。`CheckpointManager`：`create()` 扫描 ORFS 产物 + SHA-256 哈希 → `CheckpointRef`；`verify()` 完整性校验；`is_compatible()` 基于 `ParameterSpec.affects` 的阶段感知兼容性判断；`param_hash()` 确定性参数哈希 |

### tools/ — CLI 工具

| 文件 | 行数 | 职责 |
|------|------|------|
| `tools/clean.py` | 224 | **产物清理**。删除指定 platform/design 的所有 variant（除 base）+ 匹配的 runs/ 会话目录。`--dry-run` 预览、`--yes` 跳过确认 |
| `tools/visualize.py` | 384 | **优化树可视化**。从 `tree.json` + `history.json` 生成 PNG（5 层布局，绿色基线路径 + 红色最佳路径） |
| `tools/trial_inspect.py` | 199 | **Trial 查看器**。`--list` / `--latest` / `--failed` / `<trial_id>` `--stages`。支持 JSONL 索引和 `*/trial.json` 扫描两种模式 |

### configs/ — 实验配置

| 文件 | 职责 |
|------|------|
| `configs/experiments/smoke.yaml` | 阶段 A smoke test 完整声明（设计层次、环境版本、预算/seed、参数空间 v1、evaluator v1、验收条件）。每次新实验复制此文件为 `<日期>-<设计>-<方法>.yaml` |

### docs/ — 设计文档（中文）

| 文件 | 职责 |
|------|------|
| `docs/experiment-contract.md` | 实验契约：QoR 数据来源、评价函数（时序优先比较器）、Trial 记录格式、预算定义、实验公平性约束 |
| `docs/问题.txt` | 开发过程中的未解决问题与已知限制 |

### tests/ — 测试（纯 Python，无 EDA/LLM/网络依赖）

| 文件 | 行数 | 用例 | 覆盖内容 |
|------|------|------|---------|
| `tests/test_qor.py` | 137 | 21 | QoR JSON 解析（已知正确值核对）、rpt/log fallback（容差校验）、`qor_is_better()` 比较器优先级、dataclass 行为 |
| `tests/test_schemas.py` | 265 | 17 | TrialManager + CheckpointManager 集成：六问全覆盖（lineage/params/elapsed/failure/artifact/QoR）、JSONL 去重、corrupt 行跳过 |
| `tests/test_fixtures.py` | 121 | 18 | 真实 fixture 验证：ok_trial（gcd baseline 完整记录）、ok_checkpoint（3 个真实 ORFS 文件 SHA-256 已验证）、failed_trial（PL crash 模拟） |
| `tests/fixtures/legacy_run/` | — | — | 阶段 A gcd smoke test 只读回归证据（6_report.json/rpt/log + expected_qor.json） |
| `tests/fixtures/stage_b/` | — | — | 阶段 B 真实 fixture（ok_trial.json / ok_checkpoint.json / failed_trial.json） |

### scripts/ — 辅助脚本

| 文件 | 职责 |
|------|------|
| `scripts/build_fixtures.py` | 从 ORFS 运行产物构建 test fixture（TrialRecord + CheckpointRef），一次性使用 |

### 其他目录

| 目录 | 职责 |
|------|------|
| `attachments/` | 文档用图片（架构图、优化树截图） |
| `runs/` | 运行产物（不进 git）。每个 `main.py` 调用创建一个 `<时间戳>/` 会话目录，内含 `trials.jsonl`（索引）、`<trial_id>/trial.json`（TrialRecord）、`agenticpd.log`、`history.json`、`tree.json`。`clean.py` 可一键清理 |

## 环境准备

1. **前提**：WSL Ubuntu，Python >= 3.10，ORFS 已可正常运行
   （`cd flow && make DESIGN_CONFIG=./designs/sky130hd/gcd/config.mk` 能跑通）。

2. **安装 Python 依赖**：
   ```bash
   pip3 install -r flow/agenticpd/requirements.txt
   ```

3. **配置 API key**（永不写进代码/commit）：
   ```bash
   # 方式 A：.env 文件（推荐）
   cp flow/agenticpd/.env.example flow/agenticpd/.env
   # 编辑 .env 填入真实 key

   # 方式 B：环境变量
   export DEEPSEEK_API_KEY=sk-...
   ```

## 运行

所有命令在 `flow/` 目录下执行：

```bash
cd flow

# 完整优化：基线 + N 次迭代（需要 API key）
python3 agenticpd/main.py --iterations 10 --platform nangate45 --design gcd

# 断点续跑（自动取 runs/ 下最新一次）
python3 agenticpd/main.py --resume

# 调试模式（零 token / 零 EDA）：
python3 agenticpd/main.py --parse-only base               # 只解析已有 variant 的 QoR
python3 agenticpd/main.py --baseline-only                 # 只跑一次基线 ORFS
python3 agenticpd/main.py --dry-run --mock-orfs --iterations 5  # 全 mock 秒级跑完
python3 agenticpd/main.py --dry-run --iterations 2        # MockLLM + 真实 ORFS

# 清理指定设计的所有产物（base 不受影响）：
python3 agenticpd/tools/clean.py --target nangate45 gcd --dry-run   # 预览
python3 agenticpd/tools/clean.py --target nangate45 gcd             # 确认后删除
python3 agenticpd/tools/clean.py --target nangate45 gcd --yes       # 跳过确认直接删

# 从已有运行生成树可视化：
python3 agenticpd/tools/visualize.py runs/20260718_210019
```

常用选项：`--design`、`--platform`、`--timeout`（秒）、`--wns-tol`/`--tns-tol`（ps）、
`--log-level DEBUG`（完整 prompt 输出到 agenticpd.log）。

## 输出位置

| 内容 | 路径 | 说明 |
|---|---|---|
| 最佳产物 | `flow/results/<plat>/<design>/agenticpd_best/` | 最终 GDS/DEF/网表 + 报告 + `agenticpd_summary.json` |
| 每轮迭代产物 | `flow/{results,logs,reports,objects}/<plat>/<design>/agenticpd_iter<N>/` | FLOW_VARIANT 隔离，`base` 永不触碰 |
| 优化树 PNG | `runs/<时间戳>/optimization_tree.png` | 每次运行结束后自动生成 |
| history.json | `runs/<时间戳>/history.json` | 平面优化日志（完整字段见下方） |
| tree.json | `runs/<时间戳>/tree.json` | 优化树 T：节点 + 父子关系 + E(n) |
| agenticpd.log | `runs/<时间戳>/agenticpd.log` | 框架日志；`--log-level DEBUG` 含完整 prompt |
| iterN_{stage}.make.log | `runs/<时间戳>/iterN_{stage}.make.log` | 各阶段 ORFS make stdout/stderr |
| fastroute_iterN.tcl | `runs/<时间戳>/fastroute_iterN.tcl` | 每轮生成的定制布线层容量脚本 |
| config_snapshot.json | `runs/<时间戳>/config_snapshot.json` | 当次运行的完整配置存档 |

## 日志格式

控制台日志使用紧凑的 `#N [AGENT] ...` 格式（无时间戳），httpx/openai 的 HTTP 请求日志已抑制：

```
========== Iter #1 ==========
#1 [Judge Agent] branch_node = iter0_PL
#1 [Judge Agent] branch_stage = CTS
#1 [Judge Agent] @CTS Agent: 采用较大的聚类规模和直径以减少缓冲器插入……
#1 [Judge Agent] @RT Agent: 保持默认布线参数，减少变量……
#1 [CTS Agent] 按法官提示增大簇规模和直径以减少缓冲器插入、降低功耗……
#1 [CTS Agent] set CTS params...
#1 [CTS Agent] CTS_CLUSTER_SIZE: 100
#1 [CTS Agent] CTS_CLUSTER_DIAMETER: 200
#1 [CTS Agent] SETUP_SLACK_MARGIN: 0.05
#1 [ORFS] make cts...
#1 [ORFS] CTS done!(2.7s)
#1 [ORFS] CTS QoR: 4_1_cts_tns_ps=0.0, 4_1_cts_ws_ps=31.1
……
#1 [ORFS] Iter #1 finish!(37.2s)
#1 [ORFS] Iter #1 final QoR: WNS=35.7ps TNS=0.0ps Area=873.8um2 Power=3.3377mW
#1 [OPTIMIZER] ★ Global best updated to Iter #1: WNS=35.7ps …

=================== Final Results ===================
[OPTIMIZER] #0 WNS=11.4ps TNS=0.0ps Area=714.7um2 Power=2.6323mW
[OPTIMIZER] #1 WNS=35.7ps TNS=0.0ps Area=873.8um2 Power=3.3377mW  *BEST*
[OPTIMIZER] Global best: Iter #1
```

日志文件 `agenticpd.log` 格式与控制台一致（无时间戳）。

## 参数空间

定义于 `config.py::PARAM_SPACE`。换设计/工艺时需重新审视各参数范围。

| 阶段 | 参数 | 类型/范围 | 默认 | 说明 |
|---|---|---|---|---|
| FP  | CORE_UTILIZATION | int 20–50 | 38 | 核心区利用率（%）。越高面积越小但布线越拥挤 |
| FP  | CORE_ASPECT_RATIO | float 0.5–2.0 | 1.0 | 核心区高宽比。1.0 为正方形 |
| PL  | PLACE_DENSITY_LB_ADDON | float 0.0–0.2 | 不设 | 布局密度余量。设置后实际密度 = 下界 + 余量 |
| PL  | CELL_PAD_IN_SITES_GLOBAL_PLACEMENT | int 0–3 | 0 | 全局布局阶段单元 padding（site 数） |
| CTS | CTS_CLUSTER_SIZE | int 10–200 | 不设 | 时钟 sink 聚类最大 sink 数 |
| CTS | CTS_CLUSTER_DIAMETER | int 20–400 | 不设 | 聚类最大直径（μm） |
| CTS | SETUP_SLACK_MARGIN | float 0–0.2 | 0.0 | repair_timing setup 裕量（ns） |
| RT  | FASTROUTE_LAYER_ADJUSTMENT | float 0.1–0.3 | 0.2 | 伪参数：生成 fastroute.tcl 并传 FASTROUTE_TCL |
| RT  | GRT_CONGESTION_ITERATIONS | int 10–50 | 30 | 伪参数：渲染进 GLOBAL_ROUTE_ARGS 的 -congestion_iterations |

两条实现说明（详见 `config.py` 注释）：

1. sky130hd 的 `FASTROUTE_TCL` 会绕过 `ROUTING_LAYER_ADJUSTMENT` 环境变量
   （层容量硬编码 0.2），因此 RT 容量参数采用官方 AutoTuner 同款方案：
   按模板生成自定义 fastroute.tcl。
2. QoR 提取以 `logs/.../6_report.json` 的 JSON metrics 为主（rpt/log 正则兜底）；
   时序值在 JSON 中为 ns，框架统一 ×1000 转 ps。

修改参数空间只需编辑 `config.py` 的 `PARAM_SPACE`，prompt 与校验会自动适配。

---

## 核心数据结构

### 优化树 T — `optimization_tree.py`（论文 §3）

`OptimizationTree` 管理一棵有根树。根节点 `node_id="root"`（stage="root"）代表综合网表。
每次成功迭代在树中挂载一条阶段节点链（FP→PL→CTS→RT），节点 `node_id` 格式为
`"iter<N>_<STAGE>"`（如 `"iter2_PL"`）。

**节点字段**（`OptimNode` dataclass）：

| 字段 | 类型 | 说明 |
|---|---|---|
| node_id | str | `"root"` 或 `"iter<N>_<STAGE>"` |
| iteration | int | 创建该节点的迭代号 |
| stage | str | `"root"` / `"FP"` / `"PL"` / `"CTS"` / `"RT"` |
| variant | str | 该阶段产物的 FLOW_VARIANT（分支复制时使用） |
| params | dict | **仅本阶段**的参数 {name: value} |
| stage_qor | dict\|None | 本阶段执行后的中间 QoR 快照 |
| parent_id | str\|None | 父节点 node_id（root 为 None） |
| children_ids | list[str] | 子节点 ID 列表 |
| branch_count | int | E(n)：该节点被选为分支起点的次数 |

**关键方法**：
- `branchable_nodes(max_branch_count)` — 可分支节点（非 root、非 RT 叶子、E(n) < max）
- `ancestors(node_id)` — root→…→parent 链（不含自身），用于获取 Bef QoR
- `get_params_chain(node_id)` — 路径上各阶段参数汇总 {stage: params}
- `get_path_qor_summary(node_id)` — 路径上各阶段中间 ws
- `add_path(iteration, parent_id, stages_chain)` — 沿父节点挂载新节点链
- `increment_branch_count(node_id)` — E(n) += 1
- `to_dict()` / `from_dict()` — JSON 序列化

### 历史记录 H — `optimizer.py` 维护

与树并行的平面日志 `List[Dict]`，每条记录结构（构造于 `Optimizer._record()`）：

```jsonc
{
  "iteration": 1,
  "status": "ok",                         // "ok" | "failed"
  "variant": "agenticpd_iter1",
  "params": {                             // 完整四阶段参数（Bef 继承 + 下游新生成）
    "FP":  {"CORE_UTILIZATION": 35, "CORE_ASPECT_RATIO": 0.85},
    "PL":  {"PLACE_DENSITY_LB_ADDON": 0.08, "CELL_PAD_IN_SITES_GLOBAL_PLACEMENT": 0},
    "CTS": {"CTS_CLUSTER_SIZE": 86, "CTS_CLUSTER_DIAMETER": 172, "SETUP_SLACK_MARGIN": 0.0},
    "RT":  {"FASTROUTE_LAYER_ADJUSTMENT": 0.18, "GRT_CONGESTION_ITERATIONS": 26}
  },
  "qor": {"wns_ps": -1343.18, "tns_ps": -60770.1, "area_um2": 5053.6, "power_w": 0.00868},
  "stage_qor": {                          // 各阶段执行后的中间时序（ps）
    "FP": {"2_1_floorplan_ws_ps": -1154.1, "2_1_floorplan_tns_ps": -48237.9},
    "PL": {"3_5_place_dp_ws_ps": -1579.2}
  },
  "failed_stage": null,                   // 失败时记录崩溃阶段名
  "error": null,                          // 失败时记录错误摘要
  "elapsed_s": 151.4,
  "branch_node": "iter0_FP",              // 分支起点节点（基线轮为 null）
  "branch_stage": "PL",                   // 重跑起始阶段 b_k（基线轮为 null）
  "judge_decision": {                     // 法官完整决策（基线轮为 null）
    "branch_node": "iter0_FP",
    "branch_stage": "PL",
    "hints": {"FP": "", "PL": "降低 addon", "CTS": "缩小 cluster", "RT": "保持"},
    "reason": "PL 瓶颈分最大"
  },
  "stage_reasons": {                      // 下游各阶段智能体的调参理由
    "PL": "按 hint 降低 addon", "CTS": "…", "RT": "…"
  }
}
```

tree.json 与 history.json 每轮结束后由 `Optimizer._persist()` 同时原子落盘
（先写 `.tmp` 再 `os.replace`，中途崩溃不会损坏旧文件）。
树节点仅在迭代成功时挂载（失败轮产物不完整、不可作为未来分支起点）。

---

## 数据流详解：每轮迭代中各智能体的信息载体

### 0. Observation Tool（论文 §4.2）— `agents.py`

**不调 LLM，纯计算。**

- `compute_exploration_balance(tree, max_branch_count)` → `dict[str, int]`
  遍历 `branchable_nodes()`，返回 {node_id: branch_count}（即 E(n) 表）
- `compute_stage_bottleneck(history, best_qor)` → `dict[str, float]`
  从历史中各阶段 ok 轮提取所有中间 ws 的最佳值，计算 `best_ws - stage_best_ws`
  （正值越大 = 该阶段越是瓶颈）
- `build_observation_summary(tree, history, best_qor, max_branch_count)` → `str`
  组装为 Markdown 表格文本块，作为 Judge user prompt 的"搜索状态概要"段

可验证：`--log-level DEBUG` 后查看 agenticpd.log 中 user prompt 首段。

### 1. Judge 的输入与输出（论文 §4）— `agents.py::JudgeAgent`

**system prompt**（`JudgeAgent.system_prompt()`，每轮相同）：
角色定义 + QoR 优先级/容差 + 四个阶段参数说明表（从 `config.PARAM_SPACE` 渲染）+
分支机制说明（树、E(n)/B(s) 含义、branch_node 与 branch_stage 一致性约束）+ 决策原则。

**user prompt**（`JudgeAgent.build_user_prompt()`，每轮重建）四部分：
1. 观测概要（`build_observation_summary()` 输出）
2. 当前最佳详情（QoR + 完整参数）
3. 近期历史（`format_history()`，15 条，含分支信息如 `#2 [ok, from iter0_FP@PL] …`，
   失败轮带参数不省略，最佳标 `*BEST*`）
4. JSON schema

**输出**（论文 D_k = (n_hat, b_k, {hint_s})）：

```json
{"branch_node": "iter0_PL",
 "branch_stage": "CTS",
 "hints": {"FP": "", "PL": "", "CTS": "缩小 cluster 降低局部 skew", "RT": "保持层容量 0.2"},
 "reason": "CTS 瓶颈分最大（+95ps），iter0_PL 仅被分支 1 次"}
```

- **branch_node**：可分支节点表中的 node_id，或 `"ROOT"`。选哪个节点唯一决定重跑起点
  （一致性修正 `b := next_stage(node.stage)` 在 `Optimizer.run_iteration()` 中执行，
  若 LLM 输出不一致以 node 为准修正，WARNING 日志可见）
- **branch_stage**：重跑起始阶段，受一致性约束
- **hints**：为 {b}∪Aft(b) 各阶段专属提示；Bef 阶段的 hint 被忽略
- **健壮性**：非法 stage/node_id 时重问一次；LLM 彻底失败退化为
  "ROOT + 轮询选阶段 + 兜底 hints"（ERROR 日志可见）

**可验证代码**：
- 观测概要 `agents.py::build_observation_summary()` / `compute_stage_bottleneck()` / `compute_exploration_balance()`
- prompt `agents.py::JudgeAgent.system_prompt()` / `build_user_prompt()`
- 输出校验与兜底 `agents.py::JudgeAgent.validate()` / `act()`
- 一致性修正 / ROOT 回退 `optimizer.py::Optimizer.run_iteration()` 步骤 5-6

### 2. StageAgent 的输入与输出（论文 §5）— `agents.py::StageAgent`

**只有 s ∈ {b} ∪ Aft(b) 的智能体被调用**——Bef 阶段参数从树祖先继承，对应论文 §3.2
的"零成本复用"。

**context 字段**（`Optimizer.run_iteration()` 步骤 7 组装，
对应 ctx_s = ({Q_k(i)}_{i∈Bef(s)}, e_s, hint_s)）：

| 字段 | 论文符号 | 来源 |
|---|---|---|
| upstream_qor | {Q_k(i)}_{i∈Bef(s)} | 树祖先 stage_qor + 分支节点自身 stage_qor |
| cross_iteration_exp | e_s | `Optimizer._cross_exp(s)`：本阶段作为 branch_stage 的最近 5 条 |
| hint | hint_s | Judge 输出 `hints[stage]` |
| global_best | （基线参考） | `Optimizer.best_entry` |

**user prompt**（`StageAgent.build_user_prompt()`）四部分：
1. 本分支 Bef 阶段已完成的 QoR（ws/tns per stage），明确告知"这些阶段不会重跑"
2. 跨迭代经验 e_s（params + 该阶段中间 ws）
3. 全局最佳（QoR + 本阶段在最佳轮的参数，作为保守参考基线）
4. 法官专属 hint

**输出**：

```json
{"params": {"CTS_CLUSTER_SIZE": 60, "CTS_CLUSTER_DIAMETER": 120, "SETUP_SLACK_MARGIN": 0.05},
 "reason": "按 hint 缩小 cluster 降 skew"}
```

三重清洗（`validate()`）：丢弃未知键 → `ParamSpec.cast()` 类型强转 + clamp →
缺键用全局最佳→默认值兜底补齐。LLM 彻底失败整组走兜底，主循环不中断。

**可验证代码**：
- 下游阶段筛选 `optimizer.py::_downstream_stages()`
- Bef 参数/QoR 继承 `optimization_tree.py::get_params_chain()` / `ancestors()`
- 跨迭代经验 `optimizer.py::Optimizer._cross_exp()`
- prompt 渲染 `agents.py::StageAgent.build_user_prompt()`
- 输出清洗 `agents.py::StageAgent.validate()` / `_fallback_params()`

### 3. 信息流总览（一轮迭代，与论文 §6 伪代码逐行对照）

```
tree.json + history.json（Optimizer 维护，每轮结束原子落盘）
   │
   ├─→ ObservationTool（纯计算）：
   │     E(n) ← tree.branchable_nodes()     B(s) ← compute_stage_bottleneck()
   │     ──→ build_observation_summary() → 观测概要（Markdown 表格）
   │
   ├─→ Judge：
   │     system(角色+参数表+分支规则) + user(观测概要+最佳详情+历史15条)
   │     ──→ {branch_node: n_hat, branch_stage: b, hints: {s: text}}
   │           │（Optimizer 做一致性修正：b := next_stage(n_hat.stage)）
   │           │（n_hat 不在树中 → 回退 ROOT；n_hat 是叶子 → 回退 ROOT+FP）
   │           ▼
   ├─→ Bef 阶段（s 在 b 之前）：
   │     参数 ← tree.get_params_chain(n_hat)     QoR ← ancestors(n_hat) + [n_hat]
   │     <不调 LLM，不重跑 EDA>
   │           │
   ├─→ 下游阶段 s ∈ {b} ∪ Aft(b)，逐阶段流水线：
   │     ctx = upstream_qor(继承) + cross_iteration_exp(s) + hints[s] + global_best
   │     ──→ StageAgent → {params, reason} → ORFSRunner.run_stage(s, …) → 真实阶段 QoR
   │     （下一个 StageAgent 通过 live_upstream_qor 看到上一阶段的真实 QoR）
   │           │
   ├─→ 所有下游阶段完成后：
   │     ORFSRunner.run_finish() → 最终 QoR（WNS/TNS/Area/Power）
   │           │
   └─→ Optimizer 登记：
        history 追加条目 + tree.add_path(branch_node → 下游新节点)
        + tree.increment_branch_count(branch_node)
        + qor_is_better 判断 → 更新 best_idx → _persist() 原子落盘
```

## 分支执行实现细节 — `orfs/`

逐阶段流水线的执行方式：

1. **建立基线**：从 ROOT 分支则清空 variant；否则 `copy_parent_results()` 复制父
   variant 的 results/objects/logs/reports 四目录到新 variant。
2. **逐阶段循环**：对每个 s ∈ {b} ∪ Aft(b)：
   - StageAgent 生成参数（已看到流水线中先前阶段的实际 QoR）
   - `run_stage(s, …)`：`make clean_<s>` → `make <s_target>` → 解析阶段 QoR
   - 阶段 QoR 追加到 live_upstream_qor 供下一个 StageAgent 使用
3. **收尾**：`run_finish()` 执行 `make finish` 并解析最终四指标 QoR

旧的 `branch_from()` 接口（一次 `make all` 加定点 clean）保留作为备用，但逐阶段
流水线是 `Optimizer.run_iteration()` 的默认路径。

**已知约束**：`SETUP_SLACK_MARGIN` 同时影响 FP/CTS/GRT 的 repair_timing。
分支重跑下游时以本轮新值生效——上游固化产物中仍是分支前的旧值，这是阶段划分近似
的固有误差（论文同样存在）。

---

## 配置说明 — `config.py`

`FrameworkConfig`（dataclass）是框架唯一配置入口，关键字段：

| 字段 | 默认值 | 说明 |
|---|---|---|
| platform / design | sky130hd / ibex | 目标设计；CLI `--platform` `--design` 可覆盖 |
| flow_dir | 由 `__file__` 推导 | ORFS 根目录 |
| run_dir | 每次运行自动创建 | `agenticpd/runs/<时间戳>/` |
| make_target | all | synth→floorplan→place→cts→route→finish |
| timeout_s | 3600 | 单轮超时（秒） |
| iterations | 10 | 优化迭代次数（不含基线） |
| history_window | 15 | 喂给 Judge 的历史窗口大小 |
| max_branch_count | 3 | 单节点最大分支次数，超限从 branchable_nodes 排除 |
| variant_prefix | agenticpd_iter | 每轮 FLOW_VARIANT 前缀 |
| best_variant_name | agenticpd_best | 最佳导出目录名 |
| wns_tol_ps / tns_tol_ps | 10 / 50 | QoR 比较容差（ps） |
| llm_model | deepseek-v4-pro | LLM 模型 ID |
| llm_temperature | 0.6 | LLM 温度参数 |

全局常量（由 `__file__` 推导，各模块统一引用）：
- `FLOW_DIR` — ORFS 根目录
- `AGENTICPD_DIR` — `flow/agenticpd/`
- `RUNS_DIR` — `flow/agenticpd/runs/`（目录名可通过 `RUNS_DIR_NAME` 配置）
- `ENV_FILENAME` — `.env`
- `ORFS_CATEGORIES` — `["results", "logs", "reports", "objects"]`

CLI 参数（`--iterations`、`--design`、`--platform`、`--timeout`、`--wns-tol`、
`--tns-tol`）默认值均为 `None`（argparse），只有用户显式传参时才覆盖 config.py。

---

## QoR 比较器 — `utils.py::qor_is_better()`

论文 §6 第 13 行的"候选优于历史最优"判断。双方均需完整 QoR（失败/不完整恒输）：

1. 双方 WNS >= 0（均收敛）→ 多余正裕量无价值，跳步骤 3
2. |ΔWNS| > wns_tol_ps → WNS 大者胜；否则 |ΔTNS| > tns_tol_ps → TNS 大者胜
3. 功耗小者胜；仍平则面积小者胜；完全打平判不优（保守保留旧最佳）

---

## 树可视化 — `visualize_tree.py`

`main.py` 运行结束后自动调用，在 run_dir 下生成 `optimization_tree.png`。

效果：
- 5 层布局（root → FP → PL → CTS → RT），层内按迭代号左小右大排列
- 圆圈节点，标注 `迭代号_阶段`（如 `0_FP`、`2_CTS`）、root 标注 `Root`
- **绿色粗箭头**：基线路径（root→iter0_FP→iter0_PL→iter0_CTS→iter0_RT）
- **红色粗箭头**：最佳路径（从 history.json QoR 自动找出最优迭代，回溯整条路径）
- 灰色细箭头：其余普通边
- 最佳与基线重合时自动跳过绿色避免重叠，图例标注 `= baseline`
- 图片尺寸随节点数动态调整，150 DPI

```bash
# 也可独立运行
python3 agenticpd/tools/visualize.py runs/20260718_210019
```

## 产物清理 — `clean.py`

删除指定 platform/design 的所有 AgenticPD 产物。`base` 基线**严格保护，永不删除**。

### 基本用法

```bash
# ---- 预览（不删文件，先看清理范围）----
python3 agenticpd/tools/clean.py sky130hd gcd --dry-run

# ---- 交互确认后删除 ----
python3 agenticpd/tools/clean.py sky130hd gcd

# ---- 跳过确认直接删除 ----
python3 agenticpd/tools/clean.py sky130hd gcd --yes

# 两种写法等效：
python3 agenticpd/tools/clean.py sky130hd gcd
python3 agenticpd/tools/clean.py --target sky130hd gcd
```

### 清理范围

删除两类目录：

| 清理对象 | 路径 | 说明 |
|---------|------|------|
| ORFS 产物 variant | `results/` `logs/` `reports/` `objects/{platform}/{design}/` | 该设计下除 `base` 以外的所有 variant（如 `agenticpd_iter*`） |
| AgenticPD runs | `agenticpd/runs/` | 匹配该 platform + design 的运行目录（通过 `config_snapshot.json` 识别） |

**不受影响的内容**：
- `base` 基线产物（ORFS 默认运行结果），永远不会被删除
- 其他 platform/design 的产物（需要单独指定）
- `agenticpd/runs/` 中不匹配该设计的目录

### 常见场景

```bash
# 迭代实验后，清理所有 variant 只保留 base 基线
python3 agenticpd/tools/clean.py sky130hd gcd --yes

# 换设计前，清理旧设计的全部痕迹
python3 agenticpd/tools/clean.py sky130hd ibex --yes

# 批量清理所有开发设计（手动逐个执行，不支持通配符）
python3 agenticpd/tools/clean.py sky130hd gcd --yes
python3 agenticpd/tools/clean.py sky130hd aes --yes
python3 agenticpd/tools/clean.py sky130hd ibex --yes
```

> **注意**：清理操作通过 `shutil.rmtree` 直接删除，不可恢复。建议先用 `--dry-run` 预览，再决定是否执行。失败 trial 的 artifact 也会一并清除——如果某次实验的结果需要保留，先备份再清理。

---

## 故障兜底矩阵

| 故障 | 行为 |
|---|---|
| ORFS make 非零退出 / 超时 | `detect_failed_stage` 定位；history 记 FAILED 条目（参数保留）；继续下轮 |
| 6_report.json 缺失但退出 0 | rpt 正则兜底；仍缺按 failed 处理 |
| LLM API 错误（429/5xx/超时） | 指数退避重试 ×3 → Judge 退化为 ROOT+轮询、Stage 复用最优参数 |
| LLM 输出非 JSON / 字段非法 | 回喂错误重问 ×3 → 同上兜底 |
| Judge branch_node 不在树中 | Optimizer 回退 ROOT（WARNING） |
| Judge branch_stage 与 node 不一致 | Optimizer 以 node 为准强制修正（WARNING） |
| Ctrl-C / 崩溃 | history+tree 每轮已原子落盘；finally 中导出当前最佳 |
| history/tree JSON 损坏 (--resume) | 改名 .corrupt，警告后重建 |

---

## 已知问题

### 1. WSL pyc 缓存同步延迟

Windows 通过 `\\wsl.localhost` UNC 路径 Edit `.py` 文件后，WSL 侧已有 `.pyc`
mtime 可能比新 `.py` 更新（9p 同步延迟），Python 会加载旧 bytecode。
**每次 Edit 后必须先清 pycache**：
```bash
rm -rf flow/agenticpd/__pycache__/
```

### 2. finish__design__instance__area 重复键

`logs/.../6_report.json` 中 `finish__design__instance__area` 出现两次，
后者是 stdcell-only 面积（正确值）。依赖 CPython `json.load` 后键覆盖取后者。
见 `utils.py::QoR.from_report_json()`。

### 3. 逐阶段执行与批量生成的近似

论文 §6 第 10 行在每个下游阶段执行后才获得该阶段 QoR 并传给下一个阶段。
当前实现通过逐阶段流水线（`run_stage()` 后立即将真实 QoR 追加到
live_upstream_qor）部分缓解了此问题，但当前阶段 StageAgent 的 prompt 中
仍然不包含同级下游阶段（尚未执行）的 QoR，对同级串行依赖的建模是近似。
