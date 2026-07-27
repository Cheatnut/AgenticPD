# legacy_run — 阶段 A smoke test 回归夹具

**来源**：`sky130hd/gcd`，`--baseline-only`，2026-07-27  
**用途**：纯 Python 测试的只读输入，禁止回写

## 目录结构

```
legacy_run/
├── README.md          ← 本文件
├── expected_qor.json  ← 手工核对的已知正确 QoR 值
├── history.json       ← 运行 history（1 条 baseline 记录）
├── tree.json          ← 优化树（root → FP → PL → CTS → RT）
└── qor/
    ├── 6_report.json  ← ORFS 最终 QoR（JSON，正式来源）
    ├── 6_report.log   ← ORFS 运行时 log（fallback 兜底）
    └── 6_finish.rpt   ← ORFS 最终 STA 报告（fallback 兜底）
```

## 测试使用规则

- 所有测试从临时目录运行，禁止写回本目录
- expected_qor.json 中的值是手工核对项，改动需要同时更新注释说明原因
- 新增 fixture 文件需更新本 README
