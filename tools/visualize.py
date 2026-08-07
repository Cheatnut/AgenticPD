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
matplotlib.use("Agg")  # headless backend, file output only
import matplotlib.pyplot as plt
from matplotlib.patches import Circle
import matplotlib.patches as mpatches

# ---------------------------------------------------------------------------
# Layer ordering: root at top, RT at bottom
# ---------------------------------------------------------------------------
LAYER_ORDER = ["root", "FP", "PL", "CTS", "RT"]
LAYER_IDX = {s: i for i, s in enumerate(LAYER_ORDER)}  # root=0, FP=1, ...

# Baseline path: root → iter0_FP → iter0_PL → iter0_CTS → iter0_RT
BASELINE_EDGES = {
    ("root", "iter0_FP"),
    ("iter0_FP", "iter0_PL"),
    ("iter0_PL", "iter0_CTS"),
    ("iter0_CTS", "iter0_RT"),
}


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_tree(tree_path: Path) -> Dict[str, Dict[str, Any]]:
    """Load tree.json, returning {node_id: node_dict}."""
    if not tree_path.is_file():
        raise FileNotFoundError(f"tree.json not found: {tree_path}")
    data = json.loads(tree_path.read_text(encoding="utf-8"))
    return data.get("nodes", {})


def load_history(history_path: Path) -> List[Dict[str, Any]]:
    """Load trial history from trials.jsonl (one JSON object per line)."""
    if not history_path.is_file():
        return []
    text = history_path.read_text(encoding="utf-8")
    if text.strip().startswith("{"):
        # trials.jsonl: one TrialRecord per line.
        # Dedup by trial_id (last-wins — create=“running” then update=“ok”),
        # keep only “ok” trials, assign sequential iteration numbers starting
        # from 0 so they align with tree node names (iter0_*, iter1_*, …).
        # Dedup by trial_id (last-wins: create="running" → update="ok"),
        # keeping first-appearance order for chronological iteration numbering.
        trials: dict[str, dict] = {}
        trial_order: list[str] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                tr = json.loads(line)
            except json.JSONDecodeError:
                continue
            tid = tr.get("trial_id")
            if not tid:
                continue
            if tid not in trial_order:
                trial_order.append(tid)
            trials[tid] = tr  # last-wins dedup (status "running" → "ok")

        entries = []
        for tid in trial_order:  # chronological order
            tr = trials[tid]
            if tr.get("status") != "ok":
                continue
            entries.append({
                "iteration": len(entries),  # 0, 1, 2, … — matches tree
                "status": "ok",
                "params": tr.get("params", {}),
                "qor": tr.get("final_qor"),
                "elapsed_s": sum(
                    sr.get("elapsed_s", 0) for sr in tr.get("stage_results", [])
                ),
            })
        return entries
    return []


# ---------------------------------------------------------------------------
# Best iteration lookup (reuses utils.qor_is_better logic, inlined to
# avoid circular imports)
# ---------------------------------------------------------------------------

def _qor_from_entry(entry: Dict[str, Any]) -> Optional[Dict[str, Optional[float]]]:
    """Extract QoR dict from a history entry."""
    qor = entry.get("qor")
    if not isinstance(qor, dict):
        return None
    # All four metrics must be present to count as complete
    for key in ("wns_ps", "tns_ps", "area_um2", "power_w"):
        if qor.get(key) is None:
            return None
    return qor


def _qor_is_better(new: Dict[str, Optional[float]],
                   old: Optional[Dict[str, Optional[float]]],
                   wns_tol: float = 10.0,
                   tns_tol: float = 50.0) -> bool:
    """Whether `new` is strictly better than `old` (consistent with
    utils.qor_is_better logic)."""
    if old is None:
        return True

    nw, nt, na, np_ = (new["wns_ps"], new["tns_ps"],
                       new["area_um2"], new["power_w"])
    ow, ot, oa, op = (old["wns_ps"], old["tns_ps"],
                      old["area_um2"], old["power_w"])

    # Both converged: compare power/area
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
    """Find the iteration with the best QoR from history."""
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
# Tree structure processing
# ---------------------------------------------------------------------------

