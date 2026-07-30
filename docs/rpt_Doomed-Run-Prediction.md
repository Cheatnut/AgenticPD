# Doomed-Run-Prediction 详细研读报告

> **论文：** *Doomed Run Prediction in Physical Design by Exploiting Sequential Flow and Graph Learning*  
> **核心判断：**这是一篇“早停预测器”论文，不是 Agentic optimizer。它证明早期 placement/CTS 的动态网表状态能够预测 post-route TNS，但没有真正完成“是否应终止”的风险决策层。

## 1. 研究问题

并行 PPA exploration 中，大量 PD run 到最后才发现 timing 无法收敛。若能在 routing 前预测最终 TNS，便可停止无望运行，把算力转给更有希望的候选。

论文把问题简化为 supervised regression：

```text
三个早期 PD stage 的 netlist graph
        ↓ shared GNN
三个 64 维 graph vector
        ↓ LSTM
predicted post-route TNS
        ↓ 与目标阈值比较（论文未具体实现）
continue / stop
```

## 2. 方法

### 2.1 为什么用 GNN

不同设计和不同阶段的 netlist 节点数、边数均不同，普通定长向量难以直接表示。共享 GNN 使用 6 个 cell-level 特征和连接关系，把任意规模动态图编码成 64 维 graph vector（PDF 第 4–5 页，Table I、Figure 6）。

六个特征覆盖：

- worst slack；
- input/output slew；
- driving-net switching power；
- cell internal power；
- leakage power。

它们均可从 timing/power report 与 technology file 确定性提取，适合作为 Agent 平台的结构化 observation。

### 2.2 为什么用 LSTM

Detailed place、opt. place、opt. CTS 不是三个独立快照。后阶段网表由前阶段经过 buffer 插入、删除和逻辑优化演化而来。LSTM 把三个表示当成长度 3 的时间序列，学习跨阶段依赖（PDF 第 2–3、6 页）。

### 2.3 两种预测方式

| 方式 | 输入 | 优点 | 代价 |
|---|---|---|---|
| Per-stage NN | 单个阶段的 GNN vector | 可更早预测 | 精度较低 |
| Sequential LSTM | 三阶段 GNN vector | 精度最高 | 必须运行到 opt. CTS |

这形成实际部署的成本—精度曲线，而不是“越准越好”的单目标问题。

## 3. 数据集与评价

### 3.1 数据生成

7 个设计都在 TSMC 28 nm 下运行。每个设计先找到 `WNS>0` 的最大综合频率，再向上收紧 10 档频率；每档配 5 个 density，共 50 run，合计 350 run（PDF 第 3 页）。

训练/测试按 design 划分：

- 训练：JPEG、LEON、ECG、LDPC、TATE；
- 测试：AES、VGA。

按 design 切分优于随机按 run 切分，因为后者会把同一 netlist 的近邻配置泄漏到训练与测试两侧。

### 3.2 结果

All-stage LSTM：

| Design | RMSE | NRMSE | CC |
|---|---:|---:|---:|
| AES | 0.47 ns | 5.4% | 0.91 |
| VGA | 9.34 ns | 5.2% | 0.95 |

在 VGA 上，detailed-place NRMSE 为 `12.6%`，opt-place 为 `8.3%`，opt-CTS 为 `6.5%`，LSTM 为 `5.2%`。晚阶段信息和跨阶段历史都提高了精度（PDF 第 8 页，Table III）。

## 4. 创新点

1. 把 PD flow 显式建模为阶段序列，而不是只看一个静态快照。
2. 在多个动态网表阶段共享同一个 GNN encoder。
3. 同时提供 per-stage 与 sequential prediction，展示早停时机与精度的取舍。
4. 用 design-level holdout 测试未见网表。

## 5. 关键局限

### 5.1 “预测 TNS”不等于“正确早停”

论文标题强调 doomed-run prediction，但实验只报告 RMSE、NRMSE 和 correlation。生产系统真正需要的是：

