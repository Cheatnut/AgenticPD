# AgenticPD — LLM 多智能体驱动的物理设计 QoR 优化框架

复现论文 *"AgenticPD: Stage-Aware Agentic Framework for Physical Design QoR
Optimization"* 的原型实现：1 个法官智能体（JudgeAgent）+ 4 个阶段智能体
（FP/PL/CTS/RT），迭代调整 OpenROAD Flow Scripts（ORFS）的流程参数，
优化 WNS / TNS / Area / Power 四项 QoR 指标。支持 nangate45、sky130hd 等
ORFS 兼容的任意工艺/设计组合。

核心机制：**优化树 + 分支复用**（Bef 阶段零成本继承）→ **观测工具**（探索平衡度
E(n) + 阶段瓶颈 B(s)）→ **逐阶段流水线**（StageAgent 调 LLM 生成参数 → make 单阶段
→ 获取真实中间 QoR → 传给下一个 StageAgent）→ **自动树可视化** → **一键清理**。

## 1. 目录结构

```
AgenticPD/
├── main.py                   # CLI 入口
├── config.py                 # 全局配置（参数空间 + 路径 + 超参）
├── optimizer.py              # 优化主循环
├── agents.py                 # Agent 层（Judge + 4×StageAgent + ObservationTool）
├── optimization_tree.py      # 优化树 T 数据结构
├── utils.py                  # QoR 解析 / 比较器 / 原子写入
├── llm_interface.py          # LLM 客户端（含 MockLLMClient）
├── orfs_interface.py         # ORFS 适配层重导出
├── managers/                 # 管理层：TrialManager + CheckpointManager
├── orfs/                     # ORFS 适配层（命令构建/解析/执行/后端）
├── schemas/                  # 数据模型（TrialRecord / StageResult / CheckpointRef）
├── tools/                    # CLI 工具（trial_inspect / visualize / clean）
├── configs/experiments/      # 实验配置（smoke.yaml）
├── docs/                     # 设计文档
├── tests/                    # 56 个纯 Python 测试
├── scripts/                  # 辅助脚本
└── runs/                     # 运行产物（不进 git）
    └── <platform>_<design>/  # 按平台+设计分组
        └── <YYYYMMDD_HHMMSS>/# 会话目录
```

> 各文件详细职责与行数见 [docs/directory-guide.md](docs/directory-guide.md)。
## 2. 环境准备

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

## 3. 运行

所有命令在 `flow/` 目录下执行：

```bash
cd flow

# ---- 真实运行（需要 API key）----

# Smoke test：只跑一次基线 ORFS，不调 LLM
python3 agenticpd/main.py --baseline-only --design gcd

# 完整优化：基线 + N 次迭代
python3 agenticpd/main.py --design gcd --iterations 3

# 断点续跑（自动取 runs/<platform>_<design>/ 下最新一次会话目录）
python3 agenticpd/main.py --resume latest

# ---- 调试模式（零 token / 零 EDA）----

# 全 mock：MockLLM + MockORFS，秒级跑完，验证控制逻辑
python3 agenticpd/main.py --mock-llm --mock-orfs --iterations 5

# MockLLM + 真实 ORFS：LLM 不花钱，但会真实跑 EDA 流程
python3 agenticpd/main.py --mock-llm --iterations 2

# ---- 工具 ----

# 清理指定设计的所有产物（base 基线不受影响）
python3 agenticpd/tools/clean.py sky130hd gcd --dry-run   # 预览
python3 agenticpd/tools/clean.py sky130hd gcd --yes       # 确认并删除

# 查看 trial 记录
python3 agenticpd/tools/trial_inspect.py --list --runs-dir agenticpd/runs/<session>
python3 agenticpd/tools/trial_inspect.py <trial_id> --stages --runs-dir agenticpd/runs/<session>

# 从已有运行生成优化树可视化
python3 agenticpd/tools/visualize.py agenticpd/runs/<session>
```

常用选项：`--design`、`--platform`、`--timeout`（秒）、`--wns-tol`/`--tns-tol`（ps）、
`--log-level DEBUG`（完整 prompt 输出到 agenticpd.log）。

## 4. 输出位置

### 5.1 ORFS 产物（`flow/` 下）

| 内容 | 路径 | 说明 |
|---|---|---|
| 最佳产物 | `results/<plat>/<design>/agenticpd_best/` | 最终 GDS/DEF/网表 + 报告 + `agenticpd_summary.json` |
| 每轮迭代产物 | `{results,logs,reports,objects}/<plat>/<design>/agenticpd_iter<N>/` | FLOW_VARIANT 隔离，`base` 永不触碰 |

### 5.2 会话目录（`agenticpd/runs/<platform>_<design>/<YYYYMMDD_HHMMSS>/`）

每次 `main.py` 调用在对应设计子目录下创建独立会话：

