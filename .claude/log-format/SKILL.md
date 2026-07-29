# AgenticPD 日志格式规范

本 Skill 定义项目中所有脚本的屏幕/文件日志的统一格式。遵循此规范可保证日志可 grep、可审计、可跨模块对齐。

## 快速参考模板

```
[MAIN] AgenticPD start: platform=<p> design=<d> run_dir=<d>
========== Iter #0 (Baseline, full run from ROOT) ==========
#0 [ORFS] make floorplan...
#0 [ORFS] floorplan done!(3.5s)
#0 [ORFS] floorplan QoR: 2_1_floorplan_ws_ps=-1154.1
[OPTIMIZER] ★ Global best updated to Iter #0: WNS=-1410.0ps TNS=-56400.0ps Area=5090.0um2 Power=8.0000mW
Trial abc12345 created (parent=None, branch=None)
#1 [Judge Agent] branch_node = root
#1 [Judge Agent] @FP Agent: [plan]
#1 [FP Agent] CORE_UTILIZATION: 35
#1 [ORFS] make floorplan...
[OPTIMIZER] Trial abc12345 finalized: status=ok elapsed=123.4s
=================== Final Results ===================
[OPTIMIZER] Global best: Iter #3
[MAIN] Optimization tree visualization saved to <path>
```

## 核心规则

### 1. 四级前缀体系

| 前缀 | 含义 | 示例 |
|------|------|------|
| `[MODULE]` | 全局事件，与具体迭代无关 | `[MAIN]`, `[OPTIMIZER]` |
| `#N [MODULE]` | 迭代 N 范围内的事件 | `#3 [FP Agent]`, `#0 [ORFS]` |
| `#N [MODULE] @STAGE` | Agent 决策/调度，跨 stage 引用 | `#1 [Judge Agent] @FP Agent:` |
| 无前缀 | 仅 trial 创建/复现等关键生命周期事件 | `Trial abc12345 created (parent=..., branch=...)` |

### 2. 模块标识（`[MODULE]`）

必须使用以下之一，新模块先在本文档注册：

- `[MAIN]` — 入口、启动参数、最终收尾
- `[OPTIMIZER]` — 搜索编排、全局最优更新、trial 收尾
- `[ORFS]` — ORFS 后端调用、stage 开始/完成、QoR 解析
- `[Judge Agent]` — 法官决策
- `[FP Agent]`, `[PL Agent]`, `[CTS Agent]`, `[RT Agent]` — 各阶段 Agent
- `[checkpoint]` — checkpoint 创建、验证、兼容性判断
- `[TrialManager]` — trial 持久化
- `[cp_fork_verify]` — Stage C 验证脚本

### 3. 迭代前缀（`#N`）

- 迭代号紧跟 `#`，与内容间有一个空格：`#3 [ORFS] make route...`
- Baseline（第 0 轮）使用 `#0`，不加特殊标记
- 非迭代上下文（启动、收尾、全局最优更新）不使用迭代前缀

### 4. 分隔线与标题

- 迭代开始：`========== Iter #N ==========`
- 含特殊说明：`========== Iter #0 (Baseline, full run from ROOT) ==========`
- 最终结果：`=================== Final Results ===================`
- 实验阶段：`=== Stage C Checkpoint-Fork Verification ===`
- 步骤标题：`STEP 1: BASELINE (per-stage: FP->PL->CTS->RT->finish)`

规则：
- 迭代级用 `==========`（10 个等号），标题两侧各一个空格
- 步骤级用 `---` 或 `----` 分隔线（`log.info("-" * 50)`）
- 标题全部英文，大写可读

### 5. 关键事件格式

**Trial 创建**
```
Trial <8-char-id> created (parent=<id|None>, branch=<stage|None>)
```

**Trial 收尾**
```
[OPTIMIZER] Trial <8-char-id> finalized: status=<ok|failed> elapsed=<Xs>
```

**全局最优更新**
```
#N [OPTIMIZER] ★ Global best updated to Iter #N: WNS=<X>ps TNS=<X>ps Area=<X>um2 Power=<X>mW
```

**ORFS 后端调用**
```
#N [ORFS] make <stage|all>...
#N [ORFS] <stage> done!(<Xs>)
#N [ORFS] <stage> QoR: <key>=<value>, ...
#N [ORFS] <stage> failed (exit=<code>, elapsed=<Xs>)
```

**Agent 参数输出**
```
#N [<Stage> Agent] <PARAM_NAME>: <value>
```

### 6. QoR 数值格式

```
WNS=<X>ps  TNS=<X>ps  Area=<X>um2  Power=<X>mW
```

- WNS/TNS 单位 `ps`，保留 1 位小数
- Area 单位 `um2`，保留 1 位小数
- Power 单位 `mW`（`W * 1000`），保留 4 位小数
- 四项以两个空格分隔

### 7. 禁止项

- 禁止在日志中输出密钥、token、完整 API 请求头
- 禁止输出绝对用户路径（`/home/<user>/...`）；路径必须相对化或使用 `${PROJECT_ROOT}` 占位
- 禁止在 `[ORFS]` 之外输出原始 make 命令（含绝对路径）
- 禁止用 bare `print()` 替代 `logging` 模块；日志统一通过 `logging.getLogger(__name__)` 输出
- 禁止单行超过 200 字符

### 8. 实现要求

- 使用 Python `logging` 模块，level 按用途选择：`info`（关键事件）、`debug`（参数细节）、`error`（失败）、`warning`（非致命异常）
- logger 名称统一为模块级 `logging.getLogger(__name__)`
- 格式器使用 `"% (asctime)s [%(name)s] %(levelname)s: %(message)s"` 或等价简化版
- 所有脚本必须同时输出到 StreamHandler（stdout/stderr）和 FileHandler（session 目录下 `<name>.log`）

### 9. 新模块注册

新脚本如需自定义模块标识，先将标识和用途添加到上方"模块标识"列表中，再在代码中使用。标识命名规则：

- 功能模块用大写驼峰：`[TrialManager]`, `[CheckpointManager]`
- 验证/工具脚本用小写下划线：`[cp_fork_verify]`, `[trial_inspect]`
- Agent 保持 `[<Stage> Agent]` 格式
