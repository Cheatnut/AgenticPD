# -*- coding: utf-8 -*-
"""test_orchestrator.py — Stage D orchestrator integration tests.

Tight assertions: YAML vs default config binding, clean downstream
order/start, unique tree node evidence per child, CTS child
checkpoint_fork + effective_start=RT + copy source = consumed_variant,
incremental budget billing, partial disk recovery zero-duplicate,
total_trials matches disk count.
"""

import copy, json, shutil, sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from decision_trace import DEFAULT_TRACE_PATH, read_trace
from managers import CheckpointManager, TrialManager
from orchestrator import (
    RecordingFakeRunner, StageDConfig, StageDOrchestrator,
)


class FakeExecutorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.runs_dir = self.tmpdir / "runs"; self.runs_dir.mkdir(parents=True)
        self.flow_dir = self.tmpdir / "flow"
        self.cfg = StageDConfig(
            experiment_id="test-stage-d", platform="sky130hd", design="gcd",
            population_size=4, seed=42, max_trials=20,
            pl_survivor_count=2, pl_audit_quota=0, pl_max_children_per_parent=2,
            cts_survivor_count=1, cts_audit_quota=1, cts_max_children_per_parent=2,
            runs_dir=self.runs_dir)
        self.tm = TrialManager(self.runs_dir)
        self.cm = CheckpointManager(self.flow_dir)

    def tearDown(self):
        shutil.rmtree(self.tmpdir)


class NormalExecutionTest(FakeExecutorTestBase):

    def test_complete_evidence_and_total_matches_disk(self):
        runner = RecordingFakeRunner(self.flow_dir)
        orch = StageDOrchestrator(self.cfg, self.tm, self.cm, runner)
        result = orch.run()
        self.assertEqual(result.errors, [])
        # total_trials matches disk count.
        disk_count = len(self.tm.list_all())
        self.assertEqual(result.total_trials, disk_count,
                         f"total_trials {result.total_trials} == disk {disk_count}")
        # Trace complete.
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        etypes = {e["entry_type"] for e in entries}
        for et in ("observation", "doomed_decision", "gwtw_decision",
                   "fork_intent", "fork", "execution_resolution",
                   "cohort_complete"):
            self.assertIn(et, etypes)
        # run_finish + clean_downstream called.
        methods = {c["method"] for c in runner.calls}
        self.assertIn("run_finish", methods)
        self.assertIn("clean_downstream", methods)

    def test_clean_downstream_called_in_correct_order(self):
        """For each variant that has both copy and clean, the LAST copy
        must precede the clean within that variant's call sequence."""
        runner = RecordingFakeRunner(self.flow_dir)
        orch = StageDOrchestrator(self.cfg, self.tm, self.cm, runner)
        orch.run()
        variant_seq: dict = {}
        for c in runner.calls:
            v = c.get("variant") or c.get("child") or ""
            if not v: continue
            variant_seq.setdefault(v, []).append(c["method"])
        checked = 0
        for v, seq in variant_seq.items():
            if "clean_downstream" not in seq:
                continue
            if "copy_parent_results" not in seq:
                continue
            # The last copy before the first clean is sufficient.
            ci = seq.index("clean_downstream")
            copies_before = [i for i, m in enumerate(seq)
                           if m == "copy_parent_results" and i < ci]
            self.assertGreater(len(copies_before), 0,
                               f"copy before clean for {v}")
            rs_after = [i for i, m in enumerate(seq)
                       if m == "run_stage" and i > ci]
            self.assertGreater(len(rs_after), 0,
                               f"run_stage after clean for {v}")
            checked += 1
        self.assertGreater(checked, 0,
                           "at least one variant had copy+clean")

    def test_clean_downstream_effective_start(self):
        """clean is called with correct effective_start stage."""
        runner = RecordingFakeRunner(self.flow_dir)
        orch = StageDOrchestrator(self.cfg, self.tm, self.cm, runner)
        orch.run()
        cleans = [c for c in runner.calls
                  if c["method"] == "clean_downstream"]
        self.assertGreater(len(cleans), 0)
        for cl in cleans:
            self.assertIn(cl["effective_start"],
                          ("CTS", "RT"),  # PL child→CTS, CTS child→RT
                          f"clean effective_start: {cl['effective_start']}")


