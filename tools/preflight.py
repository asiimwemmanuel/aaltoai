#!/usr/bin/env python3
"""Everything worth knowing before committing a night to the full dataset.

    python tools/preflight.py              # check only, changes nothing
    python tools/preflight.py --probe      # also make ONE model call, to prove the key works

A full run reads a 6 GB CSV and makes one model call per column. Both of those
fail in ways that are only visible an hour in, so this checks them first: the
file, the space, the dependencies, the key, the rate limit, and the state the
last run left behind. It writes nothing except, with --probe, one line in the
decision log.

Exit code 0 means go. 1 means something will fail. Warnings do not fail.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")

CSV = ROOT / "data" / "te_process.csv"
FEATURES = ROOT / "data" / "features"
SCORED = ROOT / "artifacts" / "drift_events"
DECISION_LOG = ROOT / "artifacts" / "decision_log.jsonl"
CONFIG = ROOT / "config" / "llm.yaml"

_results: list[tuple[str, str, str]] = []


def ok(label: str, detail: str) -> None:
    _results.append(("ok", label, detail))


def warn(label: str, detail: str) -> None:
    _results.append(("warn", label, detail))


def fail(label: str, detail: str) -> None:
    _results.append(("fail", label, detail))


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def minutes(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f} min"


# --------------------------------------------------------------------------

def check_source() -> tuple[float, int]:
    """The CSV, how fast this machine actually parses it, and how big it is.

    Returns (projected parse seconds, estimated rows).

    Timing a raw byte read would be dishonest here: it measures the disk, or
    worse the page cache, and reports four seconds for a file that takes many
    minutes to ingest. What costs time is CSV parsing, so that is what gets
    timed -- a real DuckDB parse of a bounded slice, extrapolated by row count.
    """
    if not CSV.exists():
        fail("source data", f"{CSV.relative_to(ROOT)} does not exist. "
                            f"A full run has nothing to ingest.")
        return 0.0, 0

    target = CSV.resolve()
    size = target.stat().st_size
    where = f"{CSV.relative_to(ROOT)}"
    if CSV.is_symlink():
        where += f" -> {target}"
    ok("source data", f"{where}, {human(size)}")

    # Bytes per row from the head of the file, to turn a file size into a row
    # count without reading the whole thing.
    try:
        with target.open("r", encoding="utf-8", errors="replace") as fh:
            header = fh.readline()
            sample = [fh.readline() for _ in range(500)]
        sample = [line for line in sample if line]
        if not sample:
            warn("source shape", "could not read any data rows")
            return 0.0, 0
        bytes_per_row = sum(len(line.encode("utf-8")) for line in sample) / len(sample)
        est_rows = int((size - len(header.encode("utf-8"))) / bytes_per_row)
        ok("source shape", f"about {est_rows:,} rows at {bytes_per_row:.0f} bytes each, "
                           f"{len(header.split(',')):,} columns")
    except OSError as exc:
        warn("source shape", f"could not read: {exc}")
        return 0.0, 0

    # A real parse, bounded. DuckDB streams, so LIMIT stops it early.
    try:
        import duckdb
        probe_rows = 200_000
        con = duckdb.connect()
        t0 = time.time()
        con.sql(f"SELECT * FROM read_csv_auto('{target}') LIMIT {probe_rows}").fetchall()
        elapsed = max(1e-6, time.time() - t0)
        rate = probe_rows / elapsed
        projected = est_rows / rate
        ok("parse speed", f"{rate:,.0f} rows/s measured with DuckDB, so S1's single pass over "
                          f"{est_rows:,} rows is roughly {minutes(projected)}")
        if projected > 1800:
            warn("parse time", "over half an hour for S1 alone. Start it and leave it; "
                               "do not sit and watch it.")
        return projected, est_rows
    except Exception as exc:
        warn("parse speed", f"could not measure: {type(exc).__name__}: {exc}")
    return 0.0, est_rows


def check_space(est_rows: int) -> None:
    """Estimated from what the dev slice actually produced, not from a guess."""
    free = shutil.disk_usage(ROOT).free

    dev_bytes = sum(p.stat().st_size for p in FEATURES.glob("*/*.parquet")) if FEATURES.exists() else 0
    dev_rows = 0
    if dev_bytes:
        try:
            import polars as pl
            for path in FEATURES.glob("*/*.parquet"):
                dev_rows += pl.read_parquet_schema and pl.scan_parquet(path).select(pl.len()).collect().item()
        except Exception:
            dev_rows = 0

    if dev_bytes and dev_rows and est_rows:
        per_row = dev_bytes / dev_rows
        needed = int(est_rows * per_row * 1.6)   # features plus labels plus headroom
        basis = f"measured from the current {human(dev_bytes)} of Parquet over {dev_rows:,} rows"
    else:
        needed = 2 * 1024 ** 3
        basis = "no dev ingest to measure from, so this is a flat 2 GB guess"

    if free < needed:
        fail("disk space", f"{human(free)} free, and the run needs roughly {human(needed)} "
                           f"({basis})")
    else:
        ok("disk space", f"{human(free)} free, run needs roughly {human(needed)} ({basis})")


def check_scale(est_rows: int) -> None:
    """What 15 million rows do to the stages that hold everything in memory.

    This is the check that matters most and is the least obvious. S1 and S2
    stream through DuckDB and do not care how big the file is. S6 does: it
    rebuilds every run into NumPy and keeps them all, so the working set is the
    entire dataset as float64, and it writes one JSON file per scored run. On
    the dev slice that is 84 files and 25 MB of RAM; at full scale it is a
    different program.

    The repository already carries the scar -- a commit reading "Stop tracking
    20k generated drift artifacts".
    """
    if not est_rows:
        return

    SAMPLES_PER_RUN = 960          # the long form; some runs are 500
    BYTES_PER_VALUE = 8            # float64
    n_columns = 52

    schema = ROOT / "artifacts" / "schema.json"
    if schema.exists():
        try:
            doc = json.loads(schema.read_text(encoding="utf-8"))
            n_columns = max(1, len(doc.get("columns", {})) - 1)
        except json.JSONDecodeError:
            pass

    est_runs = max(1, est_rows // SAMPLES_PER_RUN)
    # S6 streams one partition at a time; it no longer holds the dataset. The
    # peak is the largest partition plus the fifteen reference runs, not
    # est_rows. This check used to report the whole-dataset figure and warned
    # about 6 GB on a run that now peaks near a tenth of that.
    est_partitions = max(1, len(list(FEATURES.glob("simulationRun=*"))))
    working_set = int(est_rows / est_partitions * n_columns * BYTES_PER_VALUE * 2)

    try:
        total_ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, AttributeError):
        total_ram = 0

    scored = sorted(SCORED.glob("*.json"))
    real = [p for p in scored if p.stat().st_size > 1000]
    per_file = (sum(p.stat().st_size for p in real) / len(real)) if real else 25 * 1024
    output = int(est_runs * per_file)

    ok("scale", f"about {est_runs:,} runs of {SAMPLES_PER_RUN} samples, {n_columns} channels")

    if total_ram and working_set > total_ram * 0.6:
        fail("S6 memory",
             f"one partition is about {human(working_set)} as float64 against "
             f"{human(total_ram)} of RAM. S6 streams, so this is the floor: it "
             f"cannot be lowered by scoring fewer runs. Ingest fewer rows per "
             f"partition.")
    elif total_ram:
        ok("S6 memory", f"streams one partition at a time, peak about "
                        f"{human(working_set)} of {human(total_ram)} RAM")

    if est_runs > 2000:
        warn("S6 output",
             f"one JSON file per scored run: about {est_runs:,} files and {human(output)} "
             f"in artifacts/drift_events/. Git already choked on this once "
             f"(\"Stop tracking 20k generated drift artifacts\"). Check .gitignore "
             f"covers it before committing anything after the run.")

    if est_runs > 500:
        warn("UI run picker", f"the shift dropdown lists runs that have both scores and "
                              f"values; {est_runs:,} is past what a dropdown is for. It is "
                              f"capped to the most eventful 200.")


# Every third-party module the pipeline imports, not a subset of them. pandas
# and pyarrow were missing here, and the cost was exact: a prod run passed
# preflight, spent four minutes in S1 rebuilding 2.3 GB of Parquet, and died in
# S3 on `import pyarrow`. S1 and S2 survive without it because DuckDB and Polars
# read Parquet themselves; S3 reads it through pandas, which cannot.
_REQUIRED = ("duckdb", "polars", "pandas", "numpy", "pyarrow", "yaml", "jsonschema")


def check_dependencies() -> None:
    missing = []
    for module in _REQUIRED:
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        fail("dependencies", f"missing: {', '.join(missing)}. Run: make setup")
    else:
        ok("dependencies", f"{', '.join(_REQUIRED)} all import")


def check_model(probe: bool) -> None:
    """The provider, the key, and the pacing -- before 52 calls depend on them."""
    if not CONFIG.exists():
        fail("model config", f"{CONFIG.relative_to(ROOT)} is missing")
        return

    import yaml
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    llm = cfg.get("llm", {})
    provider = llm.get("provider")
    model = llm.get("model")
    rate = llm.get("rate_limit_per_min")

    if provider in ("local", "stub"):
        ok("model", f"provider {provider!r} ({model}) — nothing will leave this machine")
    else:
        key_var = llm.get("api_key_env")
        if not key_var:
            fail("model", f"provider {provider!r} needs api_key_env in config/llm.yaml")
            return
        if not os.environ.get(key_var):
            fail("model", f"{key_var} is not set in this shell. "
                          f"A full run will fail on the first S4 column.")
            return
        ok("model", f"provider {provider!r} ({model}), {key_var} is set — "
                    f"EVERY call leaves this machine and is logged")

    n_columns = 52
    schema = ROOT / "artifacts" / "schema.json"
    if schema.exists():
        try:
            doc = json.loads(schema.read_text(encoding="utf-8"))
            n_columns = len(doc.get("columns", {})) or n_columns
        except json.JSONDecodeError:
            pass

    if rate:
        pace = n_columns / rate * 60
        ok("model pacing", f"{rate} calls/min, so S4's {n_columns} columns take about "
                           f"{minutes(pace)}. The column count does not grow with the "
                           f"dataset, so the model cost is the same as a dev run.")
    elif provider in ("local", "stub"):
        # Pacing exists for hosted free-tier quotas. On a model running here
        # there is no quota to respect, and throttling would only turn S4's
        # column loop into a wait for nothing. Unset is the right answer.
        ok("model pacing", f"off, correctly: {provider!r} has no quota to pace against, "
                           f"so S4's {n_columns} columns run at the model's own speed")
    else:
        warn("model pacing", "no rate_limit_per_min set; a free-tier key will hit 429s")

    if not probe:
        return

    try:
        from trust.gateway import call_model
        t0 = time.time()
        answer = call_model(
            purpose="Preflight reachability probe",
            payload={"instructions": ["Reply with the single word ok."]},
            schema_out={"type": "object", "required": ["status"],
                        "properties": {"status": {"type": "string"}}},
            stage="preflight",
        )
        latency = time.time() - t0
        if answer.get("_unparsed"):
            warn("model probe", f"answered in {latency:.1f}s but not as JSON: "
                                f"{str(answer['_unparsed'])[:60]}")
        else:
            ok("model probe", f"answered in {latency:.1f}s (call {answer.get('_call_id')})")
    except Exception as exc:
        fail("model probe", f"{type(exc).__name__}: {exc}")


def check_leftovers() -> None:
    """What the last run left behind, and what it will do to this one."""
    if DECISION_LOG.exists() and DECISION_LOG.stat().st_size:
        n = sum(1 for line in DECISION_LOG.read_text(encoding="utf-8").splitlines() if line.strip())
        warn("decision log", f"{n} entries already in artifacts/decision_log.jsonl. "
                             f"Run with --fresh-log or the two runs become one unreadable trail.")
    else:
        ok("decision log", "empty")

    files = sorted(glob.glob(str(SCORED / "*.json")))
    if not files:
        ok("scored runs", "artifacts/drift_events/ is empty")
        return

    fingerprints: dict[tuple, int] = {}
    for path in files:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        detector = data.get("detector") or {}
        limits = detector.get("limits") or {}
        key = ((detector.get("parameters") or {}).get("n_components"),
               round(float(limits.get("t2") or 0), 1))
        fingerprints[key] = fingerprints.get(key, 0) + 1

    if len(fingerprints) > 1:
        detail = ", ".join(f"{n} run(s) with k={k[0]}" for k, n in
                           sorted(fingerprints.items(), key=lambda kv: -kv[1]))
        warn("scored runs", f"{len(files)} files scored against {len(fingerprints)} different "
                            f"baselines ({detail}). --fresh-log clears them.")
    else:
        warn("scored runs", f"{len(files)} files from a previous run. --fresh-log clears them.")


def check_existing_ingest() -> None:
    partitions = sorted(FEATURES.glob("simulationRun=*"))
    if not partitions:
        ok("current ingest", "no data/features/ yet — this will be the first run")
        return
    total = sum(p.stat().st_size for p in FEATURES.glob("*/*.parquet"))
    ok("current ingest", f"{len(partitions)} partition(s), {human(total)} — "
                         f"a prod run replaces this")


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--probe", action="store_true",
                        help="make one real model call to prove the key and endpoint work")
    args = parser.parse_args()

    print(f"{BOLD}Preflight for a full run{OFF}  {DIM}nothing here changes anything"
          f"{' except one logged model call' if args.probe else ''}{OFF}\n")

    projected, est_rows = check_source()
    check_space(est_rows)
    check_dependencies()
    check_scale(est_rows)
    check_existing_ingest()
    check_model(args.probe)
    check_leftovers()

    width = max(len(label) for _, label, _ in _results) + 2
    for status, label, detail in _results:
        mark = {"ok": f"{GREEN}ok  {OFF}", "warn": f"{YELLOW}warn{OFF}", "fail": f"{RED}FAIL{OFF}"}[status]
        print(f"  {mark}  {BOLD}{label:<{width}}{OFF}{detail}")

    failures = [r for r in _results if r[0] == "fail"]
    warnings = [r for r in _results if r[0] == "warn"]
    print()

    if failures:
        print(f"{RED}{len(failures)} blocker(s). Fix these before starting a full run.{OFF}")
        return 1

    print(f"{GREEN}Ready.{OFF} {DIM}{len(warnings)} warning(s).{OFF}")
    print(f"\n{BOLD}To run it:{OFF}")
    print(f"  .venv/bin/python orchestrator.py --mode prod --fresh-log")
    print(f"  .venv/bin/python -m eval.validate_detection")
    print(f"  .venv/bin/python ui/server.py")
    if projected:
        print(f"\n{DIM}S1 is the long pole: parsing the CSV once is about {minutes(projected)} "
              f"on this machine, and the ingest reads it twice -- once for features, once for "
              f"labels. Start it and go and do something else.{OFF}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
