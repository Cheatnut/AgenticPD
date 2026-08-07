# AgenticPD 项目扫描报告（重构后基线）

**报告日期**：2026-08-07
**评估范围**：`flow/agenticpd` 全仓库（重构后的代码、docs、本地 tests、runs 证据）
**评估方式**：只读扫描 + 纯 Python 验证；未运行任何真实 ORFS、LLM 或网络。

## 1. 结论摘要

1. 本轮完成「代码消肿 + 分层重组」：源码由约 19.4k 行降至约 14.3k 行，所有单文件不超过约 600 行；重复的编排骨架、legacy 兼容层与历史 stage 入口已删除。
2. 两条运行路径保持可用：`main.py`（原始多 Agent 优化）与 `multi_agent_gwtw.py`（Doomed/GWTW Demo），数据层、Agent 层、ORFS 边界与工具链按低耦合高内聚原则重排为 `core/`、`storage/`、`agents/`、`search/`、`gwtw/`、`orfs/`、`tools/`。
3. 验证全部通过：`make check` 通过（语法编译 + 全部 CLI `--help`）；`make test` 66/66 通过；双入口 mock smoke 正常（GWTW demo 完成 8 trial、4 finish、`errors=[]`）。
4. 质量分级：P0 = 0；P1 = 1（本地测试套件与仓库可复现验证的口径差异，见 5.2）；P2 ≈ 8。代码适合作为下一轮开发的基线。

## 2. 新代码结构

```text
agenticpd/
├── main.py                   # 原始多 Agent 优化 CLI（--iterations/--baseline-only/--parse-only/--resume 等）
├── multi_agent_gwtw.py       # Doomed/GWTW Demo CLI（--config/--mock-llm/--mock-orfs）
├── config.py                 # 参数空间 PARAM_SPACE、路径推导、FrameworkConfig 唯一真相源
├── core/                     # 数据模型与通用工具
│   ├── models.py             # TrialRecord/StageResult/CheckpointRef/ExecutionResolution/FailureClass 等
│   ├── decisions.py          # MinimalObservation/DoomedDecision/GWTWDecision/DecisionTraceRef
│   ├── qor.py                # QoR 数据类与时序优先比较器
│   └── utils.py              # 日志、.env 加载、JSON 提取
├── storage/                  # 持久化层
│   ├── trial_manager.py      # Trial 生命周期 + trials.jsonl 索引
│   ├── checkpoint_manager.py # checkpoint manifest + SHA-256 + 兼容性
│   ├── decision_trace.py     # append-only 决策痕迹 writer/reader
│   └── trace_io.py           # cohort 级 trace 读写 helper
├── agents/                   # Agent 层
│   ├── llm.py                # LLMClient/MockLLMClient/LLMError
│   ├── base.py               # BaseAgent/AgentOutputError
│   ├── judge.py              # JudgeAgent
│   ├── stage.py              # FP/PL/CTS/RT StageAgent + build_stage_agents
│   └── observation.py        # 观测摘要（E(n) 探索平衡、B(s) 瓶颈）
├── search/                   # 原始搜索路径
│   ├── optimizer.py          # Optimizer 主循环/预算/历史（run_iteration 委托给 stage_pipeline）
│   ├── stage_pipeline.py     # 单次迭代：观测→Judge→参数→checkpoint 裁决→执行→QoR
│   └── tree.py               # OptimizationTree
├── gwtw/                     # Doomed/GWTW 引擎（统一编排）
│   ├── orchestrator.py       # 编排入口（薄类 + 委托）
│   ├── config.py             # 实验 YAML 配置
│   ├── proposals.py          # Agent proposal / parent selection 记录
│   ├── fake_runner.py        # 确定性 fake ORFS runner（纯 Python 测试）
│   ├── population.py         # bootstrap / 白名单 / parent 选择
│   ├── cohort_run.py         # cohort 执行编排
│   ├── cohort_common.py      # cohort 决策重建
│   ├── cohort_resume.py      # 幂等恢复 / 磁盘重建
│   ├── execution.py          # 阶段执行 / tree / 预算 helper
│   ├── doom.py               # 规则型 Doomed 分类
│   ├── scheduler.py          # GWTW 配额调度
│   ├── mutation.py           # checkpoint 下游合法参数变异
│   ├── observation.py        # 最小观测构造
│   └── resolver.py           # checkpoint 兼容性唯一裁决
├── orfs/                     # ORFS 适配层（command/backend/runner/parser/interface）
├── tools/                    # trial_inspect/trial_reproduce/clean/visualize/checkpoint_fork_*
│   └── session_visualize/    # 拆包：cli.py + data.py + render.py + template.html
├── tests/                    # 本地回归（不进 Git），按领域重组
├── configs/experiments/      # multi-agent-gwtw-demo.yml / smoke.yaml / checkpoint-fork.yaml
└── docs/                     # HANDOVER/WORKFLOW/Note/两份算法研读/usage/扫描报告
```

## 3. 已具备功能

