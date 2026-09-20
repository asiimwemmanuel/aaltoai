# Changes — morning of 20 September 2026

Branch `overnight`, continuing from `docs/MORNING_DECISIONS.md`. Everything here
was run on this machine, against the full dataset, with `make check` green after
each step. Nothing was pushed.

## Decisions taken with the operator

- Model layer: **both**. The local model is the default; a hosted one is kept
  ready so the cockpit's model switch has two working options to show.
- Full run: **prod, at high priority**, after a backup.
- `structural_class` collapse: **fix the threshold in S2 as well as the
  anchoring in S4** (the two owners were not available; this is logged here for
  them, see "For A and for B" below).
- Echoed roles: **validate and degrade**.

## Environment

- Ollama 0.34.2 installed via brew, `llama3.1:8b` pulled (4.9 GB), serving on
  11434. The `local` gateway path is now proven against a real model rather than
  an Ollama-shaped fake: `provider: local`, `egress: false`, ~1.8 s per call.
- `GEMINI_API_KEY` lives in `.env` (mode 600, already gitignored). The gateway
  reads keys only from the environment, so `.env` is sourced by the shell that
  runs the server, never read by the code. **This key was pasted into a chat and
  should be rotated.**
- The model switch now offers three ready options: `local` (no egress),
  `google` (egress), `stub` (no egress).

## The full run

Ingested 15,330,000 rows (the whole dataset), 53 columns, 2.4 GB of Parquet.
S6 scored 20,985 runs and found 5,614 events. Detection rate 16.7 %, false alarm
rate 6.5 % — against 14.8 % and 4.9 % in the README, so detection improved and
false alarms got worse. Every one of the 106 model calls ran locally with
`egress: false`.

## Five changes

### 1. `tools/preflight.py` — the dependency check was a subset

The prod run passed preflight, spent four minutes in S1 rebuilding 2.3 GB of
Parquet, and died in S3 on `ImportError: pyarrow`. S1 and S2 survive without
pyarrow because DuckDB and Polars read Parquet themselves; S3 reads it through
pandas, which cannot. The check listed `duckdb, polars, numpy, yaml, jsonschema`
and never named the two modules that were missing.

It now checks every third-party module the pipeline imports. `pyarrow` was also
installed into `.venv` — the Makefile lists it under `make setup`, but the venv
predates that line.

### 2. `tools/preflight.py` — a warning that was false on the chosen setup

"no rate_limit_per_min set; a free-tier key will hit 429s" fired on `local`,
where the absence of pacing is deliberate and there is no key to rate-limit.
No-egress providers now get an `ok` that says why pacing is off.

### 3. `config/llm.yaml` — comments that no longer described the file

Switching to `local` left Gemini's reasoning sitting above lines reading
`llama3.1:8b`. The comments were rewritten to describe what the file now says,
and the hosted settings were preserved whole as `llm_hosted_profile` so the
second option in the model switch stays a tested one.

### 4. `pipeline/s2_profiling.py` — a constant wearing the costume of a measurement

`has_plateaus = plateau_ratio > 0.05`. On the full dataset every one of the 52
columns scored between 0.0723 and 0.2866, so the flag was `True` for all of them
and separated nothing.

New `cohort_split()` asks the cohort instead of a constant: sort the ratios,
find the largest step between neighbours, compare it with the median step. If
one step dominates it becomes the threshold; if none does, the statistic does
not separate these columns and that is recorded rather than papered over. The
original 0.05 survives as an absolute floor — a necessary condition, no longer a
sufficient one.

Two fields were added inside `statistics` (Form B leaves it open, so no schema
change): `plateau_separates_cohort` and `plateau_threshold`. They let S4 tell
"this column does not plateau" from "this statistic distinguishes nobody here",
which are different things and only one of them justifies a confident claim.

On the real data it separates at 0.2073 and flags 1 column of 52.

A first version of this rule was wrong, and its own test case caught it: with
two clean groups (20 columns at 0.01 against 11 at 0.9) most gaps are zero, and
the `median_gap <= 0` guard rejected the cleanest separation there is. Zero is
now handled before the ratio test. Six cases are covered: real data, two clean
groups, no plateaus anywhere, all values identical, a single outlier, clean
groups below the floor, and a cohort too small to judge.

### 5. `pipeline/s4_semantics.py` — the model was agreeing with us, not answering

Three defects, one cause.

**The anchoring.** `structural_guess` returned `"manipulated", "high"` whenever
`has_plateaus` was set, and S2 set it for every column. That sentence went
straight into the prompt as "The statistical pre-analysis suggests
'manipulated'", and the model agreed. **Gemini and llama3.1 both did** — the
backup from the overnight Gemini run also has `structural_class: manipulated`
for all 52 columns, so this was never a property of the model.

Each branch now carries the confidence its evidence supports: leading other
columns in the lagged correlations stays `high`; plateauing more than peers
drops to `medium`; "the statistic separates nothing" is its own branch returning
`undetermined, low`.

