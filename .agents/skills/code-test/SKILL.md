---
name: code-test
description: 对 AgenticPD 执行零 LLM、零 ORFS、零网络的纯 Python 验证并输出中文验收报告时使用。运行单元测试、数据模型自检和无副作用 CLI 帮助检查；不运行 flow、mock 优化、复现、清理、修改代码或执行 Git 操作。
---

# AgenticPD 纯 Python 验证

执行前读取当前 `AGENTS.md`、`CLAUDE.md` 与 `docs/usage/cli-verification.md`。Codex 只运行验证和报告，不修改实现、不创建 Plan、不执行 Git 操作。所有命令必须从项目根目录运行。

## 验证边界

- 只验证纯 Python 数据模型、QoR 解析、fixture、checkpoint manifest 与 CLI 参数契约。
- 不调用真实 LLM、网络、OpenROAD、ORFS、PDK 或 Slurm；不读取 `.env`，不依赖既有 `runs/`。
- 不运行 `main.py` 的优化模式（包括 `--mock-llm --mock-orfs`）、`tools/trial_reproduce.py <trial-id>`、`tools/clean.py --yes`，也不执行会删除或写入运行产物的命令。
- `--help` 仅用于确认 argparse 接口，必须不带会启动执行的业务参数。

## 流程

1. 记录当前提交、工作区状态与测试文件清单；将已有未提交改动标为上下文，不归因于本次验证。
2. 运行纯 Python 核心验证、数据模型自检和 CLI 契约检查；每条命令分别记录退出码、摘要与耗时。
3. 失败时保留原始错误，按失败的测试文件或命令重跑一次以获得完整 traceback；不要修改实现。
4. 按报告模板输出结论。P0/P1 仅给 Claude 最小修复建议，等待其修复后再运行完整验证。

## 必跑命令

```bash
git status --short
rg --files tests -g 'test_*.py' | sort
make test
python3 schemas/trial.py
python3 main.py --help
python3 tools/trial_inspect.py --help
python3 tools/trial_reproduce.py --help
python3 tools/clean.py --help
```

`make test` 是主要验收门，必须运行完整 `unittest discover`，不能只挑选单个测试。当前测试职责如下；以实际输出的测试数量为准，不能将固定数量写成永久假设：

| 测试文件 | 验证内容 |
|---|---|
| `tests/test_qor.py` | fixture JSON 与文本兜底的 QoR 解析、单位转换、timing-first comparator |
| `tests/test_fixtures.py` | 成功/失败 Trial fixture、checkpoint manifest、失败阶段耗时 |
| `tests/test_schemas.py` | Trial/Stage/Checkpoint 数据模型、JSONL 去重和损坏行、临时目录中的 manifest hash 校验 |

## 结果判定

- P0：`make test`、`schemas/trial.py` 或任一必跑 CLI `--help` 非零退出；测试启动真实 LLM/ORFS/网络；验证命令写入或删除非临时项目产物。
- P1：测试发现为 0、关键测试文件缺失、测试声明的隔离边界与实际行为不符，或 CLI 帮助接口与 `docs/cli-verification.md` 的零副作用命令不一致。
- P2：测试输出噪声、断言信息不足、运行时间异常但仍通过，或可改善的覆盖建议。
- 通过：所有必跑命令退出码为 0，`make test` 显示 `OK`，且未启动被禁止的外部服务或 flow。

## 报告格式

使用中文输出，路径和代码符号保持英文：

```markdown
# AgenticPD 纯 Python 验证报告

**验证日期**：YYYY-MM-DD  
**验证范围**：<提交或工作区范围>  
**隔离声明**：未调用 LLM、ORFS/OpenROAD、PDK、网络或既有 runs；未读取 `.env`。

## 1. 验证前状态 — [PASS / 注意事项]

- 工作区：<clean / 已有改动文件>
- 发现测试：<测试文件列表>

## 2. 单元测试 — [PASS / FAIL]

| 命令 | 结果 | 测试数 | 耗时 | 证据 |
|---|---|---:|---:|---|
| `make test` | ... | ... | ... | ... |

## 3. 数据模型自检 — [PASS / FAIL]

| 命令 | 结果 | 证据 |
|---|---|---|
| `python3 schemas/trial.py` | ... | ... |

## 4. CLI 参数契约 — [PASS / FAIL]

| 命令 | 结果 | 证据 |
|---|---|---|
| `<command> --help` | ... | ... |

## 5. 问题与最小修复建议

| 严重度 | 文件或命令 | 证据与影响 | 给 Claude 的最小修复建议 |
|---|---|---|---|
| P0/P1/P2 | ... | ... | ... |

## 6. 结论

- P0：N；P1：N；P2：N。
- 验收：通过 / 需 Claude 修复后复测。
```

不要在报告中声称 mock flow 是纯 Python 验证；mock 集成闭环属于单独的 CLI 验证范围。
