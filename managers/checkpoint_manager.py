# -*- coding: utf-8 -*-
"""checkpoint_manager.py — Stage B3: Checkpoint lifecycle manager.

Manages resumable stage snapshots (checkpoints).  Each checkpoint records:
    - Which trial and stage produced it
    - A manifest of artifact files with SHA-256 hashes
    - The upstream parameter hash (used for compatibility checks)

Responsibilities:
    1. Create a checkpoint from a completed trial stage
       (scan ORFS artifact directories, hash files, build manifest)
    2. Verify checkpoint integrity (do files still exist?  hashes match?)
    3. Check parameter compatibility (can X params reuse Y's checkpoint?)

This module does NOT copy artifact files — it only records metadata that
references the existing ORFS variant directories.  Actual directory copying
for branch reuse remains in orfs_interface.py (to be refactored in Stage C).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# When run directly (python3 managers/checkpoint_manager.py), ensure the parent
# agenticpd/ directory is on sys.path BEFORE the schemas import below.
if __name__ == "__main__":
    import sys as _sys
    _parent = Path(__file__).resolve().parent.parent
    if str(_parent) not in _sys.path:
        _sys.path.insert(0, str(_parent))

from schemas.trial import CheckpointRef, TrialRecord

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Known output files per stage (relative to results/<plat>/<design>/<variant>/)
# Only these files are hashed; everything else is ignored.
# ---------------------------------------------------------------------------
STAGE_ARTIFACTS: Dict[str, List[str]] = {
    "synth": ["1_synth.odb", "1_synth.sdc", "1_2_yosys.v"],
    "FP":    ["2_floorplan.odb", "2_floorplan.sdc"],
    "PL":    ["3_place.odb", "3_place.sdc"],
    "CTS":   ["4_cts.odb", "4_cts.sdc"],
    "RT":    ["5_route.odb", "5_route.sdc", "5_route.def"],
    "finish": ["6_final.odb", "6_final.def", "6_final.v"],
}


class CheckpointManager:
    """Create, verify, and query checkpoints for resumable flow execution."""

    def __init__(self, flow_dir: Path) -> None:
        self.flow_dir = Path(flow_dir)

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        trial: TrialRecord,
        stage: str,
        platform: str,
        design: str,
        variant: str,
        param_hash: str,
        orfs_commit: str = "unresolved",
        runs_dir: Optional[Path] = None,
    ) -> CheckpointRef:
        """Build a checkpoint from a completed trial stage.

        Scans the ORFS results/logs/reports/objects directories for the
        given variant, hashes the key artifact files, and returns a
        CheckpointRef with the manifest.

        Args:
            trial: The TrialRecord for this run.
            stage: Which stage was just completed ("FP", "PL", "CTS", "RT", "finish").
            platform: e.g. "sky130hd".
            design: e.g. "gcd".
            variant: FLOW_VARIANT name (e.g. "agenticpd_iter0", "base").
            param_hash: SHA-256 of the resolved upstream parameters (as JSON).
            orfs_commit: ORFS commit SHA (or "unresolved").
            runs_dir: Session runs_dir for resolving relative artifact_dir.
                      Pass the TrialManager's runs_dir to ensure checkpoint.json
                      is written inside the correct session directory.
        """
        checkpoint_id = CheckpointRef.make_id(trial.trial_id, stage)

        # Build the artifact manifest by hashing key output files
        manifest = self._build_manifest(platform, design, variant, stage)

        cp = CheckpointRef(
            checkpoint_id=checkpoint_id,
            source_trial_id=trial.trial_id,
            stage=stage,
            param_hash=param_hash,
            orfs_commit=orfs_commit,
            created_at=datetime.now(timezone.utc).isoformat(),
            artifact_manifest=manifest,
            artifact_dir=f"results/{platform}/{design}/{variant}",
        )

        self._write_checkpoint(trial, cp, runs_dir=runs_dir)
        log.info("Checkpoint %s created (%d files)", checkpoint_id, len(manifest))
        return cp

    # ------------------------------------------------------------------
    # Verify
    # ------------------------------------------------------------------

    def verify(self, cp: CheckpointRef) -> Tuple[bool, List[str]]:
        """Check whether all files in the manifest exist and hashes match.

        Returns:
            (ok, errors): ok=True means all files are intact;
            errors is a list of human-readable problem descriptions.

        An empty artifact manifest is treated as a verification failure —
        a checkpoint with zero files is not a valid checkpoint.
        """
        errors: List[str] = []
        if not cp.artifact_manifest:
            return (False, ["Empty artifact manifest — no files to verify"])
        for entry in cp.artifact_manifest:
            filepath = self.flow_dir / entry["file"]
            if not filepath.is_file():
                errors.append(f"MISSING: {entry['file']}")
                continue
            actual_hash = self._sha256_file(filepath)
            if actual_hash != entry.get("sha256", ""):
                errors.append(
                    f"HASH MISMATCH: {entry['file']} "
                    f"(expected {entry.get('sha256', '?')[:8]}..., "
                    f"got {actual_hash[:8]}...)"
                )
        return (len(errors) == 0, errors)

    def is_compatible(self, cp: CheckpointRef,
                      new_params: Dict[str, Dict[str, Any]],
                      old_params: Dict[str, Dict[str, Any]]) -> bool:
        """Check whether new parameters are compatible with this checkpoint.

        A checkpoint at stage S is compatible when none of the parameters
        that *changed* affect any stage up to and including S.  Parameters
        that only affect downstream stages (after S) do NOT invalidate the
        checkpoint.

        Example: a checkpoint at FP is invalidated by CORE_UTILIZATION
        changes (affects FP+PL+CTS+RT), but NOT by FASTROUTE_LAYER_ADJUSTMENT
        changes (affects RT only).

        Args:
            cp:         the checkpoint to check.
            new_params: proposed parameters {stage: {param: value}}.
            old_params: parameters the checkpoint was created with.
        """
        import config

        stage_order = ["FP", "PL", "CTS", "RT"]
        cp_stage_idx = stage_order.index(cp.stage) if cp.stage in stage_order else -1

        # Collect all changed parameter names
        changed_params: set = set()
        for stage in stage_order:
            old = old_params.get(stage, {})
            new = new_params.get(stage, {})
            all_names = set(old.keys()) | set(new.keys())
            for name in all_names:
                if old.get(name) != new.get(name):
                    changed_params.add(name)

        if not changed_params:
            return True  # nothing changed

        # Check each changed parameter: does it affect stages <= cp_stage_idx?
        for name in changed_params:
            spec = config.get_param_spec(name)
            if spec is None:
                # Unknown parameter: be conservative, assume incompatible
                return False
            for affected_stage in spec.affects:
                affected_idx = stage_order.index(affected_stage) if affected_stage in stage_order else 999
                if affected_idx <= cp_stage_idx:
                    return False  # this change invalidates the checkpoint

        return True

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def load(self, trial: TrialRecord, stage: str,
             runs_dir: Optional[Path] = None) -> Optional[CheckpointRef]:
        """Load a checkpoint from the trial's artifact directory.

        Tries the per-stage path (``checkpoints/<stage>.json``) first,
        then falls back to the legacy single ``checkpoint.json`` (pre-Stage-D
        fix 2.1) for backward compatibility.
        """
        if not trial.artifact_dir:
            return None
        from schemas.trial import resolve_artifact_dir
        from config import AGENTICPD_DIR
        trial_dir = resolve_artifact_dir(
            trial.artifact_dir, runs_dir or AGENTICPD_DIR / "runs")
        if trial_dir is None:
            return None

        # 1) Per-stage path (Stage D fix 2.1)
        cp_path = self._checkpoint_path(trial_dir, stage)
        if cp_path.is_file():
            cp = self._try_load_checkpoint(cp_path, stage)
            if cp is not None:
                return cp

        # 2) Legacy single checkpoint.json (backward compat)
        legacy_path = self._legacy_checkpoint_path(trial_dir)
        if legacy_path.is_file():
            cp = self._try_load_checkpoint(legacy_path, stage)
            if cp is not None:
                log.debug("Loaded checkpoint from legacy checkpoint.json (stage=%s)", stage)
                return cp

        return None

    def load_from_dir(self, trial_dir: Path, stage: str) -> Optional[CheckpointRef]:
        """Load a checkpoint directly from a trial directory path.

        This is the filesystem-level variant of ``load()`` — it does not
        require a TrialRecord object, so it can be called before the trial
        is fully loaded.

        Tries per-stage path first, then legacy single checkpoint.json.
        """
        # 1) Per-stage path (Stage D fix 2.1)
        cp_path = self._checkpoint_path(trial_dir, stage)
        if cp_path.is_file():
            cp = self._try_load_checkpoint(cp_path, stage)
            if cp is not None:
                return cp

        # 2) Legacy single checkpoint.json (backward compat)
        legacy_path = self._legacy_checkpoint_path(trial_dir)
        if legacy_path.is_file():
            cp = self._try_load_checkpoint(legacy_path, stage)
            if cp is not None:
                log.debug("Loaded checkpoint from legacy checkpoint.json (stage=%s)", stage)
                return cp

        return None

    @staticmethod
    def _try_load_checkpoint(path: Path, expected_stage: str) -> Optional[CheckpointRef]:
        """Load a CheckpointRef from *path*; return None if missing, corrupt,
        or stage mismatch."""
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cp = CheckpointRef.from_dict(data)
            if cp.stage == expected_stage:
                return cp
        except (json.JSONDecodeError, KeyError):
            pass
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _variant_dir(self, platform: str, design: str, variant: str,
                     category: str) -> Path:
        """Return flow/<category>/<platform>/<design>/<variant>/."""
        return self.flow_dir / category / platform / design / variant

    def _build_manifest(
        self, platform: str, design: str, variant: str, stage: str
    ) -> List[Dict[str, Any]]:
        """Hash key artifact files for a stage and return the manifest list."""
        manifest: List[Dict[str, Any]] = []
        target_files = STAGE_ARTIFACTS.get(stage, [])

        for category in ("results", "logs", "reports"):
            base = self._variant_dir(platform, design, variant, category)
            if not base.is_dir():
                continue
            for filename in target_files:
                fpath = base / filename
                if not fpath.is_file():
                    continue
                rel_path = f"{category}/{platform}/{design}/{variant}/{filename}"
                file_hash = self._sha256_file(fpath)
                file_size = fpath.stat().st_size
                manifest.append({
                    "file": rel_path,
                    "size_bytes": file_size,
                    "sha256": file_hash,
                })
        return manifest

    @staticmethod
    def _sha256_file(filepath: Path, chunk_size: int = 65536) -> str:
        """Return hex digest of a file's SHA-256."""
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _checkpoint_path(trial_dir: Path, stage: str) -> Path:
        """Return the per-stage checkpoint path: ``checkpoints/<stage>.json``."""
        return trial_dir / "checkpoints" / f"{stage}.json"

    @staticmethod
    def _legacy_checkpoint_path(trial_dir: Path) -> Path:
        """Return the legacy single-checkpoint path (pre-Stage-D fix 2.1)."""
        return trial_dir / "checkpoint.json"

    @staticmethod
    def _write_checkpoint(trial: TrialRecord, cp: CheckpointRef,
                          runs_dir: Optional[Path] = None) -> None:
        """Atomically write checkpoints/<stage>.json inside the trial directory.

        Per-stage persistence (Stage D fix 2.1): each stage gets its own file
        so FP/PL/CTS checkpoints from the same trial never overwrite each other.
        """
        if not trial.artifact_dir:
            return
        from schemas.trial import resolve_artifact_dir
        from config import AGENTICPD_DIR
        trial_dir = resolve_artifact_dir(
            trial.artifact_dir, runs_dir or AGENTICPD_DIR / "runs"
        )
        if trial_dir is None:
            return
        cp_path = CheckpointManager._checkpoint_path(trial_dir, cp.stage)
        cp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp_path.with_suffix(cp_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(cp.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, cp_path)

    @staticmethod
    def param_hash(params: Dict[str, Dict[str, Any]]) -> str:
        """Compute a deterministic SHA-256 hash of resolved parameters.

        The params dict is sorted by key before hashing so the same
        parameters always produce the same hash regardless of dict ordering.
        """
        canonical = json.dumps(params, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# =============================================================================
# Self-test
# =============================================================================

if __name__ == "__main__":
    import shutil
    import sys
    import tempfile

    ok = 0
    fail = 0

    def check(cond, msg):
        global ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL: {msg}")

    # Setup: create a fake flow_dir with minimal ORFS-like structure
    tmpdir = Path(tempfile.mkdtemp())
    flow_dir = tmpdir / "flow"
    runs_dir = tmpdir / "runs"
    runs_dir.mkdir(parents=True)

    # Create fake artifact files for gcd/agenticpd_iter0 (FP + PL + CTS stages)
    variant = "agenticpd_iter0"
    for cat in ("results", "logs", "reports"):
        d = flow_dir / cat / "sky130hd" / "gcd" / variant
        d.mkdir(parents=True)
    fp_odb = flow_dir / "results" / "sky130hd" / "gcd" / variant / "2_floorplan.odb"
    fp_odb.write_text("fake odb content for floorplan")
    fp_sdc = flow_dir / "results" / "sky130hd" / "gcd" / variant / "2_floorplan.sdc"
    fp_sdc.write_text("fake sdc content")
    pl_odb = flow_dir / "results" / "sky130hd" / "gcd" / variant / "3_place.odb"
    pl_odb.write_text("fake odb content for placement")
    pl_sdc = flow_dir / "results" / "sky130hd" / "gcd" / variant / "3_place.sdc"
    pl_sdc.write_text("fake sdc content for placement")
    cts_odb = flow_dir / "results" / "sky130hd" / "gcd" / variant / "4_cts.odb"
    cts_odb.write_text("fake odb content for cts")
    cts_sdc = flow_dir / "results" / "sky130hd" / "gcd" / variant / "4_cts.sdc"
    cts_sdc.write_text("fake sdc content for cts")

    # Create a trial record with absolute artifact_dir for self-test tempdir
    from schemas.trial import TrialRecord
    trial = TrialRecord(
        trial_id="test0001",
        experiment_id="smoke-gcd-v1",
        status="ok",
        artifact_dir=str(runs_dir / "test0001"),
    )
    (runs_dir / "test0001").mkdir(parents=True)

    mgr = CheckpointManager(flow_dir)

    # -- Create FP checkpoint (per-stage path: checkpoints/FP.json) --
    phash = CheckpointManager.param_hash({"FP": {"CORE_UTILIZATION": 38}})
    cp = mgr.create(
        trial=trial,
        stage="FP",
        platform="sky130hd",
        design="gcd",
        variant=variant,
        param_hash=phash,
    )
    check(cp.checkpoint_id == "cp-test0001-FP", "checkpoint id format")
    check(len(cp.artifact_manifest) >= 1, "manifest has >=1 files")
    check(cp.param_hash == phash, "param_hash preserved")
    check(cp.stage == "FP", "stage preserved")
    # Stage D fix 2.1: per-stage path
    check((Path(trial.artifact_dir) / "checkpoints" / "FP.json").is_file(),
          "per-stage checkpoint: checkpoints/FP.json written")
    check(not (Path(trial.artifact_dir) / "checkpoint.json").exists(),
          "per-stage checkpoint: legacy checkpoint.json NOT written")

    # -- Create PL + CTS checkpoints in the SAME trial → no overwrite --
    cp_pl = mgr.create(trial, "PL", "sky130hd", "gcd", variant, phash)
    check(cp_pl.stage == "PL", "PL checkpoint stage")
    check((Path(trial.artifact_dir) / "checkpoints" / "PL.json").is_file(),
          "per-stage checkpoint: checkpoints/PL.json written")
    cp_cts = mgr.create(trial, "CTS", "sky130hd", "gcd", variant, phash)
    check(cp_cts.stage == "CTS", "CTS checkpoint stage")
    check((Path(trial.artifact_dir) / "checkpoints" / "CTS.json").is_file(),
          "per-stage checkpoint: checkpoints/CTS.json written")
    # Verify FP checkpoint still intact (not overwritten)
    check((Path(trial.artifact_dir) / "checkpoints" / "FP.json").is_file(),
          "FP checkpoint survives PL+CTS creation (no overwrite)")

    # -- Verify: files exist and hashes match --
    is_ok, errors = mgr.verify(cp)
    check(is_ok, f"verify ok (errors: {errors})")

    # -- Verify: tampered file detected --
    fp_odb.write_text("TAMPERED CONTENT")
    is_ok2, errors2 = mgr.verify(cp)
    check(not is_ok2, "verify detects tampering")

    # -- Verify: empty manifest is treated as failure --
    empty_cp = CheckpointRef(
        checkpoint_id="cp-empty-test",
        source_trial_id="test0001", stage="FP",
        param_hash=phash, orfs_commit="unresolved",
        artifact_manifest=[],  # empty — should fail verification
    )
    is_ok3, errors3 = mgr.verify(empty_cp)
    check(not is_ok3, "verify: empty manifest rejected")
    check(any("Empty" in e for e in errors3), "verify: empty manifest error message")

    # -- Compatibility (stage-aware via affects) --
    base_params = {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {}, "RT": {}}
    same_params = {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {}, "RT": {}}
    diff_fp_params = {"FP": {"CORE_UTILIZATION": 50}, "PL": {}, "CTS": {}, "RT": {}}
    rt_only_change = {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {},
                       "RT": {"GRT_CONGESTION_ITERATIONS": 50}}
    check(mgr.is_compatible(cp, same_params, base_params),
          "compatible: same params")
    check(not mgr.is_compatible(cp, diff_fp_params, base_params),
          "incompatible: FP param changed")
    check(mgr.is_compatible(cp, rt_only_change, base_params),
          "compatible: RT-only change vs FP checkpoint")

    # -- param_hash is deterministic --
    h1 = CheckpointManager.param_hash({"FP": {"A": 1, "B": 2}})
    h2 = CheckpointManager.param_hash({"FP": {"B": 2, "A": 1}})
    check(h1 == h2, "param_hash: key-order independent")

    # -- Load per-stage checkpoints --
    cp_fp_loaded = mgr.load(trial, "FP")
    check(cp_fp_loaded is not None, "load FP checkpoint from trial dir")
    check(cp_fp_loaded.checkpoint_id == cp.checkpoint_id, "loaded FP checkpoint id matches")
    cp_pl_loaded = mgr.load(trial, "PL")
    check(cp_pl_loaded is not None, "load PL checkpoint from trial dir")
    check(cp_pl_loaded.stage == "PL", "loaded PL checkpoint stage matches")
    cp_cts_loaded = mgr.load(trial, "CTS")
    check(cp_cts_loaded is not None, "load CTS checkpoint from trial dir")
    check(cp_cts_loaded.stage == "CTS", "loaded CTS checkpoint stage matches")

    # -- Load non-existent stage --
    check(mgr.load(trial, "RT") is None, "load non-existent stage RT -> None")

    # -- Legacy backward compat: single checkpoint.json --
    # Simulate an old trial where only a single checkpoint.json exists.
    legacy_trial_dir = runs_dir / "legacy_test"
    legacy_trial_dir.mkdir(parents=True)
    legacy_cp = CheckpointRef(
        checkpoint_id="cp-legacy-FP", source_trial_id="legacy",
        stage="FP", param_hash=phash, orfs_commit="unresolved",
        artifact_manifest=cp.artifact_manifest,
        artifact_dir=f"results/sky130hd/gcd/{variant}",
    )
    (legacy_trial_dir / "checkpoint.json").write_text(
        json.dumps(legacy_cp.to_dict(), ensure_ascii=False, indent=2))
    legacy_loaded = mgr.load_from_dir(legacy_trial_dir, "FP")
    check(legacy_loaded is not None, "legacy checkpoint.json load: FP found")
    check(legacy_loaded.checkpoint_id == "cp-legacy-FP", "legacy checkpoint.json: id matches")
    # Legacy with wrong stage → None
    check(mgr.load_from_dir(legacy_trial_dir, "CTS") is None,
          "legacy checkpoint.json: wrong stage -> None")

    # -- Session isolation: create() with runs_dir writes to correct session dir --
    session_runs = tmpdir / "session_runs" / "sky130hd_gcd" / "checkpoint_fork" / "20260729_test"
    session_runs.mkdir(parents=True)
    trial2 = TrialRecord(
        trial_id="test0002",
        experiment_id="session-isolation-test",
        status="ok",
        artifact_dir="iter-0-test0002",  # relative — the production default
    )
    (session_runs / "iter-0-test0002").mkdir(parents=True)
    mgr.create(
        trial=trial2, stage="FP",
        platform="sky130hd", design="gcd",
        variant=variant, param_hash=phash,
        runs_dir=session_runs,
    )
    cp_session_path = session_runs / "iter-0-test0002" / "checkpoints" / "FP.json"
    check(cp_session_path.is_file(),
          "session isolation: checkpoints/FP.json written in session dir")
    # Verify no leak to default fallback location.
    import config as _cfg
    default_leak = _cfg.AGENTICPD_DIR / "runs" / "iter-0-test0002" / "checkpoints" / "FP.json"
    check(not default_leak.exists(),
          "session isolation: no leak to default AGENTICPD_DIR/runs/")

    # Clean up
    shutil.rmtree(tmpdir)

    total = ok + fail
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed" + (f", {fail} FAILED" if fail else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail else 0)