**The prompt.** It now states the strength of the hint and says plainly that
disagreeing with it is a valid response, instead of presenting it as settled.

**The echoes.** 13 of 52 columns came back with `role` set to
`structural_class`, `semantic_role_inference` or `inference` — the model handing
back the field it was asked to fill. Nothing downstream could tell those from an
answer. `is_echo()` recognises them from our own vocabulary (the keys of
`ROLE_SCHEMA` and of the payload, plus the purpose string), so it stays correct
if the payload changes and assumes nothing about the data. An echo is an absent
answer, not a wrong one: confidence drops to `low`, status to `uncertain`, the
reasoning says what happened, and the structural class survives because it rests
on the statistics.

## Result

|  | before | after |
| --- | --- | --- |
| `structural_class` | `manipulated` × 52 | `measured` × 43, `manipulated` × 9 |
| confidence `high` | 34 | 29 |
| roles that were not roles | 13 of 52 | 0 |
| eval's structural verdict | "assigned one class to every column" | "used more than one class, but not along the family boundary" |

The 43/9 split is close to the real one in this process, which the code does not
know and never looks up. The eval still reports that the boundary is not the
right one, and that is accurate.

## What was deliberately not done

**`plateau_ratio` cannot recover the structural split, and no threshold on it
can.** It is the fraction of consecutive samples with an identical value, and it
sits at ~0.085 on every column of this dataset, sensors and actuators alike —
an artefact of the precision the values are stored at, not a physical property.
The fix above stops it from *asserting* a split it cannot support; it does not
manufacture one.

Recovering the split properly needs a different discriminator. `noise_level`
(the standard deviation of first differences) is the obvious candidate, because
an actuator steps and a sensor carries continuous noise. That is designing a new
feature inside someone else's stage, so it was not done.

## Known, still open

- `/api/runs` takes 14 seconds and returns 2.8 MB. It reads and parses ~21,000
  JSON files on every cockpit load; the "most eventful 200" cap is applied in
  the browser, so the whole set is sent and then discarded. First paint in front
  of a judge is 14 seconds of empty screen. A cached index keyed on the
  directory's mtime would fix it.
- **The PCA baseline is not reproducible, and it moved the numbers.** Investigated
  after the run; nothing was changed in `s6_drift.py`. Details below.
- Preflight estimates "16,039 runs of 960 samples" by dividing rows by an assumed
  run length. The data holds 500 `simulationRun` partitions of ~30,660 samples.
  The estimate is cosmetic but wrong.
- Everything is still local: 9 commits from the overnight session plus this
  morning's work have not been pushed.

## For A and for B

`pipeline/s2_profiling.py` and `pipeline/s4_semantics.py` were edited without
their owners, with the operator's agreement, because the collapse hit a scoring
criterion directly. Both changes are additive and behind existing field names;
`structural_guess` gained a third parameter with a default that preserves the
old behaviour on profiles written before this change.


## The false alarm rate: investigated

The rate went from 4.87 % to 6.50 % between the two full runs, and the detection
rate from 14.82 % to 16.66 %. Only one of those is a real change.

| | before | after | 95 % CI (Wilson) | verdict |
| --- | --- | --- | --- | --- |
| false alarms | 4.87 % (32/657) | 6.50 % (43/662) | [3.47, 6.79] vs [4.86, 8.64] | intervals overlap — noise |
| detection | 14.82 % (3013/20328) | 16.66 % (3386/20323) | [14.34, 15.32] vs [16.15, 17.18] | disjoint — a real improvement |

The false alarm rate is estimated on 662 fault-free runs out of 20,985. Eleven
runs of difference on that base is not a signal, and any report of that number
should carry its interval. The detection rate rests on 20,323 runs and its
improvement is solid: 373 more detections for 11 more false alarms.

### Why both moved

The detector's limits changed by a factor of three to four:

| | before | after |
| --- | --- | --- |
| `limit_t2` | 219.95 | 76.90 |
| `limit_spe` | 157.43 | 630.39 |
| baseline samples | 5,000 | 5,295 |

The reference runs are **the same fifteen ids** (`sim100_run00` … `run14`) in
both reports — `make_manifest.py` picks `sorted(runs)[:10]` and `[10:15]`, which
is deterministic. What changed is what those ids contain: 5,000 samples before,
5,295 after today's re-ingest. The old figure is exactly 10 × 500, which no real
sub-run structure produces — a `simulationRun` partition holds 42 sub-runs of
varying length, 30,660 samples in total.

So the operating point of the detector is whatever the last ingest happened to
hand it. Nobody chose 76.90, and nobody would be able to reproduce it. This is
the issue already named in `CLAUDE.md` — "the PCA baseline in `s6_drift.py` is
refitted on every run and never saved" — with a measured consequence attached:
it is worth a factor of three on the T² limit and moves every headline number.

The fix is to fit once, write the baseline and the limits to an artifact, and
load them on subsequent runs, refitting only when asked. That is a change to
`s6_drift.py` (D / Ezequiel) and was not made.
