import json
import os
import sys

# Reuse s6_drift's own run discovery instead of re-deriving run ids here.
# A simulationRun partition packs many real experiment runs back to back
# (col_time resets to 1 at each boundary); guessing "sim{N}_run00" for every
# partition, as this used to do, silently picked only the first (and often
# shortest) sub-run of each partition and ignored the rest.
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from pipeline.s6_drift import load_runs

runs_dict, _ = load_runs("data/features")
runs = sorted(runs_dict.keys())

if len(runs) >= 15:
    fit, calibration = runs[:10], runs[10:15]
else:
    # Dev mode ingests only a handful of runs. runs[:10]/[10:15] would leave
    # calibration empty and crash s6_drift.py's calibrate_limits(). Split
    # whatever is available instead, always leaving at least one run in each
    # (falling back to reusing the fit set when there is only one run total).
    fit_n = max(1, round(len(runs) * 0.7))
    fit = runs[:fit_n]
    calibration = runs[fit_n:] or fit

manifest = {
    "fit": fit,
    "calibration": calibration
}

os.makedirs("contracts", exist_ok=True)
with open("contracts/reference_manifest.json", "w") as f:
    json.dump(manifest, f, indent=2)
print("Manifest created!")
