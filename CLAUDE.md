# AGENTS.md - AgenticPD 工程规范

本文件适用于 flow/agenticpd/ 及其全部子目录。目标是把可运行 Demo 演化为
可复现、可审计、可比较的 Flow Optimization 实验平台。

---

## 1. 分支工作流与阶段管理

项目分为 A–H 共八个阶段，每个阶段在独立分支上开发：

```
main
  ├── agenticpd-stage-a    ← 阶段 A：冻结 Demo、建立可回归的最小实验
  ├── agenticpd-stage-b    ← 阶段 B：Trial / Checkpoint / Artifact 数据层
  ├── agenticpd-stage-c    ← 阶段 C：重构 ORFS Adapter 与执行后端
  ├── agenticpd-stage-d    ← 阶段 D：扩展 observation 与非 Agent baseline
  ├── agenticpd-stage-e    ← 阶段 E：AgenticPD 改造为可替换策略
  ├── agenticpd-stage-f    ← 阶段 F：GWTW Population Scheduler
  ├── agenticpd-stage-g    ← 阶段 G：Doomed Runs 第一版
  └── agenticpd-stage-h    ← 阶段 H：三部分整合与实验
```

### 1.1 分支纪律

- 每个阶段从 `main`（或上一阶段的 merge 结果）开出新分支 `agenticpd-stage-<letter>`。
- 一个分支只做一个阶段的事。不在阶段 A 分支上写阶段 B 的代码。
- 分支上所有的新增文件、注释、README 用中文；可变代码（变量名、函数名、类名、命令）保持英文。
- 阶段全部核实完成 → 把注释和文档翻译成英文 → merge 到 `main`。
- merge 之后才从新 `main` 开下一阶段分支。不把多个阶段堆在同一个分支上。

### 1.2 中文→英文翻译规则

翻译只针对注释和文档，不碰代码：

| 需要翻译 | 不需要翻译 |
|----------|-----------|
| 模块/类/函数的 docstring | 变量名、函数名、类名 |
| 行内注释（`# ...`） | 字符串常量中的 EDA 术语（如 `"WNS"`） |
| README.md 正文 | 命令行参数名、配置键 |
| AGENTS.md / CLAUDE.md / docs/ 下的文档 | 测试用例中的 EDA 专用名词 |

翻译后整个项目除了 `README.md` 之外不得有中文字符。README.md 保留中文。
翻译完成后先跑 `make test` 确保不破坏功能，再 merge。

---

## 2. 当前阶段边界

- 当前为阶段 A：冻结 Demo、建立纯 Python 回归检查与实验契约。
- 不修改 ../Makefile、../designs/、../platforms/、../tools/，也不修改 ORFS evaluator。
- 不提交真实 EDA 运行产物、.env、API key、绝对路径或 PDK 文件。
- 阶段 B 之前，不引入 GWTW、Doomed Runs、数据库、Slurm 调度或新的 LLM provider。

---

## 3. 目录职责

| 路径 | 职责 |
|------|------|
| AGENTS.md | 本文件；与 CLAUDE.md 内容完全一致，供不同 AI 工具读取 |
| CLAUDE.md | 与 AGENTS.md 内容完全一致 |
| configs/experiments/ | 可审阅实验声明；一个 YAML 对应一个可比较的实验设定 |
| docs/ | 不随运行变化的契约、设计决策和操作说明 |
| scripts/ | 可直接执行的辅助脚本；优先只依赖 Python 标准库 |
| tests/ | 不启动 ORFS 的纯 Python 测试与小型真实回归夹具 |
| tests/fixtures/legacy_run/ | 20260718 Demo 的只读最小证据，测试禁止写入 |
| runs/ | 真实 trial 临时产物；已被 Git 忽略，不能作为唯一实验记录 |
| environment_manifest.json | 脚本生成的版本快照；只记录变量名，不记录值、密钥或绝对路径 |

---

## 4. 实验与命名规则

- 新 Trial 的逻辑 ID 使用 `<experiment>-<platform>-<design>-s<seed>-<sequence>`；
  legacy Demo 的 `agenticpd_iter<N>` 保留原名，只作为回归证据。
- 每次真实实验必须有 `configs/experiments/<name>.yaml`，并记录参数空间版本、evaluator、
  预算、seed 与 design 角色（smoke/development/held-out）。
- 一个实验只能有一个明确 evaluator；QoR 原始来源必须是 ORFS 报告，不能由 Agent 文本替代。
- 真实运行前必须刷新 `environment_manifest.json`。若 ORFS、OpenROAD 或 PDK revision
  无法识别，manifest 必须明确写为 unresolved，该运行不得用于正式 QoR 对比。

---

## 5. 安全与可复现性

- `.env` 只保存本机环境变量；不可读取、打印、提交或复制其中的值。
- 日志可以记录命令、退出码、耗时、QoR 与版本；不得记录 token、完整 API 请求头或密钥。
- 路径必须由项目根和配置推导，不在 Python 代码或 YAML 中写用户目录。
- `tests/fixtures/` 中的文件是只读输入；测试使用临时目录，禁止回写夹具。
- 需要改变 parameter space、QoR comparator 或 ORFS 命令语义时，先更新
  `docs/experiment-contract.md`，再改实现和测试。

---

## 6. 验证门

每次提交至少执行：

    make test
    python3 scripts/generate_environment_manifest.py --output environment_manifest.json

测试不得依赖网络、LLM、OpenROAD、PDK 或已有 runs/。真实 smoke run 是额外验证，
不能替代单元测试。

---

## 7. 修改纪律

- 优先新增可替换模块和测试；不要在 main.py 中继续堆叠阶段 B 以后的职责。
- 对行为有影响的 bug，先添加会失败的回归测试，再修复。
- 开发期注释和文档用中文；阶段结束翻译为英文后再 merge。
- 显而易见的 Python 语法不重复注释——注释解释 EDA 语义和设计原因。
- 清理 `runs/`、ORFS `results/`/`logs/`/`reports/`/`objects/` 时必须显式指定 trial，
  不得递归删除宽泛目录。

---

## 8. AGENTS.md 与 CLAUDE.md 同步规则

- 两个文件内容完全一致，随时保持同步。
- 修改时先改 AGENTS.md，再复制为 CLAUDE.md。
- 如发现两个文件不一致，以 AGENTS.md 为准。
