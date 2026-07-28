# AgenticPD CLI 验证指南

所有命令从 `flow/agenticpd/` 目录运行。加 `--mock-llm --mock-orfs` 即可零 token、零 EDA 秒级完成。

---

## 快速验证闭环（8 条命令）

```bash
# 1. 全 mock 优化，3 轮迭代
python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 3

# 2. 列出最新会话的所有 trial（自动取最新 session）
python3 tools/trial_inspect.py --list sky130hd gcd

# 3. 查看某个 trial 的完整细节（含 per-stage）— 全局搜索，无需指定会话
python3 tools/trial_inspect.py <trial-id> --stages

# 4. 预览清理范围（--dry-run，不删任何东西）
python3 tools/clean.py sky130hd gcd --dry-run

# 5. 生成优化树可视化 PNG
python3 tools/visualize.py runs/sky130hd_gcd/$(ls -t runs/sky130hd_gcd/ | head -1)

# 6. 再跑一次——验证基线缓存命中
python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 2

# 7. 列出可复现的 trial
python3 tools/trial_reproduce.py \
  --runs-dir runs/sky130hd_gcd/$(ls -t runs/sky130hd_gcd/ | head -1) --list

# 8. 清理（删除 runs/sky130hd_gcd/ 和 ORFS variant 产物）
python3 tools/clean.py sky130hd gcd --yes
```

---

## 完整命令参考

### main.py — 优化入口

| # | 命令 | 功能 | 预期现象 |
|---|------|------|---------|
| 1 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 3` | 全 mock 优化，3 轮迭代 | `Iter #0 (Baseline)` → 缓存基线 → `Iter #1/2/3`，每轮显示法官决策 + 各阶段参数 + 最终 QoR。生成 PNG。 |
| 2 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 5 --log-level DEBUG` | 同上，DEBUG 日志 | 同上 + `agenticpd.log` 包含完整 prompt 文本（虽然是 mock 数据）。 |
| 3 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --baseline-only` | 只跑基线，不调 LLM | `Iter #0 (Baseline)` → 缓存到 `.baseline/` → 退出。零 LLM 调用。 |
| 4 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --platform nangate45` | 换平台运行 | 产物落在 `runs/nangate45_gcd/` 而非 `sky130hd_gcd/`。 |
| 5 | 跑两次：`python3 ./main.py --mock-llm --mock-orfs --design gcd --iterations 2` | 基线缓存验证 | **第一次**：`Iter #0` + `Baseline cached to`。**第二次**：`Baseline cache hit (skipping ORFS run)` + 直接从 `Iter #1` 开始。 |
| 6 | `python3 ./main.py --mock-llm --mock-orfs --design gcd --resume latest` | 从最新会话断点续跑 | `[OPTIMIZER] --resume: loaded N history entries` → 接着上次迭代号继续。 |

### tools/trial_inspect.py — Trial 查看器

| # | 命令 | 功能 | 预期现象 |
|---|------|------|---------|
| 7 | `--list <platform> <design> [seq]` | 列出某次会话的全部 trial（seq 省略则取最新） | 表格：Trial ID / Status / QoR / Elapsed。旧 trial 显示 `[no params]`。 |
| 8 | `<trial_id>` | 按 ID 全局搜索，查看单个 trial 详情 | 自动扫描所有会话，显示 parent lineage / param_diff / elapsed / QoR。 |
| 9 | `<trial_id> --stages` | 加 per-stage 明细 | 以上内容 + 每个阶段的 status / 耗时 / 中间时序值。 |
| 10 | `--latest <platform> <design>` | 最新会话中的最新 trial | 同上（#8），自动定位。 |
| 11 | `--failed <platform> <design>` | 跨所有会话列出失败 trial | 只显示 status=failed 的 trial。mock 模式不会产生失败。 |
| 11b | `--sessions <platform> <design>` | 列出某设计的所有会话 | 显示会话目录名 + 每个会话的 trial 数。 |

### tools/trial_reproduce.py — Trial 复现

| # | 命令 | 功能 | 预期现象 |
|---|------|------|---------|
| 12 | `--runs-dir <会话> --list` | 列出可复现的 trial | 显示有完整 `params` 的 trial。`[no params]` = 旧 trial，无法复现。 |
| 13 | `<trial_id> --runs-dir <会话>` | 用真实 ORFS 复现 | 提取参数 → `run_flow()` → 对比原始/复现 QoR 的 Δ。**⚠ 会真实跑 ORFS，不是 mock。** |

### tools/clean.py — 产物清理

| # | 命令 | 功能 | 预期现象 |
|---|------|------|---------|
| 14 | `sky130hd gcd --dry-run` | 预览将被删除的内容 | 列出 ORFS 产物目录 + `runs/sky130hd_gcd/` 的文件数/大小。`base directory will NOT be affected.` |
| 15 | `sky130hd gcd --yes` | 跳过确认直接删除 | 删除所有 `agenticpd_iter*` variant + 整个 `runs/sky130hd_gcd/`。`base` 受保护。 |
| 16 | `sky130hd gcd`（不加 `--yes`） | 交互式删除 | 同上列表 → 提示 `Delete N directories? [y/N]`。默认 N（安全）。 |

### tools/visualize.py — 树可视化

| # | 命令 | 功能 | 预期现象 |
|---|------|------|---------|
| 17 | `runs/sky130hd_gcd/<会话>/` | 生成优化树 PNG | `Tree image saved to .../optimization_tree.png`。绿色 = 基线路径，红色 = 最优路径。 |

### schemas/trial.py — 数据模型自检

| # | 命令 | 功能 | 预期现象 |
|---|------|------|---------|
| 18 | `python3 schemas/trial.py` | 运行内置自测 | `ALL OK`。纯 Python，零依赖。 |

### make test — 完整测试套件

| # | 命令 | 功能 | 预期现象 |
|---|------|------|---------|
| 19 | `make test` | 运行全部 56 个单元测试 | `Ran 56 tests in ...s — OK`。不需要网络、LLM、EDA。 |

---

## 验证后的目录布局

```
runs/
  sky130hd_gcd/
    .baseline/
      trial.json                     ← 共享基线缓存
    001_20260727_230000/             ← 第一次实验（序号 _ 时间戳）
      iter-1-xxxxxxxx/               ← 第一轮优化
        trial.json
      iter-2-yyyyyyyy/
        trial.json
      trials.jsonl                   ← 全局索引
      tree.json
      optimization_tree.png
      agenticpd.log
      config_snapshot.json
    002_20260727_231500/             ← 第二次实验（基线缓存命中）
      iter-1-zzzzzzzz/               ← 从 iter-1 开始（无 iter-0）
        trial.json
      ...
```

> **说明**：会话目录使用 `NNN_YYYYMMDD_HHMMSS` 格式，序号从 001 开始递增。
> 快速验证命令中 `ls -t runs/sky130hd_gcd/ | head -1` 会取到最新（修改时间最近）的会话。
