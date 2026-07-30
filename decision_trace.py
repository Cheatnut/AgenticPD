# -*- coding: utf-8 -*-
"""decision_trace.py — Stage D: append-only decision trace JSONL writer / reader.

Pure Python, no ORFS, no LLM, no network.  All paths are validated to stay
within the session runs directory — absolute paths, ``..`` traversal, and
symlink escapes are rejected at construction / read time.

Usage:
    writer = DecisionTraceWriter(runs_dir)
    ref = writer.append({
        "entry_type": "doomed_decision",
        "trial_id": "abc12345",
        "data": doomed_decision.to_dict(),
        "cohort_stage": "PL",
        "cohort_seed": 42,
    })
    # ref.trace_path == "traces/decisions.jsonl"

    entries = read_trace(runs_dir, "traces/decisions.jsonl")

    # Idempotency guard: has this cohort already been executed?
    if cohort_already_executed(runs_dir, trace_path, "PL", seed=42,
                               trial_ids=["a","b"]):
        ...
"""

from __future__ import annotations

import io
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from schemas.trial import DecisionTraceRef

log = logging.getLogger(__name__)

DEFAULT_TRACE_PATH: str = "traces/decisions.jsonl"


# =============================================================================
# Path containment
# =============================================================================


def _validate_trace_path(runs_dir: Path, trace_path: str) -> Path:
    """Reject trace paths that escape *runs_dir* and return the resolved
    absolute ``full_path``.

    Raises:
        ValueError: *trace_path* is empty, absolute, contains ``..``, or
            resolves outside *runs_dir*.
    """
    if not trace_path:
        raise ValueError("trace_path must not be empty")
    if trace_path.startswith("/"):
        raise ValueError(
            f"trace_path must be relative, got absolute: {trace_path!r}")
    if ".." in Path(trace_path).parts:
        raise ValueError(
            f"trace_path must not contain '..': {trace_path!r}")

    runs_dir = Path(runs_dir).resolve()
    full = (runs_dir / trace_path).resolve()

    # Containment: the resolved path must be inside runs_dir.
    try:
        full.relative_to(runs_dir)
    except ValueError:
        raise ValueError(
            f"trace_path {trace_path!r} resolves to {full} which is "
            f"outside runs_dir {runs_dir}"
        ) from None

    return full


# =============================================================================
# Writer
# =============================================================================


class DecisionTraceWriter:
    """Append-only JSONL writer for decision trace entries.

    Validates *trace_path* on construction so the file is always within
    *runs_dir*.
    """

    def __init__(
        self,
        runs_dir: Path,
        trace_path: str = DEFAULT_TRACE_PATH,
    ) -> None:
        self._runs_dir = Path(runs_dir).resolve()
        self._trace_path = trace_path
        self._full_path = _validate_trace_path(self._runs_dir, trace_path)

    @property
    def trace_path(self) -> str:
        return self._trace_path

    @property
    def full_path(self) -> Path:
        return self._full_path

    def append(self, entry: Dict[str, Any]) -> DecisionTraceRef:
        """Append one decision entry as a JSON line.

        Injects ``decision_id`` (``dtr-`` + 10 hex) and ``timestamp``
        (ISO-8601 UTC) when missing.  Returns a :class:`DecisionTraceRef`.
        """
        entry = dict(entry)
        decision_id: str = entry.setdefault(
            "decision_id", f"dtr-{uuid.uuid4().hex[:10]}")
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

        self._full_path.parent.mkdir(parents=True, exist_ok=True)

        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with open(self._full_path, "a+", encoding="utf-8") as fh:
            _ensure_trailing_newline(fh)
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

        return DecisionTraceRef(
            decision_id=decision_id,
            trace_path=self._trace_path,
        )


# =============================================================================
# Reader
# =============================================================================


