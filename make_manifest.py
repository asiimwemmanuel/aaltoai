"""Choose the runs that define "normal", and say out loud what that choice assumes.

S6 fits its PCA baseline on the runs named here. Everything the detector does
afterwards is relative to them, so this file decides the operating point.

It got that wrong. An earlier version took `sim{N}_run00` from fifteen
different partitions. That was replaced with `sorted(discover_run_ids())[:15]`
to fix a real bug in how runs were counted -- and the replacement quietly
collapsed the selection onto the first fifteen sub-runs of a *single*
partition. A partition holds one run per process condition back to back, so
"the first fifteen" are fifteen different conditions, not fifteen normal ones.
The detector was taught that fourteen of the conditions it is meant to catch
are normal, and stopped reporting them: detection fell from 83% to 15%, with
whole condition classes sitting at exactly 0%.

So: one candidate per partition, which is what the original did, plus the two
things it was missing -- the assumption written down instead of implied, and a
check that the candidates actually agree with each other before they are
believed.

No label is read here. Candidates are chosen by position and filtered by
statistics; `contracts/reference_manifest.json` records both.
"""
import json
import os
import sys

import numpy as np

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from pipeline.s6_drift import iter_partitions, partition_dirs

FEATURES = "data/features"
OUT = "contracts/reference_manifest.json"
N_FIT, N_CAL = 10, 5
# Look at more partitions than we need, so rejecting a candidate costs nothing.
N_CANDIDATES = 25
# Modified z-score. A candidate whose typical column sits this far from what
# the other candidates agree on is not describing the same normal.
MAX_Z = 3.5

ASSUMPTION = ("The first run of each batch is a reference period. This is an "
              "operator convention about how batches are recorded, not "
              "something inferred from the data, and it is the one assumption "
              "the baseline rests on. If batches are not recorded that way, "
              "name the reference runs explicitly instead.")


def candidate_profiles():
    """Per-column mean and spread of the first run of each partition."""
    wanted = {sim for sim, _ in partition_dirs(FEATURES)[:N_CANDIDATES]}
    ids, profiles = [], []
    for _, part in iter_partitions(FEATURES, wanted):
        first = sorted(part)[0]
        x = part[first]["x"]
        profiles.append(np.concatenate([x.mean(axis=0), x.std(axis=0)]))
        ids.append(first)
    return ids, np.array(profiles)


def disagreement(profiles):
    """How far each candidate sits from what the others agree on.

    Median and MAD across candidates, per feature, so a single odd run cannot
    drag the reference it is being compared against. A run's score is its
    typical (median) deviation, not its worst: one unusual column is a column,
    a shifted process is most of them.
    """
    med = np.median(profiles, axis=0)
    mad = np.median(np.abs(profiles - med), axis=0)
    scale = np.where(mad > 0, mad * 1.4826, np.inf)
    return np.median(np.abs(profiles - med) / scale, axis=1)


def main():
    ids, profiles = candidate_profiles()
    if not ids:
        raise SystemExit(f"no partitions found under {FEATURES}/. Run S1 first.")

    if len(ids) < N_FIT + N_CAL:
        # Dev mode ingests a handful of partitions. Split what there is rather
        # than leaving calibration empty, which crashes calibrate_limits().
        n_fit = max(1, round(len(ids) * 0.7))
        fit, cal, rejected = ids[:n_fit], ids[n_fit:] or ids[:n_fit], []
        note = f"dev-sized input: {len(ids)} candidate(s), no outlier filtering"
    else:
        score = disagreement(profiles)
        keep = [i for i in np.argsort(score) if score[i] <= MAX_Z]
        rejected = [{"run_id": ids[i], "disagreement": round(float(score[i]), 2)}
                    for i in range(len(ids)) if i not in set(keep)]
        if len(keep) < N_FIT + N_CAL:
            raise SystemExit(
                f"only {len(keep)} of {len(ids)} candidate reference runs agree "
                f"with each other; {N_FIT + N_CAL} are needed. The batches do "
                f"not share a common normal, so no baseline fitted from them "
                f"would mean anything. Name the reference runs by hand in "
                f"{OUT} instead.")
        chosen = keep[:N_FIT + N_CAL]
        fit = sorted(ids[i] for i in chosen[:N_FIT])
        cal = sorted(ids[i] for i in chosen[N_FIT:])
        note = (f"{len(ids)} candidates, {len(rejected)} rejected above a "
                f"modified z of {MAX_Z}")

    manifest = {
        "fit": fit,
        "calibration": cal,
        "selection": {
            "rule": "the first run of each batch, one per batch",
            "assumption": ASSUMPTION,
            "epistemic_status": "assumed",
            "note": note,
            "rejected": rejected,
            "uses_labels": False,
        },
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"Reference: {len(fit)} fit + {len(cal)} calibration runs, "
          f"one per batch. {note}.")
    for r in rejected:
        print(f"  rejected {r['run_id']}: disagreement {r['disagreement']}")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