def organize_layers(nodes: Dict[str, Dict[str, Any]]
                    ) -> Dict[str, List[str]]:
    """Group nodes by stage, sort each group by iteration ascending.
    Returns {stage: [node_id, ...]}.
    """
    layers: Dict[str, List[Tuple[int, str]]] = {s: [] for s in LAYER_ORDER}
    for nid, nd in nodes.items():
        stage = nd.get("stage", "")
        if stage not in LAYER_IDX:
            continue
        iteration = nd.get("iteration", -1)
        layers[stage].append((iteration, nid))
    # Sort each layer by iteration
    result: Dict[str, List[str]] = {}
    for stage in LAYER_ORDER:
        result[stage] = [nid for _, nid in sorted(layers[stage])]
    return result


def trace_path_to_root(node_id: str,
                       nodes: Dict[str, Dict[str, Any]]) -> List[str]:
    """Trace from node_id along parent_id back to root,
    returning [root, ..., node_id] path."""
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
    """Return the set of edges on the best iteration's complete path.
    Trace from iter{best_iter}_RT back to root, taking adjacent node pairs.
    If that iteration has no RT node (failed), take the deepest existing node.
    """
    # Find the deepest node for this iteration in the tree
    for stage in reversed(LAYER_ORDER[1:]):  # RT → CTS → PL → FP
        candidate = f"iter{best_iter}_{stage}"
        if candidate in nodes:
            path = trace_path_to_root(candidate, nodes)
            return {(path[i], path[i + 1]) for i in range(len(path) - 1)}
    return set()


# ---------------------------------------------------------------------------
# Main drawing function
# ---------------------------------------------------------------------------

