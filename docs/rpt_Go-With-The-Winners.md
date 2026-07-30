# Go-With-The-Winners 详细研读报告

> 论文：*“Go With the Winners” Algorithms*  
> 作者：David Aldous，Umesh Vazirani  
> 原文：`papers/Others/Go-With-The-Winners.pdf`  
> 译文：`02_translations/full_text/translated_Go-With-The-Winners.md`  
> 定位：交互式多启动、粒子复制/淘汰、稀有成功概率放大

## 1. 核心结论

这篇 1994 年论文研究一个看似简单但非常重要的问题：

> 当单条随机搜索轨迹找到好解的概率极低时，除了独立重启很多次，能否让多条轨迹相互作用，把计算资源从失败轨迹转移给仍有希望的轨迹？

作者把随机优化抽象为粒子沿随机树从根向叶移动。Algorithm 0 是单粒子随机游走；Algorithm 1 维护固定 \(B\) 个粒子，每层淘汰已经落入叶节点的“失败者”，再把空出的粒子复制到仍能继续的“赢家”位置。这就是 go with the winners（跟随赢家）。

主要理论结论：

- 简单独立重启可能需要关于深度 \(d\) 的指数时间；
- 若树的不平衡度 \($\kappa$\) 是多项式可控的，交互式粒子算法可在多项式时间找到最深叶节点；
- Algorithm 3 在合理 \(\beta=\Omega(1/d)\) 条件下，用 \(O(\kappa d^6)\) 粒子步达到常数成功概率；
- Algorithm 1 使用 \(B=\kappa\operatorname{poly}(d)\) 个粒子时，失败概率至多 \(1/4\)。

对 Agentic-PD 的意义是：把并行 flow run 视为粒子群，阶段性淘汰“注定失败”的 run，把预算复制给仍有潜力的配置。但论文也给出严格警告：策略是否有效取决于中间“存活”信号与最终成功之间的关联，即 \(\kappa\) 是否可控。若 early metric 误导，复制赢家会放大偏差。

## 2. 问题建模

### 2.1 状态空间是一棵随机树

每个状态是树顶点 \(v\)，算法只能调用随机 successor：

\[
v\rightarrow v_i
\quad\text{with probability}\quad
p(v_i|v).
\]

目标是在不知道完整树结构、不能识别同名节点、也不知道转移概率的情况下，找到深度至少 \(d\) 的顶点。

令：

\[
a(i)=\Pr(\text{单粒子至少到达深度 }i).
\]

独立重启要在多项式步内成功，必须满足：

\[
1/a(d)=\operatorname{poly}(d).
\]

若 \(a(d)\) 指数小，独立重复需要指数次 run。

### 2.2 不平衡参数 \(\kappa\)

固定已到达深度 \(i\) 的粒子位置 \(W_i\)，其继续到达深度 \(j\) 的概率是 \(a(j|W_i)\)。作者定义：

\[
\kappa_{i,j}
=
\frac{\mathbb{E}a^2(j|W_i)}
{\left(\mathbb{E}a(j|W_i)\right)^2}
\ge1,
\]

