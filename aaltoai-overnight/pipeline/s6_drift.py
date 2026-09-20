"""S6 Drift and fault detection (PCA with T2 and SPE, persistence rule, per-signal attribution).

Reads:  data/features/simulationRun=*/*.parquet, contracts/reference_manifest.json
Writes: contracts/drift_events.json (one run, --run-id) or artifacts/drift_events/<run_id>.json (all runs)

Run from the Hackathon/ folder:  python scripts/s6_drift.py
"""
import argparse
import glob
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import polars as pl

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


def load_runs(features_dir):
    """Rebuild each run from the Parquet files. A run starts when col_time is 1."""
    runs = {}
    col_ids = None
    for sim_dir in sorted(glob.glob(os.path.join(features_dir, "simulationRun=*"))):
        sim_match = re.search(r"simulationRun=([\d.]+)", sim_dir)
        if not sim_match: continue
        sim = int(float(sim_match.group(1)))
        # NOT sorted by TIME_COL: col_time resets to 1 at each sub-run boundary
        # within a simulationRun partition, so a global sort by col_time
        # interleaves all sub-runs together and destroys the very boundaries
        # this function looks for below. The Parquet file's natural (ingestion)
        # row order already keeps each sub-run's samples contiguous.
        df = pl.read_parquet(os.path.join(sim_dir, "*.parquet"))
        cols = [c for c in df.columns if c not in (TIME_COL, "simulationRun")]
        if col_ids is None:
            col_ids = cols
        elif cols != col_ids:
            raise ValueError(f"{sim_dir}: columns differ from the other partitions")
            
        t = df[TIME_COL].to_numpy()
        x = df.select(cols).to_numpy()
        
        cuts = np.where(t == 1)[0]
        if len(cuts) == 0:
            continue
            
        pieces = list(zip(np.split(t, cuts[1:]), np.split(x, cuts[1:])))
        for k, (t_piece, x_piece) in enumerate(pieces):
            runs[f"sim{sim}_run{k:02d}"] = {
                "t": t_piece,
                "x": x_piece,
            }
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
             "variance_explained": round(model.variance_explained, 4)},
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
    args = ap.parse_args()

    params = dict(DEFAULTS, persistence=args.persistence, limit_percentile=args.percentile)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    gate = read_dq_gate(args.dq_report)
    if gate and gate["verdict"] == "UNTRUSTED":
        raise SystemExit(f"DATA ALARM: S5 verdict is UNTRUSTED ({gate['source_batch_id']}). "
                         "S6 does not reason about the process on broken data.")

    runs, col_ids = load_runs(args.features)
    with open(args.manifest) as f:
        manifest = json.load(f)
    fit_ids, cal_ids = manifest["fit"], manifest["calibration"]

    model = fit_reference(np.concatenate([runs[r]["x"] for r in fit_ids]), col_ids, params["variance_target"])
    limits = calibrate_limits(model, [runs[r]["x"] for r in cal_ids], params["limit_percentile"])
    print(f"Reference: {model.n_samples} samples, {len(model.col_ids)} columns, "
          f"{model.components.shape[1]} components ({model.variance_explained:.1%} variance)")
    print(f"Limits (p{params['limit_percentile']} on {limits['n_samples']} held-out normal samples): "
          f"T2={limits['t2']:.2f} SPE={limits['spe']:.2f}")

    if args.run_id:
        result = detect_run(args.run_id, runs[args.run_id], model, limits, params, now, gate)
        write_json(args.out, result)
        print(f"{args.run_id}: {len(result['events'])} event(s) -> {args.out}")
        return

    scored = [r for r in runs if r not in set(fit_ids) | set(cal_ids)]
    total = 0
    for run_id in scored:
        result = detect_run(run_id, runs[run_id], model, limits, params, now, gate)
        write_json(os.path.join(args.out_dir, f"{run_id}.json"), result)
        total += len(result["events"])
    print(f"Scored {len(scored)} runs, {total} events -> {args.out_dir}/")


if __name__ == "__main__":
    main()