| 内容 | 路径 | 说明 |
|---|---|---|
| **trial 记录** | `<trial_id>/trial.json` | **阶段 B/C 新增**。每个 trial 的完整 `TrialRecord`（lineage/params/stage_results/final_qor/checkpoint），含 per-stage `elapsed_s` 和 `param_diff` |
| **trial 索引** | `trials.jsonl` | **阶段 B 新增**。本会话所有 trial 的全局索引（append-only，reader 去重） |
| 优化树 PNG | `optimization_tree.png` | 每次运行结束后自动生成 |
| history.json | `history.json` | 旧格式平面优化日志（与 `trials.jsonl` 共存过渡） |
| tree.json | `tree.json` | 优化树 T：节点 + 父子关系 + E(n) |
| agenticpd.log | `agenticpd.log` | 框架日志；`--log-level DEBUG` 含完整 prompt |
| iterN_{stage}.make.log | `iterN_{stage}.make.log` | 各阶段 ORFS make stdout/stderr |
| fastroute_iterN.tcl | `fastroute_iterN.tcl` | 每轮生成的定制布线层容量脚本 |
| config_snapshot.json | `config_snapshot.json` | 当次运行的完整配置存档 |

> **查看 trial**：`python3 tools/trial_inspect.py --list --runs-dir runs/<session>`

## 5. 日志格式

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

## 6. 参数空间

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

## 7. 配置说明 — `config.py`

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

## 8. QoR 比较器 — `utils.py::qor_is_better()`

论文 §6 第 13 行的"候选优于历史最优"判断。双方均需完整 QoR（失败/不完整恒输）：

1. 双方 WNS >= 0（均收敛）→ 多余正裕量无价值，跳步骤 3
2. |ΔWNS| > wns_tol_ps → WNS 大者胜；否则 |ΔTNS| > tns_tol_ps → TNS 大者胜
3. 功耗小者胜；仍平则面积小者胜；完全打平判不优（保守保留旧最佳）

---

## 9. 树可视化 — `tools/visualize.py`

`main.py` 运行结束后自动调用，在会话目录下生成 `optimization_tree.png`。

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
python3 agenticpd/tools/visualize.py <runs_session_dir>
```

## 10. 产物清理 — `clean.py`

删除指定 platform/design 的所有 AgenticPD 产物。`base` 基线**严格保护，永不删除**。

### 10.1 基本用法

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

### 10.2 清理范围

删除两类目录：

| 清理对象 | 路径 | 说明 |
|---------|------|------|
| ORFS 产物 variant | `results/` `logs/` `reports/` `objects/{platform}/{design}/` | 该设计下除 `base` 以外的所有 variant（如 `agenticpd_iter*`） |
| AgenticPD runs | `agenticpd/runs/<platform>_<design>/` | 该平台+设计的整个会话子目录 |

**不受影响的内容**：
- `base` 基线产物（ORFS 默认运行结果），永远不会被删除
- 其他 platform/design 的产物（需要单独指定）
- `agenticpd/runs/` 中不匹配该设计的目录

### 10.3 常见场景

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

## 11. 故障兜底矩阵

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

## 12. 已知问题

### 12.1 WSL pyc 缓存同步延迟

Windows 通过 `\\wsl.localhost` UNC 路径 Edit `.py` 文件后，WSL 侧已有 `.pyc`
mtime 可能比新 `.py` 更新（9p 同步延迟），Python 会加载旧 bytecode。
**每次 Edit 后必须先清 pycache**：
```bash
cd ~/OpenROAD-flow-scripts/flow/agenticpd
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
```

### 12.2 finish__design__instance__area 重复键

`logs/.../6_report.json` 中 `finish__design__instance__area` 出现两次，
后者是 stdcell-only 面积（正确值）。依赖 CPython `json.load` 后键覆盖取后者。
见 `utils.py::QoR.from_report_json()`。

### 12.3 已解决：history.json 与 trial.json 双写

阶段 C 已消除双写：`optimizer.py._persist()` 不再写入 `history.json`。
可视化从 `trials.jsonl` 读取（自动检测格式，兼容旧 `history.json`）。
`--resume` 从 `trials.jsonl` 重建内存历史。

### 12.4 per-stage elapsed_s 仅迭代模式有效

`--baseline-only` 使用 `make all` 全流程运行，不拆分单阶段，因此 baseline
trial 的 `stage_results[*].elapsed_s` 为 0。只有 `--iterations N` 的逐阶段
流水线路径会填充真实 per-stage 耗时。

---

## 延伸阅读

| 文档 | 内容 |
|------|------|
| [docs/formal-framework.md](docs/formal-framework.md) | 核心机制形式化整理：优化树、分支、Judge/StageAgent 数学定义 |
| [docs/directory-guide.md](docs/directory-guide.md) | 每个文件/目录的详细职责、行数、依赖关系 |
| [docs/data-flow.md](docs/data-flow.md) | 核心数据结构（树/历史）、每轮迭代信息流、分支执行细节 |
| [docs/experiment-contract.md](docs/experiment-contract.md) | 实验契约：QoR 来源、评价函数、公平性约束 |

