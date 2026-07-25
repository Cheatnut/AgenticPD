# -*- coding: utf-8 -*-
"""
utils.py — AgenticPD 辅助工具模块

包含：
1. 日志初始化（控制台 + 文件双通道）
2. .env 文件解析（手写实现，避免强依赖 python-dotenv）
3. LLM 输出的健壮 JSON 提取（剥 markdown 围栏、截取首尾大括号）
4. QoR 数据类及其解析（JSON metrics 为主、rpt/log 正则兜底）
5. QoR 优劣比较器（WNS 优先 → TNS → 功耗 → 面积，带容差）
6. 历史记录的原子化落盘/加载（防崩溃损坏）

直接运行本文件可执行内置自测：python3 agenticpd/utils.py
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def setup_logging(log_file: Optional[Path] = None,
                  level: int = logging.INFO) -> logging.Logger:
    """初始化 root logger：控制台 + 可选文件双通道输出。

    重复调用是安全的（会先清空已有 handler），方便测试场景反复初始化。
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    return root


# ---------------------------------------------------------------------------
# .env 解析
# ---------------------------------------------------------------------------

def load_dotenv_file(path: Path) -> None:
    """解析 KEY=VALUE 形式的 .env 文件并写入 os.environ。

    规则：忽略空行与 # 注释；不覆盖已存在的环境变量（环境优先于文件）；
    值两侧的单/双引号会被剥掉。文件不存在时静默返回（key 可能直接来自环境）。
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# 健壮 JSON 提取
# ---------------------------------------------------------------------------

class JsonParseError(Exception):
    """LLM 输出无法解析为 JSON 时抛出；raw 字段保留原始文本便于回喂重问"""

    def __init__(self, reason: str, raw: str):
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


def extract_json(text: str) -> Dict[str, Any]:
    """从 LLM 输出文本中健壮地提取一个 JSON 对象。

    处理策略（逐步降级）：
    1. 剥掉 ```json ... ``` 或 ``` ... ``` 的 markdown 代码围栏；
    2. 直接尝试 json.loads；
    3. 失败则截取首个 '{' 到最后一个 '}' 的子串再尝试；
    4. 仍失败则抛 JsonParseError（调用方据此回喂 LLM 重问）。
    """
    if not isinstance(text, str) or not text.strip():
        raise JsonParseError("LLM 返回内容为空", raw=str(text))

    cleaned = text.strip()
    # 剥 markdown 代码围栏（```json 或 ```），允许围栏前后有其他闲聊文本
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    for candidate in (cleaned,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 截取首尾大括号之间的内容（应对 JSON 前后混杂说明文字的情况）
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(cleaned[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            raise JsonParseError(f"JSON 语法错误: {e}", raw=text)

    raise JsonParseError("文本中找不到 JSON 对象", raw=text)


# ---------------------------------------------------------------------------
# QoR 数据类与解析
# ---------------------------------------------------------------------------

@dataclass
class QoR:
    """四项 QoR 指标。时序单位 ps（负值代表违例），面积 μm²，功耗 W。

    wns_ps 语义说明：取自 ORFS 的 worst slack（可为正，正值表示时序收敛且有裕量），
    与论文中"最差负时序裕量"兼容——比较器把 >=0 视为时序已收敛。
    """

    wns_ps: Optional[float] = None
    tns_ps: Optional[float] = None
    area_um2: Optional[float] = None
    power_w: Optional[float] = None

    def is_complete(self) -> bool:
        """四项指标是否全部解析成功（缺任一项的 QoR 不参与最优比较）"""
        return all(v is not None for v in
                   (self.wns_ps, self.tns_ps, self.area_um2, self.power_w))

    def to_dict(self) -> Dict[str, Optional[float]]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["QoR"]:
        if not d:
            return None
        return cls(wns_ps=d.get("wns_ps"), tns_ps=d.get("tns_ps"),
                   area_um2=d.get("area_um2"), power_w=d.get("power_w"))

    def pretty(self) -> str:
        """人类可读的单行摘要（也用于 prompt 中的历史序列化）"""
        def fmt(v: Optional[float], spec: str, unit: str) -> str:
            return "N/A" if v is None else format(v, spec) + unit
        return (f"WNS={fmt(self.wns_ps, '.1f', 'ps')} "
                f"TNS={fmt(self.tns_ps, '.1f', 'ps')} "
                f"Area={fmt(self.area_um2, '.1f', 'um2')} "
                f"Power={fmt((self.power_w or 0) * 1e3 if self.power_w is not None else None, '.4f', 'mW')}")

    # ------------------------------------------------------------------
    # 解析入口 1：JSON metrics（首选，最健壮）
    # ------------------------------------------------------------------
    @classmethod
    def from_report_json(cls, report_json: Path) -> "QoR":
        """从 finish 阶段的 6_report.json 解析 QoR。

        关键键（时序单位 ns，×1000 转 ps）：
          finish__timing__setup__ws / finish__timing__setup__tns
          finish__power__total / finish__design__instance__area

        注意：finish__design__instance__area 在文件中出现两次（前者含 fill cell，
        后者为纯标准单元面积才是正确值）。CPython 标准库 json.load 对重复键采用
        "后者覆盖前者"策略，恰好得到正确值 —— 此行为是本解析正确性的前提，
        切勿换用对重复键报错/取前者的解析器。
        """
        data = json.loads(report_json.read_text(encoding="utf-8"))
        scale = config.TIMING_UNIT_TO_PS

        def get(key: str) -> Optional[float]:
            v = data.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        ws = get("finish__timing__setup__ws")
        tns = get("finish__timing__setup__tns")
        return cls(
            wns_ps=ws * scale if ws is not None else None,
            tns_ps=tns * scale if tns is not None else None,
            area_um2=get("finish__design__instance__area"),
            power_w=get("finish__power__total"),
        )

    # ------------------------------------------------------------------
    # 解析入口 2：rpt/log 正则兜底（JSON 缺失时使用）
    # ------------------------------------------------------------------
    # 6_finish.rpt 中的时序摘要行（re.MULTILINE 逐行匹配）：
    #   "worst slack max 0.02"  —— 取 worst slack 作为 WNS（与 JSON 的 ws 语义一致；
    #                              rpt 里的 "wns max" 行在时序收敛时被钳为 0.00，
    #                              丢失正裕量信息，故不用它）
    #   "tns max 0.00"
    _RE_WORST_SLACK = re.compile(r"^worst slack max\s+(-?[\d.]+)", re.MULTILINE)
    _RE_TNS = re.compile(r"^tns max\s+(-?[\d.]+)", re.MULTILINE)
    # report_power 表格的 Total 行，第 4 个数值列为总功耗（W）：
    #   "Total                  1.41e-03   1.19e-03   1.70e-05   2.62e-03 100.0%"
    _RE_POWER_TOTAL = re.compile(
        r"^Total\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)",
        re.MULTILINE)
    # 面积行只存在于 6_report.log（6_finish.rpt 中没有！）：
    #   "Design area 716 um^2 62% utilization."
    _RE_DESIGN_AREA = re.compile(r"^Design area\s+([\d.]+)\s+um\^2", re.MULTILINE)

    @classmethod
    def from_reports_fallback(cls, finish_rpt: Path, report_log: Path) -> "QoR":
        """正则解析 6_finish.rpt（时序/功耗）与 6_report.log（面积）的兜底路径"""
        qor = cls()
        scale = config.TIMING_UNIT_TO_PS

        if finish_rpt.is_file():
            text = finish_rpt.read_text(encoding="utf-8", errors="replace")
            m = cls._RE_WORST_SLACK.search(text)
            if m:
                qor.wns_ps = float(m.group(1)) * scale
            m = cls._RE_TNS.search(text)
            if m:
                qor.tns_ps = float(m.group(1)) * scale
            m = cls._RE_POWER_TOTAL.search(text)
            if m:
                qor.power_w = float(m.group(4))

        if report_log.is_file():
            text = report_log.read_text(encoding="utf-8", errors="replace")
            m = cls._RE_DESIGN_AREA.search(text)
            if m:
                qor.area_um2 = float(m.group(1))

        return qor


# ---------------------------------------------------------------------------
# QoR 优劣比较器
# ---------------------------------------------------------------------------

def qor_is_better(new: Optional[QoR], old: Optional[QoR],
                  wns_tol_ps: float, tns_tol_ps: float) -> bool:
    """判断 new 是否严格优于 old（用于最优结果更新，打平时保留 old）。

    比较优先级（与论文一致，带容差避免噪声主导）：
    1. 失败/不完整的 QoR 恒输；
    2. 双方 WNS 均 >= 0（时序均已收敛）→ 多余正裕量无价值，直接比功耗/面积；
    3. |ΔWNS| > wns_tol_ps → WNS 大者（违例更小者）胜；
    4. |ΔTNS| > tns_tol_ps → TNS 大者胜；
    5. 功耗小者胜；仍平则面积小者胜；完全打平判 new 不优（保守）。
    """
    if new is None or not new.is_complete():
        return False
    if old is None or not old.is_complete():
        return True

    both_met = new.wns_ps >= 0 and old.wns_ps >= 0
    if not both_met:
        if abs(new.wns_ps - old.wns_ps) > wns_tol_ps:
            return new.wns_ps > old.wns_ps
        if abs(new.tns_ps - old.tns_ps) > tns_tol_ps:
            return new.tns_ps > old.tns_ps

    if new.power_w != old.power_w:
        return new.power_w < old.power_w
    if new.area_um2 != old.area_um2:
        return new.area_um2 < old.area_um2
    return False


# ---------------------------------------------------------------------------
# 历史记录持久化
# ---------------------------------------------------------------------------

def save_history_atomic(path: Path, history: List[Dict[str, Any]]) -> None:
    """原子化写入历史记录：先写 .tmp 再 os.replace，中途崩溃不会损坏旧文件"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def load_history(path: Path) -> List[Dict[str, Any]]:
    """加载历史记录；文件损坏时改名为 .corrupt 并返回空列表（重新开始）"""
    log = logging.getLogger("utils")
    if not path.is_file():
        return []
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(history, list):
            return history
        raise ValueError("history 顶层不是列表")
    except (json.JSONDecodeError, ValueError) as e:
        corrupt = path.with_suffix(path.suffix + ".corrupt")
        os.replace(path, corrupt)
        log.warning("[utils] History corrupted (%s), renamed to %s, restarting", e, corrupt)
        return []


