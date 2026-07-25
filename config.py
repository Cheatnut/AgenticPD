# -*- coding: utf-8 -*-
"""
config.py — AgenticPD 全局配置模块

本模块是整个框架唯一的"参数/路径来源"，其他模块一律从这里读取配置，
禁止在别处硬编码任何路径、参数名或取值范围。

包含三部分内容：
1. ParamSpec / PARAM_SPACE：数据驱动的可调参数空间定义。
   增删参数只需要改这里，agents.py / orfs_interface.py 会自动适配。
2. BASELINE_PARAMS：基线（第 0 次迭代）使用的参数，与 ORFS base 运行保持一致。
3. FrameworkConfig：框架运行配置（路径、超时、LLM 设置、QoR 比较容差等）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 基础路径推导（全部由本文件位置推导，不硬编码绝对路径）
# 目录结构假定为：<ORFS根>/flow/agenticpd/config.py
# ---------------------------------------------------------------------------
AGENTICPD_DIR: Path = Path(__file__).resolve().parent          # flow/agenticpd/
FLOW_DIR: Path = AGENTICPD_DIR.parent                          # flow/
RUNS_DIR_NAME: str = "runs"       # agenticpd/runs/ 目录名
ENV_FILENAME: str = ".env"        # 环境变量文件名
RUNS_DIR: Path = AGENTICPD_DIR / RUNS_DIR_NAME  # 所有运行的工作目录

# ORFS 四个产物目录名（results/logs/reports/objects），与 FrameworkConfig 路径逻辑一致
ORFS_CATEGORIES: List[str] = ["results", "logs", "reports", "objects"]

# ---------------------------------------------------------------------------
# 时序单位换算：ORFS 的 JSON metrics / 报告中时序值单位为 ns（由工艺库决定，
# sky130hd 与 nangate45 均为 ns），而本框架对外统一使用 ps，故乘以 1000。
# 若未来换用时序单位不同的工艺库，只需修改此常量。
# ---------------------------------------------------------------------------
TIMING_UNIT_TO_PS: float = 1000.0

# ---------------------------------------------------------------------------
# 参数空间定义
# ---------------------------------------------------------------------------

# 物理设计四个阶段的规范名称（顺序即流程顺序，Judge 轮询兜底也按此顺序）
STAGES: List[str] = ["FP", "PL", "CTS", "RT"]

# 参数的"落地方式"（kind）：
#   make_var             —— 直接以 NAME=value 形式拼到 make 命令行
#   fastroute_adjustment —— 伪参数：生成自定义 fastroute.tcl 并传 FASTROUTE_TCL=<路径>
#                           （sky130hd 平台的 FASTROUTE_TCL 硬编码层容量 0.2，
#                            会绕过 ROUTING_LAYER_ADJUSTMENT 环境变量，
#                            因此必须用官方 AutoTuner 同款的"生成 tcl"方案）
#   global_route_args    —— 伪参数：渲染进 GLOBAL_ROUTE_ARGS 的 -congestion_iterations
KIND_MAKE_VAR = "make_var"
KIND_FASTROUTE_ADJ = "fastroute_adjustment"
KIND_GRT_ARGS = "global_route_args"


@dataclass(frozen=True)
class ParamSpec:
    """单个可调参数的完整规格说明（数据驱动，供 prompt 渲染与合法性校验使用）"""

    name: str            # 参数名（make 变量名或伪参数名）
    stage: str           # 所属阶段：FP / PL / CTS / RT
    ptype: str           # 类型："int" 或 "float"
    vmin: float          # 取值下界（含）
    vmax: float          # 取值上界（含）
    default: Optional[float]  # 基线默认值；None 表示基线不显式传该变量（用 ORFS 默认）
    description: str     # 中文说明（会被渲染进阶段智能体的 system prompt）
    kind: str = KIND_MAKE_VAR  # 落地方式，见上方注释

    def cast(self, value: Any) -> float:
        """把 LLM 输出的原始值强转为本参数的类型并 clamp 到 [vmin, vmax]"""
        v = float(value)
        v = max(self.vmin, min(self.vmax, v))
        if self.ptype == "int":
            return int(round(v))
        return round(v, 4)  # float 保留 4 位小数，避免 make 命令行出现超长小数


# 各阶段可调参数空间（范围主要参考官方 AutoTuner 对 sky130hd/gcd 的设定，
# 见 flow/designs/sky130hd/gcd/autotuner.json）
PARAM_SPACE: Dict[str, List[ParamSpec]] = {
    "FP": [
        ParamSpec(
            name="CORE_UTILIZATION", stage="FP", ptype="int",
            vmin=20, vmax=50, default=38,
            description=(
                "核心区利用率（百分数，20~50）。越高芯片面积越小、布线越拥挤；"
                "越低时序/布线越宽松但面积增大。base 设计当前为 38。"
            ),
        ),
        ParamSpec(
            name="CORE_ASPECT_RATIO", stage="FP", ptype="float",
            vmin=0.5, vmax=2.0, default=1.0,
            description=(
                "核心区高宽比（高/宽，0.5~2.0）。影响布局形状与时钟/信号线长分布，"
                "1.0 为正方形。"
            ),
        ),
    ],
    "PL": [
        ParamSpec(
            name="PLACE_DENSITY_LB_ADDON", stage="PL", ptype="float",
            vmin=0.0, vmax=0.2, default=None,
            description=(
                "布局密度余量（0.0~0.2）。设置后实际密度 = 可行下界 + 余量，"
                "不会因密度过低导致 placer 报错。余量越小单元越分散（利于时序/布线），"
                "越大越紧凑。注意：基线未设置该参数，此时使用平台固定密度 0.60；"
                "一旦设置即改用'下界+余量'模式。"
            ),
        ),
        ParamSpec(
            name="CELL_PAD_IN_SITES_GLOBAL_PLACEMENT", stage="PL", ptype="int",
            vmin=0, vmax=3, default=0,
            description=(
                "全局布局阶段单元两侧的 padding（site 数，0~3）。"
                "增大可缓解局部拥塞、改善可布线性，但等效提高了密度压力。"
            ),
        ),
    ],
    "CTS": [
        ParamSpec(
            name="CTS_CLUSTER_SIZE", stage="CTS", ptype="int",
            vmin=10, vmax=200, default=None,
            description=(
                "时钟树 sink 聚类的最大单簇 sink 数（10~200）。"
                "减小可降低局部 skew 但增加缓冲器数量与功耗；基线未设置（用工具默认）。"
            ),
        ),
        ParamSpec(
            name="CTS_CLUSTER_DIAMETER", stage="CTS", ptype="int",
            vmin=20, vmax=400, default=None,
            description=(
                "时钟树 sink 聚类的最大簇直径（微米，20~400）。"
                "减小使时钟树更均衡但插入更多缓冲器；基线未设置（用工具默认）。"
            ),
        ),
        ParamSpec(
            name="SETUP_SLACK_MARGIN", stage="CTS", ptype="float",
            vmin=0.0, vmax=0.2,
            default=0.0,
            description=(
                "repair_timing 的 setup 裕量目标（ns，0~0.2）。"
                "增大会让工具更激进地修时序（可能增大面积/功耗）。"
                "注意：该变量同时影响 FP/GRT 阶段的 repair_timing，此处归入 CTS 管理。"
            ),
        ),
    ],
    "RT": [
        ParamSpec(
            name="FASTROUTE_LAYER_ADJUSTMENT", stage="RT", ptype="float",
            vmin=0.1, vmax=0.3, default=0.2,
            description=(
                "全局布线各层容量缩减系数（0.1~0.3，伪参数）。"
                "越大全局布线越保守（留更多裕量、绕线更多），越小越激进"
                "（线长更短但详细布线可能更难收敛）。平台默认 0.2。"
                "实现方式：生成自定义 fastroute.tcl 并通过 FASTROUTE_TCL 传入。"
            ),
            kind=KIND_FASTROUTE_ADJ,
        ),
        ParamSpec(
            name="GRT_CONGESTION_ITERATIONS", stage="RT", ptype="int",
            vmin=10, vmax=50, default=30,
            description=(
                "全局布线拥塞消除迭代上限（10~50，伪参数，渲染进 GLOBAL_ROUTE_ARGS "
                "的 -congestion_iterations）。增大给布线器更多机会消除拥塞（更慢），"
                "减小则更快但可能残留拥塞。流程默认 30。"
            ),
            kind=KIND_GRT_ARGS,
        ),
    ],
}

# 基线参数（第 0 次迭代使用，尽量与 ORFS base 运行完全一致）：
# 只包含需要显式传给 make 的参数；未列出的参数沿用 ORFS/design config 默认。
# 注意 PL/CTS 阶段基线刻意为空 —— PLACE_DENSITY_LB_ADDON 一旦设置（哪怕 0.0）
# 语义就与"不设置"不同（见 ParamSpec 描述），CTS 参数不设置则用工具内部默认。
BASELINE_PARAMS: Dict[str, Dict[str, Any]] = {
    "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
    "PL": {},
    "CTS": {},
    "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2, "GRT_CONGESTION_ITERATIONS": 30},
}

# GLOBAL_ROUTE_ARGS 模板：前半部分为 ORFS 默认值（覆盖该变量时必须带上，
# 否则会丢失默认的拥塞报告参数），{iters} 为伪参数 GRT_CONGESTION_ITERATIONS
GLOBAL_ROUTE_ARGS_TEMPLATE = (
    "-congestion_report_iter_step 5 -verbose -congestion_iterations {iters}"
)

# 自定义 fastroute.tcl 模板：内容复制自 flow/platforms/sky130hd/fastroute.tcl，
# 仅把硬编码的层容量调整系数 0.2 参数化为 {adjustment}（官方 AutoTuner 同款做法）
FASTROUTE_TCL_TEMPLATE = """\
set_global_routing_layer_adjustment $::env(MIN_ROUTING_LAYER)-$::env(MAX_ROUTING_LAYER) {adjustment}

