"""Dev-only: identify which reconstructed run is which scenario, using the labels in the raw CSV.

Writes:
  contracts/reference_manifest.dev.json  provisional list of normal runs for S6 (fit / calibration)
  eval/run_index.json                run_id -> scenario, split, onset (for evaluation only)

Run from the Hackathon/ folder:  python eval/dev_manifest.py --csv /path/to/te_process.csv
"""
import argparse
import json
import os
import sys

import duckdb
import polars as pl

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from s6_drift import load_runs  # noqa: E402

ONSET = {"train": 21, "test": 161}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="te_process.csv")
    ap.add_argument("--features", default="data/features")
    ap.add_argument("--schema", default="contracts/schema.json")
    args = ap.parse_args()

    with open(args.schema) as f:
        columns = json.load(f)["columns"]
    runs, col_ids = load_runs(args.features)
    names = [columns[c]["original_name"] for c in col_ids]

    print("Reading labels from the CSV (this scans the whole file)...")
    con = duckdb.connect()
    labels = con.execute(
        f"SELECT simulationRun, sample, faultNumber, source, {', '.join(names)} "
        f"FROM read_csv_auto('{args.csv}') WHERE simulationRun <= 2"
    ).pl().with_columns(pl.col("simulationRun").cast(pl.Int64), pl.col("sample").cast(pl.Int64))

    # Scenarios of one partition share a seed, so their first samples are identical.
    # The last sample of a run is where they have diverged: match on it with all columns.
    index = {}
    for run_id, run in runs.items():
        sim = int(run_id.split("_")[0].removeprefix("sim"))
        last = int(run["t"].max())
        row = run["x"][int(run["t"].argmax())]
        cond = (pl.col("simulationRun") == sim) & (pl.col("sample") == last)
        for name, value in zip(names, row):
            cond = cond & (pl.col(name) == float(value))
        hit = labels.filter(cond).select("faultNumber", "source").unique()
        if hit.height != 1:
            raise SystemExit(f"{run_id}: expected 1 matching scenario, found {hit.height}")
        fault = int(hit["faultNumber"][0])
        source = hit["source"][0]
        n_expected = 500 if source == "train" else 960
        if len(run["t"]) != n_expected:
            raise SystemExit(f"{run_id}: {len(run['t'])} samples but split {source} expects {n_expected}")
        index[run_id] = {
            "simulationRun": sim,
            "faultNumber": fault,
            "source": source,
            "n_samples": int(len(run["t"])),
            "onset": ONSET[source] if fault != 0 else None,
        }

    normal_train = [r for r, m in index.items() if m["faultNumber"] == 0 and m["source"] == "train"]
    normal_test = sorted(r for r, m in index.items() if m["faultNumber"] == 0 and m["source"] == "test")
    manifest = {
        "provisional": True,
        "description": "Normal runs for the development sample. fit = train normals, "
                       "calibration = one held-out test normal. The other test normals are "
                       "kept out for measuring false alarms.",
        "fit": sorted(normal_train),
        "calibration": normal_test[:1],
    }
    os.makedirs("contracts", exist_ok=True)
    with open("contracts/reference_manifest.dev.json", "w") as f:
        json.dump(manifest, f, indent=2)
    with open("eval/run_index.json", "w") as f:
        json.dump(index, f, indent=2)
    print(f"{len(index)} runs identified. fit={manifest['fit']} calibration={manifest['calibration']} "
          f"held-out normal={normal_test[1:]}")


if __name__ == "__main__":
    main()