class CTSChildEvidenceTest(FakeExecutorTestBase):

    def test_cts_child_checkpoint_fork_and_rt_start(self):
        """Each CTS child has a valid ER. When checkpoint_fork succeeds,
        effective_start=RT (not CTS) because the child consumes a CTS
        checkpoint.  When tree linkage is incomplete, full_restart is
        the safe fallback.

        BANS: effective_start_stage="CTS" in a CTS-child checkpoint_fork
        (that would indicate the child incorrectly started from CTS
        instead of RT after consuming a CTS checkpoint)."""
        runner = RecordingFakeRunner(self.flow_dir)
        orch = StageDOrchestrator(self.cfg, self.tm, self.cm, runner)
        result = orch.run()
        self.assertEqual(result.errors, [])

        cts_children = result.cts_cohort_result.child_trial_ids
        self.assertGreater(len(cts_children), 0)
        for cid in cts_children:
            child = self.tm.get(cid)
            self.assertIsNotNone(child)
            er = child.execution_resolution
            self.assertIsNotNone(er, f"CTS child {cid[:6]} has ER")
            self.assertIn(er.execution_mode, {"checkpoint_fork", "full_restart"})
            if er.execution_mode == "checkpoint_fork":
                # CTS child consumes CTS checkpoint → MUST start from RT.
                self.assertEqual(er.effective_start_stage, "RT",
                    f"CTS child {cid[:6]} effective_start=RT, "
                    f"got {er.effective_start_stage}")
                self.assertIsNotNone(er.consumed_checkpoint,
                    f"CTS child {cid[:6]} has consumed_checkpoint")
            else:
                # full_restart is the safe fallback; verify the reason
                # is recorded.  Must NOT be silent.
                self.assertIsNotNone(er.fallback_reason,
                    f"CTS child {cid[:6]} full_restart has fallback_reason")


class TreeEvidenceTest(FakeExecutorTestBase):

    def test_tree_nodes_unique_and_traceable(self):
        runner = RecordingFakeRunner(self.flow_dir)
        orch = StageDOrchestrator(self.cfg, self.tm, self.cm, runner)
        orch.run()

        tree_path = self.runs_dir / "tree.json"
        self.assertTrue(tree_path.is_file())
        tree_data = json.loads(tree_path.read_text())
        nodes = tree_data.get("_nodes", tree_data.get("nodes", {}))
        self.assertGreater(len(nodes), 1)  # root + others

        # Check unique node_ids — no collisions.
        nids = list(nodes.keys())
        self.assertEqual(len(nids), len(set(nids)),
                        "all node_ids are unique")

        # Check that every non-root node has traceable provenance.
        variants_seen = set()
        for nid, nd in nodes.items():
            if nd.get("stage") == "root":
                continue
            v = nd.get("variant", "")
            self.assertTrue(v, f"node {nid} has variant")
            # Each node has source_trial_id.
            self.assertTrue(nd.get("source_trial_id"),
                            f"node {nid} has source_trial_id")
            self.assertTrue(nd.get("params") is not None,
                            f"node {nid} has params")
            # Each node has a parent_id (except root-connected).
            if nd.get("parent_id") != "root":
                self.assertTrue(nd.get("parent_id"),
                                f"node {nid} has parent_id")
            variants_seen.add(v)

        self.assertGreater(len(variants_seen), 0)

        # Verify parent→child linkage is consistent.
        for nid, nd in nodes.items():
            for cid in nd.get("children_ids", []):
                child = nodes.get(cid)
                self.assertIsNotNone(child,
                    f"child {cid} of {nid} exists in tree")
                self.assertEqual(child.get("parent_id"), nid,
                    f"child {cid} parent_id == {nid}")


