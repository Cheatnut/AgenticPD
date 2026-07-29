# 4-ORFS 执行与 QoR 系统

## 目标

ORFS adapter 把“已验证的参数字典”翻译为 ORFS 的 `make` 调用，管理进程生命周期，再把 ORFS 报告翻译成统一的 QoR。它是搜索策略和 EDA 工具之间的边界。

## 命令构建

`orfs/command.py` 的 `build_make_cmd()` 统一构造命令。命令会带上：

- `DESIGN_CONFIG`：目标设计的 ORFS 配置；
- `FLOW_VARIANT`：本次候选的产物命名空间；
- 当前候选的 make 变量；
- 全流程目标或单阶段目标。

大多数参数可以直接成为 `NAME=value`。两个路由参数需要特殊翻译：

- `FASTROUTE_LAYER_ADJUSTMENT` 会生成 session 内的 `fastroute.tcl`，再通过 `FASTROUTE_TCL` 交给 ORFS；
- `GRT_CONGESTION_ITERATIONS` 会填入 `GLOBAL_ROUTE_ARGS` 模板，避免覆盖 ORFS 默认的其他全局路由选项。

这说明“参数名在 Python 中出现”不等于“同名环境变量会自动被 ORFS 读取”。每个参数的 delivery kind 必须明确。

## 执行后端

`orfs/backend.py` 定义抽象 `ExecutionBackend`。当前可实际使用的是 `LocalBackend`：它在本机以子进程运行 `make`，将输出写入日志，并在超时时终止整个进程组，防止子进程遗留。

`SlurmBackend` 目前只有接口 stub，尚未接入可执行的作业提交与轮询流程。因此当前项目应描述为单机同步执行，而不是 Slurm 或分布式调度系统。

## 阶段执行

`orfs/runner.py` 和 `orfs/interface.py` 将执行分成全流程与单阶段两种路径：

```text
全流程：构造 make all → 执行 → 解析各阶段观测与 finish QoR

下游重跑：复制父 variant → 清理当前阶段及下游产物
          → 逐阶段 make → 最后 make finish → 记录 StageResult
```

对单阶段重跑，执行前会调用对应的 `clean_<stage>`，避免旧 artifact 让 make 跳过本应重跑的工作。父产物复制发生在 checkpoint 已通过验证之后；没有可用父产物时必须报错，不能静默伪造继承成功。

每次 stage 执行都会记录耗时、退出码、开始/结束时间、实际命令、日志和可找到的阶段报告路径。超时、非零返回码、报告缺失都会被转成结构化失败，而不是只打印一段控制台文本。

## QoR 与失败解析

最终 QoR 的首选来源是：

```text
logs/<platform>/<design>/<variant>/6_report.json
```

解析器从中提取 WNS、TNS、面积和总功耗，并把时序值从 ns 统一换算为 ps。若 JSON 不存在，才从 `6_finish.rpt` 和 `6_report.log` 读取兼容性 fallback。fallback 用于旧产物兼容，不能与 JSON 结果当成同等精度的正式证据混写。

一个 `make` 返回 0 也不一定成功：若最终四项 QoR 不完整，Trial 仍是失败，原因属于 QoR 不完整。反过来，非零退出或超时会尝试从阶段报告判断失败停在哪一阶段。

QoR 比较的顺序由 `qor_is_better()` 固定：先看 WNS，再看 TNS，二者差异落在配置容差内时才看功耗与面积。详细数值与规则以[实验契约](实验契约.md)为准。

## 日志与路径安全

命令记录和 make 日志是审计证据，但也容易泄露用户目录。执行器在持久化前把项目根和 session 的绝对路径替换成稳定的相对标记，并将命令参数相对化。它不应记录密钥、完整请求头或 `.env` 内容。

`MockORFSRunner` 返回确定性的合成 QoR，适合测试优化循环、树和 Trial 持久化；它不调用 make、不生成真实物理设计产物，也不能用于任何真实 QoR 比较。
