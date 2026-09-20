"""Bridges S6's per-run output to the single-file artifact S7 and the UI read.

pipeline/s6_drift.py, run without --run-id, scores every non-calibration run
and writes one file per run under artifacts/drift_events/. S7 and ui/server.py
both read the single combined artifacts/drift_events.json instead (the
contract in contracts/drift_events.schema.json). Nothing in the pipeline
currently bridges the two, so this picks the run with the most detected
events -- the most useful one to hand to S7 for diagnosis -- and copies it
into place.
"""
import glob
import json
import os

SCORED_DIR = "artifacts/drift_events"
OUT_PATH = "artifacts/drift_events.json"


def main():
    files = sorted(glob.glob(os.path.join(SCORED_DIR, "*.json")))
    if not files:
        raise SystemExit(
            f"No scored runs found in {SCORED_DIR}/. Run pipeline/s6_drift.py first."
        )

    best_path, best_data, best_n = None, None, -1
    for path in files:
        with open(path) as f:
            data = json.load(f)
        n = len(data.get("events", []))
        if n > best_n:
            best_path, best_data, best_n = path, data, n

    with open(OUT_PATH, "w") as f:
        json.dump(best_data, f, indent=2)

    print(f"Selected {best_path} ({best_n} events) -> {OUT_PATH}")


if __name__ == "__main__":
    main()
