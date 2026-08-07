# AgenticPD 多 Agent 协作体系（持久化）

本文档定义项目内多 Agent 协作的固定机制：角色、状态机、交接协议与文件清单。机制固化在仓库里，任何新对话先读 `docs/team/STATE.md` 即可继续，不需要每次重新口头约定"用几个 agent"。

## 1. 设计原则

- **角色即契约**：每个角色有固定输入、输出与边界；角色之间只通过仓库内文件交接，不靠口头转述。
- **证据先于判断**：验证者只报证据数字，审查者基于证据做判断；任何 Agent 的文字总结都不是通过证据。
- **单交付包闭环**：一次只处理一个可独立验收的闭环（一个交付包），跑完 契约→实现→验证→审查 再开始下一个。
- **人做范围与红线**：用户只确认范围、实验口径和红线授权，不替 Agent 判断实现正确性。
- **防呆优先**：交接必须更新状态文件；越权修改、跳过验证、缺证据一律视为未完成。

## 2. 角色表

| 角色 | 执行方 | 输入 | 输出 | 边界（不能做） |
|---|---|---|---|---|
| 调度员 Dispatcher | Codex | 用户目标、`STATE.md`、`WORKFLOW.md` | 一次性交付包、任务路由、状态更新 | 不写实现、不跑真实实验 |
| 执行者 Executor | Claude | 交付包 | 实现 + 先失败后通过的回归测试 + 交付报告（1.给你 / 2.给Codex） | 不扩大范围、不自行 commit/merge/push |
| 验证者 Verifier | Codex | 代码 + 交付报告 | 纯 Python 验证报告（命令、退出码、测试数、证据路径），使用 `code-test` 技能 | 不改代码、不做优劣判断 |
| 审查者 Reviewer | Codex | 验证报告 + 代码 + 契约 | P0/P1/P2 分级审查与"通过/返工"结论，使用 `code-review` 技能 | 不替执行者修复、不执行 Git 写操作 |
| 用户 | 人 | 各角色报告 | 范围确认、真实实验、红线授权 | — |

## 3. 状态机

```text
IDLE → CONTRACT（调度员）→ IMPLEMENT（执行者）→ VERIFY（验证者）
      → REVIEW（审查者）→ PASS → DONE
                        → FAIL → REWORK → IMPLEMENT
```

状态记录在 `docs/team/STATE.md`，字段：

```text
deliverable_id  当前交付包编号（如 d-2026-08-07-1）
title           一句话目标
status          idle / contract / implement / verify / review / rework / done
owner           当前持有者（dispatcher / executor / verifier / reviewer）
contract_path   交付包文件路径
evidence        已验证的证据路径列表
next_action     下一步谁做什么
blockers        阻塞项（红线、外部确认、真实实验）
updated_at      最近交接时间
```

## 4. 交接协议（每次交接必须执行）

1. **出包**（Dispatcher）：按 `templates/交付包.md` 写目标/非目标/允许修改范围/必须证明的行为与反例/验证命令/证据要求/禁止改动清单；更新 `STATE.md` 为 `contract`。
2. **实现**（Executor）：只改契约允许的文件；先写失败回归测试再实现；交付报告必须列测试名+关键断言+证据路径；更新 `STATE.md` 为 `implement`→`verify`。
3. **验证**（Verifier）：按 `code-test` 技能独立重跑 `make check`、`make test`、CLI `--help`；不改代码；把命令实际输出写进验证报告；更新 `STATE.md`。
4. **审查**（Reviewer）：按 `code-review` 技能五维度扫描，核对测试断言与原始证据；P0/P1 用"请你…"格式给执行者；结论"通过"或"返工"；更新 `STATE.md`。
5. **收尾**（Dispatcher/用户）：P0/P1 为零后，由用户授权的操作才执行 commit/merge/push；更新 `STATE.md` 为 `done` 并记录 commit。

防呆规则：

- 交接不更新 `STATE.md` = 未完成，接收方拒绝开工。
- 执行者不得修改契约外文件；提交前 `git diff --stat` 必须与契约范围一致。
- 验证者与审查者必须独立复核，不得直接引用交付报告的数字。
- 子代理派发必须用**自包含任务文本**（`fork_turns=none`），禁止继承完整对话上下文——本工程已验证过：继承全上下文会导致 agent 跑去执行对话里提到的其他指令（如文档删除）。

## 5. 文件清单

```text
docs/team/
├── README.md                本章程
├── STATE.md                 运行状态（单一事实源）
├── deliverables/            交付包与报告存档（按编号）
└── templates/               交付包 / 交付报告 / 验证报告 / 审查报告模板
.claude/agents/              Claude Code 侧子代理定义（dispatcher/executor/verifier/reviewer）
.agents/skills/code-review/  Codex 审查标准（五维度扫描）
.agents/skills/code-test/    Codex 验证标准（纯 Python 门）
```

## 6. 与现有文档的关系

- `docs/WORKFLOW.md` 的五步流程 = 单个交付包的生命周期；本体系把它角色化 + 状态化。
- `AGENTS.md` / `CLAUDE.md` 只保留角色边界，具体交接读本目录。
- `docs/HANDOVER.md` 每日状态用 `STATE.md` 的摘要填充。
- 新增功能开发请求默认走本体系：Dispatcher 先出交付包，用户确认后交 Executor，Verifier/Reviewer 把关。
