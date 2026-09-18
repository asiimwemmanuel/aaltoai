"""Unit tests for S6. Run from the Hackathon/ folder:  python -m unittest tests/test_s6_drift.py"""
import json
import os
import sys
import tempfile
import unittest

import numpy as np
import polars as pl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import s6_drift as s6  # noqa: E402

COLS = [f"col_{i:03d}" for i in range(1, 7)]


def write_partition(root, sim, pieces):
    folder = os.path.join(root, f"simulationRun={sim}.0")
    os.makedirs(folder)
    frames = []
    for times, offset in pieces:
        data = {"col_time": list(times)}
        for j, c in enumerate(COLS):
            data[c] = [offset + j + 0.01 * t for t in times]
        frames.append(pl.DataFrame(data))
    pl.concat(frames).write_parquet(os.path.join(folder, "data_0.parquet"))


class TestRunRebuild(unittest.TestCase):
    def test_split_run_is_glued_back_together(self):
        with tempfile.TemporaryDirectory() as root:
            write_partition(root, 1, [(range(1, 6), 0), (range(1, 5), 100), (range(6, 9), 0)])
            runs, col_ids = s6.load_runs(root)
        self.assertEqual(col_ids, COLS)
        self.assertEqual(sorted(runs), ["sim1_run00", "sim1_run01"])
        self.assertEqual(runs["sim1_run00"]["t"].tolist(), list(range(1, 9)))
        self.assertEqual(runs["sim1_run01"]["t"].tolist(), [1, 2, 3, 4])
        self.assertAlmostEqual(runs["sim1_run00"]["x"][7, 0], 0.08)


class TestPersistence(unittest.TestCase):
    def test_short_bursts_are_ignored_and_long_ones_are_one_event(self):
        alarm = np.array([0, 1, 1, 0, 0, 1, 1, 1, 1, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0], dtype=bool)
        spans = s6.find_alarm_spans(alarm, persistence=5, release=3)
        self.assertEqual(spans, [(5, 9, 11)])

    def test_alarm_still_on_at_the_end_has_no_end(self):
        alarm = np.array([0, 0, 1, 1, 1, 1, 1, 1], dtype=bool)
        self.assertEqual(s6.find_alarm_spans(alarm, persistence=5, release=3), [(2, 6, None)])


class TestDetection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = np.random.default_rng(7)
        cls.params = dict(s6.DEFAULTS)
        fit = rng.normal(size=(2000, 6))
        cal = rng.normal(size=(1000, 6))
        cls.model = s6.fit_reference(fit, COLS, cls.params["variance_target"])
        cls.limits = s6.calibrate_limits(cls.model, [cal], cls.params["limit_percentile"])
        x = rng.normal(size=(200, 6))
        x[100:, 2] += 4.0
        cls.shifted = {"t": np.arange(1, 201), "x": x}
        cls.clean = {"t": np.arange(1, 201), "x": rng.normal(size=(200, 6))}

    def detect(self, run, gate=None):
        return s6.detect_run("test_run", run, self.model, self.limits, self.params, "now", gate)

    def test_shift_is_found_and_blamed_on_the_right_column(self):
        result = self.detect(self.shifted)
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertGreaterEqual(event["start_sample"], 101)
        self.assertLessEqual(event["start_sample"], 105)
        top = event["ranked_signals"][0]
        self.assertEqual(top["col_id"], "col_003")
        self.assertEqual(top["direction"], "above_normal")
        self.assertTrue(all(0 <= r["share"] <= 1 for r in event["ranked_signals"]))

    def test_every_event_cites_evidence_that_exists(self):
        result = self.detect(self.shifted)
        known = {e["evidence_id"] for e in result["evidence"]}
        for event in result["events"]:
            self.assertTrue(event["evidence_ids"])
            self.assertTrue(set(event["evidence_ids"]) <= known)

    def test_clean_run_raises_no_event(self):
        self.assertEqual(self.detect(self.clean)["events"], [])

    def test_s5_gate_is_recorded_as_evidence(self):
        gate = {"verdict": "DEGRADED", "source_batch_id": "b1", "failure_evidence_ids": ["ev_dq_b1_x"]}
        result = self.detect(self.shifted, gate)
        self.assertIn("ev_s6_dq_gate", result["events"][0]["evidence_ids"])


class TestGateFile(unittest.TestCase):
    def test_reads_verdict_and_failure_ids(self):
        report = {"batch_id": "b9", "trust_verdict": "UNTRUSTED",
                  "failures": [{"evidence_id": "ev_dq_b9_col_006_frozen"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(report, f)
        try:
            gate = s6.read_dq_gate(f.name)
        finally:
            os.remove(f.name)
        self.assertEqual(gate["verdict"], "UNTRUSTED")
        self.assertEqual(gate["failure_evidence_ids"], ["ev_dq_b9_col_006_frozen"])
        self.assertIsNone(s6.read_dq_gate(None))


class TestTeamRules(unittest.TestCase):
    def test_no_labels_or_original_names_in_s6(self):
        with open(os.path.join(HERE, "..", "scripts", "s6_drift.py")) as f:
            source = f.read()
        for forbidden in ("faultNumber", "fault_status", "xmeas", "xmv_"):
            self.assertNotIn(forbidden, source)

    def test_s6_does_not_import_other_stages(self):
        with open(os.path.join(HERE, "..", "scripts", "s6_drift.py")) as f:
            source = f.read()
        for other in ("s1_ingest", "s2_profiling", "s3_relations", "s4_semantics", "dq_engine"):
            self.assertNotIn(other, source)


if __name__ == "__main__":
    unittest.main()
