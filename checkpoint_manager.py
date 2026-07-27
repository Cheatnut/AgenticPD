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

        self._write_checkpoint(trial, cp)
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
        """
        errors: List[str] = []
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

    def is_compatible(self, cp: CheckpointRef, new_param_hash: str) -> bool:
        """Check whether new parameters are compatible with this checkpoint.

        Currently strict: param_hash must match exactly.  In the future,
        ParameterSpec.affects_stages could allow partial matching (e.g.
        changing a routing-only param doesn't invalidate an FP checkpoint).
        """
        return cp.param_hash == new_param_hash

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def load(self, trial: TrialRecord, stage: str) -> Optional[CheckpointRef]:
        """Load a checkpoint from the trial's artifact directory."""
        cp_path = Path(trial.artifact_dir) / "checkpoint.json" if trial.artifact_dir else None
        if not cp_path or not cp_path.is_file():
            return None
        try:
            data = json.loads(cp_path.read_text(encoding="utf-8"))
            cp = CheckpointRef.from_dict(data)
            if cp.stage == stage:
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
    def _write_checkpoint(trial: TrialRecord, cp: CheckpointRef) -> None:
        """Atomically write checkpoint.json inside the trial directory."""
        if not trial.artifact_dir:
            return
        trial_dir = Path(trial.artifact_dir)
        trial_dir.mkdir(parents=True, exist_ok=True)
        cp_path = trial_dir / "checkpoint.json"
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

    # Create fake artifact files for gcd/agenticpd_iter0 after FP stage
    for cat in ("results", "logs", "reports"):
        d = flow_dir / cat / "sky130hd" / "gcd" / "agenticpd_iter0"
        d.mkdir(parents=True)
    # Write a known file to hash
    fp_odb = flow_dir / "results" / "sky130hd" / "gcd" / "agenticpd_iter0" / "2_floorplan.odb"
    fp_odb.write_text("fake odb content for floorplan")
    fp_sdc = flow_dir / "results" / "sky130hd" / "gcd" / "agenticpd_iter0" / "2_floorplan.sdc"
    fp_sdc.write_text("fake sdc content")

    # Create a trial record
    from schemas.trial import TrialRecord
    trial = TrialRecord(
        trial_id="test0001",
        experiment_id="smoke-gcd-v1",
        status="ok",
        artifact_dir=str(runs_dir / "test0001"),
    )
    # Write trial.json so checkpoint has a home
    (runs_dir / "test0001").mkdir(parents=True)

    mgr = CheckpointManager(flow_dir)

    # -- Create checkpoint --
    phash = CheckpointManager.param_hash({"FP": {"CORE_UTILIZATION": 38}})
    cp = mgr.create(
        trial=trial,
        stage="FP",
        platform="sky130hd",
        design="gcd",
        variant="agenticpd_iter0",
        param_hash=phash,
    )
    check(cp.checkpoint_id == "cp-test0001-FP", "checkpoint id format")
    check(len(cp.artifact_manifest) >= 1, "manifest has >=1 files")
    check(cp.param_hash == phash, "param_hash preserved")
    check(cp.stage == "FP", "stage preserved")
    check(Path(trial.artifact_dir, "checkpoint.json").is_file(), "checkpoint.json written")

    # -- Verify: files exist and hashes match --
    is_ok, errors = mgr.verify(cp)
    check(is_ok, f"verify ok (errors: {errors})")

    # -- Verify: tampered file detected --
    fp_odb.write_text("TAMPERED CONTENT")
    is_ok2, errors2 = mgr.verify(cp)
    check(not is_ok2, "verify detects tampering")

    # -- Compatibility --
    same_hash = CheckpointManager.param_hash({"FP": {"CORE_UTILIZATION": 38}})
    diff_hash = CheckpointManager.param_hash({"FP": {"CORE_UTILIZATION": 50}})
    check(mgr.is_compatible(cp, same_hash), "compatible: same params")
    check(not mgr.is_compatible(cp, diff_hash), "incompatible: different params")

    # -- param_hash is deterministic --
    h1 = CheckpointManager.param_hash({"FP": {"A": 1, "B": 2}})
    h2 = CheckpointManager.param_hash({"FP": {"B": 2, "A": 1}})
    check(h1 == h2, "param_hash: key-order independent")

    # -- Load checkpoint from trial directory --
    cp_loaded = mgr.load(trial, "FP")
    check(cp_loaded is not None, "load checkpoint from trial dir")
    check(cp_loaded.checkpoint_id == cp.checkpoint_id, "loaded checkpoint id matches")

    # -- Load non-existent stage --
    check(mgr.load(trial, "CTS") is None, "load non-existent stage -> None")

    # Clean up
    shutil.rmtree(tmpdir)

    total = ok + fail
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed" + (f", {fail} FAILED" if fail else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail else 0)