- doomed threshold；
- false positive rate：误杀本可成功的 run；
- false negative rate：放过真正 doomed run；
- 提前停止节省的 CPU-hour；
- 因误判损失的潜在最优解；
- 不确定度与拒绝预测机制。

尤其 false positive 的代价很高：一条被终止的 run 不能恢复最终真值。没有成本敏感分类或 conformal prediction（保形预测）之类的置信控制，不能直接把回归输出接到 `kill job`。

### 5.2 测试规模小

只有 AES/VGA 两个未见设计、单一 TSMC 28 nm、单一工具流程。尚不能证明跨：

- technology node；
- standard-cell library；
- PD tool；
- frequency/density 之外的参数组合；
- 设计家族。

### 5.3 数据分布人为

训练数据只 sweep frequency 和 density。真实 Agentic optimization 还会改变 floorplan、placement effort、CTS 参数和 routing strategy，产生更复杂的分布漂移。部署时需要持续检测 out-of-distribution（分布外）输入。

### 5.4 t-SNE 证据有限

Figure 8 中同设计成簇，只说明表示强烈保留 design identity，不直接证明它捕获了可迁移的 timing mechanism。更有说服力的 ablation 应比较：

- 去掉 graph connectivity；
- 去掉某类节点特征；
- separate GNN vs shared GNN；
- last-stage only vs LSTM；
- GNN vs hand-crafted graph statistics。

## 6. 对 Agentic EDA 平台的价值

它适合成为 execution guard（执行守卫）：

```text
Agent 生成候选
  → 执行到 checkpoint
  → 解析 graph/report features
  → 预测 final QoR + uncertainty
  → 高置信 doomed：暂停并保留 checkpoint
  → 不确定：继续到下一 checkpoint
  → 高希望：提高调度优先级
```

不要直接删除或不可恢复地终止。第一版应采用：

1. `pause/low-priority` 代替 `kill`；
2. 保存 checkpoint 和所有 observation；
3. 设置双阈值：`safe_stop` 与 `must_continue`，中间区域继续观察；
4. 记录每次早停的 counterfactual audit sample，随机让少量 doomed 候选跑到底以估计误判；
5. 让预测器输出分布或置信区间，而不是单点 TNS。

## 7. 可复现路线

### Phase 1：无 GNN baseline

- 从 ORFS/OpenROAD 每阶段报告抽取 WNS/TNS、slew、power、cell count、buffer count、congestion。
- 用 XGBoost/LightGBM 预测 post-route TNS。
- 按 design holdout，建立早停成本曲线。

### Phase 2：图表示

- 从 OpenDB 导出 instance/net graph；
- node feature 先实现论文 6 项；
- GraphSAGE 两层、64 维、global mean pooling；
- 比较共享 encoder 与分阶段 encoder。

### Phase 3：多 checkpoint 序列

- `place_opt`、`cts`、`global_route` 三个 checkpoint；
- 先比较 MLP concat、GRU、LSTM，不预设 LSTM 必胜；
- 加入不确定度估计和 OOD detector。

### Phase 4：接入 Agent

- 预测器只提供 `risk_score`、`expected_final_qor`、`confidence`；
- Judge Agent 决定继续、分支、降优先级或暂停；
- 所有决策保留证据和可恢复 checkpoint。

## 8. 最终评价

论文提供了一个很有用的系统部件：从早期、多阶段物理状态预测最终 QoR。其方法比只看当前 WNS 更能利用网表结构和阶段演化。但“5.2% NRMSE”不能直接转化为安全早停承诺。你的平台应把它扩展成带不确定度、成本敏感阈值、审计样本和可恢复暂停的资源调度策略。

## 9. 迭代记录

| 日期 | 触发问题 | 修改章节 | 修改性质 |
|---|---|---|---|
| 2026-07-23 | 首次全文研读 | 全文 | 建立方法、证据、局限与 Agent 平台映射基线 |

