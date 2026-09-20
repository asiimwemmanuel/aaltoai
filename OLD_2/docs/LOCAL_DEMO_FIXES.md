# Getting a full local run working — changes and open items

19 Sep, written by Tommaso (role E, owns `trust/`, `ui/`, `contracts/`, `tools/`).

Goal: get `orchestrator.py --mode dev` to run S1→S7 to completion at least once
and have the operator UI show it, so there is something to demo on this
machine while everyone keeps fixing their own stage properly on their own
branch. Nothing here is a redesign. Every item below is a targeted unblock,
tagged by the owner whose file it touches, with the failure it fixed and what
the real fix should probably look like — so it's an easy diff against whatever
lands later, not a decision that preempts it.

Related: `docs/GEMINI_STATUS.md` (already in the repo) covers the earlier
404/key issue on the model provider. This doc picks up from there.

## TL;DR

- `orchestrator.py --mode dev` now completes S1→S7 and produces artifacts that
  pass `make check` (gate + schema validation), with real model answers for
  52/52 columns and a real diagnosed drift event.
- The operator UI (`ui/index.html`, `ui/diagnosis.html`, `ui/decision_log.html`)
  renders that run: sensor report, diagnosis, and audit trail all show real
  data, verified in an actual browser, not just curl.
- One correctness bug found in the process (`pipeline/s6_drift.py`'s run
  splitting) was silently producing near-empty reference runs; fixed with
  evidence attached below.
- One UI claim was actively false ("Data Sovereignty: 100% Local" while making
  112 external API calls) and has been corrected to state the guarantee that
  actually holds.

## How to reproduce

```bash
export GEMINI_API_KEY=...        # https://aistudio.google.com/apikey, must start with AIza
.venv/bin/python orchestrator.py --mode dev
.venv/bin/python ui/server.py    # then open http://localhost:8000/ui/
```

A full dev run currently takes a few minutes, mostly S4 (52 sequential model
calls, paced to respect the free-tier quota — see below).

## Changes, by owner

### A — `pipeline/s1_ingest.py`, `pipeline/s2_profiling.py`

**Symptom:** both crashed immediately with
`_duckdb.InvalidInputException: 'pandas' is required for this operation but it
was not installed` — pandas isn't installed, and per `CLAUDE.md` it shouldn't
be, for CSV-scale work.

**Fix:** both only needed pandas for a `.df()` call that DuckDB can do
natively:
- `s1_ingest.py:31` — column names via `con.sql(...).columns` instead of
  `.df().columns.tolist()`.
- `s2_profiling.py:41` — row values via the cursor's `.description` +
  `.fetchone()` instead of `.df()` + `.iloc[0]` + pandas null checks.

No behavior change, no new dependency. Should be safe to take as-is.

### B — none touched.

### C — none touched.

### D / Ezequiel — `pipeline/s6_drift.py`, `make_manifest.py`

**Symptom 1:** `calibrate_limits()` crashed with `need at least one array to
concatenate`. Root cause: `make_manifest.py` hardcoded `fit = runs[:10]`,
`calibration = runs[10:15]`, which assumes ≥15 runs exist. Dev mode ingests 2
`simulationRun` partitions, so `calibration` came out empty.

