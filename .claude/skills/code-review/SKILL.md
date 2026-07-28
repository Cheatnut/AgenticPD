---
name: code-review
description: 当用户请求代码审查、PR 审查、代码检查或需要分析代码质量时，自动触发此技能。根据项目约定和最佳实践，对指定代码进行系统化审查。
---

# AgenticPD 代码审查技能

本技能针对 `flow/agenticpd/` 项目定制的五维度代码审查，遵循 AGENTS.md/CLAUDE.md 中的工程规范。

---

## 触发条件

用户输入包含以下关键词时激活：代码审查、code review、审查、检查代码、check code、review。

---

## 审查流程

严格按以下三个阶段执行：

### 阶段一：扫描

依次对五个维度进行全项目扫描，收集所有发现。每个维度独立扫描，不做交叉判断。

### 阶段二：报告

将发现按严重程度（P0/P1/P2）分类输出。报告使用中文，文件路径和代码符号保持英文。

### 阶段三：修复

对 P0 和 P1 问题逐一修复。P2 问题仅在用户明确要求时修复，否则只记录在报告中。

---

## 维度一：文件路径硬编码检查

### 检查规则

1. **绝对路径禁止**：任何包含 `/home/`、`/Users/`、`C:\`、`/tmp/`、`/var/`、`/opt/` 的字符串字面量均为 P0 违规。
2. **相对路径参数化**：`Path("runs")`、`Path("logs")` 等裸字符串路径，若项目 `config.py` 中已有同名常量，则必须使用配置常量 —— P1。
3. **可接受例外**（无需修改）：
   - 从 `__file__` 推导的路径（如 `Path(__file__).resolve().parent / "fixtures"`）
   - ORFS 标准输出文件名（如 `6_report.json`、`2_1_floorplan.json`）—— 这些是 ORFS API 契约
   - `config.py` 中定义的路径常量本身（如 `RUNS_DIR = AGENTICPD_DIR / "runs"`）
   - 测试文件中的临时目录（如 `tempfile.mkdtemp()`）
   - docstring/注释中的示例路径

### 扫描方法

```bash
# 搜索绝对路径
grep -rnE "/home/|/Users/|C:\\\\|/tmp/|/var/|/opt/" --include="*.py" .

# 搜索裸 Path 字符串（非 config 推导）
grep -rnE 'Path\("[a-z]' --include="*.py" .
```

### 判定

- 出现用户 home 目录或其他机器特定路径 → **P0**
- 出现 `config.py` 中已有常量的重复字符串 → **P1**
- 所有路径均可从 `config.py` 或 `__file__` 推导 → **PASS**

---

## 维度二：None/null 关键字合理性检查

### 检查规则

1. **TrialRecord 字段**：`parent_trial_id`、`branch_stage`、`final_qor`、`checkpoint`、`failure`、`failed_stage`、`param_diff`、`error_message` 在何种状态下应为 `None`，检查是否符合数据模型契约。
2. **StageResult 字段**：`stage_qor` 在 `status="failed"` 或 `"skipped"` 时是否合理为空字典 vs None。
3. **runs/ 产物完整性**：检查 `trial.json` 写入路径中所有可能为 None 的字段是否都有合理的默认值或明确的 None 处理。
4. **LLM 返回值**：`MockLLMClient` 和真实 `LLMClient` 在失败路径上的返回值是否一致（都不应返回 None 导致上游 AttributeError）。
5. **config.py 默认值**：`FrameworkConfig` 中各字段的默认值，`None` 是否表示"未设置，将由上游注入"且下游有处理。

### 扫描方法

```bash
# 搜索所有 None 赋值和 None 比较
grep -rnE "(= None|is None|== None|-> None|Optional\[)" --include="*.py" .

