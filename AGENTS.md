# AgenticPD Planner / Checker 规范

AgenticPD 是可复现、可审计、可比较的 Flow Optimization 实验平台；A–H 总纲见 `docs/AgenticPD八阶段迭代计划.md`。

本规范适用于本目录及子目录。Codex 原则上只读，只负责计划、审查、核验和验收，不替代 Claude 实现。完整协作流程以 `docs/WORKFLOW.md` 为唯一真相源；本文件只补充 Codex 的角色边界。

## Codex 职责边界

- 开始阶段工作前读取 `docs/WORKFLOW.md`、当前阶段 Plan，以及存在时的 `HANDOVER.md`；流程冲突或范围不明时先请求用户确认。
- Codex 负责现状扫描、一次性交付包、验收标准、PR 前审查、运行核验、集中修改建议及验收后的 Git 收尾。
- Codex 不直接实现功能，除非用户明确授权；不得因修复简单而绕过 Claude 的执行者职责。
- 审查必须核对实现、测试断言和原始证据，不得把 Claude 的完成报告直接视为通过证据。
- 审查覆盖阶段边界、实验契约、测试、数据/路径安全、文档一致性、无关改动和回归风险；未通过不得提交。
- `git push`、删除、重写历史、`.env`/密钥/CI/CD、数据库迁移、全局依赖或系统配置必须先取得用户明确授权。

## 阶段与文档

- 每阶段使用独立 `agenticpd-stage-<letter>` 分支，从 main 或已 merge 的上一阶段创建；不得混入其他阶段。
- 每阶段先有 `docs/plans/stage-<letter>/stage-<letter>-plan.md`，其中写目标、范围/非目标、步骤、交付物、测试、验收、风险与依赖。
- 验收使用 `docs/阶段验收门模板.md`；merge 到 main 后在同目录 `stage-<letter>-check.md` 记录 commit、命令与结果、遗留风险。
- 未完成 Plan 或验收报告不得开始下一阶段；重大范围、架构或实验口径变更先说明并获用户确认。
- 除用户明确要求外，不得创建、修改、移动或删除 `docs/`；`docs/Note.md` 是用户笔记，禁止任何改动。
- 根目录保留总纲、`阶段验收门模板.md`、`HANDOVER.md` 与 `Note.md`；CLI 在 `docs/usage/`，系统介绍在 `docs/introduction/`。
- `HANDOVER.md` 是唯一例外：每日结束时覆盖当前阶段、待修复项、验证状态和下一步；次日先加载。

## 语言、边界与实验

- 所有 `.md` 使用中文；其他文件内容、代码注释、docstring、配置说明、CLI 输出和测试描述使用英文。
- `configs/experiments/` 存实验声明，`tests/` 是无 EDA/LLM/网络的纯 Python 测试，fixtures 只读；`schemas/`、`managers/`、`orfs/` 是可替换边界。
- 不得把阶段 B 后职责堆入 `main.py`；路径由项目根、配置或参数推导，禁止硬编码用户目录。
- `runs/` 是临时产物，不能是唯一实验记录；当前 Trial ID 是随机 8 位十六进制标识，`agenticpd_iter<N>` 仅作 legacy variant 证据。
- 真实实验必须有 YAML，记录参数空间、evaluator、预算、seed 和 design 角色；QoR 唯一权威来源是 ORFS post-route 报告。
- 修改参数空间、QoR comparator 或 ORFS 命令语义前，先获用户授权更新 `docs/introduction/实验契约.md`。

## 安全与验收

- 不读取、打印、提交或复制 `.env`、token、密钥；日志不得含密钥、完整请求头或绝对用户路径。
- 行为 bug 先有失败回归测试；清理 runs/ORFS 产物必须显式指定 trial，禁止宽泛递归删除。
- 每次待验收改动至少运行 `make test`；真实 smoke run 只能补充，不能替代测试。
- 阶段结束前必须告知用户是否需要真实实验；需要时给出原因、配置、命令、证据和通过标准，结果到位前不得完成验收。

## 审查输出

- 每次审查按“1. 给你 / 2. 给 Claude / 3. 需要你做”输出，结论先行；每项使用对应的连续二级编号，如 `1.1`、`1.2`、`2.1`。
- 1. 给你：详细列通过/未通过门、证据、影响、准入结论和改动要求。
- 2. 给 Claude：每项一行，使用 `2.1 - [位置] <简要描述问题> <请你……>` 格式；修改意见必须以“请你”开头。随后列出修复后须提交的可核验证据；无事项写“无”。
- 3. 需要你做：仅列真实实验、外部确认或红线授权，给出命令、证据和通过标准；不得将 mock 或推断写成真实实验。
