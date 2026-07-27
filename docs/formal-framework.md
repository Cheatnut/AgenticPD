# AgenticPD 核心机制形式化整理

> 从 README.md 拆分，原文在 1.1 ~ 1.7 节。

## 1. AgenticPD 核心机制形式化整理

### 1.1 物理设计流程的形式化

#### 1.1.1 阶段与动作空间
设物理设计流程为有序阶段序列：

$$
\mathcal{S} = (\text{FP},\ \text{PL},\ \text{CTS},\ \text{RT})
$$

每个阶段 $s \in \mathcal{S}$ 拥有自己的参数空间（动作空间）$\Theta_s$，则完整流程的动作空间为笛卡尔积：

$$
\Theta_{\mathrm{PD}} = \Theta_{\text{FP}} \times \Theta_{\text{PL}} \times \Theta_{\text{CTS}} \times \Theta_{\text{RT}}
$$

一次完整流程由一个动作元组唯一确定：

$$
\mathbf{a} = (a_{\text{FP}},\ a_{\text{PL}},\ a_{\text{CTS}},\ a_{\text{RT}}) \in \Theta_{\mathrm{PD}}
$$

其中 $a_s \in \Theta_s$ 表示在阶段 $s$ 选取的具体参数值。

#### 1.1.2 分支与前后继关系
由于阶段顺序固定，任意选定阶段 $b \in \mathcal{S}$ 可将流程划分为：

- **前置阶段**：$\mathrm{Bef}(b) = \{s \in \mathcal{S} \mid s \text{ 在 } b \text{ 之前}\}$
- **后置阶段**：$\mathrm{Aft}(b) = \{s \in \mathcal{S} \mid s \text{ 在 } b \text{ 之后}\}$

**示例**：若 $b = \text{CTS}$，则 $\mathrm{Bef(CTS)} = \{\text{FP},\text{PL}\}$，$\mathrm{Aft(CTS)} = \{\text{RT}\}$。

#### 1.1.3 QoR 度量
执行完整流程 $\mathbf{a}$ 后，获得后布线（post‑route）签核指标元组：

$$
Q(\mathbf{a}) = ( \text{WNS},\ \text{TNS},\ \text{Area},\ \text{Power} )
$$

其中 WNS（最差负时序裕量）和 TNS（总负时序裕量）为时序指标（越高越好），面积和功耗越低越好。所有优化反馈均基于该真实后布线结果，**不使用任何中间阶段代理指标**。

### 1.2 优化目标与迭代过程

给定迭代预算 $N$，优化器依次产生 $N$ 个完整流程动作 $\mathbf{a}_1, \mathbf{a}_2, \ldots, \mathbf{a}_N$，目标是最大化最优后布线 QoR（时序优先）：

$$
\max_{k \in \{1,\ldots,N\}} Q(\mathbf{a}_k)
$$

最终报告结果为历史最优候选 $\mathbf{a}^* = \arg\max_k Q(\mathbf{a}_k)$，其 QoR 已在迭代中测得，无需额外评估。

### 1.3 优化树与分支机制

#### 1.3.1 树结构定义
所有历史执行结果组织为一棵有根树 $\mathcal{T}$。根节点 $n_0$ 代表综合后的网表（PD 输入）。每次执行阶段 $s$ 时，创建一个节点：

$$
n_k^s = \big( a_k(s),\ Q_k(s) \big)
$$

其中 $a_k(s)$ 是该阶段采取的动作，$Q_k(s)$ 是该阶段执行后观测到的阶段性 QoR。  
从根到叶的每条完整路径（依次经过 FP、PL、CTS、RT）对应一个完整的动作元组 $\mathbf{a}_k$。

#### 1.3.2 分支操作
在迭代 $k$ 中，优化器选择一个已存在的中间节点 $\hat{n}$（位于某个阶段 $b \in \mathcal{S}$），从该节点出发启动新分支。则新分支：

- **复用** $\mathrm{Bef}(b)$ 阶段的所有结果（即继承祖先路径上的动作与 QoR），**成本为零**；
- **重新执行** $\{b\} \cup \mathrm{Aft}(b)$ 阶段，产生新的动作和新的节点。

新节点作为子树挂载到 $\hat{n}$ 下：

$$
\mathcal{T}_k = \mathcal{T}_{k-1} \ \cup \ \{ n_k^s \mid s \in \{b_k\} \cup \mathrm{Aft}(b_k) \}
$$

其中 $b_k$ 为第 $k$ 次迭代选定的分支阶段。特别地，若 $b_k = \text{FP}$，则从根节点分支，等价于从头运行全新流程。

> **说明**：分支机制避免了每次迭代都重复运行所有阶段，能将宝贵预算集中在有提升空间的后期阶段，实现"增量式"优化。

### 1.4 法官智能体（Judge Agent）

法官智能体由通用 LLM 引擎 $\mathcal{L}$、Prompt $\mathcal{P}_J$ 和Harness Skills $\mathcal{U}_J$ 构成：

$$
\text{Judge} = (\mathcal{L},\ \mathcal{P}_J,\ \mathcal{U}_J)
$$

#### 1.4.1 输入：优化历史
在迭代 $k$ 开始时，Harness向法官提供历史记录 $\mathcal{H}_k$：

$$
\mathcal{H}_k = \{\ (\hat{n}_i,\ b_i,\ \{Q_i(s)\}_{s \ge b_i})\ \}_{i=1}^{k-1}
$$

