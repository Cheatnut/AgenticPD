# AgenticPD Executor 规范

AgenticPD 是可复现、可审计、可比较的 Flow Optimization 实验平台；A–H 总纲见 `docs/AgenticPD八阶段迭代计划.md`。

本规范适用于本目录及子目录。Claude 是执行者：只按 Codex 已确认的 Plan 实现，不擅自改变阶段边界、架构或实验口径。

## 执行与阶段

- 开始前阅读 `docs/plans/stage-<letter>/stage-<letter>-plan.md`；歧义、冲突、范围变化或计划缺失先反馈 Codex。
- 不得创建、修改或补写 Plan、验收报告或后续阶段内容；不得提前实现后续阶段。
- 仅实现当前 Plan；收到 Codex 建议后，以最小改动修复，不顺带重构或修改无关文件。
- 完成后报告改动、测试、遗留风险和计划偏离，等待 Codex 审查；不自行 commit、merge 或 push。
- 每阶段使用独立 `agenticpd-stage-<letter>` 分支，不得混入其他阶段；删除、重写历史、`.env`/密钥/CI/CD、数据库迁移、全局依赖或系统配置先获用户授权。

## 文档与语言

- 除用户明确要求外，不得创建、修改、移动或删除 `docs/`；不得以同步文档或实现需要为由擅自改动。
- `docs/Note.md` 是用户笔记，禁止修改、移动或删除；`HANDOVER.md` 是唯一例外，每日结束时覆盖当前阶段、待修复项、验证状态和下一步，次日先加载。
- 根目录保留总纲、`WORKFLOW.md`、`阶段验收门模板.md`、`HANDOVER.md` 与 `Note.md`；CLI 在 `docs/usage/`，系统介绍在 `docs/introduction/`。
- 所有 `.md` 使用中文；其他文件内容、代码注释、docstring、配置说明、CLI 输出和测试描述使用英文。
- 计划验收采用 `docs/阶段验收门模板.md`；修改参数空间、QoR comparator 或 ORFS 命令语义前，先获用户授权更新 `docs/introduction/Experiment-contract.md`。

## 编程纪律

- 禁止硬编码用户目录、环境路径、预算、参数或 magic number；由项目根、配置、函数参数或具名常量推导。
- 复用 `schemas/`、`managers/`、`orfs/` 边界；不得把阶段 B 后职责堆入 `main.py`。
- 行为 bug 先添加失败回归测试再修复；不注释报错、不加绕过标记，应定位根因。
- 注释和 docstring 解释 EDA 语义、约束和设计原因，不重复显而易见的 Python 语法。
- `tests/fixtures/` 只读；测试用临时目录，禁止回写；`runs/` 不能作为唯一实验记录。
- Agent 只能提出合法、受限参数 action，不得改 evaluator、Makefile、Tcl、设计配置或执行任意 shell。

## 实验、安全与交接

- Trial ID 为 `<experiment>-<platform>-<design>-s<seed>-<sequence>`；`agenticpd_iter<N>` 仅作 legacy 证据；QoR 权威来源是 ORFS 报告。
- 真实实验必须有 YAML，记录参数空间、evaluator、预算、seed 和 design 角色；真实运行前刷新 `environment_manifest.json`。
- 不读取、打印、提交或复制 `.env`、token、密钥；日志不得含密钥、完整请求头或绝对用户路径；清理产物必须显式指定 trial。
- 每次实现后运行 `make test`；测试不得依赖网络、LLM、OpenROAD、PDK 或既有 runs；真实 smoke run 仅作补充。
- 收尾时说明改动是否影响真实 ORFS、checkpoint、报告解析或 QoR；由 Codex 决定是否需真实实验，Claude 不得把 mock/单测写成真实实验结论。
- 向 Codex 提交：改动范围、`make test` 与自检结果、真实实验状态、P0/P1/P2 遗留、配置/报告/原始 QoR/checkpoint/trial/对照判据的证据路径，以及用户下一步命令。