- **两条运行路径**：legacy 多 Agent 优化（Judge + 四 Stage Agent、优化树、checkpoint fork、resume、mock 模式）与 Doomed/GWTW 多候选闭环（population、PL/CTS 两级决策、pause/audit/fork、decision trace、离线 HTML 可视化）。
- **数据与证据层**：Trial/StageResult/CheckpointRef/ExecutionResolution/DoomedDecision/GWTWDecision/DecisionTraceRef；`trials.jsonl` 追加索引 + `trial.json` 原子写；checkpoint manifest 与 `affects` 兼容性裁决；append-only 决策痕迹。
- **ORFS 边界**：make 命令构造（含 `fastroute.tcl`、`GLOBAL_ROUTE_ARGS`）、LocalBackend 进程组超时、copy→clean→stage→finish、`6_report.json` 优先解析 + rpt/log 兜底、日志脱敏。
- **Doomed/GWTW**：规则型分类（hard_dead/soft_bad/survivor）、配额调度（continue/pause/audit_continue/fork）、survivor 白名单 parent 选择、合法下游变异、checkpoint 裁决、幂等恢复。
- **工具链**：trial 查看/复现、限定范围清理、优化树图、自包含 HTML 可视化、checkpoint fork 对照验证。
- **实验声明**：YAML 驱动的实验配置（`multi-agent-gwtw-demo.yml`、`smoke.yaml`、`checkpoint-fork.yaml`）。

## 4. 验证结果（2026-08-07 实测）

| 验证项 | 结果 |
|---|---|
| `make check` | 通过（compileall + 7 个 CLI `--help` 退出码 0） |
| `make test` | 66/66 通过 |
| `main.py --mock-llm --mock-orfs --iterations 1` | 退出码 0，正常结束 |
| `multi_agent_gwtw.py --mock-llm --mock-orfs` | `total_trials=8 budget_remaining=12 errors=[] finish_trials=4` |
| 真实 Demo 证据（重构前运行） | `runs/sky130hd_gcd/multi-agent-gwtw-demo_20260731_061927/`：8 trial（4 ok 带完整 QoR、4 paused）、6 个逻辑 checkpoint、trace 与可视化完整 |

## 5. 质量分级问题

按五个维度扫描（路径与参数化、None 与数据模型安全、语言规则、过时与冗余代码、文档与实现一致性）：

- **P0 = 0**。
- **P1-1**：本地测试套件与仓库可复现验证口径分离——`tests/` 不进 Git（66 项回归仅本地可用），仓库内的 `make check` 只验证语法与 CLI。历史验收记录中的测试数量（489/437）与当前实测（66）不一致，且旧数字无法从仓库复现。建议在新路线确定后把测试纳入版本管理或建立 CI 证据。
- **P2**（不阻塞，建议后置）：
  1. `gwtw/orchestrator.py` 中 `FrameworkConfig`、`Dict/List` 等少量导入仍可能冗余（不影响功能，后续用 pyflakes/mypy 收紧）；
  2. `tools/visualize.py` 仍是 legacy 树图工具，功能与 `session_visualize` 部分重叠，可评估合并；
  3. `configs/experiments/smoke.yaml` 与 `checkpoint-fork.yaml` 的字段未与 `gwtw/config.py` 的 `MultiAgentGWTWConfig` 完全统一（两条路径配置入口仍不同）；
  4. `environment_manifest.json` 中 ORFS commit 与 PDK revision 未解析，跨环境比较不可信；
  5. 模块内仍散落少量历史注释（"Stage C/D" 等），不影响行为；
  6. `trial.schema.json` 未随 `core/models.py` 拆分同步校验；
  7. wall-clock 预算不能主动中断正在执行的 ORFS stage；
  8. 无类型检查（mypy）与 CI 配置。

## 6. 风险与边界

- **测试资产漂移**：`tests/` 不进 Git，是当前最大的回归保护缺口（见 P1-1）。
- **实验规模**：仅 `sky130hd/gcd` 单设计 8 trial，统计结论不成立；扩容实验必须先固定 seed 集合、设计集合、预算与参数空间。
- **并发与中断**：`LocalBackend` 单机同步；`SlurmBackend` 为接口 stub；并行化需要独立方案。
- **算法结论**：Doomed 仍是规则分类器，GWTW 是配额调度近似；`risk_score` 不是校准概率，demo 不声称 QoR 提升。
- **环境溯源**：跨机器比较前需刷新 `environment_manifest.json` 并冻结 ORFS/OpenROAD/PDK 版本。

## 7. 扩展建议

1. **短期**：把 `tests/` 纳入版本管理（或生成测试清单快照），补 core/storage/agents/orfs 各层回归；扩大真实实验（multi-seed、多设计），刷新环境清单。
2. **中期**：从后置清单挑一项做闭环——推荐「离线 replay」（用 decision trace + trials.jsonl 回放决策，零 EDA）或「跨 session resume」。
3. **基础设施**：引入 mypy/pyflakes、把 `make check` 接入 CI、统一两条路径的实验配置入口。
4. **研究型大项（暂缓）**：学习型 Doomed 模型、并行/Slurm 调度、统计显著性对照，等数据积累后另立方案。

## 8. 附录：本次重构改动摘要

- 新增包：`core/`、`storage/`、`agents/`、`search/`、`gwtw/`；`tools/session_visualize/` 拆包。
- 删除：`schemas/`、`managers/`、`orfs_interface.py`、`scripts/build_fixtures.py`、原 `orchestrator.py`（组件编排，合并进统一编排）、`main.py --stage-d`、`configs/experiments/stage-d-smoke.yml`。
- 改名：`configs/experiments/stage-c-checkpoint-fork.yaml` → `checkpoint-fork.yaml`。
- 测试：模块自检迁入 `tests/`（`test_core_models`、`test_qor`、`test_storage_trace`、`test_gwtw_*` 等），现有测试同步 import 与 patch 路径。
- 文档：删除全部历史 stage 文档与旧报告；重写 HANDOVER/WORKFLOW/usage；新增本扫描报告。