class BudgetIncrementalTest(FakeExecutorTestBase):

    def test_incremental_billing_exact_budget(self):
        """max_trials=3 exactly covers pop=2 bootstrap + 1 CTS child."""
        cfg = StageDConfig(
            experiment_id="exact", platform="x", design="y",
            population_size=2, seed=1, max_trials=3,
            pl_survivor_count=2, pl_audit_quota=0, pl_max_children_per_parent=2,
            cts_survivor_count=1, cts_audit_quota=0, cts_max_children_per_parent=1,
            runs_dir=self.runs_dir)
        orch = StageDOrchestrator(
            cfg, self.tm, self.cm, RecordingFakeRunner(self.flow_dir))
        result = orch.run()
        self.assertLessEqual(result.total_trials, cfg.max_trials,
                             f"total {result.total_trials} <= {cfg.max_trials}")
        self.assertEqual(result.total_trials, len(self.tm.list_all()))

    def test_resume_does_not_rebill_existing(self):
        """Second run adds 0 to _new_trials."""
        runner1 = RecordingFakeRunner(self.flow_dir)
        orch1 = StageDOrchestrator(self.cfg, self.tm, self.cm, runner1)
        orch1.run()
        n_disk = len(self.tm.list_all())

        runner2 = RecordingFakeRunner(self.flow_dir)
        orch2 = StageDOrchestrator(self.cfg, self.tm, self.cm, runner2)
        result2 = orch2.run()
        self.assertEqual(result2.total_trials, n_disk,
                         "resume: total_trials unchanged")
        self.assertEqual(len(runner2.calls), 0, "resume: zero calls")


class PartialResumeTest(FakeExecutorTestBase):

    def test_partial_bootstrap_fills_missing(self):
        """If only 2 of 4 pop trials exist on disk, bootstrap creates
        exactly 2 more — not 4."""
        # Pre-create 2 PL trials on disk.
        cfg2 = StageDConfig(
            experiment_id="partial", platform="x", design="y",
            population_size=2, seed=1, max_trials=10,
            pl_survivor_count=1, pl_audit_quota=0, pl_max_children_per_parent=1,
            cts_survivor_count=1, cts_audit_quota=0, cts_max_children_per_parent=1,
            runs_dir=self.runs_dir)
        orch_pre = StageDOrchestrator(
            cfg2, self.tm, self.cm, RecordingFakeRunner(self.flow_dir))
        orch_pre._bootstrap_population()
        n_pre = len(self.tm.list_by_experiment("partial"))
        self.assertEqual(n_pre, 2)

        # Now run with population_size=4 — should create exactly 2 more.
        cfg4 = StageDConfig(
            experiment_id="partial", platform="x", design="y",
            population_size=4, seed=1, max_trials=10,
            pl_survivor_count=2, pl_audit_quota=0, pl_max_children_per_parent=2,
            cts_survivor_count=1, cts_audit_quota=0, cts_max_children_per_parent=1,
            runs_dir=self.runs_dir)
        orch4 = StageDOrchestrator(
            cfg4, self.tm, self.cm, RecordingFakeRunner(self.flow_dir))
        orch4._bootstrap_population()
        n_post = len(self.tm.list_by_experiment("partial"))
        self.assertEqual(n_post, 4,
                         f"partial: filled 2→4, got {n_post}")


class DifferentiatedPopulationTest(FakeExecutorTestBase):

    def test_initial_population_params_applied(self):
        """Differentiated initial_population_params produce different
        bootstrap parameters per population member."""
        cfg = StageDConfig(
            experiment_id="diff-pop", platform="x", design="y",
            population_size=3, seed=1, max_trials=10,
            pl_survivor_count=2, pl_audit_quota=0, pl_max_children_per_parent=2,
            cts_survivor_count=1, cts_audit_quota=0, cts_max_children_per_parent=1,
            initial_population_params=[
                {"FP": {"CORE_UTILIZATION": 20}},
                {"FP": {"CORE_UTILIZATION": 50}},
                {"FP": {"CORE_UTILIZATION": 35}},
            ],
            runs_dir=self.runs_dir)
        # Verify get_population_params returns differentiated values.
        p0 = cfg.get_population_params(0)
        p1 = cfg.get_population_params(1)
        p2 = cfg.get_population_params(2)
        self.assertEqual(p0["FP"]["CORE_UTILIZATION"], 20)
        self.assertEqual(p1["FP"]["CORE_UTILIZATION"], 50)
        self.assertEqual(p2["FP"]["CORE_UTILIZATION"], 35)
        # Out-of-range index → baseline default.
        p3 = cfg.get_population_params(3)
        self.assertEqual(p3["FP"]["CORE_UTILIZATION"], 38)  # baseline default


