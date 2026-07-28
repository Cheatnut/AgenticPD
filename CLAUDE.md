# AgenticPD Executor 规范

AgenticPD 是将 AgenticPD Demo 演化为可复现、可审计、可比较的 Flow Optimization 实验平台；总体 A–H 八阶段路线见 `goals/AgenticPD-Demo审查与迭代计划.md`。

本规范适用于本目录及全部子目录。Claude 是执行者：按 Codex 已确认的 Plan 实现，不擅自改变阶段边界、架构或实验口径。

## 1. 执行职责

- 开始前阅读对应 `docs/plan/stage-<letter>.plan.md`，确认目标、范围/非目标、交付物、测试和验收标准；歧义、冲突或范围变化先反馈 Codex。
- 不得创建、修改或补写 Plan，也不得提前实现后续阶段；Plan 由 Codex 制定、维护和验收。
- 仅实现当前 Plan；Codex 提出修改建议后，以修复问题所需的最小范围改动，不顺带重构或修改无关文件。
- 实现后报告改动、测试结果、遗留风险与任何计划偏离，等待 Codex 审查；不自行 commit、merge 或 push。
- 删除、重写历史、修改 `.env`/密钥/CI/CD、数据库迁移、全局依赖或系统配置，必须先取得用户明确授权。

## 2. 阶段、计划与语言

- A–H 每阶段使用独立 `agenticpd-stage-<letter>` 分支，从 main 或已 merge 的上一阶段创建；单分支不得混入其他阶段。
- 每个阶段必须先有 `docs/plan/stage-<letter>.plan.md`，阶段 merge 到 main 后由 Codex 在该文件追加验收状态。
- Plan 的验收标准必须采用 `docs/阶段验收门模板.md`；Claude 不得删除、降级或自行豁免其中的适用验收门。
- 项目内所有 Markdown（`.md`）文件必须使用中文，包括 `README.md`、`docs/`、`goals/` 和 Plan。
- 除 Markdown 外，所有文件内容必须使用英文，包括代码注释、docstring、配置说明、CLI 输出和测试描述；代码标识符及命令始终使用英文。

## 3. 编程纪律

- 不允许硬编码用户目录、环境特定路径、预算、参数或 magic number；路径和可变值应由项目根、配置、函数参数或具名常量推导。
- 优先复用 `schemas/`、`managers/`、`orfs/` 的既有边界；不得把阶段 B 之后职责继续堆入 `main.py`。
- 行为 bug 先添加会失败的回归测试再修复；不注释报错、不加绕过标记，应定位根因。
- 注释和 docstring 解释非显而易见的 EDA 语义、约束和设计原因；不重复 Python 语法本身。
- `tests/fixtures/` 是只读输入，测试使用临时目录，禁止回写；`runs/` 是临时产物，不能作为唯一实验记录。
- Agent 只能提出合法、受限的参数 action，不能改 evaluator、Makefile、Tcl、设计配置或执行任意 shell。

## 4. 实验、安全与验证

- Trial ID：`<experiment>-<platform>-<design>-s<seed>-<sequence>`；`agenticpd_iter<N>` 仅作 legacy 回归证据。
- 真实实验须有对应 experiment YAML，记录参数空间版本、evaluator、预算、seed 与 design 角色；QoR 唯一权威来源是 ORFS 报告。
- 修改参数空间、QoR comparator 或 ORFS 命令语义前，先更新 `docs/experiment-contract.md`，再改实现和测试。
- 不读取、打印、提交或复制 `.env`、token、密钥；日志不得包含密钥、完整请求头或绝对用户路径。
- 清理 runs 或 ORFS 产物必须显式指定 trial，禁止递归删除宽泛目录。
- 每次实现后必须运行 `make test`；测试不得依赖网络、LLM、OpenROAD、PDK 或既有 runs。真实 smoke run 只能补充验证。
- 真实运行前刷新 `environment_manifest.json`；关键 revision 为 unresolved 时，不得用于正式 QoR 对比。
- 阶段收尾时，Claude 必须向 Codex 说明改动是否影响真实 ORFS 行为、checkpoint、报告解析或 QoR；由 Codex 决定并向用户说明是否需要真实实验。Claude 不得把 mock 或单元测试写成真实实验结论。
