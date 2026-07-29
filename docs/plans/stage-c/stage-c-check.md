# 阶段 C 验收报告

## 结论

通过。阶段 C 的 checkpoint fork、ORFS 执行记录、兼容性判定和真实 QoR 对照均满足验收门。

## 合并记录

- 阶段提交：`1b66cc8 stage-c: complete checkpoint fork verification`
- main 合并提交：`ad38e781b71add7af3d9611a4aa424d797be9e9d`

## 验证结果

- 纯 Python 验证：`make test`，90/90 通过。
- 数据模型自检：`python3 schemas/trial.py` 28/28、`python3 managers/checkpoint_manager.py` 18/18、`python3 managers/trial_manager.py` 25/25 通过。
- CLI 契约：`main.py`、`trial_inspect.py`、`trial_reproduce.py`、`clean.py`、`checkpoint_fork_verify.py` 的 `--help` 均以 0 退出。
- 真实实验：`runs/sky130hd_gcd/checkpoint_fork/20260729_210832/report.json` 的 `verdict=PASS`、`acceptance_validation.passed=true`。
- Checkpoint：`cp-490da2ab-CTS` 含 `4_cts.odb`、`4_cts.sdc`；大小与 SHA-256 独立复核一致。
- 阴性对照：`FASTROUTE_LAYER_ADJUSTMENT` 被正确判为不兼容，fork 被拒绝，full restart 成功。
- 阳性对照：`GRT_CONGESTION_ITERATIONS` 的 fork 与 full restart QoR 完全一致；耗时由 116.3s 降至 94.8s，节省 21.5s（18.5%）。
- 审计记录：baseline 的五个 stage 均成功；finish 记录退出码 0、相对日志/报告路径、命令与起止时间。session 的日志、JSON、JSONL 不含 `/home/` 或 `/Users/`。

## 遗留风险

- 无 P0/P1 遗留。
- 按用户明确要求，ORFS commit 与 PDK revision 不作为本阶段验收标准。