set_routing_layers -clock $::env(MIN_CLK_ROUTING_LAYER)-$::env(MAX_ROUTING_LAYER)
set_routing_layers -signal $::env(MIN_ROUTING_LAYER)-$::env(MAX_ROUTING_LAYER)
"""


def get_param_spec(name: str) -> Optional[ParamSpec]:
    """按参数名查找 ParamSpec（找不到返回 None）"""
    for specs in PARAM_SPACE.values():
        for spec in specs:
            if spec.name == name:
                return spec
    return None


# ---------------------------------------------------------------------------
# 框架运行配置
# ---------------------------------------------------------------------------

@dataclass
class FrameworkConfig:
    """框架运行配置：路径、目标设计、超时、LLM 设置、QoR 比较容差等"""

    # ---- 目标设计（可通过 CLI 覆盖；注意 PARAM_SPACE 是 sky130hd/gcd 特化的，
    #      换设计/工艺时必须重新审视参数空间与 fastroute 模板）----
    platform: str = "sky130hd"
    design: str = "ibex"

    # ---- 路径（全部由 FLOW_DIR 推导）----
    flow_dir: Path = field(default_factory=lambda: FLOW_DIR)
    # 本次运行的工作目录（存日志、history、生成的 fastroute.tcl 等），由 main.py
    # 按时间戳创建后填入，形如 flow/agenticpd/runs/20260718_153000/
    run_dir: Optional[Path] = None

    # ---- ORFS 调用 ----
    make_target: str = "all"          # 完整流程：synth→floorplan→place→cts→route→finish
    timeout_s: int = 3600             # 单次完整流程超时（秒），gcd 通常几分钟内完成
    variant_prefix: str = "agenticpd_iter"   # 每次迭代的 FLOW_VARIANT 前缀
    best_variant_name: str = "agenticpd_best"  # 最佳结果导出目录名（与 base 平级）

    # ---- 优化循环 ----
    iterations: int = 10              # 迭代次数（不含第 0 次基线）
    history_window: int = 15          # 提示词中历史记录的窗口大小（最近 N 条）
    skip_non_target_agents: bool = False  # True 时非目标阶段直接复用最优参数（省 LLM 调用）
    max_branch_count: int = 3        # 每个树节点最多被分支的次数，超过后从 branchable_nodes 中排除
    # （分支后段数的最大深度预留，本期仅用 count 避免过探索）

    # ---- LLM（DeepSeek，OpenAI 兼容接口）----
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-pro"
    llm_temperature: float = 0.6
    llm_api_key_env: str = "DEEPSEEK_API_KEY"  # API key 的环境变量名（不硬编码 key）
    max_json_retries: int = 3         # LLM 输出 JSON 解析失败时的重问次数
    max_api_retries: int = 3          # API 网络/限流错误的重试次数

    # ---- QoR 比较容差（gcd 的 WNS 只有几十 ps 量级，此容差较敏感，CLI 可调）----
    wns_tol_ps: float = 10.0          # WNS 差异小于该值视为打平，继续比 TNS
    tns_tol_ps: float = 50.0          # TNS 差异小于该值视为打平，继续比功耗/面积

    # ------------------------------------------------------------------
    # 派生路径（统一在此处定义，其他模块不得自行拼路径）
    # ------------------------------------------------------------------
    @property
    def design_config(self) -> str:
        """DESIGN_CONFIG 相对路径（make -C flow_dir 时使用，相对 flow 目录）"""
        return f"./designs/{self.platform}/{self.design}/config.mk"

    def variant_name(self, iteration: int) -> str:
        """第 iteration 次迭代对应的 FLOW_VARIANT 名称"""
        return f"{self.variant_prefix}{iteration}"

    def results_dir(self, variant: str) -> Path:
        return self.flow_dir / "results" / self.platform / self.design / variant

    def logs_dir(self, variant: str) -> Path:
        return self.flow_dir / "logs" / self.platform / self.design / variant

    def reports_dir(self, variant: str) -> Path:
        return self.flow_dir / "reports" / self.platform / self.design / variant

    def objects_dir(self, variant: str) -> Path:
        return self.flow_dir / "objects" / self.platform / self.design / variant

    @property
    def history_path(self) -> Path:
        """历史记录 JSON 文件路径（位于本次运行的 run_dir 下）"""
        assert self.run_dir is not None, "run_dir 必须先由 main.py 初始化"
        return self.run_dir / "history.json"

    @property
    def tree_path(self) -> Path:
        """优化树 JSON 文件路径（与 history.json 并列）"""
        assert self.run_dir is not None, "run_dir 必须先由 main.py 初始化"
        return self.run_dir / "tree.json"

    @property
    def log_file(self) -> Path:
        """框架日志文件路径"""
        assert self.run_dir is not None, "run_dir 必须先由 main.py 初始化"
        return self.run_dir / "agenticpd.log"

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict（用于把运行配置存档到 run_dir，便于复现实验）"""
        d = dataclasses.asdict(self)
        d["flow_dir"] = str(self.flow_dir)
        d["run_dir"] = str(self.run_dir) if self.run_dir else None
        return d
