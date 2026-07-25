# -*- coding: utf-8 -*-
"""
orfs_interface.py — ORFS（OpenROAD Flow Scripts）调用封装

职责：
1. 把智能体生成的各阶段参数翻译成 make 命令行变量（含两个伪参数的特殊落地：
   FASTROUTE_LAYER_ADJUSTMENT → 生成自定义 fastroute.tcl；
   GRT_CONGESTION_ITERATIONS → 渲染进 GLOBAL_ROUTE_ARGS）
2. 以独立 FLOW_VARIANT 运行完整流程（带超时与进程组清理）
3. 解析最终 QoR（JSON metrics 为主、rpt/log 正则兜底）与各阶段中间 QoR
4. 流程失败时定位崩溃阶段
5. 导出最佳结果到 flow/results/<plat>/<design>/agenticpd_best/
6. branch_from()：分支运行接口存根（本期恒回落到完整重跑，未来扩展用）

重要实现说明：make 不感知命令行变量的变化——若 variant 目录里已有旧产物，
改参数后 make 会认为目标已最新而直接跳过。因此每次运行前必须保证该 variant
的四个目录树（results/logs/reports/objects）为空，这是"真正从头重跑"的关键。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from config import FrameworkConfig, ParamSpec
from utils import QoR

log = logging.getLogger("orfs")

# ---------------------------------------------------------------------------
# 流程各子步骤的 JSON metrics 文件（按执行顺序排列），用于：
# 1. detect_failed_stage：首个缺失的 json 即崩溃所在阶段
# 2. parse_stage_qor：提取各阶段中间时序作为阶段智能体的"上游 QoR"输入
# 文件名与阶段划分依据 flow/Makefile 的 do-step 规则（见方案调研记录）
# ---------------------------------------------------------------------------
STEP_JSON_SEQUENCE: List[Tuple[str, str]] = [
    ("1_synth.json", "synth"),
    ("2_1_floorplan.json", "floorplan"),
    ("2_2_floorplan_macro.json", "floorplan"),
    ("2_3_floorplan_tapcell.json", "floorplan"),
    ("2_4_floorplan_pdn.json", "floorplan"),
    ("3_1_place_gp_skip_io.json", "place"),
    ("3_2_place_iop.json", "place"),
    ("3_3_place_gp.json", "place"),
    ("3_4_place_resized.json", "place"),
    ("3_5_place_dp.json", "place"),
    ("4_1_cts.json", "cts"),
    ("5_1_grt.json", "globalroute"),
    ("5_2_route.json", "detailedroute"),
    ("5_3_fillcell.json", "route"),
    ("6_report.json", "finish"),
]

# 各阶段"代表性"中间 QoR 的来源文件（阶段智能体的上游输入）：
# FP 看 floorplan 结束、PL 看详细布局结束、CTS 看 CTS 结束、
# RT 同时看全局布线与详细布线
STAGE_QOR_SOURCES: Dict[str, List[str]] = {
    "FP": ["2_1_floorplan.json"],
    "PL": ["3_5_place_dp.json"],
    "CTS": ["4_1_cts.json"],
    "RT": ["5_1_grt.json", "5_2_route.json"],
}

# 阶段名 → ORFS make 单阶段目标（用于逐阶段流水线执行）
_STAGE_MAKE_TARGET: Dict[str, str] = {
    "FP": "floorplan",
    "PL": "place",
    "CTS": "cts",
    "RT": "route",
}


@dataclass
class RunResult:
    """一次完整 ORFS 运行的结果记录"""

    ok: bool                              # 流程是否成功且 QoR 完整
    variant: str                          # 本次运行的 FLOW_VARIANT
    qor: Optional[QoR] = None             # 最终 QoR（失败时可能为 None 或不完整）
    stage_qor: Dict[str, Dict[str, float]] = field(default_factory=dict)
    failed_stage: Optional[str] = None    # 失败所在阶段（成功时为 None）
    error: Optional[str] = None           # 错误摘要（make log 尾部等）
    elapsed_s: float = 0.0                # 运行耗时（秒）
    make_log_path: Optional[str] = None   # make 输出日志文件路径


class ORFSRunner:
    """ORFS 调用器：一个实例服务于一次优化运行（绑定一个 FrameworkConfig）"""

    def __init__(self, cfg: FrameworkConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------
    # 主入口：运行一次完整流程
    # ------------------------------------------------------------------
    def run_flow(self, stage_params: Dict[str, Dict[str, Any]],
                 variant: str, iteration: int) -> RunResult:
        """以给定各阶段参数、独立 FLOW_VARIANT 从头运行完整 RTL→GDS 流程。

        参数:
            stage_params: {"FP": {...}, "PL": {...}, "CTS": {...}, "RT": {...}}
            variant:      本次运行的 FLOW_VARIANT 名称（如 agenticpd_iter3）
            iteration:    迭代号（用于生成的 fastroute.tcl / make log 命名）
        """
        cfg = self.cfg
        # 1) 清空该 variant 的四个目录树（防止上次崩溃残留导致 make 跳步）
        self._wipe_variant(variant)

        # 2) 组装 make 命令
        make_cmd, make_log_path = self._build_make_cmd(stage_params, variant, iteration)
        make_target = make_cmd[-1] if make_cmd else "all"
        log.info("#%d [ORFS] make %s...", iteration, make_target)

        # 3) 执行（带超时与进程组清理）
        start = time.monotonic()
        returncode, timed_out = self._run_make(make_cmd, make_log_path)
        elapsed = time.monotonic() - start

        # 4) 解析结果
        result = RunResult(ok=False, variant=variant, elapsed_s=elapsed,
                           make_log_path=str(make_log_path))
        result.stage_qor = self.parse_stage_qor(variant)

        if timed_out:
            result.failed_stage = self.detect_failed_stage(variant) or "unknown"
            result.error = f"超时（>{cfg.timeout_s}s），已杀掉进程组"
            log.error("#%d [ORFS] Timeout, failed stage: %s", iteration, result.failed_stage)
            return result

        # 无论退出码如何都尝试解析 QoR（部分失败场景仍可能有完整报告）
        qor = self.parse_qor(variant)
        result.qor = qor if qor is not None else None

        if returncode != 0:
            result.failed_stage = self.detect_failed_stage(variant) or "unknown"
            result.error = (f"make 退出码 {returncode}；日志尾部：\n"
                            f"{self._tail_log(make_log_path)}")
            log.error("#%d [ORFS] make failed (exit %d), failed stage: %s",
                      iteration, returncode, result.failed_stage)
            return result

        if result.qor is None or not result.qor.is_complete():
            # 退出码为 0 但指标不完整：视为失败（残缺 QoR 不参与最优比较）
            result.failed_stage = self.detect_failed_stage(variant) or "metrics"
            result.error = "流程退出码为 0 但 QoR 指标不完整"
            log.error("#%d [ORFS] QoR incomplete: %s", iteration,
                      result.qor.to_dict() if result.qor else None)
            return result

        result.ok = True
        log.info("#%d [ORFS] Iter #%d done!(%.1fs)", iteration, iteration, elapsed)
        log.info("#%d [ORFS] Iter #%d final QoR: %s", iteration, iteration, result.qor.pretty())
        return result

    # ------------------------------------------------------------------
    # 分支接口：论文 §3.2 的"选择中间节点 n_hat 启动新分支"
    # ------------------------------------------------------------------
    _CLEAN_TARGETS: Dict[str, str] = {
        "FP": "clean_floorplan",
        "PL": "clean_place",
        "CTS": "clean_cts",
        "RT": "clean_route",
    }

    def copy_parent_results(self, parent_variant: str, new_variant: str) -> None:
        """把父 variant 的 results/objects/logs/reports 四个目录完整复制到新 variant。

        这是分支执行的前置步骤——复制后 Bef 阶段结果已就位，后续只需
        clean + make 目标阶段即可增量重跑。
        """
        cfg = self.cfg
        for get_dir in (cfg.results_dir, cfg.objects_dir, cfg.logs_dir, cfg.reports_dir):
            src = get_dir(parent_variant)
            dst = get_dir(new_variant)
            if not src.is_dir():
                log.warning("[ORFS] Parent variant %s: %s dir not found, skip copy",
                            parent_variant, src.name)
                continue
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                item_dst = dst / item.name
                if item.is_dir():
                    if item_dst.exists():
                        shutil.rmtree(item_dst)
                    shutil.copytree(item, item_dst, symlinks=True)
                else:
                    shutil.copy2(item, item_dst)

    def branch_from(self, parent_variant: str, branch_stage: str,
                    stage_params: Dict[str, Dict[str, Any]],
                    new_variant: str, iteration: int) -> RunResult:
        """论文 §3.2：从 parent_variant 的中间快照分支运行。

        原理：
        1. 复制 parent variant 的 results/objects/logs/reports 四目录到新 variant；
        2. 调用 make clean_<branch_stage> 删除分支阶段产物
           （利用 ORFS 的 per-stage clean 目标）；
        3. 以新参数调用 make all —— make 依赖检查自动跳过 Bef 阶段、
           重跑 {branch_stage} ∪ Aft(branch_stage)。
        """
        log.info("#%d [ORFS] Branch from %s @%s, new variant=%s",
                 iteration, parent_variant, branch_stage, new_variant)

        # 1) 复制父 variant 产物
        self.copy_parent_results(parent_variant, new_variant)

        # 2) 清理分支阶段产物
        clean_target = self._CLEAN_TARGETS.get(branch_stage)
        if clean_target is None:
            raise ValueError(f"未知的分支阶段：{branch_stage}")
        self._run_clean_make(new_variant, clean_target)

        # 3) 组装 make 命令并执行（make 只会重建被 clean 的阶段及下游）
        make_cmd, make_log_path = self._build_make_cmd(stage_params, new_variant, iteration)
        log.info("#%d [ORFS] make (branch from %s)...", iteration, branch_stage)

        start = time.monotonic()
        returncode, timed_out = self._run_make(make_cmd, make_log_path)
        elapsed = time.monotonic() - start

        # 4) 解析结果（与 run_flow 完全一致）
        cfg = self.cfg
        result = RunResult(ok=False, variant=new_variant, elapsed_s=elapsed,
                           make_log_path=str(make_log_path))
        result.stage_qor = self.parse_stage_qor(new_variant)

        if timed_out:
            result.failed_stage = self.detect_failed_stage(new_variant) or "unknown"
            result.error = f"超时（>{cfg.timeout_s}s），已杀掉进程组"
            log.error("#%d [ORFS] Branch timeout, failed stage: %s", iteration, result.failed_stage)
            return result

        qor = self.parse_qor(new_variant)
        result.qor = qor if qor is not None else None

        if returncode != 0:
            result.failed_stage = self.detect_failed_stage(new_variant) or "unknown"
            result.error = (f"make 退出码 {returncode}；日志尾部：\n"
                            f"{self._tail_log(make_log_path)}")
            log.error("#%d [ORFS] Branch make failed (exit %d), failed stage: %s",
                      iteration, returncode, result.failed_stage)
            return result

        if result.qor is None or not result.qor.is_complete():
            result.failed_stage = self.detect_failed_stage(new_variant) or "metrics"
            result.error = "流程退出码为 0 但 QoR 指标不完整"
            log.error("#%d [ORFS] Branch QoR incomplete: %s", iteration,
                      result.qor.to_dict() if result.qor else None)
            return result

        result.ok = True
        log.info("#%d [ORFS] Branch Iter #%d done!(%.1fs)", iteration, iteration, elapsed)
        log.info("#%d [ORFS] Branch Iter #%d final QoR: %s", iteration, iteration, result.qor.pretty())
        return result

    # ------------------------------------------------------------------
    # 逐阶段流水线接口：每次只跑一个 ORFS 阶段
    # ------------------------------------------------------------------

    def run_stage(self, stage: str,
                  stage_params: Dict[str, Dict[str, Any]],
                  variant: str, iteration: int) -> Tuple[bool, Dict[str, float]]:
        """只运行单个 ORFS 阶段（如 place、cts）并返回该阶段的中间 QoR。

        前提：variant 目录中已存在所有 Bef 阶段的完整产物（由调用方先通过
        copy_parent_results() 建立基线）。

        流程：
        1. clean 该阶段（确保不会用旧产物跳过 make）
        2. make <stage_target>（只跑到该阶段，不跑下游）
        3. 解析该阶段的中间 QoR（ws_ps / tns_ps）

        返回:
            (ok, stage_qor_dict): ok 表示阶段成功完成，stage_qor_dict 含有
            {<tag>_ws_ps, <tag>_tns_ps} 等键值。
        """
        cfg = self.cfg
        if stage not in self._CLEAN_TARGETS:
            raise ValueError(f"未知阶段：{stage}，合法值为 {'/'.join(config.STAGES)}")
        make_target = _STAGE_MAKE_TARGET[stage]

        # 1) 清理该阶段产物
        clean_target = self._CLEAN_TARGETS[stage]
        self._run_clean_make(variant, clean_target)

        # 2) 组装并执行单阶段 make
        make_cmd, make_log_path = self._build_make_cmd(
            stage_params, variant, iteration,
            target=make_target, log_suffix=f"_{stage}")
        log.info("#%d [ORFS] make %s...", iteration, make_target)

        start = time.monotonic()
        returncode, timed_out = self._run_make(make_cmd, make_log_path)
        elapsed = time.monotonic() - start

        # 3) 解析该阶段的中间 QoR
        all_stage_qor = self.parse_stage_qor(variant)
        stage_qor = all_stage_qor.get(stage, {})

        if timed_out:
            log.error("#%d [ORFS] %s timeout (%.1fs)", iteration, stage, elapsed)
            return False, stage_qor
        if returncode != 0:
            log.error("#%d [ORFS] %s make failed (exit %d, log: %s)",
                      iteration, stage, returncode, make_log_path)
            return False, stage_qor

        log.info("#%d [ORFS] %s done!(%.1fs)", iteration, stage, elapsed)
        log.info("#%d [ORFS] %s QoR: %s", iteration, stage,
                 ", ".join(f"{k}={v}" for k, v in sorted(stage_qor.items())))
        return True, stage_qor

    def run_finish(self, stage_params: Dict[str, Dict[str, Any]],
                   variant: str, iteration: int) -> RunResult:
        """在所有下游阶段完成后执行 make finish，解析最终 QoR。

        前提：variant 中已有当前迭代所有阶段的完整结果（由逐阶段 run_stage 建立）。
        make finish 只生成最终报告（6_report.json 等），不会重跑已完成阶段。
        """
        cfg = self.cfg
        make_cmd, make_log_path = self._build_make_cmd(
            stage_params, variant, iteration,
            target="finish", log_suffix="_finish")
        log.info("#%d [ORFS] make finish...", iteration)

        start = time.monotonic()
        returncode, timed_out = self._run_make(make_cmd, make_log_path)
        elapsed = time.monotonic() - start

        result = RunResult(ok=False, variant=variant, elapsed_s=elapsed,
                           make_log_path=str(make_log_path))
        result.stage_qor = self.parse_stage_qor(variant)

        if timed_out:
            result.failed_stage = self.detect_failed_stage(variant) or "finish"
            result.error = f"超时（>{cfg.timeout_s}s），已杀掉进程组"
            log.error("#%d [ORFS] finish timeout", iteration)
            return result

        qor = self.parse_qor(variant)
        result.qor = qor if qor is not None else None

        if returncode != 0:
            result.failed_stage = self.detect_failed_stage(variant) or "finish"
            result.error = (f"finish make 退出码 {returncode}；日志尾部：\n"
                            f"{self._tail_log(make_log_path)}")
            log.error("#%d [ORFS] finish failed (exit %d)", iteration, returncode)
            return result

        if result.qor is None or not result.qor.is_complete():
            result.failed_stage = "metrics"
            result.error = "finish 退出码为 0 但 QoR 指标不完整"
            log.error("#%d [ORFS] finish QoR incomplete", iteration)
            return result

        result.ok = True
        log.info("#%d [ORFS] Iter #%d finish!(%.1fs)", iteration, iteration, elapsed)
        log.info("#%d [ORFS] Iter #%d final QoR: %s", iteration, iteration, result.qor.pretty())
        return result

    def _run_clean_make(self, variant: str, clean_target: str) -> None:
        """在指定 variant 下执行 clean 目标（超时 120s，失败视为 fatal）"""
        cmd = [
            "make", "-C", str(self.cfg.flow_dir),
            f"DESIGN_CONFIG={self.cfg.design_config}",
            f"FLOW_VARIANT={variant}",
            clean_target,
        ]
        log.debug("[ORFS] clean make: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=self.cfg.flow_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            stdout, _ = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise RuntimeError(f"clean make 超时（>120s）")
        if proc.returncode != 0:
            tail = "\n".join(stdout.decode("utf-8", errors="replace").splitlines()[-10:])
            raise RuntimeError(
                f"clean make 失败（退出码 {proc.returncode}）：\n{tail}")

    # ------------------------------------------------------------------
    # 内部：目录清理 / 命令组装 / 进程执行
    # ------------------------------------------------------------------
    def _wipe_variant(self, variant: str) -> None:
        """删除该 variant 的 results/logs/reports/objects 目录树（若存在）。

        仅作用于本框架自己命名的 variant（agenticpd_iter*），绝不触碰 base。
        """
        assert variant != "base", "禁止清理 base 基线目录"
        for d in (self.cfg.results_dir(variant), self.cfg.logs_dir(variant),
                  self.cfg.reports_dir(variant), self.cfg.objects_dir(variant)):
            if d.exists():
                log.debug("[ORFS] cleaning stale dir: %s", d)
                shutil.rmtree(d)

    def _build_make_cmd(self, stage_params: Dict[str, Dict[str, Any]],
                        variant: str, iteration: int,
                        target: Optional[str] = None,
                        log_suffix: str = "") -> Tuple[List[str], Path]:
        """把各阶段参数翻译成完整 make 命令（列表形式，不经过 shell）。

        target: make 目标，默认 cfg.make_target（"all"）。逐阶段执行时传入
                单阶段目标（如 "floorplan"、"place"、"cts"、"route"、"finish"）。
        log_suffix: 日志文件名后缀（如 "FP"、"PL"），用于区分同一迭代内各阶段的日志。

        返回 (make 命令列表, make 输出日志路径)。
        """
        cfg = self.cfg
        assert cfg.run_dir is not None, "run_dir 未初始化"

        make_vars: Dict[str, str] = {}
        for stage, params in stage_params.items():
            for name, value in params.items():
                spec = config.get_param_spec(name)
                if spec is None:
                    log.warning("[ORFS] Unknown param %s=%s (not in PARAM_SPACE), ignored",
                                name, value)
                    continue
                if spec.kind == config.KIND_MAKE_VAR:
                    # 普通参数：直接 NAME=value
                    make_vars[name] = str(value)
                elif spec.kind == config.KIND_FASTROUTE_ADJ:
                    # 伪参数 1：生成自定义 fastroute.tcl 并传绝对路径
                    tcl_path = self._write_fastroute_tcl(float(value), iteration)
                    make_vars["FASTROUTE_TCL"] = str(tcl_path)
                elif spec.kind == config.KIND_GRT_ARGS:
                    # 伪参数 2：渲染进 GLOBAL_ROUTE_ARGS（必须带上 ORFS 默认前缀）
                    make_vars["GLOBAL_ROUTE_ARGS"] = (
                        config.GLOBAL_ROUTE_ARGS_TEMPLATE.format(iters=int(value)))

        cmd = [
            "make", "-C", str(cfg.flow_dir),
            f"DESIGN_CONFIG={cfg.design_config}",
            f"FLOW_VARIANT={variant}",
        ]
        cmd += [f"{k}={v}" for k, v in sorted(make_vars.items())]
        cmd.append(target or cfg.make_target)

        log_name = f"iter{iteration}{log_suffix}.make.log"
        make_log_path = cfg.run_dir / log_name
        return cmd, make_log_path

    def _write_fastroute_tcl(self, adjustment: float, iteration: int) -> Path:
        """按模板生成本次迭代的自定义 fastroute.tcl，返回绝对路径。

        写入 run_dir（而非 objects 目录——make 运行前 objects/<variant>/ 尚不存在）。
        """
        assert self.cfg.run_dir is not None
        tcl_path = self.cfg.run_dir / f"fastroute_iter{iteration}.tcl"
        tcl_path.write_text(
            config.FASTROUTE_TCL_TEMPLATE.format(adjustment=f"{adjustment:.2f}"),
            encoding="utf-8")
        return tcl_path.resolve()

    def _run_make(self, cmd: List[str], make_log_path: Path) -> Tuple[int, bool]:
        """执行 make（stdout/stderr 流式写入日志文件），返回 (退出码, 是否超时)。

        使用 start_new_session=True 创建独立进程组：超时后 killpg 可一并杀掉
        make 派生的 yosys/openroad 子进程，避免僵尸进程继续占用 CPU。
        """
        make_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(make_log_path, "w", encoding="utf-8") as fout:
            proc = subprocess.Popen(
                cmd, cwd=self.cfg.flow_dir,
                stdout=fout, stderr=subprocess.STDOUT,
                start_new_session=True)
            try:
                returncode = proc.wait(timeout=self.cfg.timeout_s)
                return returncode, False
            except subprocess.TimeoutExpired:
                log.error("[ORFS] make timeout (>%ds), killing process group %d",
                          self.cfg.timeout_s, proc.pid)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass  # 进程恰好在超时瞬间自行退出
                proc.wait()
                return -1, True

    @staticmethod
    def _tail_log(path: Path, lines: int = 20) -> str:
        """取 make 日志尾部若干行作为错误摘要（避免整个日志塞进 history）"""
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(content.splitlines()[-lines:])
        except OSError:
            return "（无法读取 make 日志）"

    # ------------------------------------------------------------------
    # 结果解析
    # ------------------------------------------------------------------
    def parse_qor(self, variant: str) -> Optional[QoR]:
        """解析最终 QoR：6_report.json 优先，rpt/log 正则兜底，全无则 None"""
        report_json = self.cfg.logs_dir(variant) / "6_report.json"
        if report_json.is_file():
            try:
                qor = QoR.from_report_json(report_json)
                if qor.is_complete():
                    return qor
                log.warning("[ORFS] 6_report.json metrics incomplete (%s), trying rpt fallback",
                            qor.to_dict())
            except (json.JSONDecodeError, OSError) as e:
                log.warning("[ORFS] Failed to parse 6_report.json (%s), trying rpt fallback", e)

        finish_rpt = self.cfg.reports_dir(variant) / "6_finish.rpt"
        report_log = self.cfg.logs_dir(variant) / "6_report.log"
        if finish_rpt.is_file() or report_log.is_file():
            return QoR.from_reports_fallback(finish_rpt, report_log)
        return None

    def parse_stage_qor(self, variant: str) -> Dict[str, Dict[str, float]]:
        """提取各阶段的中间时序指标（ws/tns，单位 ps），供阶段智能体参考。

        JSON 中键名形如 <prefix>__timing__setup__ws，各阶段前缀不同
        （floorplan/detailedplace/cts/globalroute/detailedroute），此处按后缀
        宽松匹配而非硬编码前缀，增强对 ORFS 版本差异的鲁棒性。
        """
        logs_dir = self.cfg.logs_dir(variant)
        stage_qor: Dict[str, Dict[str, float]] = {}
        for stage, json_names in STAGE_QOR_SOURCES.items():
            merged: Dict[str, float] = {}
            for json_name in json_names:
                path = logs_dir / json_name
                if not path.is_file():
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                tag = json_name.split(".")[0]  # 如 5_1_grt
                for key, value in data.items():
                    if not isinstance(value, (int, float)):
                        continue
                    if key.endswith("__timing__setup__ws"):
                        merged[f"{tag}_ws_ps"] = round(
                            float(value) * config.TIMING_UNIT_TO_PS, 1)
                    elif key.endswith("__timing__setup__tns"):
                        merged[f"{tag}_tns_ps"] = round(
                            float(value) * config.TIMING_UNIT_TO_PS, 1)
            if merged:
                stage_qor[stage] = merged
        return stage_qor

    def detect_failed_stage(self, variant: str) -> Optional[str]:
        """按执行顺序检查各子步骤 JSON 是否存在，首个缺失者即崩溃阶段。

        全部存在返回 None（说明失败原因不在流程步骤本身，如指标解析问题）。
        """
        logs_dir = self.cfg.logs_dir(variant)
        for json_name, stage in STEP_JSON_SEQUENCE:
            if not (logs_dir / json_name).is_file():
                return stage
        return None

    # ------------------------------------------------------------------
    # 最佳结果导出
    # ------------------------------------------------------------------
    def export_best(self, variant: str, best_entry: Dict[str, Any]) -> Path:
        """把最佳迭代的产物导出到 flow/results/<plat>/<design>/agenticpd_best/。

        导出内容：
        1. 该 variant 的全部 results（GDS/DEF/网表等最终产物）；
        2. 关键报告（6_report.json / 6_finish.rpt / 6_report.log），使结果自含；
        3. agenticpd_summary.json：获胜迭代号、各阶段参数与 QoR，便于溯源。
        """
        cfg = self.cfg
        best_dir = cfg.results_dir(cfg.best_variant_name)
        src_results = cfg.results_dir(variant)
        if not src_results.is_dir():
            raise FileNotFoundError(f"最佳 variant 的 results 目录不存在：{src_results}")

        best_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_results, best_dir, dirs_exist_ok=True)

        # 附带关键报告（存在才拷，兜底解析场景下部分文件可能缺失）
        for src in (cfg.logs_dir(variant) / "6_report.json",
                    cfg.logs_dir(variant) / "6_report.log",
                    cfg.reports_dir(variant) / "6_finish.rpt"):
            if src.is_file():
                shutil.copy2(src, best_dir / src.name)

        summary_path = best_dir / "agenticpd_summary.json"
        summary_path.write_text(
            json.dumps(best_entry, ensure_ascii=False, indent=2),
            encoding="utf-8")
        log.info("[ORFS] Best result exported to %s", best_dir)
        return best_dir


class MockORFSRunner(ORFSRunner):
    """伪造的 ORFS 调用器（--mock-orfs）：不跑真实流程，按参数确定性地合成 QoR。

    用途：秒级验证优化主循环、历史持久化、最优比较、prompt 渲染等逻辑，
    不消耗任何 EDA 运行时间。合成公式无物理意义，仅保证：
    1. 相同参数 → 相同 QoR（确定性，便于断言）；
    2. 参数变化会引起 QoR 变化（让比较器/Judge 有事可做）；
    3. CORE_UTILIZATION > 48 时模拟布线失败（覆盖失败处理路径）。
    """

    def run_flow(self, stage_params: Dict[str, Dict[str, Any]],
                 variant: str, iteration: int) -> RunResult:
        # 检查模拟失败条件：利用率过高
        flat: Dict[str, float] = {}
        for params in stage_params.values():
            for k, v in params.items():
                try:
                    flat[k] = float(v)
                except (TypeError, ValueError):
                    pass
        if flat.get("CORE_UTILIZATION", 38.0) > 48:
            return RunResult(ok=False, variant=variant, failed_stage="detailedroute",
                             error="mock：利用率过高导致布线失败", elapsed_s=0.1)

        qor = self._mock_stage_qor(stage_params, "RT")
        wns = qor.wns_ps or -120.0
        stage_qor = {"FP": {"2_1_floorplan_ws_ps": round(wns + 60, 1)},
                     "PL": {"3_5_place_dp_ws_ps": round(wns + 40, 1)},
                     "CTS": {"4_1_cts_ws_ps": round(wns + 20, 1)},
                     "RT": {"5_2_route_ws_ps": wns}}
        return RunResult(ok=True, variant=variant, qor=qor, stage_qor=stage_qor,
                         elapsed_s=0.1)

    def _mock_stage_qor(self, stage_params: Dict[str, Dict[str, Any]],
                         stage: str) -> QoR:
        """mock 模式：从参数确定性推算 QoR（与 run_flow 同款公式，抽取复用）"""
        flat: Dict[str, float] = {}
        for params in stage_params.values():
            for k, v in params.items():
                try:
                    flat[k] = float(v)
                except (TypeError, ValueError):
                    pass
        util = flat.get("CORE_UTILIZATION", 38.0)
        addon = flat.get("PLACE_DENSITY_LB_ADDON", 0.10)
        cluster = flat.get("CTS_CLUSTER_SIZE", 100.0)
        adj = flat.get("FASTROUTE_LAYER_ADJUSTMENT", 0.2)
        wns = -120.0 + (40 - util) * 2.0 - abs(addon - 0.05) * 300 \
            - abs(cluster - 60) * 0.3 - abs(adj - 0.22) * 200
        wns = round(wns, 1)
        return QoR(
            wns_ps=wns,
            tns_ps=round(min(0.0, wns) * 8.0, 1),
            area_um2=round(600.0 * 38.0 / max(util, 1.0), 1),
            power_w=round(1.5e-3 + util * 1e-5 + addon * 1e-3, 6),
        )

    def run_stage(self, stage: str,
                  stage_params: Dict[str, Dict[str, Any]],
                  variant: str, iteration: int) -> Tuple[bool, Dict[str, float]]:
        """mock 模式：按合成公式返回该阶段的伪 QoR（不跑真实 make）"""
        qor = self._mock_stage_qor(stage_params, stage)
        # 模拟各阶段的中间 ws，逐阶段递减（越靠后越接近最终 WNS）
        offset_map = {"FP": 60, "PL": 40, "CTS": 20, "RT": 0}
        ws = round(qor.wns_ps + offset_map.get(stage, 0), 1)
        stage_qor: Dict[str, float] = {}
        # 按 STAGE_QOR_SOURCES 中的 tag 命名
        sources = STAGE_QOR_SOURCES.get(stage, [])
        for src_name in sources:
            tag = src_name.split(".")[0]  # 如 3_5_place_dp → 3_5_place_dp
            stage_qor[f"{tag}_ws_ps"] = ws
        log.info("#%d [MOCK] %s synthesized QoR: %s", iteration, stage,
                 ", ".join(f"{k}={v}" for k, v in sorted(stage_qor.items())))
        return True, stage_qor

    def run_finish(self, stage_params: Dict[str, Dict[str, Any]],
                   variant: str, iteration: int) -> RunResult:
        """mock 模式：合成最终 RunResult（与 run_flow 相同）"""
        return self.run_flow(stage_params, variant, iteration)

    def branch_from(self, parent_variant: str, branch_stage: str,
                    stage_params: Dict[str, Dict[str, Any]],
                    new_variant: str, iteration: int) -> RunResult:
        """mock 模式：分支 = 等同 run_flow（合成 QoR）"""
        return self.run_flow(stage_params, new_variant, iteration)

    def export_best(self, variant: str, best_entry: Dict[str, Any]) -> Path:
        """mock 模式没有真实产物，仅把 summary 写到 run_dir 下以验证调用链"""
        assert self.cfg.run_dir is not None
        summary_path = self.cfg.run_dir / "mock_best_summary.json"
        summary_path.write_text(
            json.dumps(best_entry, ensure_ascii=False, indent=2),
            encoding="utf-8")
        log.info("[MOCK] Best summary written to %s", summary_path)
        return summary_path
