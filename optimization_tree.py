# -*- coding: utf-8 -*-
"""
optimization_tree.py — AgenticPD optimization tree

Paper §3 requires: all historical execution results organized as a rooted tree T.
The root node n_0 represents the post-synthesis netlist. Each time a stage s is
executed, a node n_k^s = (a_k(s), Q_k(s)) is created.

Every complete path from root to leaf (FP→PL→CTS→RT) corresponds to a complete
action tuple a_k. When branching, a new subtree is mounted from intermediate node
n_hat, reusing Bef(b) results and re-executing {b} ∪ Aft(b).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config

log = logging.getLogger("tree")

# Fixed root node identifier
ROOT_ID = "root"


@dataclass
class OptimNode:
    """A node in the optimization tree, representing one iteration's execution
    record at a specific stage"""

    node_id: str                       # "root" | "iter0_FP" | "iter2_CTS"
    iteration: int                     # iteration that created this node
    stage: str                         # "root" | "FP" | "PL" | "CTS" | "RT"
    variant: str                       # FLOW_VARIANT where this round's artifacts live
    params: Dict[str, Any] = field(default_factory=dict)   # this stage's params only
    stage_qor: Optional[Dict[str, float]] = None   # intermediate QoR snapshot after this stage
    parent_id: Optional[str] = None    # parent node_id (None for root)
    children_ids: List[str] = field(default_factory=list)
    branch_count: int = 0              # E(n): times chosen as branch origin

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "iteration": self.iteration,
            "stage": self.stage,
            "variant": self.variant,
            "params": self.params,
            "stage_qor": self.stage_qor,
            "parent_id": self.parent_id,
            "children_ids": self.children_ids,
            "branch_count": self.branch_count,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OptimNode":
        return cls(
            node_id=d["node_id"],
            iteration=d["iteration"],
            stage=d["stage"],
            variant=d["variant"],
            params=d.get("params", {}),
            stage_qor=d.get("stage_qor"),
            parent_id=d.get("parent_id"),
            children_ids=d.get("children_ids", []),
            branch_count=d.get("branch_count", 0),
        )


class OptimizationTree:
    """Optimization tree: a rooted tree structure holding all historical
    execution results.

    Usage:
    - tree = OptimizationTree()  creates root node
    - tree.add_path(0, "root", [...])  mount a stage node chain along a parent
    - tree.branchable_nodes()  returns branchable node list (with E(n))
    - tree.ancestors(node_id)  returns node chain from root to parent (excluding self)
    - Serialize: tree.to_dict() / OptimizationTree.from_dict(cfg, d)
    """

    def __init__(self):
        self._nodes: Dict[str, OptimNode] = {}
        self.root = OptimNode(
            node_id=ROOT_ID, iteration=-1, stage="root",
            variant="base",  # root has no actual variant; placeholder
        )
        self._nodes[ROOT_ID] = self.root

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def __contains__(self, node_id: str) -> bool:
        return node_id in self._nodes

    def node_count(self) -> int:
        """Return number of non-root nodes in tree (for summary / resume logs)"""
        return sum(1 for n in self._nodes.values() if n.stage != "root")

    def find_node(self, node_id: str) -> Optional[OptimNode]:
        return self._nodes.get(node_id)

    def branchable_nodes(self, max_branch_count: int = 999) -> List[OptimNode]:
        """Return nodes eligible as branch origins.

        Rules: stage ≠ "root" (root is not branchable), stage ≠ "RT"
        (leaf is not branchable), branch_count < max_branch_count
        (prevents over-exploration).
        """
        return [n for n in self._nodes.values()
                if n.stage not in ("root", "RT")
                and n.branch_count < max_branch_count]

    def ancestors(self, node_id: str) -> List[OptimNode]:
        """Return the node chain from root → ... → parent(node_id) in order,
        excluding node_id itself.

        This is the paper's Bef(s): the result list of stages preceding the
        stage s of the given node.
        """
        chain: List[OptimNode] = []
        node = self._nodes.get(node_id)
        while node is not None and node.parent_id is not None:
            parent = self._nodes.get(node.parent_id)
            if parent is None:
                break
            chain.append(parent)
            node = parent
        chain.reverse()  # root → ... → parent
        return chain

    def get_path_qor_summary(self, node_id: str) -> List[Tuple[str, Optional[float]]]:
        """Get intermediate ws for each stage along the path from root to node_id.

        Returns [(stage, ws_ps), ...], used for building the StageAgent's
        "upstream QoR in this branch" context.
        """
        chain = self.ancestors(node_id) + [self._nodes[node_id]]
        summary: List[Tuple[str, Optional[float]]] = []
        for n in chain:
            if n.stage == "root":
                continue
            ws = None
            if n.stage_qor:
                # Take the first ws_ps value from this node's stage_qor
                for k, v in n.stage_qor.items():
                    if k.endswith("_ws_ps"):
                        ws = v
                        break
            summary.append((n.stage, ws))
        return summary

    def get_params_chain(self, node_id: str) -> Dict[str, Dict[str, Any]]:
        """Aggregate per-stage params along the path from root to node_id.

        Returns {stage: params_dict}, used to build the complete stage_params
        dict (Bef inheritance + downstream new generation).
        """
        chain = self.ancestors(node_id) + [self._nodes[node_id]]
        result: Dict[str, Dict[str, Any]] = {}
        for n in chain:
            if n.stage in config.STAGES:
                result[n.stage] = dict(n.params)
        return result

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------
    def add_path(self, iteration: int, parent_id: str,
                 stages_chain: List[Tuple[str, str, Dict[str, Any],
                                          Optional[Dict[str, float]]]]
                 ) -> List[str]:
        """Mount a stage node chain along parent_id; return new node_id list.

        stages_chain: [(stage, variant, params, stage_qor), ...]
        Each tuple creates one node, linked parent→child in order.
        """
        new_ids: List[str] = []
        current_parent = parent_id
        for stage, variant, params, stage_qor in stages_chain:
            node_id = f"iter{iteration}_{stage}"
            # If same iteration + same stage already exists (resume replay),
            # skip creation
            if node_id in self._nodes:
                log.debug("Node %s already exists, skipping creation", node_id)
                current_parent = node_id
                new_ids.append(node_id)
                continue
            node = OptimNode(
                node_id=node_id,
                iteration=iteration,
                stage=stage,
                variant=variant,
                params=dict(params),
                stage_qor=dict(stage_qor) if stage_qor else None,
                parent_id=current_parent,
            )
            self._nodes[node_id] = node
            parent = self._nodes.get(current_parent)
            if parent:
                parent.children_ids.append(node_id)
            current_parent = node_id
            new_ids.append(node_id)
        return new_ids

    def increment_branch_count(self, node_id: str) -> None:
        node = self._nodes.get(node_id)
        if node:
            node.branch_count += 1

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {"nodes": {nid: n.to_dict() for nid, n in self._nodes.items()}}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OptimizationTree":
        tree = cls.__new__(cls)
        tree._nodes = {}
        for nid, nd in d.get("nodes", {}).items():
            tree._nodes[nid] = OptimNode.from_dict(nd)
        root = tree._nodes.get(ROOT_ID)
        if root is None:
            log.warning("tree.json missing root node, rebuilding empty tree")
            tree.root = OptimNode(
                node_id=ROOT_ID, iteration=-1, stage="root", variant="base")
            tree._nodes[ROOT_ID] = tree.root
        else:
            tree.root = root
        return tree


# ---------------------------------------------------------------------------
# Atomic persistence
# ---------------------------------------------------------------------------

def save_tree_atomic(path: Path, tree: OptimizationTree) -> None:
    """Atomic tree JSON write (write .tmp then os.replace; crash mid-write
    won't corrupt the old file)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(tree.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8")
    os.replace(tmp, path)


def load_tree(path: Path) -> OptimizationTree:
    """Load tree JSON; if corrupted, rename to .corrupt and return empty tree"""
    if not path.is_file():
        return OptimizationTree()
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict) and "nodes" in d:
            return OptimizationTree.from_dict(d)
        raise ValueError("tree JSON top-level missing 'nodes'")
    except (json.JSONDecodeError, ValueError) as e:
        corrupt = path.with_suffix(path.suffix + ".corrupt")
        os.replace(path, corrupt)
        log.warning("Tree JSON corrupted (%s), renamed to %s, rebuilding empty tree", e, corrupt)
        return OptimizationTree()
