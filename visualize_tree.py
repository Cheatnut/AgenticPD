# -*- coding: utf-8 -*-
"""
visualize_tree.py — Optimization tree visualization

Reads tree.json + history.json, generates a PNG of the optimization tree,
saved under runs/<run_dir>/.

Tree: 5 layers (root → FP → PL → CTS → RT), nodes within a layer ordered
left-to-right by iteration number.
- Green thick arrows: baseline path (root→iter0_FP→iter0_PL→iter0_CTS→iter0_RT)
- Red thick arrows: best path (trace of the QoR-best iteration)

Usage:
    # Call from main.py
    from visualize_tree import visualize_tree
    visualize_tree(run_dir)

    # Standalone
    python3 agenticpd/visualize_tree.py <run_dir>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import matplotlib
matplotlib.use("Agg")  # 无 GUI 后端，纯文件输出
import matplotlib.pyplot as plt
from matplotlib.patches import Circle
import matplotlib.patches as mpatches

# ---------------------------------------------------------------------------
# Layer ordering: root at top, RT at bottom
# ---------------------------------------------------------------------------
LAYER_ORDER = ["root", "FP", "PL", "CTS", "RT"]
LAYER_IDX = {s: i for i, s in enumerate(LAYER_ORDER)}  # root=0, FP=1, ...

# 基线路径：root → iter0_FP → iter0_PL → iter0_CTS → iter0_RT
BASELINE_EDGES = {
    ("root", "iter0_FP"),
    ("iter0_FP", "iter0_PL"),
    ("iter0_PL", "iter0_CTS"),
    ("iter0_CTS", "iter0_RT"),
}


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------

def load_tree(tree_path: Path) -> Dict[str, Dict[str, Any]]:
    """Load tree.json, returning {node_id: node_dict}."""
    if not tree_path.is_file():
        raise FileNotFoundError(f"tree.json not found: {tree_path}")
    data = json.loads(tree_path.read_text(encoding="utf-8"))
    return data.get("nodes", {})


def load_history(history_path: Path) -> List[Dict[str, Any]]:
    """Load history.json, returning entries as a list."""
    if not history_path.is_file():
        return []
    return json.loads(history_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 最佳迭代查找（复用 utils.qor_is_better 逻辑，但内联以避免循环导入）
# ---------------------------------------------------------------------------

def _qor_from_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Optional[float]]]:
    """从 history 条目中提取 QoR 字典。"""
    qor = entry.get("qor")
    if not isinstance(qor, dict):
        return None
    # 四项指标必须全部存在才算完整
    for key in ("wns_ps", "tns_ps", "area_um2", "power_w"):
        if qor.get(key) is None:
            return None
    return qor


def _qor_is_better(new: Dict[str, Optional[float]],
                   old: Optional[Dict[str, Optional[float]]],
                   wns_tol: float = 10.0,
                   tns_tol: float = 50.0) -> bool:
    """判断 new 是否严格优于 old（与 utils.qor_is_better 逻辑一致）。"""
    if old is None:
        return True

    nw, nt, na, np_ = (new["wns_ps"], new["tns_ps"],
                        new["area_um2"], new["power_w"])
    ow, ot, oa, op = (old["wns_ps"], old["tns_ps"],
                       old["area_um2"], old["power_w"])

    # 双方都已收敛：比功耗/面积
    both_met = (nw >= 0 and ow >= 0)
    if not both_met:
        if abs(nw - ow) > wns_tol:
            return nw > ow
        if abs(nt - ot) > tns_tol:
            return nt > ot
    if np_ != op:
        return np_ < op
    if na != oa:
        return na < oa
    return False


def find_best_iteration(history: List[Dict[str, Any]]) -> Optional[int]:
    """从 history 中找出 QoR 最优的迭代号。"""
    best_qor: Optional[Dict[str, Optional[float]]] = None
    best_iter: Optional[int] = None
    for entry in history:
        if entry.get("status") != "ok":
            continue
        qor = _qor_from_entry(entry)
        if qor is None:
            continue
        if _qor_is_better(qor, best_qor):
            best_qor = qor
            best_iter = entry.get("iteration")
    return best_iter


# ---------------------------------------------------------------------------
# 树结构处理
# ---------------------------------------------------------------------------

def organize_layers(nodes: Dict[str, Dict[str, Any]]
                    ) -> Dict[str, List[str]]:
    """将节点按 stage 分组，每组内按 iteration 升序排列。
    返回 {stage: [node_id, ...]}。
    """
    layers: Dict[str, List[Tuple[int, str]]] = {s: [] for s in LAYER_ORDER}
    for nid, nd in nodes.items():
        stage = nd.get("stage", "")
        if stage not in LAYER_IDX:
            continue
        iteration = nd.get("iteration", -1)
        layers[stage].append((iteration, nid))
    # 每层内按 iteration 排序
    result: Dict[str, List[str]] = {}
    for stage in LAYER_ORDER:
        result[stage] = [nid for _, nid in sorted(layers[stage])]
    return result


def trace_path_to_root(node_id: str,
                       nodes: Dict[str, Dict[str, Any]]) -> List[str]:
    """从 node_id 沿 parent_id 追溯到 root，返回 [root, ..., node_id] 路径。"""
    path: List[str] = []
    current: Optional[str] = node_id
    while current is not None:
        path.append(current)
        nd = nodes.get(current)
        current = nd.get("parent_id") if nd else None
    path.reverse()
    return path


def get_best_path_edges(nodes: Dict[str, Dict[str, Any]],
                        best_iter: int) -> Set[Tuple[str, str]]:
    """返回最佳迭代的完整路径边集合。
    从 iter{best_iter}_RT 追溯到 root，取相邻节点对。
    若该迭代无 RT 节点（失败），则取最深存在节点。
    """
    # 找该迭代在树中最深的节点
    for stage in reversed(LAYER_ORDER[1:]):  # RT → CTS → PL → FP
        candidate = f"iter{best_iter}_{stage}"
        if candidate in nodes:
            path = trace_path_to_root(candidate, nodes)
            return {(path[i], path[i + 1]) for i in range(len(path) - 1)}
    return set()


# ---------------------------------------------------------------------------
# 主绘制函数
# ---------------------------------------------------------------------------

def visualize_tree(run_dir: Path,
                   output_name: str = "optimization_tree.png") -> Optional[Path]:
    """生成优化树 PNG 图片，保存到 run_dir 下。

    参数:
        run_dir:      runs/<timestamp>/ 目录，内含 tree.json 和 history.json
        output_name:  输出文件名（默认 optimization_tree.png）

    返回:
        输出文件路径；若 tree.json 不存在则返回 None
    """
    tree_path = run_dir / "tree.json"
    if not tree_path.is_file():
        print(f"[visualize] tree.json not found at {tree_path}, skipping")
        return None

    nodes = load_tree(tree_path)
    history_path = run_dir / "history.json"
    history = load_history(history_path) if history_path.is_file() else []
    best_iter = find_best_iteration(history)

    # ---- 层级分组 ----
    layers = organize_layers(nodes)

    # ---- 最佳路径边集 ----
    best_edges: Set[Tuple[str, str]] = set()
    if best_iter is not None:
        best_edges = get_best_path_edges(nodes, best_iter)

    # ---- 布局参数（动态调整） ----
    max_nodes_per_layer = max(len(v) for v in layers.values())
    # 水平间距：最少 2.2 单位，节点多时适当加宽
    h_spacing = max(2.2, 12.0 / max(max_nodes_per_layer, 1))
    v_spacing = 2.8          # 层间垂直间距
    node_radius = 0.42       # 圆圈半径
    x_margin = 1.5           # 左右边距
    y_margin = 1.2           # 上下边距

    # 动态图幅（英寸）
    n_layers = len(LAYER_ORDER)
    fig_w = max(10, h_spacing * max_nodes_per_layer + 2 * x_margin)
    fig_h = v_spacing * (n_layers - 1) + 2 * y_margin
    # 限制最大尺寸
    fig_w = min(fig_w, 24)
    fig_h = min(fig_h, 16)

    dpi = 150  # 保证清晰度

    # ---- 计算每个节点的坐标 ----
    # y: root 在顶部 (y 最大)，RT 在底部 (y 最小)
    coords: Dict[str, Tuple[float, float]] = {}
    for stage in LAYER_ORDER:
        nids = layers[stage]
        y = (n_layers - 1 - LAYER_IDX[stage]) * v_spacing + y_margin
        if len(nids) == 1:
            # 单节点居中
            xs = [fig_w / 2]
        else:
            # 多节点均匀分布
            start_x = (fig_w - h_spacing * (len(nids) - 1)) / 2
            xs = [start_x + i * h_spacing for i in range(len(nids))]
        for nid, x in zip(nids, xs):
            coords[nid] = (x, y)

    # ---- 创建图形 ----
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.set_aspect("equal")
    ax.axis("off")

    # ---- 字体大小（随节点数微调） ----
    base_font = max(8, min(11, 13 - max_nodes_per_layer * 0.3))
    label_font = base_font + 2

    # ---- 收集所有边并分类 ----
    all_edges: List[Tuple[str, str]] = []  # (parent_id, child_id)
    for nid, nd in nodes.items():
        parent_id = nd.get("parent_id")
        if parent_id and parent_id in nodes:
            all_edges.append((parent_id, nid))

    # 若最佳路径与基线路径完全重合，则基线边不单独绘制（避免红绿重叠）
    baseline_edges_to_draw = BASELINE_EDGES
    if best_edges and best_edges == BASELINE_EDGES:
        baseline_edges_to_draw = set()

    normal_edges = [(p, c) for p, c in all_edges
                    if (p, c) not in baseline_edges_to_draw
                    and (p, c) not in best_edges]

    # ---- 绘制普通边（灰色细线） ----
    for parent_id, child_id in normal_edges:
        if parent_id in coords and child_id in coords:
            px, py = coords[parent_id]
            cx, cy = coords[child_id]
            ax.annotate("", xy=(cx, cy + node_radius), xytext=(px, py - node_radius),
                        arrowprops=dict(arrowstyle="->", color="#888888",
                                        lw=0.8, alpha=0.5,
                                        connectionstyle="arc3,rad=0"))

    # ---- 绘制基线路径（绿色粗线） ----
    for parent_id, child_id in baseline_edges_to_draw:
        if parent_id in coords and child_id in coords:
            px, py = coords[parent_id]
            cx, cy = coords[child_id]
            ax.annotate("", xy=(cx, cy + node_radius), xytext=(px, py - node_radius),
                        arrowprops=dict(arrowstyle="->", color="#2ca02c",
                                        lw=2.5, alpha=0.85,
                                        connectionstyle="arc3,rad=0"))

    # ---- 绘制最佳路径（红色粗线） ----
    for parent_id, child_id in best_edges:
        if parent_id in coords and child_id in coords:
            px, py = coords[parent_id]
            cx, cy = coords[child_id]
            ax.annotate("", xy=(cx, cy + node_radius), xytext=(px, py - node_radius),
                        arrowprops=dict(arrowstyle="->", color="#d62728",
                                        lw=2.5, alpha=0.85,
                                        connectionstyle="arc3,rad=0"))

    # ---- 绘制节点圆圈 + 文字 ----
    for nid, (x, y) in coords.items():
        nd = nodes.get(nid, {})
        stage = nd.get("stage", "")
        iteration = nd.get("iteration", -1)

        # 节点显示文字：root → "Root"，其他 → "迭代号_阶段"
        if stage == "root":
            label = "Root"
        else:
            label = f"{iteration}_{stage}"

        # 圆圈
        circle = Circle((x, y), node_radius, facecolor="white",
                        edgecolor="#333333", linewidth=1.5, zorder=10, clip_on=False)
        ax.add_patch(circle)

        # 圆圈内文字
        fontsize = base_font if len(label) <= 7 else base_font - 1
        ax.text(x, y, label, ha="center", va="center",
                fontsize=fontsize, fontweight="bold",
                fontfamily="monospace", zorder=11)

    # ---- 图例 ----
    legend_elements = []
    if baseline_edges_to_draw:
        legend_elements.append(
            mpatches.Patch(color="#2ca02c", alpha=0.85,
                           label="Baseline path (root→0_FP→0_PL→0_CTS→0_RT)"))
    if best_edges:
        label = f"Best path (iter #{best_iter})"
        if best_edges == BASELINE_EDGES:
            label += " = baseline"
        legend_elements.append(
            mpatches.Patch(color="#d62728", alpha=0.85, label=label))
    if legend_elements:
        ax.legend(handles=legend_elements, loc="upper right",
                  fontsize=9, framealpha=0.85, edgecolor="#cccccc")

    # ---- 标题 ----
    title = "AgenticPD Optimization Tree"
    if best_iter is not None:
        title += f"  —  Global Best: Iter #{best_iter}"
    ax.set_title(title, fontsize=label_font + 2, fontweight="bold", pad=12)

    # ---- 保存 ----
    output_path = run_dir / output_name
    fig.savefig(output_path, dpi=dpi, bbox_inches="tight",
                facecolor="white", edgecolor="none")
    plt.close(fig)

    print(f"[visualize] Tree image saved to {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Standalone CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python3 {__file__} <run_dir>")
        print(f"Example: python3 {__file__} runs/20260718_210019")
        sys.exit(1)

    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))

    run_dir = Path(sys.argv[1])
    if not run_dir.is_dir():
        print(f"Error: directory not found: {run_dir}")
        sys.exit(1)

    result = visualize_tree(run_dir)
    if result is None:
        print("Cannot generate visualization (tree.json not found or empty)")
        sys.exit(1)
