# AgenticPD CLI 使用与验证指南

所有命令从 `flow/agenticpd/` 目录运行。命令分为纯 Python、mock、真实 ORFS 三类；不要把 mock 结果写成真实实验结论。

## 命令边界

| 类别 | 是否调用 LLM | 是否调用 ORFS | 是否写入运行产物 |
|---|---:|---:|---:|
| `make test`、`--help` | 否 | 否 | 否（临时测试目录除外） |
| `trial_inspect.py`、`clean.py --dry-run` | 否 | 否 | 否 |
| `main.py --parse-only` | 否 | 否 | 是，创建 session 并写入配置快照 |
| `--mock-llm --mock-orfs` | mock | mock | 是，写入 session |
| `--baseline-only`、正常优化、trial 复现、checkpoint fork 验证 | 视命令而定 | 是 | 是 |

真实 LLM 优化需要 `.env` 中的 API key；checkpoint fork 验证不调用 LLM。不要读取、打印或提交 `.env`。

## 1. 纯 Python 验证

提交前的最低验证，不调用 LLM、ORFS、网络或 PDK：

```bash
make check
make test
python3 main.py --help
python3 multi_agent_gwtw.py --help
python3 tools/trial_inspect.py --help
python3 tools/trial_reproduce.py --help
python3 tools/clean.py --help
python3 -m tools.session_visualize --help
python3 tools/checkpoint_fork_verify.py --help
```

`make check` 只做语法编译与 CLI 契约检查；`make test` 需要本地 `tests/`（按项目约定不进 Git）。

## 2. 原始多 Agent 优化（`main.py`）

```bash
# 全 mock 调试：零 token、零 EDA，秒级完成
python3 main.py --mock-llm --mock-orfs --iterations 5

# 只跑真实 ORFS baseline，不调 LLM
python3 main.py --baseline-only --platform sky130hd --design gcd

# 真实多 Agent 优化（需要 DEEPSEEK_API_KEY）
python3 main.py --platform sky130hd --design gcd --iterations 3

# 恢复同一设计最近一次 session
python3 main.py --platform sky130hd --design gcd --resume latest

# 只解析已有 ORFS variant 的 QoR（零 token、零 EDA）
python3 main.py --parse-only base
```

参数说明：`--iterations`（基线后的搜索轮数）、`--platform`/`--design`、`--timeout`、`--wns-tol`/`--tns-tol`（QoR 比较容差）、`--mock-llm`/`--mock-orfs`、`--baseline-only`、`--parse-only VARIANT`、`--resume [RUN_DIR]`、`--log-level`。所有显式传入的选项覆盖 `config.py` 默认值。

## 3. Doomed/GWTW Demo（`multi_agent_gwtw.py`）

实验配置位于 `configs/experiments/multi-agent-gwtw-demo.yml`：

```bash
# 离线闭环：与真实 Demo 相同的编排，只替换 LLM/ORFS
python3 multi_agent_gwtw.py \
  --config configs/experiments/multi-agent-gwtw-demo.yml \
  --mock-llm --mock-orfs

# 真实闭环：真实 Judge + 四个 Stage Agent + 真实 ORFS
python3 multi_agent_gwtw.py \
  --config configs/experiments/multi-agent-gwtw-demo.yml
```

终端完成摘要给出 `total_trials`、`budget_remaining`、`errors` 与 `finish_trials`；该摘要只证明控制流结束，真实结果以 finish Trial 的 `final_qor` 与对应 ORFS post-route 报告为准。

## 4. Checkpoint Fork 对照验证（`tools/checkpoint_fork_verify.py`）

```bash
python3 tools/checkpoint_fork_verify.py \
  --config configs/experiments/checkpoint-fork.yaml
```

该工具执行 baseline、不兼容参数的阴性对照与兼容参数的 fork/full-restart 对照，并把验收结论写入 `runs/<platform>_<design>/checkpoint_fork/<时间戳>/report.json`。会启动真实 ORFS。

## 5. 查看、复现与清理

```bash
# 列出某个设计的 Trial
python3 tools/trial_inspect.py --list sky130hd gcd

# 查看一个 Trial 的阶段结果
python3 tools/trial_inspect.py <trial_id> --stages

# 为已有 session 生成离线 HTML 可视化
python3 -m tools.session_visualize \
  runs/sky130hd_gcd/<session>

# 查看清理范围，不执行删除
python3 tools/clean.py sky130hd gcd --dry-run
```

可视化输出位于 `runs/.../<session>/visualization/index.html`，可直接用浏览器打开。复现（`trial_reproduce.py`）和清理（`clean.py`）可能启动真实 EDA 或删除运行产物，执行前先阅读 `docs/introduction/实验契约.md` 并核对目标。

## 6. Session 证据结构

每次运行写入独立目录：

```text
runs/<platform>_<design>/<session>/
├── config_snapshot.json
├── trials.jsonl
├── tree.json
├── traces/decisions.jsonl
├── iter-<N>-<trial_id>/trial.json
└── visualization/            # 仅 session_visualize 生成
```

`trials.jsonl` 是追加索引，读取时按 `trial_id` last-wins；单 Trial 的当前完整状态以 `trial.json` 为准。最终 QoR 的权威来源是 ORFS `logs/<platform>/<design>/<variant>/6_report.json`。
