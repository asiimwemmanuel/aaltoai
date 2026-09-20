"""Raw series reader for the operator charts.

This module is the data half of the split the challenge's gate condition
demands, and it is deliberately a separate file so the split is visible in the
directory listing rather than asserted in a comment:

    ui/series_reader.py   reads data/features/**  ->  the operator's browser
    trust/gateway.py      reads derived summaries ->  a language model

Nothing here imports trust.gateway, and nothing here is ever passed to it. The
arrays this module returns go to an SVG renderer over localhost and stop there.
When a model needs to know something about a channel it gets `window_stats()`
instead -- eight numbers, no series -- and that is the only function in this
file whose output is allowed anywhere near a payload.

Run splitting matches pipeline/s6_drift.py exactly (a run starts where col_time
returns to 1, numbered in file order), because the chart has to line up with
the drift scores S6 wrote for the same run. It is reimplemented rather than
imported: stages talk through disk, and ui/ is not allowed to reach into one.
"""

from __future__ import annotations

import glob
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

ROOT = Path(__file__).resolve().parent.parent
FEATURES_DIR = ROOT / "data" / "features"
TIME_COL = "col_time"
PARTITION_COL = "simulationRun"

# Parsed once per process. ~25 MB for the dev dataset; the alternative is
# re-reading 8 MB of Parquet on every pan of a chart.
_CACHE: dict[str, Any] = {}

# Counted, not estimated. "It updates too fast to be real data" is a fair
# instinct and the answer is a number: this is how many values this process has
# handed to the browser, and how many have gone anywhere else (none -- nothing
# in this module can reach the network).
_SERVED = {"values": 0, "requests": 0, "columns": 0}


def served_counters() -> dict[str, Any]:
    return dict(_SERVED)


def source_files() -> list[dict[str, Any]]:
    """The files on disk the charts are drawn from, with their real sizes."""
    out = []
    for path in sorted(FEATURES_DIR.glob("*/*.parquet")):
        stat = path.stat()
        out.append({
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                        .strftime("%Y-%m-%d %H:%M:%S UTC"),
        })
    return out


class SeriesUnavailable(Exception):
    """The Parquet set is missing or does not contain the requested run."""


def _load() -> dict[str, Any]:
    if _CACHE:
        return _CACHE

    if not FEATURES_DIR.exists():
        raise SeriesUnavailable(
            f"{FEATURES_DIR.relative_to(ROOT)} does not exist. Run S1 first: "
            f"python -m pipeline.s1_ingest"
        )

    runs: dict[str, dict[str, Any]] = {}
    col_ids: list[str] | None = None

    for sim_dir in sorted(glob.glob(str(FEATURES_DIR / f"{PARTITION_COL}=*"))):
        match = re.search(rf"{PARTITION_COL}=([\d.]+)", sim_dir)
        if not match:
            continue
        sim = int(float(match.group(1)))

        # Natural row order, never sorted by TIME_COL: col_time resets to 1 at
        # every sub-run boundary, so a global sort interleaves the sub-runs and
        # destroys the boundaries this function is looking for. Same reasoning,
        # same comment, as s6_drift.load_runs -- if one changes so must the other.
        frame = pl.read_parquet(os.path.join(sim_dir, "*.parquet"))
        cols = [c for c in frame.columns if c not in (TIME_COL, PARTITION_COL)]
        if col_ids is None:
            col_ids = cols
        elif cols != col_ids:
            raise SeriesUnavailable(f"{sim_dir}: columns differ from the other partitions")

        t = frame[TIME_COL].to_numpy()
        x = frame.select(cols).to_numpy()
        cuts = np.where(t == 1)[0]
        if len(cuts) == 0:
            continue

        pieces = list(zip(np.split(t, cuts[1:]), np.split(x, cuts[1:])))
        for k, (t_piece, x_piece) in enumerate(pieces):
            runs[f"sim{sim}_run{k:02d}"] = {"t": t_piece, "x": x_piece}

    if not runs:
        raise SeriesUnavailable(f"no runs found under {FEATURES_DIR.relative_to(ROOT)}")

    _CACHE["runs"] = runs
    _CACHE["col_ids"] = col_ids or []
    return _CACHE


def available_runs() -> list[dict[str, Any]]:
    cache = _load()
    return [
        {"run_id": rid, "n_samples": int(len(run["t"]))}
        for rid, run in sorted(cache["runs"].items())
    ]


def column_ids() -> list[str]:
    return list(_load()["col_ids"])


def _run(run_id: str) -> dict[str, Any]:
    cache = _load()
    run = cache["runs"].get(run_id)
    if run is None:
        raise SeriesUnavailable(
            f"unknown run {run_id!r}. Known runs: {', '.join(sorted(cache['runs'])[:6])}..."
        )
    return run


