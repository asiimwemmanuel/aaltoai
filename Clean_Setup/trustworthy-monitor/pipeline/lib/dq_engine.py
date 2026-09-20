import os
import json
import datetime
import numpy as np

class DataQualityEngine:
    # Stage 5 (S5) Data Quality Trust Gate.
    # Evaluates incoming telemetry batches before process drift/fault reasoning.
    # Enforces: A dead sensor is not a process fault.
    # Supports standard and nested (Giorgio/S2) schema and profile formats.
    def __init__(
        self,
        schema_path='contracts/schema.json',
        profiles_path='artifacts/profiles.json',
        decision_log_path='artifacts/decision_log.jsonl',
        variance_epsilon=1e-5,
        min_window_for_stuck=5
    ):
        self.schema_path = schema_path
        self.profiles_path = profiles_path
        self.decision_log_path = decision_log_path
        self.variance_epsilon = variance_epsilon
        self.min_window_for_stuck = min_window_for_stuck
        
        self.schema = self._load_json(schema_path)
        self.profiles = self._load_json(profiles_path)
        
        meta = self.schema.get('dataset_metadata', {})
        self.time_col = meta.get('time_column_id') or self.schema.get('time_column', 'col_time')
        
        cols_entry = self.schema.get('columns', {})
        if isinstance(cols_entry, dict):
            self.monitored_cols = [k for k in cols_entry.keys() if k != self.time_col]
        elif isinstance(cols_entry, list):
            self.monitored_cols = [c['col_id'] for c in cols_entry if c.get('col_id') != self.time_col]
        else:
            self.monitored_cols = list(self.profiles.keys())

    def _load_json(self, path):
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return {}

    def _append_log(self, record):
        os.makedirs(os.path.dirname(self.decision_log_path), exist_ok=True)
        with open(self.decision_log_path, 'a') as f:
            f.write(json.dumps(record) + chr(10))

    def check_batch(self, batch_df, batch_id='batch_auto', compiled_rules=None):
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        
        data = {}
        if hasattr(batch_df, 'to_dict'):
            raw_dict = batch_df.to_dict(orient='list') if 'orient' in getattr(batch_df.to_dict, '__code__', {}).co_varnames else dict(batch_df)
            for k, v in raw_dict.items():
                data[k] = np.array(v, dtype=float)
        elif isinstance(batch_df, dict):
            for k, v in batch_df.items():
                data[k] = np.array(v, dtype=float)
        else:
            raise ValueError('batch_df must be a DataFrame or dict of lists/arrays')

        if not data:
            raise ValueError('Empty batch provided to check_batch')

        total_samples = len(next(iter(data.values())))
        failures = []
        checks_run = 0
        checks_passed = 0

        if self.time_col in data and total_samples > 1:
            checks_run += 1
            sample_series = data[self.time_col]
            diffs = np.diff(sample_series)
            if np.any(diffs != 1):
                violating_indices = np.where(diffs != 1)[0].tolist()
                failures.append({
                    'evidence_id': f'ev_dq_{batch_id}_timeliness',
                    'check_type': 'TIMELINESS_GAP',
                    'target_col': self.time_col,
                    'detail': f'Sample sequence gap/jump detected at indices {violating_indices[:5]}',
                    'severity': 'WARNING',
                    'action_taken': 'FLAG_TIMING_IRREGULARITY'
                })
            else:
                checks_passed += 1

        for col_id in self.monitored_cols:
            if col_id not in data:
                continue
            vals = data[col_id]
            col_raw_profile = self.profiles.get(col_id, {})
            stats = col_raw_profile.get('statistics', col_raw_profile)

            checks_run += 1
            nan_mask = np.isnan(vals) | np.isinf(vals)
            nan_count = int(np.sum(nan_mask))
            if nan_count > 0:
                failures.append({
                    'evidence_id': f'ev_dq_{batch_id}_{col_id}_null',
                    'check_type': 'COMPLETENESS_MISSING',
                    'target_col': col_id,
                    'detail': f'{nan_count} missing or infinite readings out of {total_samples} samples',
                    'severity': 'CRITICAL',
                    'action_taken': 'ISOLATE_SENSOR_DATA_CORRUPT'
                })
            else:
                checks_passed += 1

            checks_run += 1
            clean_vals = vals[~nan_mask]
            if len(clean_vals) >= self.min_window_for_stuck:
                std_val = float(np.std(clean_vals[-15:]))
                val_range = float(np.ptp(clean_vals[-15:]))
                if std_val < self.variance_epsilon or val_range < self.variance_epsilon:
                    last_val = float(clean_vals[-1])
                    failures.append({
                        'evidence_id': f'ev_dq_{batch_id}_{col_id}_frozen',
                        'check_type': 'FROZEN_SENSOR',
                        'target_col': col_id,
                        'detail': f'Signal flatlined at constant value {round(last_val, 4)} (variance={std_val:.2e})',
                        'severity': 'CRITICAL',
                        'action_taken': 'ISOLATE_SENSOR_AND_HALT_PROCESS_REASONING'
                    })
                else:
                    checks_passed += 1
            else:
                checks_passed += 1

            checks_run += 1
            if stats:
                mean_v = stats.get('mean')
                std_v = stats.get('std_dev', stats.get('std', 1.0))
                
                h_min = stats.get('hard_min')
                if h_min is None and mean_v is not None:
                    h_min = mean_v - 4.5 * (std_v if std_v > 1e-4 else 1.0)
                elif h_min is None:
                    h_min = -1e9

                h_max = stats.get('hard_max')
                if h_max is None and mean_v is not None:
                    h_max = mean_v + 4.5 * (std_v if std_v > 1e-4 else 1.0)
                elif h_max is None:
                    h_max = 1e9

                out_bounds = (clean_vals < h_min) | (clean_vals > h_max)
                out_count = int(np.sum(out_bounds))
                if out_count > 0:
                    failures.append({
                        'evidence_id': f'ev_dq_{batch_id}_{col_id}_range',
                        'check_type': 'OUT_OF_RANGE',
                        'target_col': col_id,
                        'detail': f'{out_count} readings outside operational envelope [{round(h_min,2)}, {round(h_max,2)}]',
                        'severity': 'WARNING',
                        'action_taken': 'FLAG_RANGE_VIOLATION'
                    })
                else:
                    checks_passed += 1
            else:
                checks_passed += 1

        rules_report = []
        if compiled_rules:
            for rule in compiled_rules:
                checks_run += 1
                r_id = rule.get('rule_id', 'UNKNOWN_RULE')
                r_text = rule.get('raw_text', '')
                r_col = rule.get('target_col')
                fn = rule.get('executable')
                
                try:
                    passed = fn(data)
                except Exception:
                    passed = False
                
                if passed:
                    checks_passed += 1
                    rules_report.append({
                        'rule_id': r_id,
                        'raw_text': r_text,
                        'target_col': r_col,
                        'status': 'PASS',
                        'violating_samples_count': 0
                    })
                else:
                    failures.append({
                        'evidence_id': f'ev_dq_{batch_id}_{r_id}_fail',
                        'check_type': 'RULE_VIOLATION',
                        'target_col': r_col,
                        'detail': f'Operating rule violation: {r_text}',
                        'severity': 'WARNING',
                        'action_taken': 'ALERT_OPERATOR_RULE_BREACH'
                    })
                    rules_report.append({
                        'rule_id': r_id,
                        'raw_text': r_text,
                        'target_col': r_col,
                        'status': 'FAIL',
                        'violating_samples_count': 1
                    })

        critical_failures = [f for f in failures if f['severity'] == 'CRITICAL']
        warning_failures = [f for f in failures if f['severity'] == 'WARNING']

        if critical_failures:
            trust_verdict = 'UNTRUSTED'
        elif warning_failures:
            trust_verdict = 'DEGRADED'
        else:
            trust_verdict = 'TRUSTED'

        report = {
            'batch_id': batch_id,
            'evaluated_at': now_str,
            'trust_verdict': trust_verdict,
            'total_samples': total_samples,
            'total_columns_checked': len(self.monitored_cols),
            'checks_run_count': checks_run,
            'checks_passed_count': checks_passed,
            'checks_failed_count': len(failures),
            'failures': failures,
            'compiled_rules_evaluated': rules_report
        }

        os.makedirs('artifacts', exist_ok=True)
        with open('artifacts/dq_report.json', 'w') as f:
            json.dump(report, f, indent=2)

        log_entry = {
            'timestamp': now_str,
            'stage': 'S5_DataQuality',
            'event': 'BATCH_EVALUATED',
            'batch_id': batch_id,
            'verdict': trust_verdict,
            'checks_passed': checks_passed,
            'checks_failed': len(failures),
            'failure_types': list(set(f['check_type'] for f in failures)),
            'evidence_ids': [f['evidence_id'] for f in failures]
        }
        self._append_log(log_entry)

        return report