# 重点检查 runs/ 目录下的 trial.json 写入路径
grep -rnE "\.get\(|\[.+\]" --include="*.py" schemas/trial.py optimizer.py managers/
```

### 常见问题模式

- `some_dict["key"]` 在 key 缺失时抛 `KeyError` → 应使用 `some_dict.get("key")` 或显式校验
- `trial.final_qor["wns_ps"]` 在 `final_qor` 为 None 时抛 `TypeError` → 应先检查 `is_complete` 或 None
- `trial.checkpoint.checkpoint_id` 在 `checkpoint` 为 None 时抛 `AttributeError` → 应先检查 None
- 函数返回 `Optional[Dict]` 但调用方未处理 None 分支 → 可能静默传播

### 判定

- 会导致 `AttributeError`、`TypeError`、`KeyError` 的 None 传播 → **P0**
- 语义不合理的 None（如 `status="ok"` 但 `final_qor=None`）→ **P1**
- None 作为合法"未设置/不适用"标记且有文档说明 → **PASS**

---

## 维度三：中文使用规范检查

### 项目语言约定（CLAUDE.md §1.1, §1.2, §7）

| 文件类型 | 语言要求 |
|----------|---------|
| `*.md`（文档） | **必须中文**（专有名词、代码符号除外） |
| `*.py`（源码） | **禁止中文**。注释、docstring 必须英文。 |
| `*.yaml`（配置） | **禁止中文** |
| `*.json`（数据/Schema） | **禁止中文** |
| `Makefile` | **禁止中文** |

### 检查规则

1. **非 md 文件中出现中文字符** → **P0**（`AGENTS.md` 和 `CLAUDE.md` 互相复制，视为文档处理，可以为中文；但内容同步规则见维度五）
2. **md 文档中出现大段英文** → **P2**（README.md 允许部分英文示例和数学公式）
3. **允许出现在任何文件中的"准中文"**：EDA 专有名词、工具报错原文、论文标题、人名。

### 扫描方法

```bash
# 检查非 md 文件中的中文字符
find . -name "*.py" -o -name "*.yaml" -o -name "*.json" -o -name "Makefile" | \
  xargs grep -lP '[\x{4e00}-\x{9fff}\x{3000}-\x{303f}\x{ff00}-\x{ffef}]' 2>/dev/null

# 检查 md 文件（确认有中文内容）
find . -name "*.md" | while read f; do
  if ! grep -qP '[\x{4e00}-\x{9fff}]' "$f" 2>/dev/null; then
    echo "WARNING: $f has NO Chinese characters"
  fi
done
```

### 判定

- `.py`/`.yaml`/`.json`/`Makefile` 中出现中文 → **P0**
- `.md` 文件中完全没有中文字符 → **P1**
- 所有文件符合上表 → **PASS**

---

## 维度四：过时/冗余代码检查

### 检查规则

1. **死代码**：import 后从未使用的模块、定义后从未调用的函数/类、赋值后从未读取的变量。
2. **已删除功能的残留**：如 `branch_from()` 已被删除，但仍有 import 引用或文档提及（文档侧见维度五）。
3. **双写/双重存储**：同一数据是否有两条写入路径（如 history.json + trials.jsonl 双写）——是否已清理。
4. **向后兼容层**：`orfs_interface.py`（16 行 re-export）——是否有外部引用？若已无引用应标记为待清理（P2）。
5. **遗留 Mock/Stub**：`SlurmBackend` 的 `NotImplementedError` —— 是否已有计划实现（可接受），还是应该删除。
6. **重复逻辑**：多处实现相同功能的代码是否应该抽取为公共函数。

### 扫描方法

```bash
# 检查 Python 死代码（需要 AST 分析或 IDE 工具）
# 快速方法：搜索所有 def/class 定义，再搜索调用点
grep -rn "^def \|^class " --include="*.py" .
# 对比 grep -rn "function_name\(" --include="*.py" .

# 检查 orfs_interface.py 的引用
grep -rn "from orfs_interface import\|import orfs_interface" --include="*.py" .