每个历史条目包含：第 $i$ 次迭代的分支起始节点 $\hat{n}_i$、分支阶段 $b_i$，以及该分支执行后各阶段（从 $b_i$ 到 RT）的 QoR。

#### 1.4.2 观测工具（Observation Tool）
Harness $\mathcal{U}_J$ 内置的观测工具根据 $\mathcal{H}_k$ 和当前树 $\mathcal{T}_k$，计算自适应概要 $\mathcal{A}_k$，包含两个关键信号：

- **探索平衡度** $E(n)$：每个节点 $n$ 被选为分支起点的次数，用于识别过探索/欠探索区域。
- **阶段瓶颈** $B(s)$：每个阶段的当前 QoR 与历史最优的差距，用于定位当前最薄弱的环节。

观测工具将这两个信号连同树结构快照组装成搜索状态概要(search state profile)，作为法官的观测输入（而非原始历史转储，以控制 token 开销）。

#### 1.4.3 决策输出
基于概要，法官产生决策：

$$
\mathcal{D}_k = (\hat{n}_k,\ b_k,\ \{ \text{hint}_s \}_{s \in \{b_k\}\cup \mathrm{Aft}(b_k)} )
$$

- $\hat{n}_k$：选定的分支节点（平衡探索与利用，由 $E(n)$ 引导）；
- $b_k$：选定的分支阶段（通常选择瓶颈最大的阶段 $B(s)$）；
- 为每个将要执行的下游阶段提供一条文本提示（hint），指导该阶段智能体如何调整参数。


### 1.5 阶段智能体（Stage Agent）

每个阶段 $s$ 拥有一个专属的阶段智能体：

$$
\text{StageAgent}_s = (\mathcal{L},\ \mathcal{P}_s,\ \mathcal{U}_s)
$$

其中 $\mathcal{P}_s$ 是该阶段的系统提示（描述职责、参数范围、优化目标等），$\mathcal{U}_s$ 是该阶段的 **PD 技能**，负责与后端工具交互（执行阶段、返回 QoR）。

#### 1.5.1 执行上下文
在法官选定分支 $b_k$ 后，依次执行 $s \in \{b_k\} \cup \mathrm{Aft}(b_k)$。对于每个阶段 $s$，Harness为它构建上下文：

$$
\text{ctx}_s = \big( \{Q_k(i)\}_{i \in \mathrm{Bef}(s)},\ e_s,\ \text{hint}_s \big)
$$

- $\{Q_k(i)\}_{i \in \mathrm{Bef}(s)}$：当前分支中上游阶段（已完成）的 QoR 结果；
- $e_s$：该阶段跨迭代的历史经验（如之前尝试过的参数及结果）；
- $\text{hint}_s$：法官专门给该阶段的提示。

#### 1.5.2 动作生成与执行
阶段智能体根据 $\text{ctx}_s$ 推理，输出一个具体动作 $a_k(s) \in \Theta_s$。然后Harness调用其 PD 技能执行该阶段：

$$
a_k(s) = \pi_s(\text{ctx}_s), \quad Q_k(s) = \text{Execute}(s,\ a_k(s))
$$

执行完成后，$Q_k(s)$ 被记录，并作为上游 QoR 传递给下一个阶段。

> **文字说明**：每个阶段智能体只关注本阶段的参数调整，无需了解全局树结构，降低了单个智能体的决策复杂度。法官负责全局导航，阶段智能体负责局部优化，形成清晰的职责分离。



### 1.6 整体优化循环（伪代码）

```
输入：设计 D，迭代预算 N，初始动作 a0
输出：最优动作 a* 及其后布线 QoR Q*

1. 运行初始完整流程 (a0)，记录所有阶段 QoR，初始化树 T 和历史 H
2. Q* = Q(a0), a* = a0
3. for k = 1 to N do
4.     A_k = ObservationTool(T, H)          // 生成自适应概要
5.     (n_hat, b, hints) = Judge(H, A_k)    // 法官决策
6.     复用 Bef(b) 的结果（从节点 n_hat 继承）
7.     for s in {b} ∪ Aft(b) do             // 按顺序执行
8.         ctx = BuildContext(s, n_hat, hints[s])  // 构建上下文
9.         a_k(s) = StageAgent_s(ctx)               // 阶段智能体生成动作
10.        Q_k(s) = ExecuteStage(s, a_k(s))          // 执行并获取 QoR
11.    end for
12.    更新 T 和 H（加入新节点）
13.    如果当前候选的时序指标优于 Q* 且满足面积/功耗约束，则更新 (a*, Q*)
14. end for
15. 返回 (a*, Q*)
```

### 1.7 关键设计要点总结

| 组件 | 功能 | 输入 | 输出 |
|------|------|------|------|
| **Judge Agnet** | 全局导航：选择分支节点和分支阶段，生成 hint | 历史记录 + 自适应概要 | 分支决策 + 各阶段 hint |
| **Observation Tool** | 压缩历史为探索平衡度和阶段瓶颈 | 树 T + 历史 H | 自适应概要 A |
| **Stage Agent** | 局部优化：为所属阶段生成具体参数 | 上游 QoR + 跨迭代经验 + hint | 该阶段动作 |
| **Optimization Tree** | 存储所有尝试及其 QoR，支持分支复用 | - | 搜索空间结构 |
| **Harness** | 协调智能体调度、上下文组装、工具执行 | - | 完整的迭代闭环 |