\[
\kappa=\max_{0\le i<j\le d'}\kappa_{i,j}.
\]

它是一个 normalized second moment（归一化二阶矩）：若同一层所有存活位置的后续成功概率接近，\(\kappa\) 小；若少数位置拥有几乎全部成功概率，\(\kappa\) 大。

这比“树是否平衡”的几何直觉更精确。算法不需要知道哪条路径正确，但要求当前存活粒子群中，真正有希望的路径不能稀有到完全采不到。

### 2.3 参数 \(\beta\)

\[
\beta=
\min_{0\le i<d'}
\frac{a(i+1)}{a(i)}.
\]

\(\beta\) 是相邻深度的最小条件存活概率。若某一层是极窄 bottleneck，\(\beta\) 很小，估计相邻层 survival ratio 会很困难。

## 3. 四个算法

### 3.1 Algorithm 0：单粒子

从根出发，每步随机选子节点，到叶即停。它既是 baseline，也是定义 \(a(i)\)、\(p(v)\) 和 \(\kappa\) 的基础。

优点是简单、无交互；缺点是一次早期错误分支就不可恢复，单次成功概率 \(a(d)\) 可能指数小。

### 3.2 Algorithm 1：固定群体的跟随赢家

每层维护 \(B\) 个粒子：

1. 识别已经位于叶节点的粒子；
2. 把这些粒子重新分配到非叶粒子的位置；
3. 所有粒子继续随机走一步。

总群体恒定，计算预算不会增长。它相当于 sequential Monte Carlo 中的 resampling，也类似遗传算法的 selection，但没有 crossover。

困难在于粒子之间产生依赖。Figure 2 说明：复制/条件化之后，某顶点的期望粒子数不再简单正比于单粒子访问概率 \(p(v)\)。因此不能直接用独立样本分析。

### 3.3 Algorithm 2：可分析的 branching process

若知道理想相邻存活率：

\[
\theta_i=\frac{a(i+1)}{a(i)},
\]

就让每个非叶粒子独立产生均值 \(\theta_i^{-1}\) 的后代，再向下移动。这样粒子独立繁殖/死亡，便于做期望与方差分析。

Lemma 2：

\[
\mathbb{E}S_i=B\frac{a(i)}{s_i},
\qquad
s_i=\prod_{r=1}^{i-1}\theta_r.
\]

当使用理想 \(\theta_i\) 时，\(\mathbb{E}S_d=B\)，且：

\[
\operatorname{var}(S_d)\le\kappa Bd.
\]

取 \(B=2\kappa d\)，由 Chebyshev inequality 得到至少 \(1/2\) 成功概率。

问题是 \(a(i)\) 未知，所以 Algorithm 2 不能直接实现。

### 3.4 Algorithm 3：用分组采样估计 \(\theta_i\)

Algorithm 3 用 \(d-1\) 组粒子。第 \(i\) 组专门估计深度 \(i\) 的非叶比例 \(\theta_i\)，其余组使用该估计做独立繁殖。若任一组超过 \(10B\)，立即停止，以把工作量限制为：

\[
\le5Bd^2.
\]

直觉是：

\[
\frac{a(i+1)}{a(i)}\theta_i^{-1}\approx1,
\]

所以每组粒子数保持在 \(B\) 附近。

Theorem 3 的失败概率界为：

\[
O\left(
B^{-1}\kappa d^4
\left(1+\frac{1}{\beta d}\right)
\right),
\]

即标准引用形式：

\[
O\left(
B^{-1}\kappa d^4
\bigl(1+1/(\beta d)\bigr)
\right).
\]

这里两项在排版上是相乘关系。若 \(\beta=\Omega(1/d)\)，取 \(B=O(\kappa d^4)\)，总粒子步为 \(O(\kappa d^6)\)。

## 4. 证明结构

### 4.1 Lemma 2：一阶矩与二阶矩

令 \(X_v\) 为到达顶点 \(v\) 的粒子数，\(S_n=\sum_{v\in V_n}X_v\)。沿路径条件期望可得：

\[
\mathbb{E}X_w=\frac{Bp(w)}{s_n},
\]

进而得到 \(\mathbb{E}S_n=Ba(n)/s_n\)。

方差证明的关键是：

- \(N(c)\) 的方差不超过 \(c\)；
- multinomial-like 分配中不同盒子计数协方差非正；
- 用 conditional variance/covariance 递归传播；
- 用最后公共祖先分解两个叶顶点计数的 covariance；
- \(\kappa\) 正好控制不同祖先子树后续成功概率的二阶矩。

因此：

\[
\operatorname{var}(S_n)
\le
\kappa B\frac{a^2(n)}{s_n^2}
\sum_{j=0}^{n}\frac{s_j}{a(j)}.
\]

### 4.2 Theorem 3：估计误差不爆炸

作者定义事件 \(A_j\)，要求采样得到的 \(\theta_{j-1}\) 与目标值处于 \(b_d\) 倍范围，其中：

\[
b_d=
\frac{1+1/(2d)}
{1-1/(2d)},
\qquad
b_d^{d-1}<e.
\]

所以逐层相乘的误差不会指数爆炸，而只累积到常数 \(e\)。

Lemma 5 用 Chebyshev inequality 控制：

- \(\theta_k\) 估计失败；
- 第 \(k\) 组粒子数过度偏离期望。

Lemma 6 控制粒子组超过 \(10B\) 导致提前停止的概率。对层与组 union bound 后得到 Theorem 3。

### 4.3 Theorem 1：把 branching 算法耦合回固定粒子数

先让 Algorithm 2 的期望群体略微增长：

\[
\mathbb{E}S_i
=
B(1+1/p_1(d))^i.
\]

再逐层删去多余粒子，使其变成固定 \(B\) 个粒子的 Algorithm 1。证明要控制：

- 每层总粒子数偏离期望的幅度；
- 单个粒子的后代数；
- 删除某粒子后，其后代在未来层造成的误差。

用 Chebyshev、Chernoff 与归纳证明误差只多项式增长，从而 Algorithm 1 以高概率到达深度 \(d\)。

## 5. 与模拟退火的联系

作者把能量函数 \(f\) 的 sublevel set：

\[
\{s:f(s)\le h\}
\]

的 connected component 组织成树。温度下降对应粒子从树的高层向叶节点移动；分叉对应模拟退火在不同 basin 之间作不可逆选择；叶节点对应 local minimum。

这不是模拟退火的精确动力学，而是一种抽象。论文承认，真实问题中子树大小与深度之间的关联很难用 Markov chain 技术证明。树模型的价值在于把这种“中间存活信号是否预测最终深度”的要求压缩进 \(\kappa\)。

## 6. 创新点

- 给“并行搜索 + 淘汰失败 run + 复制成功 run”提供严格理论模型。
- 证明交互式 run 可比独立 restart 指数级更高效。
- 用 \(\kappa\) 描述当前层后续成功概率的不均匀性。
- 用不可实现但可分析的 Algorithm 2 作为桥梁，再构造可实现 Algorithm 3。
- 揭示 resampling 引入的粒子依赖，不能把复制后的粒子误当独立样本。
- 将随机树模型与 polynomial-time simulated annealing 联系起来。

## 7. 局限性

### 7.1 “叶节点=失败”过于理想化

Algorithm 1 能明确判断粒子是否还有子节点。真实优化中，一个 run 暂时停滞不等于永远失败；early termination classifier 会有 false negative。错误淘汰唯一有希望的 run 后，复制无法恢复。

### 7.2 只能前进，不能回溯

树模型中每步不可逆。真实 physical design 可修改历史参数、重启某阶段或从 checkpoint 回滚。DAG/graph 状态空间中的重复状态、循环和跨层跳转会改变理论。

### 7.3 \(\kappa\) 不可直接观测

理论复杂度依赖 \(\kappa\)，但实际问题中不知道每个中间状态最终成功概率，因此难以验证“多项式可控”假设。

### 7.4 粒子复制不增加信息多样性

若复制后使用相同随机种子、相同 LLM prompt 或相同工具 nondeterminism，子 run 会高度相关。理论假设后续随机选择独立；工程中必须主动注入多样性。

### 7.5 没有实验部分

论文是理论分析，没有在模拟退火或 EDA 上给数值实验。应用到 Agentic-PD 的收益必须重新实证。

### 7.6 复杂度界较松

\(O(\kappa d^6)\) 是多项式保证，但对昂贵的 P&R run 仍可能不可接受。理论证明“不是指数”并不等于实际高效。

## 8. 对 Agentic-PD / ORFS-Agent 的映射

### 8.1 粒子对应什么

| 论文对象 | Agentic-PD 对应 |
|---|---|
| particle | 一个 flow 配置及其 checkpoint |
| depth | 已完成的 flow stage / 优化轮次 |
| non-leaf | 仍满足可恢复条件的 run |
| leaf before \(d\) | 提前失败或预测无望的 run |
| deepest leaf | 达到最终 PPA/DRV gate 的设计 |
| reproduction | 从 checkpoint fork 新参数分支 |
| \(B\) | 并行 run / 总计算预算 |
| \(\kappa\) | 相同 early score 下最终成功率的不均匀性 |

### 8.2 适合的执行策略

可实现一个 bounded population：

```text
初始化 B 个差异化配置
for stage in flow_stages:
    执行到 stage checkpoint
    提取 stage metrics 和 failure signature
    淘汰硬失败及高置信 doomed runs
    从存活 run 中按潜力和多样性选择 parent
    fork 新配置，补回 B 个
最终只按 detailed-route / timing / power gate 排名
```

### 8.3 必须加入多样性约束

原算法只复制位置，再依赖独立随机后续。Agentic-PD 中 fork 后应改变：

- 参数扰动；
- LLM sampling seed；
- action template；
- exploration objective；
- memory subset。

否则群体会发生 mode collapse，形式上有 \(B\) 个 run，实际上只有一条策略。

### 8.4 Survivor 判断要保守

可分三类：

- **hard dead**：工具失败、不可恢复 DRC、资源超限；
- **soft bad**：当前 WNS/overflow 较差，但仍可能修复；
- **promising**：指标和趋势良好。

只对 hard dead 强制淘汰；soft bad 需保留少量 exploration quota。这样降低 false-negative pruning。

### 8.5 用历史数据估计经验 \(\kappa\)

在每个 stage，把 run 按 early feature bucket 分组，统计最终 success probability 的离散程度。可用：

\[
\widehat{\kappa}
=
\frac{\mathbb{E}[\hat p_{\text{final}}^2]}
{\mathbb{E}[\hat p_{\text{final}}]^2}
\]

作为经验诊断：

- \(\widehat{\kappa}\) 小：early feature 能稳定预测后续，适合 aggressive pruning；
- \(\widehat{\kappa}\) 大：同类中只有极少数最终成功，必须增加探索和特征。

这比直接照搬理论界更有现实价值。

### 8.6 与 Doomed-Run-Prediction 的组合

该论文提供资源重新分配原则，Doomed-Run-Prediction 提供“谁可能是叶节点”的预测器。组合时应记录：

- pruning threshold；
- false-negative rate；
- 被淘汰 run 的反事实抽样复跑；
- 节省 wall-clock；
- 最终 Pareto hypervolume 损失。

没有反事实复跑，无法知道预测器是否误杀赢家。

## 9. 建议实验

在一个小 ORFS design 上：

1. 采样 40–80 个 flow 配置；
2. 在 synthesis、global placement、CTS、global route 设置 checkpoint；
3. 建立三种 baseline：
   - independent random restart；
   - top-k greedy；
   - go-with-the-winners with diversity；
4. 保持总 CPU-hours 相同；
5. 比较：
   - 首次达标时间；
   - 达标概率；
   - 最终 hypervolume；
   - 唯一策略数；
   - 误淘汰赢家率；
   - 节省的无效 run 时间。

最重要的不是证明理论复杂度，而是验证：

> 当前 stage 的指标是否真的足以区分“还有希望”与“已无希望”。

## 10. 总体评价

该论文与 LLM 无关，也不是 EDA 论文，但它为 Agentic flow optimization 提供了比“多开几个 run”更严谨的搜索思想：并行候选之间可以通过淘汰与复制共享预算，从而放大稀有成功事件。

它同时指出这种策略的根本风险：复制机制只在中间存活信号与最终目标具有稳定关联时有效。对 Agentic-PD，工程重点应放在 calibrated pruning、checkpoint fork、diversity preservation 和 final-flow verification，而不是机械实现“保留 top-k”。

## 迭代记录

- 2026-07-24：建立扫描全文研读基线；重建全部算法、定理、证明结构与 simulated annealing 映射；补充对 Agentic-PD 群体搜索、失败预测和多样性控制的迁移方案。