def read_trace(
    runs_dir: Path,
    trace_path: str,
) -> List[Dict[str, Any]]:
    """Read all valid entries from a decision trace JSONL file.

    Corrupt JSON, blank lines, and trailing partial lines are silently
    skipped.  Returns ``[]`` when the file does not exist.
    """
    full_path = _validate_trace_path(Path(runs_dir), trace_path)
    if not full_path.is_file():
        return []

    entries: List[Dict[str, Any]] = []
    with open(full_path, "r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                log.debug(
                    "[decision_trace] skipping corrupt line %d in %s: %s",
                    lineno, trace_path, exc,
                )
                continue
            entries.append(entry)

    return entries


# =============================================================================
# Cohort identity helpers (stable across runs)
# =============================================================================


def make_cohort_id(
    decision_stage: str,
    seed: int,
    trial_ids: List[str],
    survivor_count: int = 0,
    audit_quota: int = 0,
    population_size: int = 0,
    max_children_per_parent: int = 0,
    doomed_rule_version: str = "",
    scheduler_version: str = "",
    planner_version: str = "",
) -> str:
    """Return a collision-resistant, deterministic cohort identifier.

    The id is ``<stage>-s<seed>-<sha256_prefix>`` where the hash covers:
    trial IDs, planning config, and version strings — so different rules
    or scheduler versions on the same trial set produce different ids.
    """
    import hashlib
    canonical = json.dumps({
        "stage": decision_stage,
        "seed": seed,
        "trial_ids": sorted(trial_ids),
        "survivor_count": survivor_count,
        "audit_quota": audit_quota,
        "population_size": population_size,
        "max_children_per_parent": max_children_per_parent,
        "doomed_rule_version": doomed_rule_version,
        "scheduler_version": scheduler_version,
        "planner_version": planner_version,
    }, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    return f"{decision_stage}-s{seed}-{digest}"


# =============================================================================
# Idempotency guard (sentinel-based, not per-trial)
# =============================================================================


def _filter_by_cohort(
    entries: List[Dict[str, Any]],
    cohort_id: str,
) -> List[Dict[str, Any]]:
    """Return entries whose ``cohort_id`` matches."""
    return [e for e in entries if e.get("cohort_id") == cohort_id]


def cohort_decision_written(
    runs_dir: Path,
    trace_path: str,
    decision_stage: str,
    seed: int,
    trial_ids: List[str],
    survivor_count: int = 0,
    audit_quota: int = 0,
    population_size: int = 0,
    max_children_per_parent: int = 0,
    doomed_rule_version: str = "",
    scheduler_version: str = "",
    planner_version: str = "",
) -> bool:
    """Return True if every *trial_id* already has a gwtw_decision for this
    cohort (isolated by cohort_id, not just stage+seed)."""
    cohort_id = make_cohort_id(
        decision_stage, seed, trial_ids,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    cohort_entries = _filter_by_cohort(
        read_trace(runs_dir, trace_path), cohort_id)
    trial_ids_seen: set = set()
    for entry in cohort_entries:
        if entry.get("entry_type") == "gwtw_decision":
            trial_ids_seen.add(entry.get("trial_id", ""))
    return set(trial_ids).issubset(trial_ids_seen)


def cohort_already_executed(
    runs_dir: Path,
    trace_path: str,
    decision_stage: str,
    seed: int,
    trial_ids: List[str],
    survivor_count: int = 0,
    audit_quota: int = 0,
    population_size: int = 0,
    max_children_per_parent: int = 0,
    doomed_rule_version: str = "",
    scheduler_version: str = "",
    planner_version: str = "",
) -> bool:
    """Return True if a ``cohort_complete`` sentinel exists for this cohort_id."""
    cohort_id = make_cohort_id(
        decision_stage, seed, trial_ids,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    cohort_entries = _filter_by_cohort(
        read_trace(runs_dir, trace_path), cohort_id)
    for entry in cohort_entries:
        if entry.get("entry_type") == "cohort_complete":
            return True
    return False


def write_cohort_complete(
    writer: "DecisionTraceWriter",
    decision_stage: str,
    seed: int,
    trial_ids: List[str],
    survivor_count: int = 0,
    audit_quota: int = 0,
    population_size: int = 0,
    max_children_per_parent: int = 0,
    doomed_rule_version: str = "",
    scheduler_version: str = "",
    planner_version: str = "",
) -> DecisionTraceRef:
    """Write a ``cohort_complete`` sentinel.  Call AFTER all fork children
    are created and persisted."""
    cohort_id = make_cohort_id(
        decision_stage, seed, trial_ids,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    return writer.append({
        "entry_type": "cohort_complete",
        "cohort_id": cohort_id,
        "cohort_stage": decision_stage,
        "cohort_seed": seed,
        "trial_ids": sorted(trial_ids),
        "survivor_count": survivor_count,
        "population_size": population_size,
        "data": {"status": "complete"},
    })


# =============================================================================
# Fork intent helpers
# =============================================================================


def write_fork_intents(
    writer: "DecisionTraceWriter",
    cohort_id: str,
    decision_stage: str,
    seed: int,
    fork_plans: List[Any],  # List[ForkPlan]
) -> List[DecisionTraceRef]:
    """Persist ALL fork intents to trace BEFORE executing any of them.

    Each intent records the deterministic metadata needed to recreate
    the child (parent, checkpoint, param, derived_seed) so recovery can
    replay intents one-by-one without re-planning.
    """
    refs: List[DecisionTraceRef] = []
    for idx, fp in enumerate(fork_plans):
        refs.append(writer.append({
            "entry_type": "fork_intent",
            "cohort_id": cohort_id,
            "cohort_stage": decision_stage,
            "cohort_seed": seed,
            "intent_index": idx,
            "parent_trial_id": fp.fork_request.parent_trial_id,
            "checkpoint_id": fp.checkpoint_id,
            "param_name": fp.evidence.param_name,
            "old_value": fp.evidence.old_value,
            "new_value": fp.evidence.new_value,
            "derived_seed": fp.derived_seed,
            "data": {
                "decision_stage": decision_stage,
                "param_stage": fp.evidence.stage,
            },
        }))
    return refs


def read_fork_intents(
    runs_dir: Path,
    trace_path: str,
    cohort_id: str,
) -> List[Dict[str, Any]]:
    """Read fork intents for *cohort_id* in intent_index order."""
    entries = _filter_by_cohort(
        read_trace(runs_dir, trace_path), cohort_id)
    intents = [e for e in entries if e.get("entry_type") == "fork_intent"]
    intents.sort(key=lambda e: e.get("intent_index", 0))
    return intents


# =============================================================================
# Helpers
# =============================================================================


def _ensure_trailing_newline(fh) -> None:
    """If the open file ends without ``\\n``, write one so the next append
    starts on a clean line (crash-recovery measure)."""
    try:
        pos = fh.tell()
        if pos == 0:
            return
        fh.seek(max(0, pos - 1))
        last_byte = fh.read(1)
        fh.seek(pos)
        if last_byte and last_byte != "\n":
            fh.write("\n")
    except (OSError, io.UnsupportedOperation):
        pass


# =============================================================================
# Self-test
# =============================================================================

if __name__ == "__main__":
    import shutil
    import sys
    import tempfile

    ok = 0
    fail_count = 0

    def check(cond, msg):
        global ok, fail_count
        if cond:
            ok += 1
        else:
            fail_count += 1
            print(f"  FAIL: {msg}")

    tmpdir = Path(tempfile.mkdtemp())
    runs_dir = tmpdir / "runs"
    runs_dir.mkdir(parents=True)

    # -- containment: absolute path rejected --
    try:
        DecisionTraceWriter(runs_dir, "/etc/passwd")
        check(False, "absolute trace_path should raise ValueError")
    except ValueError as e:
        check("absolute" in str(e).lower(), f"absolute path msg: {e}")

    # -- containment: .. traversal rejected --
    try:
        DecisionTraceWriter(runs_dir, "../escape/trace.jsonl")
        check(False, ".. trace_path should raise ValueError")
    except ValueError as e:
        check(".." in str(e), f".. path msg: {e}")

    # -- containment: symlink escape rejected --
    escape_dir = tmpdir / "escape"
    escape_dir.mkdir()
    symlink = runs_dir / "traces" / "escape_link"
    symlink.parent.mkdir(parents=True, exist_ok=True)
    symlink.symlink_to(escape_dir)
    try:
        DecisionTraceWriter(runs_dir, "traces/escape_link/../decisions.jsonl")
        check(False, "symlink .. traversal should raise ValueError")
    except ValueError as e:
        check("outside runs_dir" in str(e).lower()
              or ".." in str(e), f"symlink escape msg: {e}")

    # -- containment: resolved-outside rejected --
    try:
        DecisionTraceWriter(runs_dir, "traces/../../outside.jsonl")
        check(False, "resolved-outside should raise ValueError")
    except ValueError as e:
        check("outside runs_dir" in str(e).lower()
              or ".." in str(e), f"outside msg: {e}")

    # -- containment: empty rejected --
    try:
        DecisionTraceWriter(runs_dir, "")
        check(False, "empty trace_path should raise ValueError")
    except ValueError as e:
        check("must not be empty" in str(e).lower(), f"empty msg: {e}")

    # -- containment: valid relative path accepted --
    writer = DecisionTraceWriter(runs_dir, "traces/decisions.jsonl")
    check(writer.trace_path == "traces/decisions.jsonl",
          "valid relative path accepted")

    # -- read_trace also validates --
    try:
        read_trace(runs_dir, "/absolute/trace.jsonl")
        check(False, "read_trace absolute should raise ValueError")
    except ValueError as e:
        check("absolute" in str(e).lower(), f"read absolute msg: {e}")

    # 1. Basic append + round-trip (entries include cohort_id for isolation)
    _cid1 = make_cohort_id("PL", 42, ["abc12345"])
    ref1 = writer.append({
        "entry_type": "doomed_decision",
        "cohort_id": _cid1,
        "trial_id": "abc12345",
        "data": {"risk_class": "survivor", "risk_score": 1.0},
        "cohort_stage": "PL", "cohort_seed": 42,
    })
    check(isinstance(ref1, DecisionTraceRef), "ref type")
    check(ref1.decision_id.startswith("dtr-"), "decision_id prefix")
    check(len(ref1.decision_id) == 14, "decision_id length")
    check(ref1.trace_path == "traces/decisions.jsonl", "ref trace_path")

    ref2 = writer.append({
        "entry_type": "gwtw_decision",
        "cohort_id": _cid1,
        "trial_id": "abc12345",
        "data": {"action": "continue", "decision_stage": "PL"},
        "cohort_stage": "PL", "cohort_seed": 42,
    })

    entries = read_trace(runs_dir, "traces/decisions.jsonl")
    check(len(entries) == 2, f"round-trip 2 entries: {len(entries)}")
    check(entries[0]["decision_id"] == ref1.decision_id, "entry 0 id match")
    check(entries[1]["decision_id"] == ref2.decision_id, "entry 1 id match")

    # 2. make_cohort_id is stable and collision-resistant
    cid1 = make_cohort_id("PL", 42, ["abcd1234", "efab5678"])
    cid2 = make_cohort_id("PL", 42, ["efab5678", "abcd1234"])
    check(cid1 == cid2,
          f"cohort_id stable regardless of trial order: {cid1} vs {cid2}")
    check(cid1.startswith("PL-s42-"), f"cohort_id prefix: {cid1}")
    check(len(cid1) > 12, f"cohort_id has hash suffix: {cid1}")

    # 2b. Collision resistance: different trial sets → different ids
    cid_a = make_cohort_id("PL", 42, ["aaaa1111", "bbbb2222"])
    cid_b = make_cohort_id("PL", 42, ["aaaa1111", "cccc3333"])
    check(cid_a != cid_b,
          f"different trial sets → different ids: {cid_a} vs {cid_b}")

    # 2c. Config-aware: different quotas → different ids
    cid_cfg1 = make_cohort_id("PL", 42, ["aaaa1111"],
                              survivor_count=2, audit_quota=0,
                              population_size=4, max_children_per_parent=2)
    cid_cfg2 = make_cohort_id("PL", 42, ["aaaa1111"],
                              survivor_count=1, audit_quota=0,
                              population_size=4, max_children_per_parent=2)
    check(cid_cfg1 != cid_cfg2,
          f"different config → different ids: {cid_cfg1} vs {cid_cfg2}")

    # 2d. Same config, same trials → same id
    cid_rep1 = make_cohort_id("CTS", 7, ["x1", "x2"],
                              survivor_count=1, audit_quota=1,
                              population_size=3, max_children_per_parent=1)
    cid_rep2 = make_cohort_id("CTS", 7, ["x1", "x2"],
                              survivor_count=1, audit_quota=1,
                              population_size=3, max_children_per_parent=1)
    check(cid_rep1 == cid_rep2, "same config+trials → same id")

    # 3. cohort_decision_written (isolated by cohort_id, not just stage+seed)
    # The entries written above have cohort_id embedded; check with matching config.
    _cid3 = make_cohort_id("PL", 42, ["abc12345"])
    check(cohort_decision_written(runs_dir, "traces/decisions.jsonl",
          "PL", seed=42, trial_ids=["abc12345"]),
          "cohort_decision_written: True (default config matches)")
    check(not cohort_decision_written(runs_dir, "traces/decisions.jsonl",
          "PL", seed=42, trial_ids=["abc12345", "unknown"]),
          "cohort_decision_written: False when missing")

    # 4. cohort_already_executed requires cohort_complete sentinel
    check(not cohort_already_executed(runs_dir, "traces/decisions.jsonl",
          "PL", seed=42, trial_ids=["abc12345"]),
          "cohort_already_executed: False before sentinel written")

    # Write sentinel → now complete.
    write_cohort_complete(writer, "PL", seed=42, trial_ids=["abc12345"])
    check(cohort_already_executed(runs_dir, "traces/decisions.jsonl",
          "PL", seed=42, trial_ids=["abc12345"]),
          "cohort_already_executed: True after sentinel")
    # Different stage/seed still false.
    check(not cohort_already_executed(runs_dir, "traces/decisions.jsonl",
          "CTS", seed=42, trial_ids=["abc12345"]),
          "cohort_already_executed: False for different stage")
    check(not cohort_already_executed(runs_dir, "traces/decisions.jsonl",
          "PL", seed=99, trial_ids=["abc12345"]),
          "cohort_already_executed: False for different seed")

    # Verify sentinel entry is in trace.
    all_e = read_trace(runs_dir, "traces/decisions.jsonl")
    sentinels = [e for e in all_e if e["entry_type"] == "cohort_complete"]
    check(len(sentinels) == 1, f"1 sentinel: {len(sentinels)}")
    check(sentinels[0]["trial_ids"] == ["abc12345"],
          "sentinel trial_ids")

    # 5. Crash recovery: decisions written, no sentinel → can resume forks
    writer2 = DecisionTraceWriter(runs_dir, "traces/recovery.jsonl")
    _rcid = make_cohort_id("PL", 7, ["x", "y"],
                           survivor_count=1, population_size=3)
    writer2.append({"entry_type": "gwtw_decision", "trial_id": "x",
                    "cohort_id": _rcid,
                    "data": {"action": "continue"},
                    "cohort_stage": "PL", "cohort_seed": 7})
    writer2.append({"entry_type": "gwtw_decision", "trial_id": "y",
                    "cohort_id": _rcid,
                    "data": {"action": "pause"},
                    "cohort_stage": "PL", "cohort_seed": 7})
    check(cohort_decision_written(runs_dir, "traces/recovery.jsonl",
          "PL", seed=7, trial_ids=["x", "y"],
          survivor_count=1, population_size=3),
          "pre-fork: decisions written")
    check(not cohort_already_executed(runs_dir, "traces/recovery.jsonl",
          "PL", seed=7, trial_ids=["x", "y"],
          survivor_count=1, population_size=3),
          "pre-fork: NOT complete (no sentinel)")
    # Simulate forks done + sentinel.
    writer2.append({"entry_type": "fork", "trial_id": "child1",
                    "cohort_id": _rcid,
                    "cohort_stage": "PL", "cohort_seed": 7,
                    "data": {"checkpoint_id": "cp-x"}})
    write_cohort_complete(writer2, "PL", seed=7, trial_ids=["x", "y"],
                          survivor_count=1, population_size=3)
    check(cohort_already_executed(runs_dir, "traces/recovery.jsonl",
          "PL", seed=7, trial_ids=["x", "y"],
          survivor_count=1, population_size=3),
          "post-fork+sentinel: complete")

    # 6. Corrupt line + truncated line skipped
    corrupt_path = runs_dir / "traces" / "corrupt.jsonl"
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text(
        '{"decision_id":"ok-1","entry_type":"test","trial_id":"t1","data":{}}\n'
        'bad line\n'
        '{"decision_id":"ok-2","entry_type":"test","trial_id":"t2","data":{}}\n'
        '{"decision_id":"bad","entry_type":"t","trial_id"',
        encoding="utf-8",
    )
    r = read_trace(runs_dir, "traces/corrupt.jsonl")
    check(len(r) == 2, f"corrupt skip: 2 valid, got {len(r)}")
    check(r[0]["decision_id"] == "ok-1", "corrupt keep ok-1")
    check(r[1]["decision_id"] == "ok-2", "corrupt keep ok-2")

    # 4. Empty / missing → []
    empty = runs_dir / "traces" / "empty.jsonl"
    empty.parent.mkdir(parents=True, exist_ok=True)
    empty.write_text("", encoding="utf-8")
    check(read_trace(runs_dir, "traces/empty.jsonl") == [], "empty → []")
    check(read_trace(runs_dir, "traces/nonexistent.jsonl") == [], "missing → []")

    # 5. Custom decision_id / timestamp preserved
    ref_custom = writer.append({
        "decision_id": "dtr-custom-01",
        "timestamp": "2025-01-01T00:00:00Z",
        "entry_type": "observation",
        "trial_id": "x",
        "data": {"stage_wns_ps": -50},
        "cohort_stage": "PL", "cohort_seed": 7,
    })
    check(ref_custom.decision_id == "dtr-custom-01", "custom id preserved")
    entries_c = read_trace(runs_dir, "traces/decisions.jsonl")
    check(entries_c[-1]["timestamp"] == "2025-01-01T00:00:00Z",
          "custom timestamp preserved")

    # 6. Append does not mutate caller's dict
    original = {"entry_type": "t", "trial_id": "y", "data": {}}
    before = dict(original)
    writer.append(original)
    check(original == before, "caller dict not mutated")

    # 7. Decision IDs are unique
    ids = set()
    for _ in range(10):
        ids.add(writer.append({"entry_type": "t", "trial_id": "z",
                               "data": {}}).decision_id)
    check(len(ids) == 10, f"10 unique decision_ids: {len(ids)}")

    shutil.rmtree(tmpdir)

    total = ok + fail_count
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed"
          + (f", {fail_count} FAILED" if fail_count else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail_count else 0)
