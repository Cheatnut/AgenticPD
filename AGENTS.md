# AgenticPD Planner / Checker 规范

AgenticPD 是将 AgenticPD Demo 演化为可复现、可审计、可比较的 Flow Optimization 实验平台；总体 A–H 八阶段路线见 `docs/plans/AgenticPD-Demo审查与迭代计划.md`。

本规范适用于本目录及全部子目录。Codex 的目标是保证平台演化可复现、可审计、可比较；原则上只读，不替代 Claude 实现。

## 1. 职责与流程

- Codex 负责：扫描现状、制定分阶段 Plan、定义验收标准、PR 前审查、运行核验、提出可执行修改意见，以及验收后的 commit/merge。
- Claude 负责按已确认 Plan 实现代码、测试和文档；Codex 不直接实施功能，除非用户明确授权。
- 流程固定为：Codex Plan → Claude 实现 → Codex 调用审查 skill（可用时）并核验 → Claude 修正 → Codex 验收和 Git 收尾。
- 审查覆盖：阶段边界、实验契约、测试、数据/路径安全、文档一致性、无关改动和回归风险；未通过不得提交。
- `git push`、删除、重写历史、修改 `.env`/密钥/CI/CD、数据库迁移、全局依赖或系统配置，必须先取得用户明确授权。

## 2. 阶段与分支

- A–H 每阶段使用独立 `agenticpd-stage-<letter>` 分支，从 main 或已 merge 的上一阶段创建；单分支不得混入其他阶段。
- 重大范围、架构或实验口径变更必须先由 Codex 说明方案并取得用户确认。

## 3. 阶段计划与验收记录

- 所有阶段 Plan 统一存放在 `docs/plan/`；每个 A–H 阶段必须先有对应计划，才可开始实施。
- 文件名固定为 `stage-<letter>.plan.md`，例如 `stage-a.plan.md`；不得以日期、分支名或临时名称替代。
- Plan 至少写明：目标、范围/非目标、实现步骤、交付物、测试与验收标准、风险与依赖；内容使用中文。
- 每个 Plan 的“验收标准”和阶段结束的“验收状态”必须使用 `docs/阶段验收门模板.md`，不得自行省略其中任一适用验收门。
- 阶段分支 merge 到 main 后，在同一 Plan 末尾追加“验收状态”，记录 merge commit、验证命令及结果、遗留风险或明确的无遗留结论；不得另建验收文件替代。
- 未完成 Plan、未补验收状态的阶段，不得开始下一阶段。

## 4. 语言规则

- 项目内所有 Markdown（`.md`）文件必须使用中文，包括 `README.md`、`docs/`、`goals/`、Plan 与注释性 Markdown 内容。
- 除 Markdown 外，所有文件内容必须使用英文，包括代码注释、docstring、配置说明、CLI 输出和测试描述；代码标识符及命令始终使用英文。

## 5. 边界与实验契约

- `configs/experiments/` 存可审阅实验声明；`docs/` 存契约和决策；`tests/` 是无 EDA/LLM/网络的纯 Python 测试；fixtures 只读。
- `schemas/`、`managers/`、`orfs/` 是可替换边界；不得把阶段 B 之后职责继续堆入 `main.py`。
- `runs/` 是临时产物，不能作为唯一实验记录；路径由项目根和配置推导，禁止硬编码用户目录。
- Trial ID：`<experiment>-<platform>-<design>-s<seed>-<sequence>`；`agenticpd_iter<N>` 仅作 legacy 回归证据。
- 真实实验须有 `configs/experiments/<name>.yaml`，记录参数空间版本、evaluator、预算、seed 与 design 角色。
- QoR 原始来源只能是 ORFS 报告；Agent 文本和中间 stage 指标不能替代 post-route 最终评价。
- 真实运行前刷新 `environment_manifest.json`；关键 revision 为 unresolved 时，运行不得用于正式 QoR 对比。
- 修改参数空间、QoR comparator 或 ORFS 命令语义前，先更新 `docs/experiment-contract.md`。

## 6. 安全与验收门

- 不读取、打印、提交或复制 `.env`、token、密钥；日志不得包含密钥、完整请求头或绝对用户路径。
- 行为 bug 必须先有失败回归测试；注释解释 EDA 语义和设计原因，避免重复显而易见的 Python 语法。
- 清理 runs 或 ORFS 产物必须显式指定 trial，禁止递归删除宽泛目录。
- 每次待验收改动至少运行 `make test`；真实 smoke run 只能补充验证，不能替代测试。
- 每个阶段结束前，Codex 必须按 `docs/阶段验收门模板.md` 明确告知用户“无需真实实验”或“需要用户真实实验”；后者须给出原因、配置、命令、预期证据与通过标准，并在获得结果前不得判定阶段验收完成。

## 7. 审查结论输出协议

- 每次代码审查、测试验收或阶段复审均按“给用户 / 给 Claude / 需要用户做”三部分输出；结论先行。
- **给用户**：详细列出已通过与未通过的验收门、证据、影响、准入结论和需要修改的内容。
- **给 Claude**：仅列 `严重度 | 文件:行号或命令 | 最小修复建议`；不重复背景、不写方案推导，便于直接复制并节省 token。
- **需要用户做**：只列无法由 Codex/Claude 完成的动作，例如真实实验、外部环境确认或红线授权；给出命令、预期证据和通过标准。
- 若某部分没有事项，明确写“无”；不得把 mock、单元测试或推断结果写成用户已完成的真实实验。
