# AgenticPD 核心数据结构与数据流

> 从 README.md 拆分，原文在 8 ~ 10 节。

## 1. 核心数据结构

### 1.1 优化树 T — `optimization_tree.py`（论文 §3）

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

### 1.2 历史记录 H — `optimizer.py` 维护

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

## 2. 数据流详解：每轮迭代中各智能体的信息载体

### 2.1 Observation Tool（论文 §4.2）— `agents.py`

**不调 LLM，纯计算。**

- `compute_exploration_balance(tree, max_branch_count)` → `dict[str, int]`
  遍历 `branchable_nodes()`，返回 {node_id: branch_count}（即 E(n) 表）
- `compute_stage_bottleneck(history, best_qor)` → `dict[str, float]`
  从历史中各阶段 ok 轮提取所有中间 ws 的最佳值，计算 `best_ws - stage_best_ws`
  （正值越大 = 该阶段越是瓶颈）
- `build_observation_summary(tree, history, best_qor, max_branch_count)` → `str`
  组装为 Markdown 表格文本块，作为 Judge user prompt 的"搜索状态概要"段

可验证：`--log-level DEBUG` 后查看 agenticpd.log 中 user prompt 首段。

### 2.2 Judge 的输入与输出（论文 §4）— `agents.py::JudgeAgent`

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

### 2.3 StageAgent 的输入与输出（论文 §5）— `agents.py::StageAgent`

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

### 2.4 信息流总览（一轮迭代，与论文 §6 伪代码逐行对照）

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

## 3. 分支执行实现细节 — `orfs/`

逐阶段流水线的执行方式：

1. **建立基线**：从 ROOT 分支则清空 variant；否则 `copy_parent_results()` 复制父
   variant 的 results/objects/logs/reports 四目录到新 variant。
2. **逐阶段循环**：对每个 s ∈ {b} ∪ Aft(b)：
   - StageAgent 生成参数（已看到流水线中先前阶段的实际 QoR）
   - `run_stage(s, …)`：`make clean_<s>` → `make <s_target>` → 解析阶段 QoR
   - 阶段 QoR 追加到 live_upstream_qor 供下一个 StageAgent 使用
3. **收尾**：`run_finish()` 执行 `make finish` 并解析最终四指标 QoR

**已知约束**：`SETUP_SLACK_MARGIN` 同时影响 FP/CTS/GRT 的 repair_timing。
分支重跑下游时以本轮新值生效——上游固化产物中仍是分支前的旧值，这是阶段划分近似
的固有误差（论文同样存在）。

---
