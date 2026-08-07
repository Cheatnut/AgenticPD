# AgenticPD 交接记录

## 当前状态

- 日期：2026-08-07
- 当前分支：`main`
- 里程碑：完成「代码消肿 + 分层重组」重构，仓库以新结构为基线，准备下一轮功能开发。

## 最近完成（2026-08-07 重构）

- 包结构重组：`core/`（数据模型与通用工具）、`storage/`（Trial/Checkpoint/决策痕迹持久化）、`agents/`（Judge/Stage Agent 与 LLM 客户端）、`search/`（legacy 优化循环与优化树）、`gwtw/`（Doomed/GWTW 编排与调度）、`tools/`（CLI 与可视化）。
- 超大文件拆分：所有源码单文件不超过约 600 行；原 `multi_agent_gwtw_orchestrator.py`（1926 行）拆为 `gwtw/orchestrator.py` + `config/proposals/fake_runner/population/cohort_run/cohort_common/cohort_resume/execution`；原 `schemas/trial.py`（1042 行）拆为 `core/models.py` + `core/decisions.py`。
- 消除重复：删除原 Stage D 组件编排器与 `main.py --stage-d` 入口，Doomed/GWTW 只保留统一编排路径；删除 `orfs_interface.py` 兼容层、`scripts/build_fixtures.py` legacy 脚本。
- 模块自检迁入 `tests/`，测试按领域重组；`make check` 改为纯语法 + CLI 契约检查，`make test` 为完整回归套件。
- 历史 stage 文档、旧项目报告与阶段计划已删除；`docs/` 只保留 HANDOVER、WORKFLOW、Note、两份算法研读、usage/ 与新扫描报告。

## 最近验证（全部实测通过）

- `make check`：通过（compileall + 全部 CLI `--help` 退出码 0）。
- `make test`：66/66 通过。
- 双入口 mock smoke（零 LLM、零 EDA）：`main.py --mock-llm --mock-orfs --iterations 1` 正常结束；`multi_agent_gwtw.py --config configs/experiments/multi-agent-gwtw-demo.yml --mock-llm --mock-orfs` 完成 8 trial、4 finish、`errors=[]`。

## 已知风险与待处理项

- `tests/` 仍按项目约定仅保留在本地（`.gitignore` 排除），新 clone 只能跑 `make check`；重新开发路线确定后建议评估是否纳入版本管理。
- wall-clock 预算只在阶段边界检查，不能主动中断正在执行的 ORFS stage。
- 真实实验规模仍小（单设计 8 trial），不足以支撑统计性 QoR 结论。
- 通用 replay、跨 session resume、版本化参数空间与学习型 Doomed Predictor 为后置项。
- `environment_manifest.json` 中 ORFS commit 与 PDK revision 仍为 unresolved，跨环境比较前需刷新。
- `docs/Note.md` 是用户笔记，保持只读。

## 下一步入口

1. 先读取本文件、`AGENTS.md`、`docs/WORKFLOW.md` 与 `docs/AgenticPD项目扫描报告.md`。
2. 多 Agent 协作体系已固化：角色与交接协议见 `docs/team/README.md`，当前状态见 `docs/team/STATE.md`；新功能开发默认走 Dispatcher→Executor→Verifier→Reviewer。
3. 下一轮开发路线待用户确认后另立方案；当前基线为「两条可运行入口 + 分层包结构 + 66 项回归」。
4. 修改实验契约、参数空间或 QoR comparator 前先取得用户授权。
