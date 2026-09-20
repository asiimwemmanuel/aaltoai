import unittest
import json
import os
import numpy as np

from pipeline.lib.dq_engine import DataQualityEngine
from pipeline.lib.rule_compiler import RuleCompiler
from eval.inject_fault import (
    inject_frozen_sensor,
    inject_missing_data,
    inject_out_of_bounds,
    inject_timestamp_gap
)

class TestDataQualityEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = DataQualityEngine(
            schema_path='contracts/schema.json',
            profiles_path='artifacts/profiles.json',
            decision_log_path='artifacts/decision_log.jsonl'
        )
        cls.compiler = RuleCompiler(schema_path='contracts/schema.json')

    def _generate_synthetic_clean_batch(self, samples=20):
        # Generates clean data based on profiles.json (supporting nested statistics)
        time_col = self.engine.time_col
        batch = {time_col: list(range(1, samples + 1))}
        for col_id, p in self.engine.profiles.items():
            stats = p.get('statistics', p)
            mean = stats.get('mean', 50.0)
            std = stats.get('std_dev', stats.get('std', 1.0))
            std = std if std > 1e-4 else 0.5
            noise = np.random.normal(0, std * 0.1, samples)
            batch[col_id] = (mean + noise).tolist()
        return batch

    def test_01_clean_batch_is_trusted(self):
        batch = self._generate_synthetic_clean_batch()
        report = self.engine.check_batch(batch, batch_id='test_batch_clean')
        
        self.assertEqual(report['trust_verdict'], 'TRUSTED')
        self.assertEqual(report['checks_failed_count'], 0)
        self.assertEqual(len(report['failures']), 0)
        self.assertTrue(os.path.exists('artifacts/dq_report.json'))

    def test_02_frozen_sensor_is_untrusted(self):
        batch = self._generate_synthetic_clean_batch()
        target = 'col_007'
        corrupted, meta = inject_frozen_sensor(batch, target_col=target, freeze_val=2705.0, samples=15)
        
        report = self.engine.check_batch(corrupted, batch_id='test_batch_frozen')
        
        self.assertEqual(report['trust_verdict'], 'UNTRUSTED')
        frozen_failures = [f for f in report['failures'] if f['check_type'] == 'FROZEN_SENSOR']
        self.assertGreaterEqual(len(frozen_failures), 1)
        self.assertEqual(frozen_failures[0]['target_col'], target)
        self.assertEqual(frozen_failures[0]['severity'], 'CRITICAL')

    def test_03_missing_data_is_untrusted(self):
        batch = self._generate_synthetic_clean_batch()
        target = 'col_012'
        corrupted, meta = inject_missing_data(batch, target_col=target, count=4)
        
        report = self.engine.check_batch(corrupted, batch_id='test_batch_missing')
        
        self.assertEqual(report['trust_verdict'], 'UNTRUSTED')
        null_failures = [f for f in report['failures'] if f['check_type'] == 'COMPLETENESS_MISSING']
        self.assertGreaterEqual(len(null_failures), 1)
        self.assertEqual(null_failures[0]['target_col'], target)

    def test_04_out_of_range_triggers_alert(self):
        batch = self._generate_synthetic_clean_batch()
        target = 'col_007'
        corrupted, meta = inject_out_of_bounds(batch, target_col=target, spike_value=999999.0)
        
        report = self.engine.check_batch(corrupted, batch_id='test_batch_range')
        
        range_failures = [f for f in report['failures'] if f['check_type'] == 'OUT_OF_RANGE' and f['target_col'] == target]
        self.assertEqual(len(range_failures), 1)
        self.assertEqual(range_failures[0]['target_col'], target)

    def test_05_rule_compiler_and_evaluation(self):
        rules_text = [
            'Reactor pressure must not exceed 2900',
            'col_009 between 50 and 200',
            'col_051 at least 999999.0' # Intentionally failing rule
        ]
        compiled = self.compiler.compile_rules(rules_text)
        self.assertEqual(len(compiled), 3)

        batch = self._generate_synthetic_clean_batch()
        report = self.engine.check_batch(batch, batch_id='test_batch_rules', compiled_rules=compiled)

        rule_results = report['compiled_rules_evaluated']
        self.assertEqual(len(rule_results), 3)
        self.assertEqual(rule_results[0]['status'], 'PASS')
        self.assertEqual(rule_results[1]['status'], 'PASS')
        self.assertEqual(rule_results[2]['status'], 'FAIL')

    def test_06_decision_log_appended(self):
        with open('artifacts/decision_log.jsonl', 'r') as f:
            lines = f.readlines()
        self.assertGreaterEqual(len(lines), 2)
        last_entry = json.loads(lines[-1])
        self.assertEqual(last_entry['stage'], 'S5_DataQuality')
        self.assertIn('verdict', last_entry)

if __name__ == '__main__':
    unittest.main()
