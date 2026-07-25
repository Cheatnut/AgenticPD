# -*- coding: utf-8 -*-
"""
optimization_tree.py — AgenticPD 优化树

论文 §3 要求：所有历史执行结果组织为一棵有根树 T。根节点 n_0 代表综合后的网表。
每次执行阶段 s 时创建一个节点 n_k^s = (a_k(s), Q_k(s))。

从根到叶的每条完整路径（FP→PL→CTS→RT）对应一个完整的动作元组 a_k。
分支时从中间节点 n_hat 挂载新子树，复用 Bef(b) 的结果，重新执行 {b} ∪ Aft(b)。
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

# 根节点固定标识
ROOT_ID = "root"


@dataclass
class OptimNode:
    """优化树中的一个节点，代表某次迭代在某个阶段的执行记录"""

    node_id: str                       # "root" | "iter0_FP" | "iter2_CTS"
    iteration: int                     # 创建该节点的迭代号
    stage: str                         # "root" | "FP" | "PL" | "CTS" | "RT"
    variant: str                       # 本轮 artifact 所在的 FLOW_VARIANT
    params: Dict[str, Any] = field(default_factory=dict)   # 仅本阶段的参数
    stage_qor: Optional[Dict[str, float]] = None   # 本阶段执行后的中间 QoR 快照
    parent_id: Optional[str] = None    # 父节点 node_id（root 为 None）
    children_ids: List[str] = field(default_factory=list)
    branch_count: int = 0              # E(n): 被选为分支起源点的次数

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
    """优化树：所有历史执行结果的有根树结构。

    使用方式：
    - tree = OptimizationTree()  创建根节点
    - tree.add_path(0, "root", [...])  沿 parent 挂载一条阶段节点链
    - tree.branchable_nodes()  返回可分支节点列表（带 E(n)）
    - tree.ancestors(node_id)  返回从根到 parent 的节点链（不含自身）
    - 序列化：tree.to_dict() / OptimizationTree.from_dict(cfg, d)
    """

    def __init__(self):
        self._nodes: Dict[str, OptimNode] = {}
        self.root = OptimNode(
            node_id=ROOT_ID, iteration=-1, stage="root",
            variant="base",  # 根无实际 variant，填占位
        )
        self._nodes[ROOT_ID] = self.root

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def __contains__(self, node_id: str) -> bool:
        return node_id in self._nodes

    def node_count(self) -> int:
        """返回树中非 root 节点数量（用于 summary / resume 日志）"""
        return sum(1 for n in self._nodes.values() if n.stage != "root")

    def find_node(self, node_id: str) -> Optional[OptimNode]:
        return self._nodes.get(node_id)

    def branchable_nodes(self, max_branch_count: int = 999) -> List[OptimNode]:
        """返回可作为分支起点的节点列表。

        规则：stage ≠ "root"（根不可分支）、stage ≠ "RT"（叶子不可分支）、
        branch_count < max_branch_count（防止过探索）。
        """
        return [n for n in self._nodes.values()
                if n.stage not in ("root", "RT")
                and n.branch_count < max_branch_count]

    def ancestors(self, node_id: str) -> List[OptimNode]:
        """返回从 root → ... → parent(node_id) 的节点链（按顺序，不含 node_id 自身）。

        论文中的 Bef(s)：以该节点所在阶段 s 的前置阶段结果列表。
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
        """获取从 root 到 node_id 的完整路径上各阶段中间 ws。

        返回 [(stage, ws_ps), ...]，用于阶段智能体的"本分支上游 QoR"构建。
        """
        chain = self.ancestors(node_id) + [self._nodes[node_id]]
        summary: List[Tuple[str, Optional[float]]] = []
        for n in chain:
            if n.stage == "root":
                continue
            ws = None
            if n.stage_qor:
                # 取该节点 stage_qor 里的第一个 ws_ps 值
                for k, v in n.stage_qor.items():
                    if k.endswith("_ws_ps"):
                        ws = v
                        break
            summary.append((n.stage, ws))
        return summary

    def get_params_chain(self, node_id: str) -> Dict[str, Dict[str, Any]]:
        """从 root 到 node_id 路径上各阶段的参数汇总。

        返回 {stage: params_dict}，用于构建完整 stage_params dict（Bef 继承 + 下游新生成）。
        """
        chain = self.ancestors(node_id) + [self._nodes[node_id]]
        result: Dict[str, Dict[str, Any]] = {}
        for n in chain:
            if n.stage in config.STAGES:
                result[n.stage] = dict(n.params)
        return result

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------
    def add_path(self, iteration: int, parent_id: str,
                 stages_chain: List[Tuple[str, str, Dict[str, Any],
                                          Optional[Dict[str, float]]]]
                 ) -> List[str]:
        """沿 parent_id 挂载一条阶段节点链，返回新节点的 node_id 列表。

        stages_chain: [(stage, variant, params, stage_qor), ...]
        每个 tuple 创建一个节点，按顺序父子相连。
        """
        new_ids: List[str] = []
        current_parent = parent_id
        for stage, variant, params, stage_qor in stages_chain:
            node_id = f"iter{iteration}_{stage}"
            # 若同 iteration + 同 stage 已存在（resume 重放），跳过创建
            if node_id in self._nodes:
                log.debug("节点 %s 已存在，跳过创建", node_id)
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
    # 序列化
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
            log.warning("tree.json 缺失 root，重建空树")
            tree.root = OptimNode(
                node_id=ROOT_ID, iteration=-1, stage="root", variant="base")
            tree._nodes[ROOT_ID] = tree.root
        else:
            tree.root = root
        return tree


# ---------------------------------------------------------------------------
# 原子化持久化
# ---------------------------------------------------------------------------

def save_tree_atomic(path: Path, tree: OptimizationTree) -> None:
    """原子化写入树 JSON（先写 .tmp 再 os.replace，中途崩溃不会损坏旧文件）"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(tree.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8")
    os.replace(tmp, path)


def load_tree(path: Path) -> OptimizationTree:
    """加载树 JSON；文件破损时改名为 .corrupt 并返回空树"""
    if not path.is_file():
        return OptimizationTree()
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(d, dict) and "nodes" in d:
            return OptimizationTree.from_dict(d)
        raise ValueError("tree JSON 顶层缺少 nodes")
    except (json.JSONDecodeError, ValueError) as e:
        corrupt = path.with_suffix(path.suffix + ".corrupt")
        os.replace(path, corrupt)
        log.warning("树 JSON 损坏（%s），已改名 %s，重建空树", e, corrupt)
        return OptimizationTree()
