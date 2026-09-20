import copy
import numpy as np

def inject_frozen_sensor(batch_data, target_col='col_006', freeze_val=None, samples=15):
    corrupted = copy.deepcopy(batch_data)
    if target_col in corrupted:
        arr = np.array(corrupted[target_col], dtype=float)
        val = freeze_val if freeze_val is not None else float(arr[0])
        samples_to_freeze = min(samples, len(arr))
        arr[-samples_to_freeze:] = val
        corrupted[target_col] = arr.tolist()
    return corrupted, {
        'injection_type': 'FROZEN_SENSOR',
        'target_col': target_col,
        'detail': f'Frozen at value {freeze_val} for last {samples} samples'
    }

def inject_missing_data(batch_data, target_col='col_012', count=5):
    corrupted = copy.deepcopy(batch_data)
    if target_col in corrupted:
        arr = np.array(corrupted[target_col], dtype=float)
        indices = np.random.choice(len(arr), size=min(count, len(arr)), replace=False)
        arr[indices] = np.nan
        corrupted[target_col] = arr.tolist()
    return corrupted, {
        'injection_type': 'COMPLETENESS_MISSING',
        'target_col': target_col,
        'detail': f'{count} NaN values injected'
    }

def inject_out_of_bounds(batch_data, target_col='col_006', spike_value=99999.0):
    corrupted = copy.deepcopy(batch_data)
    if target_col in corrupted:
        arr = np.array(corrupted[target_col], dtype=float)
        arr[-1] = spike_value
        corrupted[target_col] = arr.tolist()
    return corrupted, {
        'injection_type': 'OUT_OF_RANGE',
        'target_col': target_col,
        'detail': f'Spike injected at {spike_value}'
    }

def inject_timestamp_gap(batch_data, time_col='sample', gap=5):
    corrupted = copy.deepcopy(batch_data)
    if time_col in corrupted:
        arr = np.array(corrupted[time_col], dtype=float)
        if len(arr) > 2:
            arr[-2:] += gap
        corrupted[time_col] = arr.tolist()
    return corrupted, {
        'injection_type': 'TIMELINESS_GAP',
        'target_col': time_col,
        'detail': f'Time gap jump of +{gap} injected'
    }

if __name__ == '__main__':
    print('Hardware Fault Injector library ready.')