# 检查 branch_from 残留
grep -rn "branch_from" --include="*.py" --include="*.md" .
```

### 判定

- 会导致运行时错误的死 import → **P0**
- 确认无人调用的函数/类（无合理保留理由）→ **P1**
- 有合理保留理由的代码 → **PASS**（需在报告中说明理由：如 SlurmBackend stub 是为阶段 F 预留接口）

---

## 维度五：文档与代码同步检查

### 检查规则

1. **CLI 接口一致性**：`tools/*.py` 的实际 argparse 接口与 `docs/cli-verification.md` 和 `README.md` 中的命令示例是否一致。
2. **目录结构一致性**：`docs/directory-guide.md` 和 `README.md` 中描述的文件/目录是否真实存在；真实存在的文件是否都有文档覆盖。
3. **交付物记录**：`AgenticPD-Demo审查与迭代计划.md`（外部文档）中的阶段交付记录是否与实际代码一致。
4. **数据流描述**：README.md 中的数据流图（tree.json + history.json → ...）是否与当前实现（tree.json + trials.jsonl → ...）一致。
5. **AGENTS.md ↔ CLAUDE.md**：两个文件的阶段描述、目录职责、提交纪律是否完全一致。
6. **功能描述准确性**：`--dry-run` vs `--mock-llm`、`visualize_tree.py` vs `tools/visualize.py` 等命名是否在文档中正确反映。

### 扫描方法

```bash
# 对比 CLI 工具的实际 help 与文档中的命令
python3 tools/trial_inspect.py --help
python3 tools/clean.py --help
python3 tools/trial_reproduce.py --help
python3 main.py --help

# 对比 README 目录表与实际文件列表
diff <(grep -E '^\|.*\.py\|' README.md | grep -oP '`[^`]+`' | tr -d '`' | sort) \
     <(find . -name "*.py" -not -path "./.git/*" -not -path "./__pycache__/*" | sed 's|^\./||' | sort)

# 检查 AGENTS.md 和 CLAUDE.md 是否一致
diff AGENTS.md CLAUDE.md

# 检查所有文档中引用的文件是否真实存在
grep -roP '`[a-z_/]+\.(py|md|yaml|json)`' docs/ README.md | tr -d '`' | while read f; do
  [ -f "$f" ] || echo "MISSING: $f"
done
```

### 判定

- 文档中的 CLI 命令实际无法执行（参数名/flag 错误）→ **P0**
- 文档引用的文件不存在 → **P1**
- 文档描述的功能行为与实际代码不一致 → **P1**
- AGENTS.md 与 CLAUDE.md 内容不同步 → **P0**（按 CLAUDE.md §8 规则，以 AGENTS.md 为准）
- 文件存在但文档中未列出 → **P2**

---

## 报告格式

审查完成后，按以下格式输出报告：

```markdown
# AgenticPD 代码审查报告

**审查日期**：YYYY-MM-DD
**审查范围**：flow/agenticpd/

---

## 1. 维度一：路径硬编码 — [PASS / N issues]

| 严重度 | 文件:行号 | 问题 | 修复方案 |
|--------|----------|------|---------|
| P0/P1  | ...      | ...  | ...     |

## 2. 维度二：None/null 安全性 — [PASS / N issues]

| 严重度 | 文件:行号 | 问题 | 修复方案 |
|--------|----------|------|---------|

## 3. 维度三：中文使用规范 — [PASS / N issues]

| 严重度 | 文件:行号 | 问题 | 修复方案 |
|--------|----------|------|---------|

## 4. 维度四：过时/冗余代码 — [PASS / N issues]

| 严重度 | 文件:行号 | 问题 | 修复方案 |
|--------|----------|------|---------|

## 5. 维度五：文档代码同步 — [PASS / N issues]

| 严重度 | 文件 | 问题 | 修复方案 |
|--------|------|------|---------|

## 6. 总结

- P0: N 个（必须修复）
- P1: N 个（应当修复）
- P2: N 个（建议修复）
```

---

## 修复规则

1. **P0 问题**：立即修复，修复后验证 `make test` 仍通过。
2. **P1 问题**：逐一修复，每处修复后确认无副作用。
3. **P2 问题**：仅在报告中列出，询问用户是否修复。
4. 修复完成后重新运行扫描确认清零。

---

## 提交规则

所有 P0 和 P1 修复完成后，**必须 commit 并 push**。P2 修复同理（若用户选择修复）。

### Commit 规范

- **分支**：在当前分支上直接 commit，除非命中 CLAUDE.md 红线（如涉及 `.env`、密钥、CI/CD 等需要先问用户）。
- **Commit message 格式**：`code-review: <简短描述>`

  示例：
  ```
  code-review: fix 4 P0 + 18 P1 issues from five-dimension review
  code-review: fix hardcoded paths in build_fixtures.py
  code-review: fix TrialManager.get() missing iter-{N}- prefix
  ```

- **多维度审查**：一次完整的五维度审查 → 一个聚合 commit。
- **单项修复**：单独修复某个问题时 → 独立的 commit。

### Push 规范

- 审查修复 commit 完成后，立即 `git push`。
- 若当前分支有上游跟踪：`git push`。
- 若当前分支无上游跟踪（首次 push）：`git push --set-upstream origin <branch>`。
- Push 前确认：不涉及 `.env`、密钥、token、API key。

### 流程

```
扫描 → 报告 → 修复(P0/P1) → 验证(make test) → 扫描确认清零 → commit → push
```

---

## 已知可接受例外（审查时自动跳过）

| 文件/位置 | 内容 | 理由 |
|-----------|------|------|
| `config.py::PARAM_SPACE` 中的参数 `description` 字段 | 含中文说明 | 这些是给 LLM prompt 用的参数说明，需要中文语义（已改为英文？检查） |
| `orfs/parser.py` 中的 JSON 文件名 | `2_1_floorplan.json` 等 | ORFS 标准输出格式，不可修改 |
| `docs/` 目录下所有 `.md` 文件 | 中文内容 | 符合项目语言约定 |
| `managers/trial_manager.py:16` | `Path("agenticpd/runs")` | docstring 中的示例，非执行路径 |
| `SlurmBackend` 中的 `NotImplementedError` | stub | 阶段 F 前按计划实现 |

---

## 执行

用户输入 `/code-review` 或等效触发词后，严格按本文档的三阶段流程执行。
不要跳过扫描直接给结论。不要只扫描不修复（P0/P1）。修复完成必须 commit 并 push。

完整流程：**扫描 → 报告 → 修复 → 验证(`make test`) → 扫描确认清零 → commit → push**
