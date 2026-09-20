"""S6 Drift and fault detection (PCA with T2 and SPE, persistence rule, per-signal attribution).

Reads:  data/features/simulationRun=*/*.parquet, contracts/reference_manifest.json
Writes: contracts/drift_events.json (one run, --run-id) or artifacts/drift_events/<run_id>.json (all runs)

Run from the Hackathon/ folder:  python scripts/s6_drift.py
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import polars as pl

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.lib.run_boundaries import rebuild_runs

TIME_COL = "col_time"


def matmul(a, b):
    # einsum avoids spurious floating-point warnings from the macOS BLAS in NumPy 2.0
    return np.einsum("ij,jk->ik", a, b)

STAGE = "S6_drift"

DEFAULTS = {
    "variance_target": 0.90,
    "limit_percentile": 99.0,
    "persistence": 5,
    "release": 10,
    "attribution_window": 30,
    "top_signals": 5,
    "shift_threshold": 1.0,
    "variability_high": 1.5,
    "variability_low": 0.5,
    "ramp_lookback": 20,
    "ramp_ratio": 0.8,
    "ramp_min_samples": 6,
    "severity_high_ratio": 10.0,
    "severity_medium_ratio": 3.0,
}


def partition_dirs(features_dir, sims=None):
    """The partition directories, in a stable order, optionally only some sims."""
    out = []
    for sim_dir in sorted(glob.glob(os.path.join(features_dir, "simulationRun=*"))):
        sim_match = re.search(r"simulationRun=([\d.]+)", sim_dir)
        if not sim_match:
            continue
        sim = int(float(sim_match.group(1)))
        if sims is not None and sim not in sims:
            continue
        out.append((sim, sim_dir))
    return out


def sim_of(run_id):
    """sim100_run07 -> 100. The partition a run id came out of."""
    m = re.match(r"sim(\d+)_run\d+$", run_id)
    if not m:
        raise SystemExit(f"{run_id}: not a run id this stage produces (sim<N>_run<KK>).")
    return int(m.group(1))


def discover_run_ids(features_dir):
    """Every run id, reading only the time column.

    The full dataset is 15.3 M rows across 53 columns. Materialising all of it
    to answer "which runs exist" costs about 6 GB of resident memory and pushes
    a 16 GB machine into swap -- which is what made this stage and
    make_manifest.py look hung rather than slow. The boundaries live in
    col_time alone, so read that one column.
    """
    ids = []
    for sim, sim_dir in partition_dirs(features_dir):
        t = pl.read_parquet(os.path.join(sim_dir, "*.parquet"),
                            columns=[TIME_COL])[TIME_COL].to_numpy()
        n = len(rebuild_runs(t, sim_dir))
        ids.extend(f"sim{sim}_run{k:02d}" for k in range(n))
    return ids


def iter_partitions(features_dir, sims=None, col_ids=None):
    """Yield (col_ids, runs) one partition at a time.

    Holding all 500 partitions at once is 6.4 GB of float64 for no reason:
    scoring is per run and the reference baseline needs fifteen of them. The
    caller keeps only what it is using, so peak memory is one partition.
    """
    for sim, sim_dir in partition_dirs(features_dir, sims):
        # NOT sorted by TIME_COL: col_time resets to 1 at each sub-run boundary
        # within a simulationRun partition, so a global sort by col_time
        # interleaves all sub-runs together and destroys the very boundaries
        # this function looks for below. The Parquet file's natural (ingestion)
        # row order already keeps each sub-run's samples contiguous.
        # NOT sorted by TIME_COL: col_time resets to 1 at each sub-run
        # boundary, so a global sort interleaves every sub-run together.
        # rebuild_runs() below recovers the boundaries from col_time without
        # sorting and without trusting the Parquet row order.
        df = pl.read_parquet(os.path.join(sim_dir, "*.parquet"))
        cols = [c for c in df.columns if c not in (TIME_COL, "simulationRun")]
        if col_ids is None:
            col_ids = cols
        elif cols != col_ids:
            raise ValueError(f"{sim_dir}: columns differ from the other partitions")

        t = df[TIME_COL].to_numpy()
        x = df.select(cols).to_numpy()
        del df

        runs = {}
        for k, idx in enumerate(rebuild_runs(t, sim_dir)):
            runs[f"sim{sim}_run{k:02d}"] = {"t": t[idx], "x": x[idx]}
        yield col_ids, runs


def load_runs(features_dir, sims=None):
    """Every run, in memory at once. Only for the reference set -- see iter_partitions."""
    runs, col_ids = {}, None
    for col_ids, part in iter_partitions(features_dir, sims, col_ids):
        runs.update(part)
    return runs, col_ids


@dataclass
class ReferenceModel:
    col_ids: list
    mean: np.ndarray
    std: np.ndarray
    keep: np.ndarray
    components: np.ndarray
    eigenvalues: np.ndarray
    variance_explained: float
    n_samples: int
    # Set by main(). Every artifact can then say which baseline produced it,
    # which is what makes two runs comparable.
    fingerprint: str = ""
    fitted_at: str = ""

    def score(self, x):
        z = (x[:, self.keep] - self.mean) / self.std
        scores = matmul(z, self.components)
        residual = z - matmul(scores, self.components.T)
        t2 = ((scores ** 2) / self.eigenvalues).sum(axis=1)
        spe = (residual ** 2).sum(axis=1)
        return z, scores, residual, t2, spe


def fit_reference(x, col_ids, variance_target):
    mean = x.mean(axis=0)
    std = x.std(axis=0, ddof=1)
    keep = std > 1e-12
    z = (x[:, keep] - mean[keep]) / std[keep]
    _, s, vt = np.linalg.svd(z, full_matrices=False)
    eig = s ** 2 / (len(z) - 1)
    k = int(np.searchsorted(np.cumsum(eig) / eig.sum(), variance_target) + 1)
    return ReferenceModel(
        col_ids=[c for c, kept in zip(col_ids, keep) if kept],
        mean=mean[keep],
        std=std[keep],
        keep=keep,
        components=vt[:k].T,
        eigenvalues=eig[:k],
        variance_explained=float(eig[:k].sum() / eig.sum()),
        n_samples=len(z),
    )


def calibrate_limits(model, x_runs, percentile):
    t2s, spes = [], []
    for x in x_runs:
        _, _, _, t2, spe = model.score(x)
        t2s.append(t2)
        spes.append(spe)
    t2 = np.concatenate(t2s)
    spe = np.concatenate(spes)
    return {
        "t2": float(np.percentile(t2, percentile)),
        "spe": float(np.percentile(spe, percentile)),
        "n_samples": int(len(t2)),
    }


def find_alarm_spans(alarm, persistence, release):
    """Return (start, detected, end) index triples. end is None while the alarm is still on."""
    n = len(alarm)
    spans = []
    i = 0
    while i <= n - persistence:
        if not alarm[i:i + persistence].all():
            i += 1
            continue
        start = i
        detected = i + persistence - 1
        last_alarm = detected
        quiet = 0
        j = detected + 1
        ended = False
        while j < n:
            if alarm[j]:
                quiet = 0
                last_alarm = j
            else:
                quiet += 1
                if quiet >= release:
                    ended = True
                    break
            j += 1
        spans.append((start, detected, last_alarm if ended else None))
        if not ended:
            break
        i = j + 1
    return spans


def attribute(model, z, scores, residual, lo, hi, statistic, params):
    zw = z[lo:hi]
    if statistic == "t2":
        contrib = np.clip(zw * matmul(scores[lo:hi] / model.eigenvalues, model.components.T), 0, None)
    else:
        contrib = residual[lo:hi] ** 2
    contrib = contrib.mean(axis=0)
    total = contrib.sum()
    share = contrib / total if total > 0 else np.zeros_like(contrib)
    zmean = zw.mean(axis=0)
    zstd = zw.std(axis=0)
    ranked = []
    for j in np.argsort(-share)[:params["top_signals"]]:
        if abs(zmean[j]) >= params["shift_threshold"]:
            direction = "above_normal" if zmean[j] > 0 else "below_normal"
        elif zstd[j] >= params["variability_high"]:
            direction = "more_variable"
        elif zstd[j] <= params["variability_low"]:
            direction = "less_variable"
        else:
            direction = "unspecified"
        ranked.append({"col_id": model.col_ids[j], "share": round(float(share[j]), 4), "direction": direction})
    return ranked


def classify_onset(ratio, start, params):
    if start < 5:
        return "unclassified"
    lo = max(0, start - params["ramp_lookback"])
    ramp = 0
    for value in ratio[lo:start][::-1]:
        if value > params["ramp_ratio"]:
            ramp += 1
        else:
            break
    return "gradual" if ramp >= params["ramp_min_samples"] else "abrupt"


def severity_level(peak_ratio, params):
    if peak_ratio >= params["severity_high_ratio"]:
        return "high"
    if peak_ratio >= params["severity_medium_ratio"]:
        return "medium"
    return "low"


def evidence_item(evidence_id, kind, subject, value, method, now):
    return {
        "evidence_id": evidence_id,
        "stage": STAGE,
        "kind": kind,
        "subject": subject,
        "value": value,
        "computed_at": now,
        "method": method,
    }


def read_dq_gate(path):
    if path is None:
        return None
    with open(path) as f:
        report = json.load(f)
    return {
        "verdict": report["trust_verdict"],
        "source_batch_id": report.get("batch_id"),
        "failure_evidence_ids": [f["evidence_id"] for f in report.get("failures", [])],
    }


def detect_run(run_id, run, model, limits, params, now, gate=None):
    t = run["t"]
    z, scores, residual, t2, spe = model.score(run["x"])
    ratio_t2 = t2 / limits["t2"]
    ratio_spe = spe / limits["spe"]
    ratio = np.maximum(ratio_t2, ratio_spe)
    spans = find_alarm_spans(ratio > 1.0, params["persistence"], params["release"])

    evidence = [
        evidence_item(
            "ev_s6_baseline", "reference_baseline", [],
            {"n_samples": model.n_samples, "n_columns": len(model.col_ids),
             "n_components": int(model.components.shape[1]),
             "variance_explained": round(model.variance_explained, 4),
             "fingerprint": model.fingerprint, "fitted_at": model.fitted_at},
            "pca_on_normal_reference_runs", now),
        evidence_item(
            "ev_s6_limits", "control_limits", [],
            {"t2": round(limits["t2"], 4), "spe": round(limits["spe"], 4),
             "percentile": params["limit_percentile"], "n_samples": limits["n_samples"]},
            "percentile_on_held_out_normal_runs", now),
    ]
    gate_ids = []
    if gate:
        gate_ids = ["ev_s6_dq_gate"]
        evidence.append(evidence_item(
            "ev_s6_dq_gate", "data_trust_gate", [],
            {"verdict": gate["verdict"], "source_batch_id": gate["source_batch_id"],
             "failure_evidence_ids": gate["failure_evidence_ids"]},
            "read_from_s5_dq_report", now))
    events = []
    for idx, (start, detected, end) in enumerate(spans, start=1):
        stop = (end if end is not None else len(t) - 1) + 1
        excess_t2 = np.clip(ratio_t2[start:stop] - 1, 0, None).sum()
        excess_spe = np.clip(ratio_spe[start:stop] - 1, 0, None).sum()
        statistic = "t2" if excess_t2 >= excess_spe else "spe"
        peak = float(ratio[start:stop].max())
        window_hi = min(start + params["attribution_window"], stop)
        ranked = attribute(model, z, scores, residual, start, window_hi, statistic, params)
        event_id = f"{run_id}_ev{idx:02d}"
        top = [r["col_id"] for r in ranked]
        score_id = f"ev_s6_{event_id}_score"
        attr_id = f"ev_s6_{event_id}_attribution"
        evidence.append(evidence_item(
            score_id, "score_exceedance", top,
            {"statistic": statistic, "peak_ratio_to_limit": round(peak, 3),
             "persistence_samples": params["persistence"],
             "start_sample": int(t[start]), "detected_at_sample": int(t[detected])},
            "persistent_limit_exceedance", now))
        evidence.append(evidence_item(
            attr_id, "signal_contribution", top,
            {"shares": {r["col_id"]: r["share"] for r in ranked},
             "window_samples": int(window_hi - start)},
            f"{statistic}_variable_contribution", now))
        events.append({
            "event_id": event_id,
            "start_sample": int(t[start]),
            "detected_at_sample": int(t[detected]),
            "end_sample": int(t[end]) if end is not None else None,
            "severity": severity_level(peak, params),
            "type": classify_onset(ratio, start, params),
            "statistic": statistic,
            "ranked_signals": ranked,
            "evidence_ids": ["ev_s6_baseline", "ev_s6_limits", *gate_ids, score_id, attr_id],
        })

    return {
        "batch": {"batch_id": run_id, "run_id": run_id, "n_samples": int(len(t)),
                  "first_sample": int(t[0]), "last_sample": int(t[-1])},
        "detector": {
            "family": "pca_t2_spe",
            "parameters": {**params, "n_components": int(model.components.shape[1])},
            "limits": {"t2": round(limits["t2"], 4), "spe": round(limits["spe"], 4)},
        },
        "events": events,
        "score_series": {
            "sample": t.tolist(),
            "t2": np.round(t2, 4).tolist(),
            "spe": np.round(spe, 4).tolist(),
            "limit_t2": round(limits["t2"], 4),
            "limit_spe": round(limits["spe"], 4),
        },
        "evidence": evidence,
    }


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)


REFERENCE_MODEL = "contracts/reference_model.json"


def model_fingerprint(fit_ids, cal_ids, col_ids, params):
    """Identity of a baseline: the runs it was fitted on, the columns, the parameters.

    Two runs of this stage are comparable only if this string matches. It is
    written into the saved model and into every drift event.
    """
    payload = json.dumps({
        "fit": list(fit_ids),
        "calibration": list(cal_ids),
        "columns": list(col_ids),
        "variance_target": params["variance_target"],
        "limit_percentile": params["limit_percentile"],
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def save_reference(path, model, limits, fingerprint, now):
    write_json(path, {
        "fingerprint": fingerprint,
        "fitted_at": now,
        "col_ids": model.col_ids,
        "mean": model.mean.tolist(),
        "std": model.std.tolist(),
        "keep": [bool(k) for k in model.keep],
        "components": model.components.tolist(),
        "eigenvalues": model.eigenvalues.tolist(),
        "variance_explained": model.variance_explained,
        "n_samples": model.n_samples,
        "limits": limits,
    })


def load_reference(path):
    with open(path) as f:
        d = json.load(f)
    model = ReferenceModel(
        col_ids=d["col_ids"],
        mean=np.array(d["mean"], dtype=float),
        std=np.array(d["std"], dtype=float),
        keep=np.array(d["keep"], dtype=bool),
        components=np.array(d["components"], dtype=float),
        eigenvalues=np.array(d["eigenvalues"], dtype=float),
        variance_explained=float(d["variance_explained"]),
        n_samples=int(d["n_samples"]),
        fingerprint=d.get("fingerprint", ""),
        fitted_at=d.get("fitted_at", ""),
    )
    return model, d["limits"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--features", default="data/features")
    ap.add_argument("--manifest", default="contracts/reference_manifest.json")
    ap.add_argument("--dq-report", help="S5 dq_report.json; S6 stops when its verdict is UNTRUSTED")
    ap.add_argument("--run-id", help="score one run and write contracts/drift_events.json")
    ap.add_argument("--out", default="contracts/drift_events.json")
    ap.add_argument("--out-dir", default="artifacts/drift_events")
    ap.add_argument("--persistence", type=int, default=DEFAULTS["persistence"])
    ap.add_argument("--percentile", type=float, default=DEFAULTS["limit_percentile"])
    ap.add_argument("--model", default=REFERENCE_MODEL,
                    help="saved PCA baseline; reused unless --refit")
    ap.add_argument("--refit", action="store_true",
                    help="fit the baseline again and overwrite the saved one")
    args = ap.parse_args()

    params = dict(DEFAULTS, persistence=args.persistence, limit_percentile=args.percentile)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    gate = read_dq_gate(args.dq_report)
    if gate and gate["verdict"] == "UNTRUSTED":
        raise SystemExit(f"DATA ALARM: S5 verdict is UNTRUSTED ({gate['source_batch_id']}). "
                         "S6 does not reason about the process on broken data.")

    with open(args.manifest) as f:
        manifest = json.load(f)
    fit_ids, cal_ids = manifest["fit"], manifest["calibration"]
    reference_ids = set(fit_ids) | set(cal_ids)

    # Only the reference partitions are held in memory. Everything else is
    # streamed one partition at a time further down.
    ref_runs, col_ids = load_runs(args.features, {sim_of(r) for r in reference_ids})
    missing = [r for r in fit_ids + cal_ids if r not in ref_runs]
    if missing:
        raise SystemExit(
            f"{args.manifest} names {len(missing)} run(s) that are not in "
            f"{args.features}: {', '.join(missing[:5])}"
            f"{' ...' if len(missing) > 5 else ''}. Re-run make_manifest.py.")

    # What normal means is decided once and then held still. Refitting on every
    # batch lets slow drift walk into the definition of normal, which is the
    # exact failure this challenge is about: the monitor adapts to the fault and
    # stops seeing it. It also makes two runs incomparable, which is how a
    # re-ingest silently changed every limit and the blamed signal overnight.
    fingerprint = model_fingerprint(fit_ids, cal_ids, col_ids, params)

    if os.path.exists(args.model) and not args.refit:
        model, limits = load_reference(args.model)
        if model.fingerprint != fingerprint:
            raise SystemExit(
                f"{args.model} was fitted for a different configuration "
                f"({model.fingerprint or 'unknown'} != {fingerprint}). The "
                f"reference runs, the column set or the limit parameters have "
                f"changed since. Pass --refit if that is intended; the baseline "
                f"must never move without someone deciding that it should.")
        print(f"Reference: loaded {args.model}, fitted {model.fitted_at}, "
              f"{model.n_samples} samples, {len(model.col_ids)} columns, "
              f"{model.components.shape[1]} components "
              f"({model.variance_explained:.1%} variance)")
    else:
        model = fit_reference(np.concatenate([ref_runs[r]["x"] for r in fit_ids]),
                              col_ids, params["variance_target"])
        limits = calibrate_limits(model, [ref_runs[r]["x"] for r in cal_ids],
                                  params["limit_percentile"])
        model.fingerprint, model.fitted_at = fingerprint, now
        save_reference(args.model, model, limits, fingerprint, now)
        print(f"Reference: FITTED and saved to {args.model}. "
              f"{model.n_samples} samples, {len(model.col_ids)} columns, "
              f"{model.components.shape[1]} components "
              f"({model.variance_explained:.1%} variance)")
    print(f"Limits (p{params['limit_percentile']} on {limits['n_samples']} held-out normal samples): "
          f"T2={limits['t2']:.2f} SPE={limits['spe']:.2f}")

    if args.run_id:
        one, _ = load_runs(args.features, {sim_of(args.run_id)})
        if args.run_id not in one:
            raise SystemExit(f"{args.run_id}: no such run in {args.features}.")
        result = detect_run(args.run_id, one[args.run_id], model, limits, params, now, gate)
        write_json(args.out, result)
        print(f"{args.run_id}: {len(result['events'])} event(s) -> {args.out}")
        return

    del ref_runs

    # Everything already in out_dir was scored against whatever baseline was
    # current at the time. Leaving it there mixes two operating points in one
    # directory and eval reports "BASELINES DISAGREE" -- or worse, does not,
    # because the stale files happen to be from runs this pass no longer
    # scores. A scoring pass owns its output directory.
    stale = glob.glob(os.path.join(args.out_dir, "*.json"))
    for path in stale:
        os.unlink(path)
    if stale:
        print(f"Cleared {len(stale)} run(s) scored against an earlier baseline.")

    n_scored = total = 0
    for _, part in iter_partitions(args.features, col_ids=col_ids):
        for run_id, run in part.items():
            if run_id in reference_ids:
                continue
            result = detect_run(run_id, run, model, limits, params, now, gate)
            write_json(os.path.join(args.out_dir, f"{run_id}.json"), result)
            total += len(result["events"])
            n_scored += 1
    print(f"Scored {n_scored} runs, {total} events -> {args.out_dir}/")


if __name__ == "__main__":
    main()
