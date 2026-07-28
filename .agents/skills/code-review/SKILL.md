---
name: code-review
description: 对 AgenticPD 进行代码审查、PR 审查、代码检查或质量分析时使用。按路径、None 安全、语言、冗余代码、文档与实现一致性五个维度扫描，输出分级问题和交给 Claude 的最小修复建议；不直接修改实现或执行 Git 操作。
---

# AgenticPD 代码审查

执行前读取当前 `AGENTS.md` 与 `CLAUDE.md`；它们优先于本技能。Codex 只负责扫描、报告与验收，Claude 负责修复。本技能不得创建或修改 `docs/plan/` 中的 Plan。

## 工作流

1. 独立扫描下列五个维度，收集证据；不要边扫描边修改。
2. 以 P0/P1/P2 报告发现，并为每项给出文件、行号、影响和最小修复建议。
3. 将 P0/P1 建议交给 Claude；Claude 修复后，仅审查该修复及必要回归范围。
4. 复核通过后运行 `make test`，报告验收结论。仅在用户明确要求且不触及红线时，Codex 执行 commit 或 merge；`git push` 必须再次取得用户明确授权。

严重度：P0 为安全、数据完整性或确定运行时故障；P1 为可复现性、契约或显著维护风险；P2 为低风险改进。P2 只报告，不建议 Claude 修改，除非用户明确要求。

## 一：路径与参数化

- 搜索 Python 中的 `/home/`、`/Users/`、`C:\\`、`/tmp/`、`/var/`、`/opt/` 字符串字面量；机器特定绝对路径为 P0。
- 搜索裸 `Path("...")`。若已有配置常量可表达该路径，重复字面量为 P1。
- 允许：从 `__file__` 推导的路径、`config.py` 的路径常量、测试临时目录、注释/docstring 中的示例，以及 ORFS 固定产物名。

```bash
rg -n --glob '*.py' '/home/|/Users/|C:\\\\|/tmp/|/var/|/opt/' .
rg -n --glob '*.py' 'Path\("[a-z]' .
```

## 二：None 与数据模型安全

- 检查 `TrialRecord` 的 optional 字段是否与 status 一致，特别是 `final_qor`、`checkpoint`、`failure`、`failed_stage` 与 `param_diff`。
- 检查 `StageResult.stage_qor` 在 failed/skipped 状态下的语义，及 trial JSON 的序列化/反序列化默认值。
- 检查 LLM、parser、backend 和 config 的 Optional 返回值是否在调用点被显式处理；可能触发 AttributeError、TypeError 或 KeyError 为 P0。

```bash
rg -n --glob '*.py' '= None|is None|== None|Optional\[' .
rg -n --glob '*.py' '\.get\(|\[[^]]+\]' schemas/trial.py optimizer.py managers/ orfs/ agents.py
```

## 三：语言规则

- 所有 `.md` 必须以中文撰写；专有名词、代码符号、命令、URL 与必要英文引用不算违规。
- 除 `.md` 外的文件必须使用英文；代码注释、docstring、配置说明、CLI 输出与测试描述中出现中文为 P0。
- `.md` 完全没有中文字符为 P1；文档中的少量必要英文不是问题。

```bash
find . -type f ! -name '*.md' ! -path './.git/*' ! -path './__pycache__/*' -print0 | \
  xargs -0 grep -lP '[\x{4e00}-\x{9fff}\x{3000}-\x{303f}\x{ff00}-\x{ffef}]' 2>/dev/null
find . -name '*.md' -print0 | while IFS= read -r -d '' file; do
  grep -qP '[\x{4e00}-\x{9fff}]' "$file" || echo "NO_CHINESE: $file"
done
```

## 四：过时与冗余代码

- 查找未使用 import、不可达函数/类、无消费者的数据写入、重复实现和已删除功能的残留引用。
- `orfs_interface.py` re-export 与 `SlurmBackend` stub 是否可保留，必须依据当前阶段 Plan 和调用点判断，不能仅凭存在判为问题。
- 确认无调用且无兼容性或计划依据的代码为 P1；仅可读性或重构建议为 P2。

```bash
rg -n --glob '*.py' '^(def|class) ' .
rg -n 'branch_from|orfs_interface|history\.json' --glob '*.py' --glob '*.md' .
```

## 五：文档、Plan 与实现一致性

- 对照实际 argparse `--help` 与 `README.md`、`docs/` 中的命令示例；不可执行的示例为 P0。
- 检查文档引用的文件、目录和模块是否存在；不存在为 P1。检查数据流、trial 存储、目录职责与实际实现是否一致；不一致为 P1。
- 检查当前阶段有 `docs/plan/stage-<letter>.plan.md`，并确认 Plan 的范围、交付物和验收状态与分支/实现一致；缺失或矛盾为 P1。
- `AGENTS.md` 与 `CLAUDE.md` 分属 Planner/Checker 与 Executor，内容不同是预期行为；只检查各自职责、共享实验契约和语言规则是否矛盾。

```bash
python3 main.py --help
python3 tools/trial_inspect.py --help
python3 tools/trial_reproduce.py --help
python3 tools/clean.py --help
rg -n -o '`[^`]+\.(py|md|yaml|json)`' README.md docs/ goals/ | sort
find docs/plan -maxdepth 1 -name 'stage-?.plan.md' -type f | sort
```

## 报告格式

使用中文输出，路径和代码符号保持英文：

```markdown
# AgenticPD 代码审查报告

**审查日期**：YYYY-MM-DD  
**审查范围**：<路径或变更范围>

## 1. 路径与参数化 — [PASS / N issues]
## 2. None 与数据模型安全 — [PASS / N issues]
## 3. 语言规则 — [PASS / N issues]
## 4. 过时与冗余代码 — [PASS / N issues]
## 5. 文档、Plan 与实现一致性 — [PASS / N issues]

每项问题使用：`P0/P1/P2 | 文件:行号 | 证据与影响 | 给 Claude 的最小修复建议`。

## 总结

- P0：N；P1：N；P2：N。
- 结论：通过 / 需 Claude 修复后复审。
- 已执行验证：<命令与结果>。
```

不要跳过扫描直接给结论，不要替 Claude 修复，也不要自动 commit、merge 或 push。