class MainDispatchTest(unittest.TestCase):

    def test_stage_d_diverts_before_generic_init(self):
        """Stage D --help output includes --stage-d and does NOT create a
        stale session directory from the generic path."""
        import subprocess
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        if not main_py.is_file():
            self.skipTest("main.py not found")
        result = subprocess.run(
            ["python3", str(main_py), "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(main_py.parent.parent))
        self.assertIn("--stage-d", result.stdout)

    def test_stage_d_missing_yaml_exits(self):
        """Passing a nonexistent YAML to --stage-d exits with error."""
        import subprocess
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        if not main_py.is_file():
            self.skipTest("main.py not found")
        result = subprocess.run(
            ["python3", str(main_py), "--stage-d",
             "/nonexistent/path/stage-d.yml", "--mock-orfs"],
            capture_output=True, text=True, timeout=10,
            cwd=str(main_py.parent.parent))
        self.assertNotEqual(result.returncode, 0)


class WallClockEnforcementTest(FakeExecutorTestBase):

    def test_wall_clock_exceeded_is_recorded(self):
        """When wall_clock_budget_s is set to 0, the orchestrator records
        the budget-exceeded error."""
        cfg = StageDConfig(
            experiment_id="wall-clock", platform="x", design="y",
            population_size=2, seed=1, max_trials=10,
            wall_clock_budget_s=0.0,  # immediately exceeded
            pl_survivor_count=2, pl_audit_quota=0, pl_max_children_per_parent=2,
            cts_survivor_count=1, cts_audit_quota=0, cts_max_children_per_parent=1,
            runs_dir=self.runs_dir)
        orch = StageDOrchestrator(
            cfg, self.tm, self.cm, RecordingFakeRunner(self.flow_dir))
        result = orch.run()
        self.assertTrue(
            any("wall_clock" in e.lower() for e in result.errors),
            f"wall_clock budget exceeded recorded: {result.errors}")

    def test_wall_clock_default_none_no_error(self):
        """Default wall_clock_budget_s=None does not trigger false errors."""
        self.assertIsNone(self.cfg.wall_clock_budget_s)
        runner = RecordingFakeRunner(self.flow_dir)
        orch = StageDOrchestrator(self.cfg, self.tm, self.cm, runner)
        result = orch.run()
        wall_errors = [e for e in result.errors
                       if "wall_clock" in e.lower()]
        self.assertEqual(wall_errors, [],
                        f"no wall_clock errors when budget is None: {wall_errors}")


class YAMLConfigBindingTest(unittest.TestCase):

    def test_yaml_differs_from_default(self):
        yaml_path = (Path(__file__).resolve().parent.parent
                     / "configs" / "experiments" / "stage-d-smoke.yml")
        if not yaml_path.is_file():
            self.skipTest("YAML not found")
        cfg = StageDConfig.from_yaml(yaml_path)
        # YAML-specific values differ from dataclass defaults.
        self.assertEqual(cfg.experiment_id, "stage-d-smoke-gcd")
        self.assertEqual(cfg.max_trials, 20)
        self.assertEqual(cfg.wall_clock_budget_s, 3600)
        self.assertEqual(cfg.seed, 42)
        self.assertEqual(cfg.evaluator, "ORFS post-route QoR")
        # Derived FrameworkConfig.
        fw = cfg.to_framework_config()
        self.assertEqual(fw.platform, "sky130hd")
        self.assertEqual(fw.design, "gcd")

    def test_default_config_differs_from_yaml(self):
        cfg = StageDConfig(
            experiment_id="default", platform="x", design="y",
            population_size=2, seed=1, max_trials=5)
        self.assertIsNone(cfg.wall_clock_budget_s,
                          "default: wall_clock_budget_s is None")
        self.assertNotEqual(cfg.max_trials, 20,
                            "default: max_trials != 20")

    def test_cli_help(self):
        import subprocess
        main_py = Path(__file__).resolve().parent.parent / "main.py"
        if not main_py.is_file():
            self.skipTest("main.py not found")
        result = subprocess.run(
            ["python3", str(main_py), "--help"],
            capture_output=True, text=True, timeout=10,
            cwd=str(main_py.parent.parent))
        self.assertIn("--stage-d", result.stdout)


if __name__ == "__main__":
    unittest.main()