def save_tree_atomic(path: Path, tree) -> None:
    """原子化写入树 JSON。此处仅为避免循环导入的桩：
    实际调用转发到 optimization_tree.save_tree_atomic。
    """
    from optimization_tree import save_tree_atomic as _impl
    _impl(path, tree)


def load_tree(path: Path):
    """加载树 JSON。实际调用转发到 optimization_tree.load_tree。"""
    from optimization_tree import load_tree as _impl
    return _impl(path)


# ---------------------------------------------------------------------------
# 内置自测：python3 agenticpd/utils.py
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """extract_json 脏样本 + qor_is_better 手写用例表的快速自测"""
    # --- extract_json ---
    cases_ok = [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        '好的，参数如下：\n```\n{"a": 1}\n```\n以上。',
        '前置说明 {"a": 1} 后置说明',
    ]
    for c in cases_ok:
        assert extract_json(c) == {"a": 1}, f"extract_json 失败: {c!r}"
    for bad in ("", "完全没有 json", '{"a": }'):
        try:
            extract_json(bad)
            raise AssertionError(f"应当抛出 JsonParseError: {bad!r}")
        except JsonParseError:
            pass

    # --- qor_is_better（容差 wns=10ps, tns=50ps）---
    q = lambda w, t, a, p: QoR(wns_ps=w, tns_ps=t, area_um2=a, power_w=p)
    table = [
        # (new, old, 期望, 说明)
        (q(-50, -500, 700, 2e-3), None, True, "old 为 None 时 new 恒胜"),
        (None, q(-50, -500, 700, 2e-3), False, "new 为 None 恒输"),
        (q(-30, -500, 700, 2e-3), q(-50, -500, 700, 2e-3), True, "WNS 差>容差, 大者胜"),
        (q(-45, -300, 700, 2e-3), q(-50, -500, 700, 2e-3), True, "WNS 打平比 TNS"),
        (q(-45, -480, 700, 1e-3), q(-50, -500, 700, 2e-3), True, "WNS/TNS 均平比功耗"),
        (q(-45, -480, 600, 2e-3), q(-50, -500, 700, 2e-3), True, "功耗平比面积"),
        (q(5, 0, 700, 2e-3), q(20, 0, 700, 1e-3), False, "双方收敛比功耗, new 更费电"),
        (q(5, 0, 700, 1e-3), q(20, 0, 700, 2e-3), True, "双方收敛比功耗, new 省电胜"),
        (q(-50, -500, 700, 2e-3), q(-50, -500, 700, 2e-3), False, "完全打平保留 old"),
        (q(None, -500, 700, 2e-3), q(-50, -500, 700, 2e-3), False, "不完整 QoR 恒输"),
    ]
    for new, old, expect, desc in table:
        got = qor_is_better(new, old, wns_tol_ps=10.0, tns_tol_ps=50.0)
        assert got == expect, f"qor_is_better 用例失败: {desc} (got={got})"

    print("utils.py 自测全部通过 ✔")


if __name__ == "__main__":
    _self_test()
