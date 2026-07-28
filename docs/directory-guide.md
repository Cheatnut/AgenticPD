# AgenticPD 目录结构与文件分工详解

> 从 README.md 拆分，原文在 2.1 ~ 2.11 节。

## 1. 目录结构

### 1.1 根目录 — 入口与规范

| 文件 | 职责 |
|------|------|
| `AGENTS.md` | Codex 工程规范（与 CLAUDE.md 内容完全一致） |
| `CLAUDE.md` | Claude 工程规范（与 AGENTS.md 内容完全一致） |
| `README.md` | 本文件 —— 项目说明、使用指南、架构文档 |
| `Makefile` | 验证入口：`make test`（纯 Python，无 EDA/LLM 依赖） |
| `.env` | API key（不提交，本地环境变量） |
| `.env.example` | `.env` 模板（不含真实 key，供参考） |
| `.gitignore` | 忽略 runs/、.env、pycache 等 |
| `requirements.txt` | Python 依赖（目前仅 `openai`） |
| `environment_manifest.json` | 环境版本快照（OpenROAD 版本、Python 版本等） |
| `trial.schema.json` | TrialRecord 的 JSON Schema 定义（draft 2020-12） |

### 1.2 核心模块（根目录 `.py`）

| 文件 | 行数 | 职责 |
|------|------|------|
| `main.py` | 208 | CLI 入口：`--baseline-only` / `--mock-llm` / `--resume` / `--iterations N`，初始化 LLM/Runner/Optimizer，启动优化循环 |
| `config.py` | 361 | **全局配置唯一来源**。定义 9 个可调参数（`ParamSpec`，含类型/范围/默认值/影响阶段）、`FrameworkConfig`（路径/超参/LLM 设置）、全局路径常量 |
| `optimizer.py` | 501 | **优化主循环**。实现论文 §6 伪代码：建树 → ObservationTool → Judge 决策 → 分支 → 逐阶段流水线 → 记录历史。阶段 C 接入 `TrialManager` |
| `agents.py` | 573 | **Agent 层**。`JudgeAgent`（全局导航，选分支节点和阶段）、`StageAgent ×4`（FP/PL/CTS/RT 各阶段参数生成）、`ObservationTool`（计算 E(n) 探索平衡度 + B(s) 阶段瓶颈） |
| `llm_interface.py` | 184 | **LLM 客户端**。DeepSeek/OpenAI-compatible API 调用，含指数退避重试、JSON 回喂重问、`MockLLMClient`（零 token 调试） |
| `utils.py` | 396 | **工具集**。`QoR` 数据类（从 6_report.json / rpt 解析）、`qor_is_better()` 时序优先比较器、原子 JSON 写入、日志配置 |
| `optimization_tree.py` | 266 | **优化树 T**。有根树数据结构：节点增删查、E(n) 分支计数、Bef 阶段参数/QoR 继承、JSON 序列化/反序列化 |

### 1.3 orfs/ — ORFS 适配层（阶段 C 拆分）

| 文件 | 行数 | 职责 |
|------|------|------|
| `orfs/command.py` | 96 | **make 命令构建**。将 `{stage: {param: value}}` 转为 `make DESIGN_CONFIG=... FLOW_VARIANT=... NAME=value ...` 命令行。处理三类参数：普通 make 变量、FastRoute TCL 生成、GRT 参数渲染 |
| `orfs/parser.py` | 150 | **报告解析**。`parse_qor()` 从 6_report.json（优先）或 rpt/log 正则兜底提取 WNS/TNS/Area/Power；`parse_stage_qor()` 提取各阶段中间时序；`detect_failed_stage()` 定位崩溃阶段。定义 `STEP_JSON_SEQUENCE`、`STAGE_QOR_SOURCES` 等 ORFS 常量 |
| `orfs/runner.py` | 199 | **阶段执行**。`execute_stage()` 单阶段 make（clean → make → 解析，返回 `StageResult`）；`execute_flow()` 完整 RTL→GDS；`run_make()` 委托给 `ExecutionBackend` |
| `orfs/backend.py` | 163 | **执行后端抽象**。`LocalBackend`（subprocess + 进程组超时清理）；`SlurmBackend`（stub，接口 sbatch/squeue/scancel，待服务器部署） |
| `orfs/interface.py` | 366 | **ORFS 编排器**。`ORFSRunner`（绑定 `FrameworkConfig`，提供 `run_flow/run_stage/run_finish/copy_parent_results/export_best`）；`MockORFSRunner`（零 EDA 调试）；`RunResult` 数据类 |
| `orfs/__init__.py` | 4 | 包标记 |
| `orfs_interface.py` | 16 | **向后兼容重导出层**。`from orfs_interface import ORFSRunner, RunResult` 保持旧 import 路径有效 |

### 1.4 schemas/ — 数据模型（阶段 B）