def _decimate(values: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """Thin a series for drawing while keeping every local extreme.

    A plain stride would hide exactly the spike the operator is looking for, so
    each output bucket contributes its min and its max in their original
    positional order. Returns (indices, values).
    """
    n = len(values)
    if n <= max_points:
        return np.arange(n), values

    n_buckets = max(1, max_points // 2)
    bounds = np.linspace(0, n, n_buckets + 1).astype(int)
    keep: list[int] = []
    for lo, hi in zip(bounds[:-1], bounds[1:]):
        if hi <= lo:
            continue
        chunk = values[lo:hi]
        lo_i = lo + int(np.argmin(chunk))
        hi_i = lo + int(np.argmax(chunk))
        keep.extend(sorted({lo_i, hi_i}))
    keep = sorted(set(keep) | {0, n - 1})
    idx = np.array(keep)
    return idx, values[idx]


def series(
    run_id: str,
    col_ids: list[str],
    to_sample: int | None = None,
    from_sample: int = 1,
    max_points: int = 1200,
) -> dict[str, Any]:
    """Return the drawable series for one or more channels of one run.

    `to_sample` is the simulated clock: the UI asks for everything up to the
    current tick and nothing after it, so a replay cannot accidentally show the
    operator a future the detector has not reached yet.
    """
    run = _run(run_id)
    cache = _load()
    all_cols = cache["col_ids"]

    t = run["t"]
    n = len(t)
    hi = n if to_sample is None else max(1, min(int(to_sample), n))
    lo = max(0, int(from_sample) - 1)
    if lo >= hi:
        lo = max(0, hi - 1)

    wanted = [c for c in col_ids if c in all_cols]
    out_series: dict[str, list[float]] = {}
    idx: np.ndarray | None = None
    for col_id in wanted:
        values = run["x"][lo:hi, all_cols.index(col_id)]
        if idx is None:
            # Every channel in one response shares the first channel's sample
            # positions, so the browser draws them against a single x axis
            # without having to re-align anything.
            idx, _ = _decimate(values, max_points)
        out_series[col_id] = [round(float(v), 6) for v in values[idx]]

    samples = [int(v) for v in t[lo:hi][idx]] if idx is not None else []
    _SERVED["requests"] += 1
    _SERVED["columns"] += len(out_series)
    _SERVED["values"] += sum(len(v) for v in out_series.values())
    return {
        "run_id": run_id,
        "from_sample": int(t[lo]) if n else 0,
        "to_sample": int(t[hi - 1]) if n else 0,
        "n_samples_total": n,
        "sample": samples,
        "series": out_series,
        "decimated": bool(idx is not None and len(idx) < (hi - lo)),
    }


def window_stats(run_id: str, col_id: str, start: int, end: int) -> dict[str, Any]:
    """Eight numbers describing one channel over one window.

    The only function here whose result may be handed to trust.gateway. Every
    value is an aggregate: no element of the series survives into the output,
    so what reaches a model describes the data without containing it.
    """
    run = _run(run_id)
    all_cols = _load()["col_ids"]
    if col_id not in all_cols:
        raise SeriesUnavailable(f"unknown channel {col_id!r}")

    n = len(run["t"])
    lo = max(0, int(start) - 1)
    hi = max(lo + 1, min(int(end), n))
    values = run["x"][lo:hi, all_cols.index(col_id)]
    if len(values) == 0:
        raise SeriesUnavailable(f"empty window {start}..{end} for {col_id}")

    half = max(1, len(values) // 2)
    first_half = float(np.mean(values[:half]))
    second_half = float(np.mean(values[-half:]))

    return {
        "col_id": col_id,
        "n": int(len(values)),
        "mean": round(float(np.mean(values)), 6),
        "std_dev": round(float(np.std(values, ddof=1)) if len(values) > 1 else 0.0, 6),
        "min_val": round(float(np.min(values)), 6),
        "max_val": round(float(np.max(values)), 6),
        "slope_per_100": round(_slope(values) * 100.0, 6),
        "half_to_half_shift": round(second_half - first_half, 6),
    }


def _slope(values: np.ndarray) -> float:
    if len(values) < 2:
        return 0.0
    x = np.arange(len(values), dtype=float)
    return float(np.polyfit(x, values.astype(float), 1)[0])


def co_movement(run_id: str, col_id: str, others: list[str],
                start: int = 1, end: int | None = None, max_lag: int = 15) -> list[dict]:
    """Does each of `others` actually move with `col_id`, and does it lead or follow?

    The detector's attribution ranks channels by how much of the residual they
    account for, which is not the same question. A channel can carry 6% of the
    blame and have a correlation of -0.06 with the channel that carried 83% --
    that is the arithmetic spreading blame around, not a second channel doing
    anything. Telling those two cases apart is the difference between five
    suspects and one.

    Returns one aggregate per channel: a correlation, a lag and a verdict. No
    series leaves this function, so the result is safe to put in a payload.
    """
    run = _run(run_id)
    all_cols = _load()["col_ids"]
    if col_id not in all_cols:
        raise SeriesUnavailable(f"unknown channel {col_id!r}")

    n = len(run["t"])
    lo = max(0, int(start) - 1)
    hi = n if end is None else max(lo + 2, min(int(end), n))
    anchor = run["x"][lo:hi, all_cols.index(col_id)].astype(float)
    if len(anchor) < 8:
        return []

    def _z(values: np.ndarray) -> np.ndarray | None:
        spread = values.std()
        return None if spread < 1e-12 else (values - values.mean()) / spread

    za = _z(anchor)
    if za is None:
        return []

    out: list[dict] = []
    for other in others:
        if other == col_id or other not in all_cols:
            continue
        zb = _z(run["x"][lo:hi, all_cols.index(other)].astype(float))
        if zb is None:
            continue

        best_r, best_lag = 0.0, 0
        for lag in range(-max_lag, max_lag + 1):
            if lag >= 0:
                a, b = za[:len(za) - lag or None], zb[lag:]
            else:
                a, b = za[-lag:], zb[:len(zb) + lag]
            if len(a) < 8 or len(a) != len(b):
                continue
            r = float(np.corrcoef(a, b)[0, 1])
            if abs(r) > abs(best_r):
                best_r, best_lag = r, lag

        strength = abs(best_r)
        out.append({
            "col_id": other,
            "correlation": round(best_r, 3),
            "lag_samples": best_lag,
            "leads_or_follows": ("concurrent" if best_lag == 0 else
                                 "follows" if best_lag > 0 else "leads"),
            "verdict": ("moves together" if strength >= 0.5 else
                        "weakly related" if strength >= 0.25 else
                        "unrelated"),
        })
    out.sort(key=lambda d: -abs(d["correlation"]))
    return out
