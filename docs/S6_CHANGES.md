# S6 — what was wrong and what changed

20 Sep 2026, branch `overnight`. Files touched: `pipeline/s6_drift.py`,
`pipeline/lib/run_boundaries.py` (new), `make_manifest.py`, `orchestrator.py`,
`eval/validate_detection.py`, `ui/series_reader.py`.

S6 is D / Ezequiel's stage. It was edited with the operator's agreement because
the stage could not run at all and the numbers it had produced were wrong.

## 1. Run boundaries were read off the physical row order, which is not reliable

Everything that rebuilds runs did the same thing: split a partition wherever
`col_time` returns to 1, number the pieces in Parquet row order.

DuckDB's partitioned writer rotates its buffer. A partition comes back as
`48..500` followed by `1..47`. Splitting on `col_time == 1` then glues the head
of one run onto the tail of the one before it. **54 of the 500 partitions are
rotated somewhere.** Only 14 are rotated at row zero, which is why the guard
added earlier — "does this partition start at 1?" — saw a quarter of the damage
and passed the rest. Two partitions are rotated twice, in different places.

`SET threads TO 1` and `preserve_insertion_order` in S1 did not fix it:
`data/features` was written *after* that change and is still rotated.

The fix does not use row order. Every run is a contiguous `1..L` in `col_time`,
so cut the partition where `col_time` does not advance by one, then give each
headless fragment back the fragment holding its missing first samples. A
fragment with no head anywhere in the partition (two exist: `simulationRun=2.0`
and `=249.0`) is kept as a short run and named on stdout, not stitched onto its
neighbour.

This lives in `pipeline/lib/run_boundaries.py`. It is a library, not a stage, so
`eval/` and `ui/` import it instead of restating the rule. All three copies had
already drifted; the UI was drawing one run while the detector had scored
another.

### What it moved

| | before | after |
| --- | --- | --- |
| runs found | 20,985 | 21,002 |
| fault-free runs in eval | 657 | 1,000 |
| detection rate | 16.7 % | 15.1 % |
| false alarm rate | 6.5 % | **0.0 %** (0 of 1,000) |
| `limit_t2` / `limit_spe` | 76.90 / 630.39 | 223.83 / 155.74 |
| baseline samples | 5,295 | 5,000 (= 10 × 500, as it should be) |

The old false alarm rate was measured on a fault-free set that had faulty rows
stitched into it. 1,000 fault-free runs out of 21,002 is the number the labels
actually support.

## 2. The baseline is now fitted once and saved

Already drafted before this session; kept and now exercised.
`contracts/reference_model.json` holds the PCA baseline, the control limits and
a fingerprint over the reference run ids, the column set and the limit
parameters. Later runs load it. If the fingerprint no longer matches, the stage
stops and says so rather than quietly refitting; `--refit` is the deliberate
override. `eval/validation_report.json` now reports `all_runs_agree: true`.

## 3. Memory: the stage no longer holds the whole dataset

`load_runs()` materialised all 15.3 M rows × 52 columns — about 6.4 GB — before
scoring anything, and `make_manifest.py` called it just to list run ids. On a
16 GB machine that is swap, which is why both looked hung rather than slow.

- `iter_partitions()` yields one partition at a time; only the fifteen
  reference runs are held.
- `discover_run_ids()` reads the `col_time` column alone.
- `load_runs(features_dir, sims)` can restrict itself to named partitions.

`make_manifest.py`: 6.4 GB and minutes → 85 MB and 1.5 s, same manifest.
Full S6: **43 s for 20,987 runs.**

## 4. S5's verdict now actually reaches S6

`orchestrator.py` added the S5 stage but ran S6 with no `--dq-report`, so the
gate S5 exists to provide was never read and its evidence never reached the
drift events. The orchestrator now passes
`--dq-report artifacts/dq_report.json`.

## Still open

- The rotation is an S1 / DuckDB bug. S6 now reconstructs around it, so the
  pipeline is correct either way, but `data/features` is still not written in
  ingest order and S1 (A) should be looked at.
- `simulationRun=2.0` and `=249.0` each hold one truncated run. S5 already
  reports `2.0` as DEGRADED. Nothing is inferred about `249.0` yet.
- `/api/runs` still reads ~21,000 JSON files on every cockpit load.

---

# The detection rate: why it was 15 % and not 85 %

Asked after the above landed: "yesterday it was 80-something percent and it was
detecting channel 21". Both halves of that memory are right, and neither was a
regression from the work above.

## channel 21

`col_021` is the top blamed channel for one process condition, in 1,000 of
1,000 runs. It was never the detector's overall behaviour — it was one
condition's signature, read off the per-condition breakdown.

## 80-something percent

The corpus-wide rate really was ~83 % once, and it fell to 15 % in commit
`08abe28c`. One line did it.

The original `make_manifest.py` built its reference set as `sim{N}_run00` for
each partition — **one run per batch, fifteen batches.** That was replaced with
`sorted(discover_run_ids())[:15]` while fixing a genuine bug in how runs were
counted. The replacement collapsed the selection onto the first fifteen
sub-runs of a *single* batch. A batch holds one run per process condition, back
to back, so those fifteen are fifteen different conditions.

The PCA baseline was therefore fitted on runs containing the very conditions it
exists to detect, and calibrated on five more. The detector learned that
fourteen of them are normal and stopped reporting them:

| | contaminated baseline | clean baseline |
| --- | --- | --- |
| detection rate | 15.1 % | **84.9 %** |
| false alarm rate | 0.0 % | 6.1 % |
| conditions at 0 % | 14 of 20 | 0 of 20 |
| components kept | 16 | 31 |
| `limit_t2` / `limit_spe` | 223.8 / 155.7 | 51.3 / 11.6 |
| median detection position | 317 | 165 |

The 0.0 % false alarm rate was not a good result. A baseline wide enough to
contain fourteen fault conditions is wide enough to contain anything, so
nothing ever crossed it.

After the fix, every condition detects at 100 % except three, which sit at
6–8 %, and one at 78 %. Three conditions being near-undetectable by a
variance-based monitor is expected behaviour, not a defect: they change the
process without moving it outside the envelope the reference runs describe.
They should be named as out of reach for this detector rather than hidden in an
average.

## What `make_manifest.py` does now

One candidate per batch, which is what the original did, plus the two things it
was missing:

- **The assumption is written down.** "The first run of each batch is a
  reference period" is an operator convention about how batches are recorded,
  not a fact from the data. It is in the manifest under `selection.assumption`
  with `epistemic_status: "assumed"`, so it can be contradicted.
- **The candidates are checked against each other.** 25 candidates are
  profiled, each scored by how far its typical column sits from what the others
  agree on (median / MAD, so one odd run cannot drag the comparison), and
  anything past a modified z of 3.5 is dropped and named. If too few agree, the
  stage stops rather than fitting a baseline that means nothing.

No label is read. Candidates are chosen by position, filtered by statistics,
and `contracts/reference_manifest.json` records both. `make gate` passes.

## Also fixed here

A scoring pass now clears `artifacts/drift_events/` before writing. Runs scored
against an earlier baseline used to survive alongside new ones; eval caught it
as `BASELINES DISAGREE`, which is exactly the check working, but the directory
should not need a separate cleanup step to be correct.

## Commands

    make detect     # S6 + eval, reuses the saved baseline. ~75 s. Reproducible.
    make refit      # same, but decides a new baseline. Moves every limit.
    make run-prod   # every stage including the model calls in S4/S7.
    make run        # 2 batches, seconds, for checking the wiring.
    make ui         # serve the cockpit against what is in artifacts/ now.
