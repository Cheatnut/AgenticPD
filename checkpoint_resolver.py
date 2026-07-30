# -*- coding: utf-8 -*-
"""checkpoint_resolver.py — Stage D: checkpoint-aware execution resolution.

Pure Python resolver that turns a Judge/Policy branch request into an
ExecutionResolution after verifying manifest integrity and parameter
compatibility against available ancestor checkpoints.

Key rules:
1. Verify manifest (all artifact files exist and hashes match).
2. Check compatibility via ParamSpec.affects (candidate vs checkpoint params).
3. Select the latest (deepest-stage) compatible checkpoint.
4. If no compatible checkpoint → full_restart, effective_start_stage=FP.
5. Unknown parameters → conservative incompatible.
6. Incompatible artifacts must never be copied or consumed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from config import STAGES
from managers import TrialManager, CheckpointManager
from optimization_tree import OptimizationTree
from schemas.trial import CheckpointAuditEntry, ExecutionResolution

log = logging.getLogger(__name__)

# Stage order for checkpoint fallback: find latest compatible, then the next
# stage becomes effective_start_stage.
#   FP checkpoint compatible → effective_start_stage = PL
#   PL checkpoint compatible → effective_start_stage = CTS
#   CTS checkpoint compatible → effective_start_stage = RT
#   none compatible           → full_restart, effective = FP
_STAGE_TO_EFFECTIVE: Dict[str, str] = {"FP": "PL", "PL": "CTS", "CTS": "RT"}


def _resolve_effective_start(
    compatible_stage: Optional[str],
) -> Tuple[str, str]:
    """Return (execution_mode, effective_start_stage).

    Args:
        compatible_stage: the latest compatible checkpoint stage, or None.
    """
    if compatible_stage is None:
        return ("full_restart", "FP")
    return ("checkpoint_fork", _STAGE_TO_EFFECTIVE[compatible_stage])


def resolve_checkpoint(
    requested_parent_node_id: str,
    requested_start_stage: str,
    candidate_params: Dict[str, Dict[str, Any]],
    inherited_params: Dict[str, Dict[str, Any]],
    tree: OptimizationTree,
    trial_mgr: TrialManager,
    checkpoint_mgr: CheckpointManager,
    runs_dir: "Optional[Path]" = None,
) -> ExecutionResolution:
    """Resolve a branch request against available ancestor checkpoints.

    Args:
        requested_parent_node_id: tree node the Judge/Policy chose.
        requested_start_stage:    stage the Judge/Policy wants to start from.
        candidate_params:         complete per-stage params (inherited + newly
                                  generated) for this trial.
        inherited_params:         params inherited from tree ancestors (Bef
                                  stages only).  Currently unused in decision
                                  logic; reserved for Optimizer-level defensive
                                  validation and audit enrichment.
        tree:                     the optimization tree.
        trial_mgr:                trial manager (for looking up source trials).
        checkpoint_mgr:           checkpoint manager (for loading + verifying).
        runs_dir:                 session runs_dir for resolving relative
                                  artifact paths; defaults to AGENTICPD_DIR/runs.

    Returns:
        ExecutionResolution recording what was actually executed and why,
        with a full ``checkpoint_audit_trail`` of every checkpoint examined.
    """
    # Initialise with conservative defaults: full_restart, no consumed
    # checkpoint, all audit fields empty/false.
    resolution = ExecutionResolution(
        requested_parent_node_id=requested_parent_node_id,
        requested_start_stage=requested_start_stage,
        effective_start_stage="FP",      # will be corrected if a cp is consumed
        execution_mode="full_restart",
        # consumed_* all default to None (correct for full_restart)
    )

    # 1) Get ancestor chain from root to requested parent node (inclusive of
    #    the branch origin itself, which is the last Bef-stage node).
    requested_node = tree.find_node(requested_parent_node_id)
    if requested_node is None:
        resolution.fallback_reason = (
            f"requested node '{requested_parent_node_id}' not in tree")
        return resolution

    ancestors = tree.ancestors(requested_parent_node_id)
    # ancestors() returns root→...→parent(requested_node), excluding the
    # node itself.  For checkpoint resolution we also consider the node
    # itself if it represents a completed Bef stage.
    chain = list(ancestors)
    if requested_node.stage in STAGES:
        chain.append(requested_node)
    # Exclude the root node (has no source_trial / checkpoint).
    chain = [n for n in chain if n.stage != "root"]

    if not chain:
        resolution.fallback_reason = (
            "no ancestor nodes with source trials (root-only tree)")
        return resolution

    # 2) Build the list of available checkpoints from ancestor nodes,
    #    ordered from deepest (latest) stage to shallowest (earliest).
    #    Each entry: (node, checkpoint_ref, source_trial_params).
    available: List[Tuple[Any, Any, Dict[str, Dict[str, Any]]]] = []
    for node in reversed(chain):  # deepest first
        if node.source_trial_id is None:
            continue
        src_trial = trial_mgr.get(node.source_trial_id)
        if src_trial is None:
            continue
        cp = checkpoint_mgr.load(src_trial, node.stage, runs_dir=runs_dir)
        if cp is None:
            continue
        available.append((node, cp, src_trial.params))

    if not available:
        resolution.fallback_reason = (
            "no ancestor checkpoint found (source trials may be unavailable "
            "or checkpoints not yet created)")
        return resolution

    # 3) For each available checkpoint (deepest first), verify manifest
    #    and check compatibility.  Every checkpoint examined produces a
    #    CheckpointAuditEntry so the full decision trail is preserved —
    #    we no longer overwrite a handful of flat fields on each iteration.
    audit_trail: List[CheckpointAuditEntry] = []
    consumed_node: Any = None   # tree node of the consumed checkpoint
    consumed_cp: Any = None     # CheckpointRef that was consumed

    for node, cp, old_params in available:
        # 3a) Manifest verification
        manifest_ok, manifest_errors = checkpoint_mgr.verify(cp)

        if not manifest_ok:
            audit_trail.append(CheckpointAuditEntry(
                checkpoint_id=cp.checkpoint_id,
                stage=cp.stage,
                source_trial_id=cp.source_trial_id,
                manifest_verified=False,
                manifest_errors=manifest_errors,
                compatibility_checked=False,
                is_compatible=False,
                invalidating_parameters=[],
                rejection_reason=(
                    f"manifest verification failed: {'; '.join(manifest_errors)}"
                ),
            ))
            log.info("[RESOLVER] checkpoint %s manifest FAILED: %s",
                     cp.checkpoint_id, manifest_errors)
            continue  # try earlier checkpoint

        # 3b) Parameter compatibility
        is_compat = checkpoint_mgr.is_compatible(cp, candidate_params, old_params)
        invalidating = _find_invalidating_params(
            candidate_params, old_params, checkpoint_stage=cp.stage)

        if not is_compat:
            audit_trail.append(CheckpointAuditEntry(
                checkpoint_id=cp.checkpoint_id,
                stage=cp.stage,
                source_trial_id=cp.source_trial_id,
                manifest_verified=True,
                manifest_errors=[],
                compatibility_checked=True,
                is_compatible=False,
                invalidating_parameters=invalidating,
                rejection_reason=(
                    f"parameter incompatibility: {', '.join(invalidating)}"
                    if invalidating else "parameter incompatibility"
                ),
            ))
            log.info("[RESOLVER] checkpoint %s incompatible: %s",
                     cp.checkpoint_id, invalidating)
            continue

        # 3c) This checkpoint is compatible — consume it
        audit_trail.append(CheckpointAuditEntry(
            checkpoint_id=cp.checkpoint_id,
            stage=cp.stage,
            source_trial_id=cp.source_trial_id,
            manifest_verified=True,
            manifest_errors=[],
            compatibility_checked=True,
            is_compatible=True,
            invalidating_parameters=[],
            rejection_reason=None,  # consumed, not rejected
        ))
        consumed_node = node
        consumed_cp = cp
        break

    # 4) Persist the full audit trail on the resolution regardless of outcome.
    resolution.checkpoint_audit_trail = audit_trail

    # 5) Populate flat fields from the consumed checkpoint (backward compat),
    #    or build a detailed fallback_reason for full_restart.
    if consumed_cp is not None:
        mode, eff = _resolve_effective_start(consumed_node.stage)
        resolution.effective_start_stage = eff
        resolution.execution_mode = mode
        resolution.consumed_checkpoint = consumed_cp.checkpoint_id
        resolution.consumed_node_id = consumed_node.node_id
        resolution.consumed_variant = consumed_node.variant
        resolution.manifest_verified = True
        resolution.manifest_errors = []
        resolution.compatibility_checked = True
        resolution.is_compatible = True
        resolution.invalidating_parameters = []

        # If consumed is NOT the deepest available checkpoint, record the
        # fallback_reason explicitly so auditors can see that a deeper
        # checkpoint was rejected.
        deepest = available[0][1]
        if consumed_cp.checkpoint_id != deepest.checkpoint_id:
            rejected = [e for e in audit_trail if not e.is_compatible]
            parts = []
            for e in rejected:
                parts.append(
                    f"{e.checkpoint_id}({e.stage}): {e.rejection_reason}"
                )
            resolution.fallback_reason = (
                f"fell back from deeper checkpoint(s): {'; '.join(parts)}"
            )
        else:
            resolution.fallback_reason = None  # no fallback occurred

        log.info("[RESOLVER] checkpoint %s (%s) selected: effective=%s mode=%s variant=%s",
                 consumed_cp.checkpoint_id, consumed_node.stage,
                 eff, mode, consumed_node.variant)
    else:
        # No compatible checkpoint found — full restart.
        # Build a comprehensive fallback_reason from the audit trail.
        parts = []
        for e in audit_trail:
            parts.append(
                f"{e.checkpoint_id}({e.stage}): {e.rejection_reason}"
            )
        resolution.fallback_reason = (
            f"no compatible checkpoint among {len(available)} ancestor(s); "
            f"all rejected: {'; '.join(parts)}"
        )
        resolution.effective_start_stage = "FP"
        resolution.execution_mode = "full_restart"
        # consumed_* remain None (correct for full_restart)

        # Backward compat: populate flat fields from the deepest (first)
        # audit entry.  When a checkpoint IS consumed, flat fields describe
        # the consumed checkpoint.  When NO checkpoint is consumed
        # (full_restart), flat fields are a direct copy of the deepest,
        # first-attempted checkpoint's audit entry.
        if audit_trail:
            deepest = audit_trail[0]
            resolution.manifest_verified = deepest.manifest_verified
            resolution.manifest_errors = list(deepest.manifest_errors)
            resolution.compatibility_checked = deepest.compatibility_checked
            resolution.is_compatible = deepest.is_compatible
            resolution.invalidating_parameters = list(
                deepest.invalidating_parameters)
        # else: no audit entries → keep conservative dataclass defaults
        # (manifest_verified=False, manifest_errors=[], etc.)

        log.info("[RESOLVER] fallback: full_restart (effective=FP)")

    return resolution


def _find_invalidating_params(
    new_params: Dict[str, Dict[str, Any]],
    old_params: Dict[str, Dict[str, Any]],
    checkpoint_stage: Optional[str] = None,
) -> List[str]:
    """Return names of params that actually invalidate a checkpoint.

    When *checkpoint_stage* is provided, only params whose ``affects``
    includes a stage at or before the checkpoint stage are listed.
    Unknown parameters are always included (conservative).

    When *checkpoint_stage* is None, all changed params are listed.
    """
    stage_order = ["FP", "PL", "CTS", "RT"]
    cp_stage_idx = (stage_order.index(checkpoint_stage)
                    if checkpoint_stage and checkpoint_stage in stage_order
                    else -1)

    invalidating: List[str] = []
    for stage in STAGES:
        old = old_params.get(stage, {})
        new = new_params.get(stage, {})
        all_names = set(old.keys()) | set(new.keys())
        for name in sorted(all_names):
            ov = old.get(name)
            nv = new.get(name)
            if ov != nv:
                spec = config.get_param_spec(name)
                if spec is None:
                    # Unknown parameter: conservative, always invalidating
                    invalidating.append(f"{name} (unknown)")
                elif cp_stage_idx < 0:
                    # No checkpoint stage → list all changed params
                    invalidating.append(name)
                else:
                    # Check if this param affects stages ≤ checkpoint stage
                    for affected_stage in spec.affects:
                        affected_idx = stage_order.index(affected_stage)
                        if affected_idx <= cp_stage_idx:
                            invalidating.append(name)
                            break
    return invalidating


# =============================================================================
# Self-test
# =============================================================================

if __name__ == "__main__":
    import shutil
    import sys
    import tempfile
    from pathlib import Path
    from unittest.mock import patch, MagicMock

    ok = 0
    fail = 0

    def check(cond, msg):
        global ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL: {msg}")

    tmpdir = Path(tempfile.mkdtemp())

    # Build a minimal tree: root → FP(iter0) → PL(iter0) → CTS(iter0)
    from optimization_tree import OptimizationTree, ROOT_ID
    tree = OptimizationTree()
    fp_node_id = tree.add_path(
        0, ROOT_ID,
        [("FP", "v0", {"CORE_UTILIZATION": 38}, {"fp_ws_ps": -1154.0})],
        source_trial_id="trialfp00",
    )[0]
    pl_node_id = tree.add_path(
        0, fp_node_id,
        [("PL", "v0", {"CELL_PAD_IN_SITES_GLOBAL_PLACEMENT": 0}, {"pl_ws_ps": -1200.0})],
        source_trial_id="trialpl00",
    )[0]
    cts_node_id = tree.add_path(
        0, pl_node_id,
        [("CTS", "v0", {}, {"cts_ws_ps": -1180.0})],
        source_trial_id="trialct00",
    )[0]

    # Smoke: resolve from CTS node with no checkpoints → full_restart
    from managers import TrialManager, CheckpointManager
    runs_dir = tmpdir / "runs"
    runs_dir.mkdir(parents=True)
    tm = TrialManager(runs_dir)
    cm = CheckpointManager(tmpdir / "flow")

    res = resolve_checkpoint(
        cts_node_id, "RT",
        candidate_params={"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {}, "RT": {}},
        inherited_params={"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {}},
        tree=tree, trial_mgr=tm, checkpoint_mgr=cm,
    )
    check(res.execution_mode == "full_restart",
          f"no checkpoints → full_restart (got {res.execution_mode})")
    check(res.effective_start_stage == "FP",
          f"no checkpoints → FP (got {res.effective_start_stage})")
    check(res.requested_parent_node_id == cts_node_id, "requested node preserved")
    check(res.requested_start_stage == "RT", "requested stage preserved")
    check(res.fallback_reason is not None, "fallback_reason populated")

    # Missing node → full_restart
    res2 = resolve_checkpoint(
        "nonexistent", "RT",
        candidate_params={}, inherited_params={},
        tree=tree, trial_mgr=tm, checkpoint_mgr=cm,
    )
    check(res2.execution_mode == "full_restart", "missing node → full_restart")
    check("not in tree" in (res2.fallback_reason or ""), "missing node reason")

    # Empty chain (root-only tree) → full_restart
    empty_tree = OptimizationTree()
    res3 = resolve_checkpoint(
        ROOT_ID, "FP",
        candidate_params={"FP": {}, "PL": {}, "CTS": {}, "RT": {}},
        inherited_params={"FP": {}, "PL": {}, "CTS": {}},
        tree=empty_tree, trial_mgr=tm, checkpoint_mgr=cm,
    )
    check(res3.execution_mode == "full_restart", "root-only → full_restart")

    shutil.rmtree(tmpdir)

    total = ok + fail
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed" + (f", {fail} FAILED" if fail else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail else 0)
