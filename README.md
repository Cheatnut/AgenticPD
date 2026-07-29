# AgenticPD — LLM 多智能体驱动的物理设计 QoR 优化框架

复现 *AgenticPD: Stage-Aware Agentic Framework for Physical Design QoR Optimization*：
1 个 Judge + 4 个 StageAgent（FP/PL/CTS/RT），迭代调整 ORFS 流程参数，
优化 WNS/TNS/Area/Power。核心机制：优化树 + 分支复用（Bef 阶段零成本继承）
→ 观测工具（E(n) + B(s)）→ 逐阶段流水线 → 自动树可视化。

## 目录结构

```
agenticpd/
├── main.py              # CLI 入口
├── config.py            # 全局配置：9 个 ParamSpec、FrameworkConfig、路径常量
├── optimizer.py         # 优化主循环（论文 §6 伪代码）
├── agents.py            # JudgeAgent + 4×StageAgent + ObservationTool
├── llm_interface.py     # LLM 客户端（DeepSeek/OpenAI，指数退避重试，MockLLMClient）
├── utils.py             # QoR 数据类、qor_is_better() 比较器、日志配置
├── optimization_tree.py # 有根优化树数据结构
├── orfs/                # ORFS 适配层：命令构建、报告解析、阶段执行、执行后端
├── schemas/             # 数据模型：TrialRecord、StageResult、CheckpointRef、FailureClass
├── managers/            # TrialManager（生命周期）、CheckpointManager（创建/验证/兼容性）
├── tools/               # CLI 工具：clean.py、visualize.py、trial_inspect.py
├── configs/experiments/ # 实验 YAML 配置
├── tests/               # 纯 Python 测试（无 EDA/LLM/网络依赖）
├── scripts/             # 辅助脚本（build_fixtures.py）
├── docs/                # 设计文档（中文）：八阶段计划、实验契约、使用指南、验收门模板
└── runs/                # 运行产物目录（不进 git）
```

## 依赖

- Python >= 3.10
- `openai`（LLM API 调用）
- ORFS 已可正常运行（`cd flow && make DESIGN_CONFIG=...` 能跑通）

```bash
pip3 install -r flow/agenticpd/requirements.txt
```

API key 配置：

```bash
cp flow/agenticpd/.env.example flow/agenticpd/.env  # 编辑填入真实 key
# 或 export DEEPSEEK_API_KEY=sk-...
```

## 使用

所有命令在 `flow/` 目录下执行：

```bash
cd flow

# Smoke test：只跑基线，不调 LLM
python3 agenticpd/main.py --baseline-only --design gcd

# 完整优化：基线 + N 次迭代
python3 agenticpd/main.py --design gcd --iterations 3

# 断点续跑
python3 agenticpd/main.py --resume latest

# 全 mock 调试（零 token / 零 EDA）
python3 agenticpd/main.py --mock-llm --mock-orfs --iterations 5

# 查看 trial
python3 agenticpd/tools/trial_inspect.py --list sky130hd gcd
python3 agenticpd/tools/trial_inspect.py <trial_id> --stages

# 清理产物（base 基线永不删除）
python3 agenticpd/tools/clean.py sky130hd gcd --dry-run
python3 agenticpd/tools/clean.py sky130hd gcd --yes

# 优化树可视化
python3 agenticpd/tools/visualize.py agenticpd/runs/<session>
```

常用选项：`--platform`、`--timeout`（秒）、`--wns-tol`/`--tns-tol`（ps）、`--log-level DEBUG`。

## 验证

```bash
cd flow/agenticpd
make test    # 纯 Python，无 EDA/LLM/网络依赖
```

## 详情见 docs/

| 文档 | 内容 |
|------|------|
| `docs/experiment-contract.md` | QoR 数据来源、评价函数、Trial 格式、预算定义、公平性约束 |
| `docs/usage/` | CLI 工具详细用法 |
| `docs/introduction/` | 系统介绍、架构说明 |
