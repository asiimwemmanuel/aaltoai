"""Ground truth check. EVAL ONLY -- this file is allowed to read the labels.

CONTRACTS.md section 3 quarantines `faultNumber` and `fault_status` to this
directory: they exist to score the system, never to run it. Nothing here writes
into `artifacts/`, nothing here is imported by `pipeline/`, and nothing here is
ever passed to `trust.gateway`. The report it writes lands in `eval/` and is
read by one page that says EVAL ONLY across the top.

It answers four questions an operator should be allowed to ask before trusting
any of this:

  1. When a fault was actually injected, did the detector fire, and how late?
  2. On a clean run, did it fire anyway?
  3. Do the channels it blames have anything to do with the fault?
  4. Did the semantic stage recover the structural split that is really there?

Question 4 is answered without this file ever naming a column. The original
names are read from `artifacts/schema.json` at runtime and grouped by their own
alphabetic prefix, so the script measures whether S4 separated the families that
exist in the data without being told what those families are. That is the
difference between validating a claim and feeding it the answer.

    python -m eval.validate_detection            # writes eval/validation_report.json
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LABELS_DIR = ROOT / "data" / "labels"
SCORED_DIR = ROOT / "artifacts" / "drift_events"
SCHEMA_PATH = ROOT / "artifacts" / "schema.json"
SEMANTICS_PATH = ROOT / "artifacts" / "semantics.json"
DQ_PATH = ROOT / "artifacts" / "dq_report.json"
MANIFEST_PATH = ROOT / "contracts" / "reference_manifest.json"
OUT_PATH = ROOT / "eval" / "validation_report.json"

LABEL_COL = "faultNumber"
STATUS_COL = "fault_status"
TIME_COL = "col_time"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------
# Rebuilding the same runs S6 scored
# --------------------------------------------------------------------------

def load_label_runs() -> dict[str, dict]:
    """Split the label Parquet into runs exactly the way S6 splits the features.

    Same rule, same order: a run starts where col_time returns to 1, numbered in
    file order. If this drifts from `pipeline/s6_drift.load_runs`, every number
    below is aligned to the wrong run and the report is worse than useless, so
    the rule is restated here rather than imported -- eval does not reach into
    a stage either.
    """
    import polars as pl

    runs: dict[str, dict] = {}
    for sim_dir in sorted(glob.glob(str(LABELS_DIR / "simulationRun=*"))):
        match = re.search(r"simulationRun=([\d.]+)", sim_dir)
        if not match:
            continue
        sim = int(float(match.group(1)))
        frame = pl.read_parquet(os.path.join(sim_dir, "*.parquet"))

        t = frame[TIME_COL].to_list()
        fault = frame[LABEL_COL].to_list() if LABEL_COL in frame.columns else [None] * len(t)
        status = frame[STATUS_COL].to_list() if STATUS_COL in frame.columns else [None] * len(t)

        cuts = [i for i, v in enumerate(t) if v == 1]
        if not cuts:
            continue
        bounds = cuts + [len(t)]
        for k in range(len(cuts)):
            lo, hi = bounds[k], bounds[k + 1]
            runs[f"sim{sim}_run{k:02d}"] = {
                "time": t[lo:hi],
                "fault": fault[lo:hi],
                "status": status[lo:hi],
            }
    return runs


def summarise_truth(run: dict) -> dict:
    """What actually happened in this run, according to the labels."""
    faults = [f for f in run["fault"] if f is not None]
    non_zero = [f for f in faults if f and f != 0]
    counts = Counter(int(f) for f in non_zero)
    dominant = counts.most_common(1)[0][0] if counts else 0

    first_faulty = None
    for sample, (fault, status) in enumerate(zip(run["fault"], run["status"]), start=1):
        bad = (fault not in (None, 0, 0.0)) or (str(status).lower() == "faulty")
        if bad:
            first_faulty = run["time"][sample - 1]
            break

    faulty_samples = sum(
        1 for fault, status in zip(run["fault"], run["status"])
        if (fault not in (None, 0, 0.0)) or (str(status).lower() == "faulty")
    )
    return {
        "fault_number": dominant,
        "is_faulty": bool(non_zero) or faulty_samples > 0,
        "first_faulty_sample": first_faulty,
        "faulty_samples": faulty_samples,
        "n_samples": len(run["time"]),
    }


def load_scored_runs() -> dict[str, dict]:
    out = {}
    for path in sorted(SCORED_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        run_id = (data.get("batch") or {}).get("run_id") or path.stem
        data["_file"] = path.name
        out[run_id] = data
    return out


def reference_runs() -> set[str]:
    """The runs the detector was fitted and calibrated on.

    Scoring a detector on the data it was fitted on measures nothing, so these
    are held out of every rate below and reported separately.
    """
    if not MANIFEST_PATH.exists():
        return set()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return set(manifest.get("fit") or []) | set(manifest.get("calibration") or [])


def _fingerprint_of(data: dict) -> tuple:
    detector = data.get("detector") or {}
    limits = detector.get("limits") or {}
    baseline = next(
        (e.get("value") for e in (data.get("evidence") or [])
         if isinstance(e, dict) and e.get("kind") == "reference_baseline"),
        {},
    ) or {}
    return (
        (detector.get("parameters") or {}).get("n_components"),
        round(float(limits.get("t2") or 0), 2),
        round(float(limits.get("spe") or 0), 2),
        baseline.get("n_samples"),
    )


def fingerprint_groups(scored: dict[str, dict]) -> dict:
    """Which runs were scored against which baseline.

    Two people comparing notes need this before anything else: a run scored
    against a different baseline is not comparable to one scored against yours,
    and the usual cause is a stale file left over from an earlier pipeline run
    rather than anything either person did wrong. Whatever the majority of runs
    agree on is treated as the current baseline; the rest are named and held
    out, because a file that disagrees is either old or broken and either way
    it is not evidence.
    """
    groups: dict[tuple, list[str]] = defaultdict(list)
    for run_id, data in scored.items():
        groups[_fingerprint_of(data)].append(run_id)

    ordered = sorted(groups.items(), key=lambda kv: -len(kv[1]))
    current = ordered[0][0] if ordered else None
    return {
        "current": {
            "n_components": current[0] if current else None,
            "limit_t2": current[1] if current else None,
            "limit_spe": current[2] if current else None,
            "baseline_samples": current[3] if current else None,
            "runs": len(ordered[0][1]) if ordered else 0,
        },
        "disagreeing": [
            {
                "n_components": fp[0], "limit_t2": fp[1], "limit_spe": fp[2],
                "baseline_samples": fp[3], "runs": len(run_ids),
                "run_ids": sorted(run_ids)[:20],
                "files": sorted(scored[r].get("_file", r) for r in run_ids)[:20],
            }
            for fp, run_ids in ordered[1:]
        ],
        "all_runs_agree": len(ordered) <= 1,
        "excluded_run_ids": sorted(r for fp, ids in ordered[1:] for r in ids),
    }


# --------------------------------------------------------------------------
# 1-3: did it fire, when, and on what
# --------------------------------------------------------------------------

def compare_runs(truth: dict[str, dict], scored: dict[str, dict],
                 excluded: set[str] | None = None) -> dict:
    excluded = excluded or set()
    rows = []
    for run_id in sorted(set(truth) & set(scored)):
        if run_id in excluded:
            continue
        fact = summarise_truth(truth[run_id])
        events = scored[run_id].get("events") or []
        detections = sorted(e.get("detected_at_sample") or e.get("start_sample") or 0 for e in events)
        first_detection = detections[0] if detections else None

        # NOT a detection delay, and it must not be called one. These labels
        # mark the whole run as carrying fault N; they do not record the sample
        # the fault was introduced at. All that can honestly be measured is how
        # far into the run the first detection landed.
        position = first_detection if fact["is_faulty"] and first_detection else None

        blamed = Counter()
        for event in events:
            for rank, signal in enumerate((event.get("ranked_signals") or [])[:3]):
                if signal.get("col_id"):
                    blamed[signal["col_id"]] += 3 - rank

        rows.append({
            "run_id": run_id,
            "fault_number": fact["fault_number"],
            "is_faulty": fact["is_faulty"],
            "first_faulty_sample": fact["first_faulty_sample"],
            "faulty_samples": fact["faulty_samples"],
            "n_events": len(events),
            "first_detection_sample": first_detection,
            "detected": bool(events),
            "first_detection_position": position,
            "run_fraction_at_detection": (round(position / fact["n_samples"], 3)
                                          if position and fact["n_samples"] else None),
            "top_blamed": [c for c, _ in blamed.most_common(3)],
            "outcome": (
                "hit" if fact["is_faulty"] and events else
                "miss" if fact["is_faulty"] else
                "false_alarm" if events else
                "correct_silence"
            ),
        })

    outcomes = Counter(r["outcome"] for r in rows)
    faulty = [r for r in rows if r["is_faulty"]]
    clean = [r for r in rows if not r["is_faulty"]]
    positions = [r["first_detection_position"] for r in rows
                 if r["first_detection_position"] is not None]

    return {
        "runs_compared": len(rows),
        "runs_with_a_fault": len(faulty),
        "runs_without_a_fault": len(clean),
        "outcomes": dict(outcomes),
        "detection_rate": round(outcomes["hit"] / len(faulty), 4) if faulty else None,
        "false_alarm_rate": round(outcomes["false_alarm"] / len(clean), 4) if clean else None,
        "median_first_detection_position": statistics.median(positions) if positions else None,
        "first_detection_position_range": [min(positions), max(positions)] if positions else None,
        "position_caveat": ("Position is samples from the start of the run, not a "
                            "detection delay: these labels mark the run, not the sample "
                            "the fault began at."),
        "per_run": rows,
    }


def blame_by_fault(rows: list[dict]) -> list[dict]:
    """Does the same fault number make the detector blame the same channels?

    A detector that blames a consistent, small set of channels for a given fault
    is learning something. One that blames a different set every time is not,
    whatever its detection rate says.
    """
    grouped = defaultdict(Counter)
    runs_per_fault = Counter()
    for row in rows:
        if not row["is_faulty"] or not row["detected"]:
            continue
        runs_per_fault[row["fault_number"]] += 1
        for col in row["top_blamed"]:
            grouped[row["fault_number"]][col] += 1

    out = []
    for fault_number, counter in sorted(grouped.items()):
        n_runs = runs_per_fault[fault_number]
        top = counter.most_common(4)
        out.append({
            "fault_number": fault_number,
            "runs": n_runs,
            "channels_blamed": [
                {"col_id": c, "in_runs": n, "consistency": round(n / n_runs, 3)} for c, n in top
            ],
            "distinct_channels_blamed": len(counter),
        })
    return out


# --------------------------------------------------------------------------
# 4: did the semantic stage recover the structure that is really there
# --------------------------------------------------------------------------

def structural_families() -> dict:
    """Group the original column names by their own alphabetic prefix.

    This script is not told what the families mean and does not care. It reads
    the names S1 recorded, strips the trailing index off each, and counts what
    is left. Whatever families exist in the source naming fall out on their own.
    """
    if not SCHEMA_PATH.exists():
        return {"families": {}, "note": "artifacts/schema.json is missing"}

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    families = defaultdict(list)
    for col_id, meta in (schema.get("columns") or {}).items():
        if col_id == (schema.get("dataset_metadata") or {}).get("time_column_id", "col_time"):
            continue
        original = str((meta or {}).get("original_name") or "")
        prefix = re.sub(r"[\W_]*\d+$", "", original).strip("_- ") or "unnamed"
        families[prefix].append(col_id)

    return {
        "families": {k: sorted(v) for k, v in sorted(families.items(), key=lambda kv: -len(kv[1]))},
        "sizes": {k: len(v) for k, v in sorted(families.items(), key=lambda kv: -len(kv[1]))},
    }


def semantic_agreement(families: dict) -> dict:
    """Did S4 put the two families in different classes, or in the same one?"""
    if not SEMANTICS_PATH.exists():
        return {"note": "artifacts/semantics.json is missing"}

    semantics = json.loads(SEMANTICS_PATH.read_text(encoding="utf-8"))
    if not isinstance(semantics, dict):
        return {"note": "unexpected semantics shape"}

    per_family = {}
    for family, cols in (families.get("families") or {}).items():
        classes = Counter()
        for col_id in cols:
            node = semantics.get(col_id) or {}
            classes[node.get("structural_class") or "unclassified"] += 1
        per_family[family] = dict(classes)

    all_classes = Counter()
    for counts in per_family.values():
        all_classes.update(counts)

    # A single class covering every column means the split was not recovered at
    # all, whatever the confidence attached to each individual answer.
    separated = len(all_classes) > 1 and all(
        len(counts) == 1 for counts in per_family.values()
    )
    return {
        "classes_per_family": per_family,
        "distinct_classes_overall": len(all_classes),
        "class_totals": dict(all_classes),
        "families_cleanly_separated": separated,
        "verdict": (
            "S4 assigned one class to every column: the structural split present in the "
            "source naming was not recovered." if len(all_classes) == 1 else
            "S4 separated the families cleanly." if separated else
            "S4 used more than one class, but not along the family boundary."
        ),
    }


def detector_fingerprint(scored: dict[str, dict]) -> dict:
    """Six numbers that say whether two people are looking at the same detector.

    Runs scored against different baselines are not comparable, and the usual
    way that happens is two machines having ingested different slices of the
    dataset. Printing the fingerprint makes that a five-second comparison.
    """
    for data in scored.values():
        detector = data.get("detector") or {}
        baseline = next(
            (e.get("value") for e in (data.get("evidence") or [])
             if isinstance(e, dict) and e.get("kind") == "reference_baseline"),
            {},
        ) or {}
        return {
            "family": detector.get("family"),
            "n_components": (detector.get("parameters") or {}).get("n_components"),
            "limit_t2": (detector.get("limits") or {}).get("t2"),
            "limit_spe": (detector.get("limits") or {}).get("spe"),
            "baseline_samples": baseline.get("n_samples"),
            "baseline_columns": baseline.get("n_columns"),
            "variance_explained": baseline.get("variance_explained"),
            "runs_scored": len(scored),
        }
    return {}


def _finite(node):
    """Replace NaN and infinity with null, at any depth. See main()."""
    import math
    if isinstance(node, float):
        return node if math.isfinite(node) else None
    if isinstance(node, dict):
        return {k: _finite(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_finite(v) for v in node]
    return node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(OUT_PATH))
    args = parser.parse_args()

    if not LABELS_DIR.exists():
        print(f"{LABELS_DIR.relative_to(ROOT)} does not exist. Run S1 first.")
        return 1
    scored = load_scored_runs()
    if not scored:
        print(f"No scored runs in {SCORED_DIR.relative_to(ROOT)}. Run S6 first.")
        return 1

    truth = load_label_runs()
    fingerprints = fingerprint_groups(scored)
    reference = reference_runs()
    excluded = set(fingerprints["excluded_run_ids"]) | reference

    comparison = compare_runs(truth, scored, excluded=excluded)
    families = structural_families()

    trust = {}
    if DQ_PATH.exists():
        try:
            dq = json.loads(DQ_PATH.read_text(encoding="utf-8"))
            trust = {
                "verdict": dq.get("trust_verdict"),
                "checks_run": dq.get("checks_run_count"),
                "checks_failed": dq.get("checks_failed_count"),
                "batch_id": dq.get("batch_id"),
            }
        except json.JSONDecodeError:
            pass

    report = {
        "generated_at": _now(),
        "eval_only": True,
        "note": ("Produced from data/labels/ by eval/validate_detection.py. Nothing in "
                 "pipeline/ reads this file, and nothing in it is ever sent to a model."),
        "detector": detector_fingerprint(scored),
        "baselines": fingerprints,
        "held_out": {
            "reference_runs": sorted(reference),
            "reference_note": ("Fitted and calibrated on. Scoring the detector on these "
                               "would measure nothing."),
            "stale_or_disagreeing_runs": fingerprints["excluded_run_ids"],
            "stale_note": ("Scored against a different baseline than the majority of runs "
                           "-- almost always files left behind by an earlier pipeline run. "
                           "Delete them and re-run S6 to clear this."),
            "total_excluded": len(excluded),
        },
        "detection": {k: v for k, v in comparison.items() if k != "per_run"},
        "per_run": comparison["per_run"],
        "blame_consistency": blame_by_fault(comparison["per_run"]),
        "structure": {**families, "semantic_agreement": semantic_agreement(families)},
        "data_quality": trust,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # allow_nan=False would raise; what is wanted is null. Python happily writes
    # a bare NaN into JSON and every JSON.parse on earth rejects it, so a browser
    # reading this report gets an empty object and renders "undefined" everywhere
    # with no error to explain it. Found exactly that way.
    out_path.write_text(json.dumps(_finite(report), indent=2) + "\n", encoding="utf-8")

    det = report["detection"]
    print(f"wrote {out_path.relative_to(ROOT)}")
    print(f"  runs compared          {det['runs_compared']}  "
          f"({det['runs_with_a_fault']} with a fault, {det['runs_without_a_fault']} without)")
    print(f"  detection rate         {det['detection_rate']}")
    print(f"  false alarm rate       {det['false_alarm_rate']}")
    print(f"  median position        {det['median_first_detection_position']} samples into the run")
    print(f"  held out               {len(excluded)} runs "
          f"({len(reference)} reference, {len(fingerprints['excluded_run_ids'])} stale)")
    if not fingerprints["all_runs_agree"]:
        print(f"  BASELINES DISAGREE     {len(fingerprints['disagreeing'])} other fingerprint(s) "
              f"in artifacts/drift_events/")
    print(f"  structure              {report['structure']['semantic_agreement'].get('verdict')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
