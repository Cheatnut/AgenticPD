# AgenticPD CLI 使用与验证指南

所有命令从 `flow/agenticpd/` 目录运行。命令分为纯 Python、mock、真实 ORFS 三类；不要把 mock 结果写成真实实验结论。

## 命令边界

| 类别 | 是否调用 LLM | 是否调用 ORFS | 是否写入运行产物 |
|---|---:|---:|---:|
| `make test`、数据模型自检、`--help` | 否 | 否 | 否（临时测试目录除外） |
| `trial_inspect.py`、`--list`、`clean.py --dry-run` | 否 | 否 | 否 |
| `main.py --parse-only` | 否 | 否 | 是，创建 session 并写入配置快照 |
| `--mock-llm --mock-orfs` | mock | mock | 是，写入 session |
| `--baseline-only`、正常优化、trial 复现、checkpoint fork 验证 | 视命令而定 | 是 | 是 |

真实 LLM 优化需要 `.env` 中的 API key；checkpoint fork 验证不调用 LLM。不要读取、打印或提交 `.env`。

## 1. 纯 Python 验证

以下是提交前的最低验证，不调用 LLM、ORFS、网络或 PDK：

```bash
make test
python3 schemas/trial.py
python3 managers/checkpoint_manager.py
python3 managers/trial_manager.py
python3 main.py --help
python3 tools/trial_inspect.py --help
python3 tools/trial_reproduce.py --help
python3 tools/clean.py --help
python3 tools/checkpoint_fork_verify.py --help
```

`make test` 的测试数会随项目演进变化，应以实际输出的 `OK` 为准。`--help` 仅验证 CLI 参数契约，不启动业务执行。

## 2. 无 EDA 的查询与检查

```bash
# 解析既有 variant 的 QoR；只读 ORFS 已有报告。
python3 main.py --parse-only agenticpd_baseline

# 列出某设计的 session；列出最新 session 的 trial。
python3 tools/trial_inspect.py --sessions sky130hd gcd
python3 tools/trial_inspect.py --list sky130hd gcd

# 查看单个 trial 和逐阶段记录。
python3 tools/trial_inspect.py <trial-id> --stages

# 列出可复现 trial，不启动复现。
python3 tools/trial_reproduce.py --runs-dir runs/sky130hd_gcd/<session> --list

# 仅预览清理范围，不删除任何文件。
python3 tools/clean.py sky130hd gcd --dry-run
```

`--parse-only` 依赖已有 ORFS 报告；报告不存在或 QoR 不完整时会以非零退出。`trial_inspect.py` 的 `--list`、`--latest`、`--failed` 和 `--sessions` 均为只读查询。

## 3. Mock 优化闭环

```bash
python3 main.py --mock-llm --mock-orfs --design gcd --iterations 3
python3 main.py --mock-llm --mock-orfs --design gcd --baseline-only
python3 main.py --mock-llm --mock-orfs --design gcd --resume latest
```

mock 模式会创建 session、trial、树和日志，用于检查优化编排与持久化。它不执行 OpenROAD，生成的 QoR 是合成值，不能用于性能结论、阶段验收或真实 QoR 对比。

常用运行参数：

| 参数 | 作用 |
|---|---|
| `--iterations N` | baseline 之外的优化轮数 |
| `--platform PLATFORM`、`--design DESIGN` | 覆盖默认目标 |
| `--timeout SEC` | 单次 ORFS 命令超时 |
| `--wns-tol PS`、`--tns-tol PS` | 通用优化器的 QoR 比较容差 |
| `--resume [RUN_DIR]` | 恢复指定 session；省略路径时恢复最新 session |
| `--log-level LEVEL` | `DEBUG` 会输出完整 prompt，日志处理时仍须遵守密钥与路径规则 |

## 4. 真实 ORFS 运行

以下命令会调用 ORFS；运行前确认 ORFS 环境、platform/design 配置、磁盘空间和目标 variant 范围。

```bash
# 真实 baseline；不调用 LLM。
python3 main.py --baseline-only --platform sky130hd --design gcd

# Mock LLM + 真实 ORFS；用于零 token 的执行链验证。
python3 main.py --mock-llm --platform sky130hd --design gcd --iterations 2

# 真实 LLM + 真实 ORFS；需要有效的 .env 配置。
python3 main.py --platform sky130hd --design gcd --iterations 2
```

通用优化器的 baseline cache 位于 `runs/<platform>_<design>/.baseline/`；ORFS 的共享 baseline variant 为 `agenticpd_baseline`。开始新运行前，运行器会清理旧的 AgenticPD variant，基线 variant 除外，因此不要把无关产物置于这些 variant 目录中。

## 5. Checkpoint fork 对照实验

```bash
python3 tools/checkpoint_fork_verify.py \
  --config configs/experiments/stage-c-checkpoint-fork.yaml
```

该命令不调用 LLM，但会真实执行 baseline、full restart 与兼容 checkpoint fork。它在 `runs/<platform>_<design>/checkpoint_fork/<时间戳>/` 写入：

- `report.json`：总 verdict、QoR、耗时和验收规则结果；
- `checkpoint_evidence.json`：checkpoint manifest 与校验结果；
- `trials.jsonl` 与 `iter-*/trial.json`：trial 和阶段审计记录；
- `experiment.log`：实验日志。

通过时，报告的 `verdict` 为 `PASS`，且 `acceptance_validation.passed` 为 `true`。应额外检查 session 的 `.log`、`.json`、`.jsonl` 不含绝对用户路径。

## 6. Trial 复现与可视化

```bash
# 真实 ORFS 复现：读取 trial 参数，运行新的 suffixed variant，并比较 QoR。
python3 tools/trial_reproduce.py <trial-id> \
  --runs-dir runs/sky130hd_gcd/<session>

# 生成优化树图；需要 session 内已有 tree.json。
python3 tools/visualize.py runs/sky130hd_gcd/<session>
```

trial 复现会真实运行 ORFS；`--export` 还会导出最佳结果。仅当 trial 状态为 `ok` 且保存了完整四阶段参数时才能复现。

## 7. 清理

`clean.py` 的清理粒度是 design，不是单个 trial：它会覆盖该 design 的多个 ORFS variant 与全部 session。日常只能使用 `--dry-run`；实际删除必须先取得用户明确授权，并核对输出目标。

```bash
python3 tools/clean.py --target sky130hd gcd --dry-run
```

工具保护名为 `base` 的 ORFS variant，但这不代表其他 variant 或 `runs/` session 可以无审查删除。

## 8. Session 目录

通用优化 session 使用如下结构：

```text
runs/
  sky130hd_gcd/
    .baseline/
      trial.json
    001_YYYYMMDD_HHMMSS/
      config_snapshot.json
      trials.jsonl
      tree.json
      agenticpd.log
      iter-<n>-<trial-id>/
        trial.json
```

checkpoint fork 实验使用独立的 `checkpoint_fork/<时间戳>/` session。ORFS 原始 artifact 位于 `flow` 下的 `logs/`、`reports/`、`results/`、`objects/`；session 中保存的是其可审计索引和派生证据。