| 文件 | 行数 | 职责 |
|------|------|------|
| `schemas/trial.py` | 465 | **四个核心数据类**。`FailureClass`（5 种失败类型枚举）、`StageResult`（单阶段执行记录，elapsed/exit_code/stage_qor/failure）、`CheckpointRef`（可恢复存档点，artifact manifest + SHA-256）、`TrialRecord`（一次完整 RTL→GDS 运行，lineage/QoR/stage_results/checkpoint）。含 JSONL 追加/读取工具 |
| `schemas/__init__.py` | 13 | 包导出 |

### 1.5 managers/ — 管理层（阶段 B）

| 文件 | 行数 | 职责 |
|------|------|------|
| `managers/trial_manager.py` | 263 | **Trial 生命周期管理**。`TrialManager`：`create()` 生成 UUID + 写入 `runs/<id>/trial.json`；`update()` 覆盖 + 追加 `trials.jsonl` 索引；`get/list_all/list_by_status/latest` 查询。原子写入（.tmp → os.replace） |
| `managers/checkpoint_manager.py` | 392 | **Checkpoint 生命周期管理**。`CheckpointManager`：`create()` 扫描 ORFS 产物 + SHA-256 哈希 → `CheckpointRef`；`verify()` 完整性校验；`is_compatible()` 基于 `ParameterSpec.affects` 的阶段感知兼容性判断；`param_hash()` 确定性参数哈希 |

### 1.6 tools/ — CLI 工具

| 文件 | 行数 | 职责 |
|------|------|------|
| `tools/clean.py` | 224 | **产物清理**。删除指定 platform/design 的所有 variant（除 base）+ 匹配的 runs/ 会话目录。`--dry-run` 预览、`--yes` 跳过确认 |
| `tools/visualize.py` | 384 | **优化树可视化**。从 `tree.json` + `trials.jsonl`（兼容旧 `history.json`）生成 PNG（5 层布局，绿色基线路径 + 红色最佳路径） |
| `tools/trial_reproduce.py` | — | **Trial 复现**。从 TrialRecord 提取完整参数重跑 ORFS，对比原始/复现 QoR。`--list` / `<trial_id>` `--runs-dir` |
| `tools/trial_inspect.py` | 291 | **Trial 查看器**。`--sessions` / `--list` / `--latest` / `--failed` + `<platform> <design> [seq]`，或 `<trial_id>` 按 ID 全局搜索 |

### 1.7 configs/ — 实验配置

| 文件 | 职责 |
|------|------|
| `configs/experiments/smoke.yaml` | 阶段 A smoke test 完整声明（设计层次、环境版本、预算/seed、参数空间 v1、evaluator v1、验收条件）。每次新实验复制此文件为 `<日期>-<设计>-<方法>.yaml` |

### 1.8 docs/ — 设计文档（中文）

| 文件 | 职责 |
|------|------|
| `docs/experiment-contract.md` | 实验契约：QoR 数据来源、评价函数（时序优先比较器）、Trial 记录格式、预算定义、实验公平性约束 |
| `docs/问题.txt` | 开发过程中的未解决问题与已知限制 |

### 1.9 tests/ — 测试（纯 Python，无 EDA/LLM/网络依赖）

| 文件 | 行数 | 用例 | 覆盖内容 |
|------|------|------|---------|
| `tests/test_qor.py` | 137 | 21 | QoR JSON 解析（已知正确值核对）、rpt/log fallback（容差校验）、`qor_is_better()` 比较器优先级、dataclass 行为 |
| `tests/test_schemas.py` | 265 | 17 | TrialManager + CheckpointManager 集成：六问全覆盖（lineage/params/elapsed/failure/artifact/QoR）、JSONL 去重、corrupt 行跳过 |
| `tests/test_fixtures.py` | 121 | 18 | 真实 fixture 验证：ok_trial（gcd baseline 完整记录）、ok_checkpoint（3 个真实 ORFS 文件 SHA-256 已验证）、failed_trial（PL crash 模拟） |
| `tests/fixtures/legacy_run/` | — | — | 阶段 A gcd smoke test 只读回归证据（6_report.json/rpt/log + expected_qor.json） |
| `tests/fixtures/stage_b/` | — | — | 阶段 B 真实 fixture（ok_trial.json / ok_checkpoint.json / failed_trial.json） |

### 1.10 scripts/ — 辅助脚本

| 文件 | 职责 |
|------|------|
| `scripts/build_fixtures.py` | 从 ORFS 运行产物构建 test fixture（TrialRecord + CheckpointRef），一次性使用 |

### 1.11 其他目录

| 目录 | 职责 |
|------|------|
| `attachments/` | 文档用图片（架构图、优化树截图） |
| `runs/` | 运行产物（不进 git）。每个 `main.py` 调用创建一个 `<时间戳>/` 会话目录，内含 `trials.jsonl`（索引）、`<trial_id>/trial.json`（TrialRecord）、`agenticpd.log`、`history.json`、`tree.json`。`clean.py` 可一键清理 |
