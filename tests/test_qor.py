# -*- coding: utf-8 -*-
"""test_qor.py — QoR parser regression tests based on gcd smoke fixture."""
import json, sys, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils import QoR, qor_is_better

FIXTURE_DIR = Path(__file__).resolve().parent / 'fixtures' / 'legacy_run'
QOR_DIR = FIXTURE_DIR / 'qor'

def _load_expected():
    with open(FIXTURE_DIR / 'expected_qor.json', encoding='utf-8') as f:
        return json.load(f)


class JsonReportParsingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report_json = QOR_DIR / '6_report.json'
        cls.expected = _load_expected()['json_source']

    def test_json_file_exists(self):
        self.assertTrue(self.report_json.is_file())

    def test_parses_all_four_metrics(self):
        qor = QoR.from_report_json(self.report_json)
        self.assertTrue(qor.is_complete(), f'incomplete: {qor.to_dict()}')

    def test_wns_matches_expected(self):
        qor = QoR.from_report_json(self.report_json)
        self.assertAlmostEqual(qor.wns_ps, self.expected['wns_ps'], places=2)

    def test_tns_matches_expected(self):
        qor = QoR.from_report_json(self.report_json)
        self.assertAlmostEqual(qor.tns_ps, self.expected['tns_ps'], places=1)

    def test_area_matches_expected(self):
        qor = QoR.from_report_json(self.report_json)
        self.assertAlmostEqual(qor.area_um2, self.expected['area_um2'], places=2)

    def test_power_matches_expected(self):
        qor = QoR.from_report_json(self.report_json)
        self.assertAlmostEqual(qor.power_w, self.expected['power_mw'] / 1000.0, places=5)

    def test_wns_is_negative_for_gcd_baseline(self):
        qor = QoR.from_report_json(self.report_json)
        self.assertLess(qor.wns_ps, 0, f'gcd baseline WNS should be negative, got {qor.wns_ps}ps')

    def test_units_correctly_scaled_from_ns_to_ps(self):
        raw = json.loads(self.report_json.read_text(encoding='utf-8'))
        raw_ws_ns = raw['finish__timing__setup__ws']
        qor = QoR.from_report_json(self.report_json)
        self.assertAlmostEqual(qor.wns_ps, raw_ws_ns * 1000, places=3)


class TextFallbackParsingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.finish_rpt = QOR_DIR / '6_finish.rpt'
        cls.report_log = QOR_DIR / '6_report.log'
        cls.tol = _load_expected()['text_fallback_approx']

    def test_fallback_files_exist(self):
        self.assertTrue(self.finish_rpt.is_file())
        self.assertTrue(self.report_log.is_file())

    def test_fallback_has_known_gap_vs_json(self):
        json_qor = QoR.from_report_json(QOR_DIR / '6_report.json')
        fb = QoR.from_reports_fallback(self.finish_rpt, self.report_log)
        self.assertIsNotNone(fb.wns_ps, 'fallback failed to parse WNS')
        self.assertAlmostEqual(fb.wns_ps, json_qor.wns_ps, delta=self.tol['wns_ps_tolerance'])
        self.assertAlmostEqual(fb.tns_ps, json_qor.tns_ps, delta=self.tol['tns_ps_tolerance'])
        self.assertAlmostEqual(fb.area_um2, json_qor.area_um2, delta=self.tol['area_um2_tolerance'])
        self.assertAlmostEqual(fb.power_w, json_qor.power_w, delta=self.tol['power_mw_tolerance'] / 1000.0)


class QoRComparatorTest(unittest.TestCase):
    def setUp(self):
        self.baseline = QoR(wns_ps=-1460.0, tns_ps=-61747.0, area_um2=5400.0, power_w=0.00938)
        self.better_wns = QoR(wns_ps=-800.0, tns_ps=-30000.0, area_um2=5400.0, power_w=0.00938)
        self.better_tns = QoR(wns_ps=-1460.0, tns_ps=-40000.0, area_um2=5400.0, power_w=0.00938)
        self.lower_power = QoR(wns_ps=-1460.0, tns_ps=-61747.0, area_um2=5400.0, power_w=0.00800)
        self.tol = (10.0, 50.0)

    def test_better_wns_wins(self):
        self.assertTrue(qor_is_better(self.better_wns, self.baseline, *self.tol))

    def test_worse_wns_loses(self):
        self.assertFalse(qor_is_better(self.baseline, self.better_wns, *self.tol))

    def test_better_tns_wins_when_wns_tied(self):
        self.assertTrue(qor_is_better(self.better_tns, self.baseline, *self.tol))

    def test_lower_power_wins_when_timing_tied(self):
        self.assertTrue(qor_is_better(self.lower_power, self.baseline, *self.tol))

    def test_none_loses(self):
        self.assertFalse(qor_is_better(None, self.baseline, *self.tol))
        self.assertFalse(qor_is_better(QoR(wns_ps=-100.0), self.baseline, *self.tol))

    def test_none_old_always_wins(self):
        self.assertTrue(qor_is_better(self.baseline, None, *self.tol))

    def test_exact_tie_keeps_old(self):
        clone = QoR(wns_ps=-1460.0, tns_ps=-61747.0, area_um2=5400.0, power_w=0.00938)
        self.assertFalse(qor_is_better(clone, self.baseline, *self.tol))

    def test_both_timing_met_skips_to_power(self):
        met1 = QoR(wns_ps=50.0, tns_ps=0.0, area_um2=5000.0, power_w=0.010)
        met2 = QoR(wns_ps=10.0, tns_ps=0.0, area_um2=5000.0, power_w=0.008)
        self.assertTrue(qor_is_better(met2, met1, *self.tol))


class QoRDataClassTest(unittest.TestCase):
    def test_is_complete(self):
        self.assertTrue(QoR(wns_ps=-1,tns_ps=-2,area_um2=100,power_w=0.001).is_complete())
        self.assertFalse(QoR().is_complete())
        self.assertFalse(QoR(wns_ps=-1).is_complete())

    def test_pretty_format(self):
        qor = QoR(wns_ps=-1460.3, tns_ps=-61747.6, area_um2=5400.2, power_w=0.00937965)
        s = qor.pretty()
        self.assertIn('-1460.3ps', s)
        self.assertIn('5400.2um2', s)
        self.assertIn('9.3796', s)

    def test_roundtrip(self):
        o = QoR(wns_ps=-1,tns_ps=-2,area_um2=100,power_w=0.001)
        r = QoR.from_dict(o.to_dict())
        self.assertEqual(o.wns_ps, r.wns_ps)
        self.assertEqual(o.tns_ps, r.tns_ps)
        self.assertEqual(o.area_um2, r.area_um2)
        self.assertEqual(o.power_w, r.power_w)


if __name__ == '__main__':
    unittest.main()