def visualize_tree(run_dir: Path,
                   output_name: str = "optimization_tree.png") -> Optional[Path]:
    """Generate optimization tree PNG, saved under run_dir.

    Args:
        run_dir:      runs/<timestamp>/ directory, containing tree.json and trials.jsonl
        output_name:  output filename (default optimization_tree.png)

    Returns:
        Output file path; None if tree.json not found.
    """
    tree_path = run_dir / "tree.json"
    if not tree_path.is_file():
        print(f"[visualize] tree.json not found at {tree_path}, skipping")
        return None

    nodes = load_tree(tree_path)
    history_path = run_dir / "trials.jsonl"
    history = load_history(history_path) if history_path.is_file() else []
    best_iter = find_best_iteration(history)

    # ---- Layer grouping ----
    layers = organize_layers(nodes)

    # ---- Best-path edge set ----
    best_edges: Set[Tuple[str, str]] = set()
    if best_iter is not None:
        best_edges = get_best_path_edges(nodes, best_iter)

    # ---- Layout parameters (dynamic sizing) ----
    max_nodes_per_layer = max(len(v) for v in layers.values())
    # Horizontal spacing: at least 2.2 units, wider when nodes are dense
    h_spacing = max(2.2, 12.0 / max(max_nodes_per_layer, 1))
    v_spacing = 2.8          # inter-layer vertical spacing
    node_radius = 0.42       # circle radius
    x_margin = 1.5           # left/right margin
    y_margin = 1.2           # top/bottom margin

    # Dynamic figure size (inches)
    n_layers = len(LAYER_ORDER)
    fig_w = max(10, h_spacing * max_nodes_per_layer + 2 * x_margin)
    fig_h = v_spacing * (n_layers - 1) + 2 * y_margin
    # Cap maximum size
    fig_w = min(fig_w, 24)
    fig_h = min(fig_h, 16)

    dpi = 150  # ensures clarity

    # ---- Compute coordinates for each node ----
    # y: root at top (largest y), RT at bottom (smallest y)
    coords: Dict[str, Tuple[float, float]] = {}
    for stage in LAYER_ORDER:
        nids = layers[stage]
        y = (n_layers - 1 - LAYER_IDX[stage]) * v_spacing + y_margin
        if len(nids) == 1:
            # Single node centered
            xs = [fig_w / 2]
        else:
            # Multiple nodes evenly distributed
            start_x = (fig_w - h_spacing * (len(nids) - 1)) / 2
            xs = [start_x + i * h_spacing for i in range(len(nids))]
        for nid, x in zip(nids, xs):
            coords[nid] = (x, y)

    # ---- Create figure ----
    fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=dpi)
    ax.set_xlim(0, fig_w)
    ax.set_ylim(0, fig_h)
    ax.set_aspect("equal")
    ax.axis("off")

    # ---- Font size (scales slightly with node count) ----
    base_font = max(8, min(11, 13 - max_nodes_per_layer * 0.3))
    label_font = base_font + 2

    # ---- Collect and classify all edges ----
    all_edges: List[Tuple[str, str]] = []  # (parent_id, child_id)
    for nid, nd in nodes.items():
        parent_id = nd.get("parent_id")
        if parent_id and parent_id in nodes:
            all_edges.append((parent_id, nid))

    # If best path fully coincides with baseline path, skip green edges
    # (avoid red-green overlap)
    baseline_edges_to_draw = BASELINE_EDGES
    if best_edges and best_edges == BASELINE_EDGES:
        baseline_edges_to_draw = set()

    normal_edges = [(p, c) for p, c in all_edges
                    if (p, c) not in baseline_edges_to_draw
                    and (p, c) not in best_edges]

    # ---- Draw normal edges (thin gray) ----
    for parent_id, child_id in normal_edges:
        if parent_id in coords and child_id in coords:
            px, py = coords[parent_id]
            cx, cy = coords[child_id]
            ax.annotate("", xy=(cx, cy + node_radius), xytext=(px, py - node_radius),
                        arrowprops=dict(arrowstyle="->", color="#888888",
                                        lw=0.8, alpha=0.5,
                                        connectionstyle="arc3,rad=0"))

    # ---- Draw baseline path (thick green) ----
    for parent_id, child_id in baseline_edges_to_draw:
        if parent_id in coords and child_id in coords:
            px, py = coords[parent_id]
            cx, cy = coords[child_id]
            ax.annotate("", xy=(cx, cy + node_radius), xytext=(px, py - node_radius),
                        arrowprops=dict(arrowstyle="->", color="#2ca02c",
                                        lw=2.5, alpha=0.85,
                                        connectionstyle="arc3,rad=0"))

    # ---- Draw best path (thick red) ----
    for parent_id, child_id in best_edges:
        if parent_id in coords and child_id in coords:
            px, py = coords[parent_id]
            cx, cy = coords[child_id]
            ax.annotate("", xy=(cx, cy + node_radius), xytext=(px, py - node_radius),
                        arrowprops=dict(arrowstyle="->", color="#d62728",
                                        lw=2.5, alpha=0.85,
                                        connectionstyle="arc3,rad=0"))

    # ---- Draw node circles + labels ----
    for nid, (x, y) in coords.items():
        nd = nodes.get(nid, {})
        stage = nd.get("stage", "")
        iteration = nd.get("iteration", -1)

        # Node label: root → "Root", others → "iteration_stage"
        if stage == "root":
            label = "Root"
        else:
            label = f"{iteration}_{stage}"

        # Circle
        circle = Circle((x, y), node_radius, facecolor="white",
                        edgecolor="#333333", linewidth=1.5, zorder=10, clip_on=False)
        ax.add_patch(circle)

        # Text inside circle
        fontsize = base_font if len(label) <= 7 else base_font - 1
        ax.text(x, y, label, ha="center", va="center",
                fontsize=fontsize, fontweight="bold",
                fontfamily="monospace", zorder=11)

    # ---- Legend ----
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

    # ---- Title ----
    title = "AgenticPD Optimization Tree"
    if best_iter is not None:
        title += f"  —  Global Best: Iter #{best_iter}"
    ax.set_title(title, fontsize=label_font + 2, fontweight="bold", pad=12)

    # ---- Save ----
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