**Symptom 2 (found while fixing #1, more serious):** even after making the
split adaptive, the fit reference came out as `n_samples=10` for 10 runs —
about 1 sample per run, obviously wrong for a PCA baseline. Root cause,
confirmed with real data:

```python
# pipeline/s6_drift.py, load_runs()
df = pl.read_parquet(...).sort(TIME_COL)   # <- the bug
cuts = np.where(t == 1)[0]                 # run boundaries, assumed in file order
```

`col_time` resets to 1 at every sub-run boundary within a `simulationRun`
partition (confirmed: partition `simulationRun=1.0` has 42 sub-runs, each
960 or 500 samples long, in natural file order). Sorting the whole partition
by `col_time` before looking for resets interleaves all 42 sub-runs together,
so every `col_time == 1` row ends up bunched at the front and the "boundaries"
found afterward are mostly meaningless 1-row slivers. Removing the `.sort()`
(the Parquet file's natural order already keeps each sub-run contiguous, since
S1 doesn't reorder rows on write) fixes it: same partition now yields a
proper `n_samples=6547` reference with 13–16 PCA components.

**make_manifest.py**, separately, generated run ids like `sim1_run00` by
guessing from directory names — always `_run00`, never `_run01` onward — which
doesn't match the real sub-run ids `load_runs()` produces. It happened to not
crash (the guessed id was always a valid key), it just always picked the same
single, often-shortest sub-run. Fixed by having `make_manifest.py` import and
call `pipeline.s6_drift.load_runs()` directly instead of re-deriving run ids,
so there is one source of truth for what a "run" is. Also made the fit/
calibration split adaptive to however many runs are actually available
(70/30, falls back to reusing the fit set if there's only one run total)
instead of the hardcoded `[:10]`/`[10:15]`.

**Suggested real fix:** the adaptive split is a reasonable permanent behavior.
The `.sort(TIME_COL)` removal should be reviewed by whoever owns this file —
it was probably added to guard against out-of-order Parquet row groups in
some other context, and if S1's ingest is ever changed to run multi-threaded
or with an explicit reorder, this assumption (file order == chronological
order) breaks again silently. Might be worth S1 writing an explicit
monotonic row-sequence column if this needs to be bulletproof.

### E (me) — `trust/gateway.py`, `config/llm.yaml`, `contracts/drift_events.schema.json`, `ui/decision_log.html`

**S4 gave up after 1 model call** (`HTTP Error 503` then `429 Too Many
Requests`, `[S4] No model for the remaining columns`). Real cause: Google's
free tier for `gemini-3.6-flash` allows 5 requests/minute, and by the time I
tested, its free daily quota was already exhausted from earlier testing this
session. `trust/gateway.py` had no retry/pacing at all, so the very first
transient failure permanently disabled the model for the rest of the run
(that's `s4_semantics.py`'s own intended fallback behavior — correct, just
triggered by something gateway-level that shouldn't have failed so easily).

Fixed in `trust/gateway.py` (`_post_json`): retries on 429/500/502/503/504,
parsing the server's actual `Retry-After` header or `retryDelay` field instead
of guessing, plus proactive client-side rate limiting (`_throttle()`,
configured via a new optional `rate_limit_per_min` key in `config/llm.yaml`)
so the quota is respected instead of discovered via errors.

Also switched `config/llm.yaml`'s `model` from `gemini-3.6-flash` to
`gemini-flash-lite-latest` — config-only change, no code touched, which is the
whole point of Deliverable 8. The `-flash` preview model's free quota was
already spent; `-flash-lite-latest` took 8 back-to-back calls with no 429
when I checked. This is explicitly an interim choice (commented in the
config) — swap back once on a paid tier, or to `local` once Ollama is up per
`docs/GEMINI_STATUS.md`.

**`artifacts/drift_events.json` failed `make validate`.** The schema's `anyOf`
had two shapes, both written for an older/different drift detector — a flat
list of univariate 3-sigma deviations. That's not what `pipeline/s6_drift.py`
(the real PCA/T2/SPE detector) emits. Added a third `anyOf` branch
("Form C") to `contracts/drift_events.schema.json` matching what
`detect_run()` actually returns (`batch`/`detector`/`events`/`score_series`/
`evidence`). Per `CONTRACTS.md` rule 9, this widens the schema without
removing or renaming anything — Form A and B still validate.

**UI showed a false claim.** `ui/decision_log.html` hardcoded
`✓ Data Sovereignty: 100% Local` regardless of provider. With `google`
configured, the decision log correctly shows `112/112 external calls` right
next to it — the badge was simply wrong. Its own tooltip already stated the
true, provider-independent guarantee ("no raw plant telemetry left local
memory"), so the fix was just to say that instead: now reads
`✓ Zero Raw-Data Egress`, with the tooltip clarifying that external calls can
still happen when an egress provider is selected. This is a correctness fix,
not a style change — the old wording actively misrepresented the gate
condition the whole challenge is graded on.

### Root-level glue — `orchestrator.py`, `select_drift_events.py` (new)

**S7 crashed** with `TypeError: list indices must be integers or slices, not
str` reading `artifacts/drift_events.json`. That file was a stale placeholder
matching the old drift-detector shape (see above). The deeper issue:
`pipeline/s6_drift.py`, run the way the orchestrator calls it (no
`--run-id`), scores every non-calibration run and writes one file per run
into `artifacts/drift_events/`. Nothing produces the single combined
`artifacts/drift_events.json` that S7 and `ui/server.py` both actually read —
that wiring was simply never finished.

Added `select_drift_events.py` (root-level, same pattern as the existing
`make_manifest.py`): picks whichever scored run has the most detected events
and copies it to `artifacts/drift_events.json`. Wired into `orchestrator.py`
as a new step between S6 and S7. This is a genuine placeholder choice, not a
principled one — "most events" is a reasonable pick for a demo, but a real
S7 should probably run diagnosis over every scored run (or every run with at
least one event), not just one. Flagging for whoever ends up owning the
S6→S7 handoff contract properly.

## Suggested follow-ups (not blocking, but worth doing for real)

1. **S4 should fail loudly on total model failure**, not just silently
   degrade to the structural heuristic for every remaining column — already
   flagged in `docs/GEMINI_STATUS.md`, still true.
2. **S6→S7 should probably run over every scored run**, not one hand-picked
   file — see `select_drift_events.py` above.
3. **`pipeline/s6_drift.py`'s reliance on Parquet row order** to reconstruct
   run boundaries is fragile; worth an explicit ordering guarantee from S1 if
   ingestion ever changes.
4. **S5 is still missing from `orchestrator.py`** (pre-existing known item in
   `CLAUDE.md` — unrelated to this session's fixes, still open).
5. `ui/diagnosis.html`'s "Show technical evidence" toggle didn't visibly do
   anything when I clicked it during verification — noted, not investigated;
   may be WIP.
