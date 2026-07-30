# -*- coding: utf-8 -*-
"""test_decision_trace.py — Stage D decision trace unit tests.

Covers containment validation, round-trip, corrupt-line skipping,
and cohort_already_executed.
"""

import copy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decision_trace import (
    DEFAULT_TRACE_PATH,
    DecisionTraceWriter,
    cohort_already_executed,
    cohort_decision_written,
    read_trace,
)
from schemas.trial import DecisionTraceRef


class ContainmentTest(unittest.TestCase):
    """trace_path must stay within runs_dir."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.runs_dir = self.tmpdir / "runs"
        self.runs_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_absolute_path_rejected(self):
        with self.assertRaises(ValueError):
            DecisionTraceWriter(self.runs_dir, "/etc/passwd")

    def test_dotdot_rejected(self):
        with self.assertRaises(ValueError):
            DecisionTraceWriter(self.runs_dir, "../escape/trace.jsonl")

    def test_resolved_outside_rejected(self):
        with self.assertRaises(ValueError):
            DecisionTraceWriter(self.runs_dir, "traces/../../outside.jsonl")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            DecisionTraceWriter(self.runs_dir, "")

    def test_valid_relative_accepted(self):
        w = DecisionTraceWriter(self.runs_dir, "traces/decisions.jsonl")
        self.assertEqual(w.trace_path, "traces/decisions.jsonl")

    def test_read_trace_also_validates(self):
        with self.assertRaises(ValueError):
            read_trace(self.runs_dir, "/absolute/trace.jsonl")


class DecisionTraceWriterTest(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.runs_dir = self.tmpdir / "runs"
        self.runs_dir.mkdir(parents=True)
        self.writer = DecisionTraceWriter(self.runs_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_first_append_creates_file(self):
        self.assertFalse(self.writer.full_path.is_file())
        self.writer.append({"entry_type": "t", "trial_id": "a", "data": {}})
        self.assertTrue(self.writer.full_path.is_file())

    def test_append_returns_valid_ref(self):
        ref = self.writer.append(
            {"entry_type": "gwtw", "trial_id": "t1", "data": {}})
        self.assertIsInstance(ref, DecisionTraceRef)
        self.assertEqual(ref.trace_path, DEFAULT_TRACE_PATH)
        self.assertTrue(ref.decision_id.startswith("dtr-"))
        self.assertEqual(len(ref.decision_id), 14)

    def test_round_trip_two_entries(self):
        r1 = self.writer.append(
            {"entry_type": "doomed", "trial_id": "a",
             "data": {"risk_class": "survivor"}})
        r2 = self.writer.append(
            {"entry_type": "gwtw", "trial_id": "a",
             "data": {"action": "continue"}})
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["decision_id"], r1.decision_id)
        self.assertEqual(entries[1]["decision_id"], r2.decision_id)

    def test_corrupt_line_skipped(self):
        p = self.runs_dir / DEFAULT_TRACE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '{"decision_id":"ok","entry_type":"t","trial_id":"x","data":{}}\n'
            'garbage\n'
            '{"decision_id":"ok2","entry_type":"t","trial_id":"y","data":{}}\n',
            encoding="utf-8")
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        self.assertEqual(len(entries), 2)

    def test_truncated_last_line_skipped(self):
        p = self.runs_dir / DEFAULT_TRACE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            '{"decision_id":"ok","entry_type":"t","trial_id":"x","data":{}}\n'
            '{"decision_id":"bad","entry_type":"t","trial_',
            encoding="utf-8")
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["decision_id"], "ok")

    def test_empty_file_returns_empty_list(self):
        p = self.runs_dir / DEFAULT_TRACE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
        self.assertEqual(read_trace(self.runs_dir, DEFAULT_TRACE_PATH), [])

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(
            read_trace(self.runs_dir, "traces/nonexistent.jsonl"), [])

    def test_append_does_not_mutate_caller_dict(self):
        orig = {"entry_type": "t", "trial_id": "a", "data": {}}
        before = dict(orig)
        self.writer.append(orig)
        self.assertEqual(orig, before)

    def test_decision_ids_unique(self):
        ids = set()
        for _ in range(15):
            ids.add(self.writer.append(
                {"entry_type": "t", "trial_id": "z", "data": {}}).decision_id)
        self.assertEqual(len(ids), 15)


class CohortAlreadyExecutedTest(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.runs_dir = self.tmpdir / "runs"
        self.runs_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _write_decisions(self, stage, seed, trial_ids, **cfg):
        from decision_trace import make_cohort_id
        w = DecisionTraceWriter(self.runs_dir)
        cid = make_cohort_id(stage, seed, trial_ids, **cfg)
        for tid in trial_ids:
            w.append({"entry_type": "gwtw_decision", "trial_id": tid,
                      "cohort_id": cid,
                      "data": {"action": "continue"},
                      "cohort_stage": stage, "cohort_seed": seed})

    def _write_complete_sentinel(self, stage, seed, trial_ids):
        from decision_trace import write_cohort_complete
        w = DecisionTraceWriter(self.runs_dir)
        write_cohort_complete(w, stage, seed, trial_ids)

    def test_decisions_written_no_sentinel_not_complete(self):
        self._write_decisions("PL", 42, ["a", "b"])
        # Decisions exist → decision_written is True.
        self.assertTrue(
            cohort_decision_written(self.runs_dir, DEFAULT_TRACE_PATH,
                                    "PL", seed=42, trial_ids=["a", "b"]))
        # No sentinel → NOT complete.
        self.assertFalse(
            cohort_already_executed(self.runs_dir, DEFAULT_TRACE_PATH,
                                    "PL", seed=42, trial_ids=["a", "b"]))

    def test_sentinel_makes_complete(self):
        self._write_decisions("PL", 42, ["a", "b"])
        self._write_complete_sentinel("PL", 42, ["a", "b"])
        self.assertTrue(
            cohort_already_executed(self.runs_dir, DEFAULT_TRACE_PATH,
                                    "PL", seed=42, trial_ids=["a", "b"]))

    def test_partial_decisions_not_complete(self):
        self._write_decisions("PL", 42, ["a"])
        self.assertFalse(
            cohort_decision_written(self.runs_dir, DEFAULT_TRACE_PATH,
                                    "PL", seed=42, trial_ids=["a", "b"]))
        self.assertFalse(
            cohort_already_executed(self.runs_dir, DEFAULT_TRACE_PATH,
                                    "PL", seed=42, trial_ids=["a", "b"]))

    def test_different_stage_returns_false(self):
        self._write_decisions("PL", 42, ["a"])
        self._write_complete_sentinel("PL", 42, ["a"])
        self.assertFalse(
            cohort_already_executed(self.runs_dir, DEFAULT_TRACE_PATH,
                                    "CTS", seed=42, trial_ids=["a"]))

    def test_different_seed_returns_false(self):
        self._write_decisions("PL", 42, ["a"])
        self._write_complete_sentinel("PL", 42, ["a"])
        self.assertFalse(
            cohort_already_executed(self.runs_dir, DEFAULT_TRACE_PATH,
                                    "PL", seed=99, trial_ids=["a"]))

    def test_empty_trace_returns_false(self):
        self.assertFalse(
            cohort_already_executed(self.runs_dir, DEFAULT_TRACE_PATH,
                                    "PL", seed=42, trial_ids=["a"]))

    def test_make_cohort_id_stable_and_collision_resistant(self):
        from decision_trace import make_cohort_id
        # Order-independent.
        a = make_cohort_id("PL", 42, ["abcd1234", "efab5678"])
        b = make_cohort_id("PL", 42, ["efab5678", "abcd1234"])
        self.assertEqual(a, b,
                         "cohort_id stable regardless of trial order")
        self.assertTrue(a.startswith("PL-s42-"))
        # Different trial sets → different ids.
        c = make_cohort_id("PL", 42, ["abcd1234", "9999ffff"])
        self.assertNotEqual(a, c,
                            "different trial sets → different ids")
        # Different config → different ids.
        d = make_cohort_id("PL", 42, ["abcd1234", "efab5678"],
                           survivor_count=1, population_size=3)
        self.assertNotEqual(a, d,
                            "different config → different ids")
        # Same config + trials → same id.
        e = make_cohort_id("PL", 42, ["abcd1234", "efab5678"],
                           survivor_count=2, audit_quota=0,
                           population_size=4, max_children_per_parent=2)
        f = make_cohort_id("PL", 42, ["abcd1234", "efab5678"],
                           survivor_count=2, audit_quota=0,
                           population_size=4, max_children_per_parent=2)
        self.assertEqual(e, f, "same config → same id")


if __name__ == "__main__":
    unittest.main()
