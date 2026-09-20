# The live cockpit — what was added, and what it touched

19 Sep, written by Tommaso (role E, owns `trust/`, `ui/`, `contracts/`, `tools/`).

A new operator screen: notifications first, one clean overview, and a drill-down
per channel with charts, a projection, a hypothesis carrying a confidence
percentage, a chat, and a Markdown memory file per unit. Plus a clock you can
scrub, so the whole shift can be replayed in front of a judge.

Everything here is **additive**. No existing screen changed behaviour; no stage
under `pipeline/` was touched. `make check` passes.

## TL;DR

| | |
|---|---|
| New files | `ui/cockpit.html`, `ui/charts.js`, `ui/cockpit_api.py`, `ui/series_reader.py` |
| Changed | `ui/server.py` (dispatch only), `config/llm.yaml` (4 gate keys), `trust/decision_log.py` (+1 field), `contracts/decision_log_entry.schema.json` (+2 kinds, +1 field, 1 widened pattern), the three existing pages (one nav link each) |
| Untouched | every file under `pipeline/`, `eval/`, `tools/`, every artifact writer |
| New artifact | `artifacts/machine_context/<machine_id>.md`, owned by the operator |

## The rule that mattered most, and how it is kept

The gate condition is that raw data never leaves. The cockpit does two things
that look like they pull in opposite directions: it draws 960-sample charts,
and it asks a language model what it makes of them. So the two are separated
structurally rather than by intention:

```
data/features/**  ->  ui/series_reader.py  ->  /api/series   ->  the browser
artifacts/*.json  ->  ui/cockpit_api.py    ->  call_model()  ->  the model
```

- `ui/series_reader.py` does not import `trust.gateway`, and nothing that reads
  it passes its output there. It is a separate file so the split shows up in a
  directory listing.
- The one function in it whose output may reach a model is `window_stats()`:
  eight aggregates over a window, no element of the series among them.
- `ui/cockpit_api.py` is split by a banner into `get_*` (reads disk, answers the
  browser, no model) and `ask_*` (builds a summary, calls the one gateway).
- The gate in `trust/gateway.py` re-checks all of it on every call. It is not
  taking this file's word for anything.

**You can see this from the UI.** Audit view → open any channel → the last box
prints the complete payload that was sent, exactly as it was serialised. If a
sample value ever appeared there, the gate would have a bug and that box is
where it would be visible.

Verified live: a unit notes file containing an original column name was refused
with `blocked by the trust gate: payload contains the forbidden token 'xmeas'`.
The refusal is surfaced to the operator, not swallowed.

## What changed in shared files, and why

### `config/llm.yaml` — 4 keys added to `gate.allowed_payload_keys`

`event_context`, `sensor_summary`, `machine_context`, `chat_history`. Same
category as the existing `rule_text` and `question`: aggregates a stage already
wrote, window statistics, or text a human typed. Every other check still runs on
them, including the forbidden-substring list — which is deliberate, because the
notes file is free text an operator controls.

### `trust/decision_log.py` — a `context` field

Cockpit entries are about a run, an event and a unit, none of which are column
ids. Putting them in `subject` would have broken what `subject` is for (it is
what makes the log filterable by channel, and its schema pattern says so). A new
optional `context` object carries them instead. Adding a field to an artifact
you own is CONTRACTS.md §9; nothing was removed or renamed.

### `contracts/decision_log_entry.schema.json`

- `kind` gains `hypothesis` and `context_write`, for the same reason `qa` exists.
- `context` added, as above.
- `evidence_ids` pattern widened from `^ev_[0-9]{4,}$` to `^ev_[A-Za-z0-9_]+$`.
  **Not cosmetic:** every evidence id this project actually produces looks like
  `ev_s2_col_021_dyn`, so the old pattern matched none of them and every real
  entry was quietly validating against Form B instead of Form A. The schema now
  describes what the stages emit.

### `ui/server.py`

Two route tables and one `_dispatch` helper, about 30 lines. All cockpit logic
lives in `ui/cockpit_api.py`. Existing routes are untouched.

## The pieces, and what each one honestly is

**Charts** (`ui/charts.js`, ~400 lines, no dependency). Plain SVG: no CDN, no
build step, no framework — the same rule the rest of this UI works under, and
the right one for a terminal that may never see the internet. Colours are the
validated default palette (blue/orange, worst adjacent CVD ΔE 24.7 on white);
status colours are the reserved four and always ship beside a word.

Two statistics with different units (T² and SPE) are each divided by their own
control limit so 1.0 means "at the limit" and they share one axis. No chart in
here has two y-scales.

**The clock.** One number, `S.tick`. The score series, the inbox, the tiles, the
projection and the charts are all derived from it, so there is no second clock
to fall out of step. The page opens at the end of the shift with every alert
unread — the "I have not looked at this screen all day" state — and
`Replay from start` plays it forward at 1×, 4× or 16×.

**The projection** is least squares over the last stretch of the channel,
extrapolated to the edge of its own ±2σ range, computed in the browser. It is
labelled on screen as arithmetic on data that never left the machine, because
that is what it is. It is not a model and not a forecast.

**The hypothesis** carries a confidence percentage, an epistemic status,
evidence ids, up to three checks an operator can actually perform, and a
`what_would_change_my_mind` line. Everything is logged.

**The unit's notes** (`artifacts/machine_context/<id>.md`) are read back before
every hypothesis and every chat answer. The model may *draft* an addition; only
an operator pressing save writes the file, and that write goes in the decision
log. This is the "each unit grows its own brain" mechanic, and it demonstrably
works — same code, same data, same question:

> *before any notes:* "The evidence does not contain historical context for
> earlier problems, so I cannot determine if this is the same issue."
> — 30%, `uncertain`
>
> *after an operator wrote three lines of history:* "This matches the recurring
> pattern on Channel 21 where a sticking recirculation loop valve causes expected
> dips during changeovers. Check the valve first as noted in the unit history."
> — 80%, `inferred`

That contrast is worth showing to a judge directly.

**Channel labels.** The operator view says "Channel 21"; the audit view says
`col_021`. The model only ever sees the id, and its answers are rewritten for
the operator view by swapping the identifier for its own label — one identifier
for its label, nothing added, nothing removed, and the audit view shows the
answer verbatim. No physical meaning is attached anywhere.

## Deliberately not done

- **A charting library over a CDN.** Barred by the project's own UI rule, and
  wrong for an air-gapped plant terminal. The inline SVG renderer replaces it.
- **A fine-tuned model.** No time, and the memory file plus retrieved evidence
  does the same job in a way an auditor can read. Both of these were agreed as
  possible later additions, not dropped silently.

## Open items this work surfaced

- **Egress.** Every hypothesis and chat answer currently goes to Google, because
  `config/llm.yaml` says `provider: google` while Ollama is being set up. The
  decision log records each call and the egress counter is honest about it.
  Switching to `local` is one line, and the cockpit needs no change.
- **S4's output is thin.** All 52 channels come back `structural_class:
  "manipulated"`, and 32 of them have the identical role string. The cockpit
  shows this faithfully rather than hiding it, which is why every tile reads
  "manipulated". Owner B's call, not a UI fix.
- **`end_sample` can be `null`** on a still-open event (`sim2_run38_ev18`). The
  UI falls back to `start_sample`; S6 may want to emit the current sample
  instead.
- **Run naming.** `series_reader` finds 84 runs, `artifacts/drift_events/` holds
  83 — one run is the calibration reference and is scored by nobody. The run
  picker only offers runs that have both, so this is invisible to an operator,
  but it is worth a look from whoever owns S6.

## How to run it

```bash
export GEMINI_API_KEY=...
.venv/bin/python ui/server.py            # then open http://localhost:8000/ui/cockpit.html
```

Start on `sim2_run38` (18 events) or `sim1_run16` (17). A run with zero events
is a valid and boring demo; the picker sorts the interesting ones first.

---

# Round two — five screens, a plant halt, and a way to check the numbers

Same day, same owner. Still additive: no stage under `pipeline/` was touched,
`make check` passes, and every existing screen still works.

## What was found before anything was built

Three things worth knowing regardless of the UI:

**1. We are running on the dev slice, and the full dataset is here.**
`orchestrator.py --mode dev` passes `--dev` to S1, which does
`WHERE simulationRun <= 2`. `data/te_process.csv` is a symlink to a 6.0 GB file
that exists. The full run is a decision, not a blocker.

**2. Channel 21 does not drag everything with it — it drags one channel.**
All 18 events in `sim2_run38` are the same phenomenon: same leader (col_021 at
73–83%), same shape, recurring every ~44 samples. Of the channels the detector
also blames:

| channel | r with col_021 | inside the events | best lag |
|---|---|---|---|
| col_009 | −0.50 | −0.67 | −1 sample |
| col_002 | −0.06 | −0.08 | +12, r 0.08 |
| col_042 | −0.03 | +0.05 | +6, r −0.11 |

col_009 genuinely co-moves. col_002 and col_042 are **attribution noise** — PCA
residual attribution spreads a little blame around and the old UI presented that
as evidence. `series_reader.co_movement()` now measures it and the drill-down
shows the two apart. (Also: col_051 correlates −0.94 with col_021 and the
detector never mentions it at all. Worth a look from whoever owns S6.)

**3. A `simulationRun` is a shift.** 960 contiguous samples starting where the
clock resets: one production cycle. The dropdown says "Line 2, shift 38" now.

## The screens

`ui/cockpit.html` is a five-screen app behind a hash router (`#home`,
`#overview`, `#outbox`, `#raw`, `#validation`), so a tab left open on the raw
room is still the raw room tomorrow.

- **Home** — one thing and one question: a full-width plant-state banner and a
  large chat box. Nothing else.
- **Overview** — the previous screen, unchanged: clock, inbox, 52 live tiles.
  It is one click away and it kept its behaviour.
- **Outbox** — tickets forwarded to a desk, with unit, channel, physical
  identity, reason, evidence and status. A local queue; nothing is sent
  anywhere and the screen says so.
- **Raw room** — everything unfiltered: a multi-channel chart, a paged raw-value
  table, the grouped decision log, and links to the three older pages.
- **Check** — the ground truth report. See below.

## The plant halt

A high-severity detection arriving during a replay stops the clock at the sample
it arrived on and switches to Home with a black `LINE STOPPED` banner. It does
not restart on its own. The operator acknowledges, calls it a false alarm, or
opens it first — and all three are written to the decision log. Restarting is
always a person's decision, which is also what makes the demo stop on the
interesting frame instead of running past it.

## Physical identity, without a lookup table

`POST /api/physical` asks the model what kind of equipment behaves like this
channel, from statistics only. The answer carries a confidence, alternatives, a
`what_would_settle_it` line, and the channels that genuinely co-move with it.
**Nothing is written until an operator accepts it.** Accepted identities land in
`artifacts/machine_context/<unit>.state.json` and from then on the cockpit, the
sensor report and the diagnosis page all use them.

There is no dictionary in this repository mapping a statistical class to a piece
of equipment, and there must not be: that is precisely the hand-fed knowledge
that forfeits the autonomy criterion. The diagnosis page therefore says
"Nobody has said what Channel 21 physically is yet" rather than guessing — and
once someone has, the headline reads *"recirculation valve — a sudden step
change"* with the name of the operator who accepted it.

## Sources, and citations that were made up

Every model answer already returned `evidence_ids`; they were only ever shown in
the audit view, which is backwards — the operator is the one being asked to act.
There is now a **Based on:** line under every claim, in words
("which channels the detector blamed", "the channel's behaviour over time").

While testing, an answer cited `"machine_context"` and a bare column id as if
they were evidence objects. `check_citations()` now checks every id against the
402 that actually exist across the artifacts, and an answer whose sources cannot
be found says so on the card instead of looking sourced.

## Carried predictions

At the end of a shift, `POST /api/shift_review` drafts what the next shift should
expect. The operator keeps or discards each one. Kept predictions appear on Home
next shift, and when the channel they name is flagged again the card switches
from "watch" to "happening" with a button to record that it came true — which is
also how a prediction gets found out when it was wrong.

## Check: the ground truth page

`eval/validate_detection.py` scores the pipeline against `data/labels/`. It lives
in `eval/` because that is where the labels are quarantined, it makes no model
calls, and `get_validation()` serves the report to exactly one page that carries
an EVAL ONLY banner. Current numbers, on the dev slice:

| | |
|---|---|
| Faults caught | **15%** (10 of 66 runs that carried a fault) |
| False alarms | **0%** (0 of 3 clean runs) |
| First detection | 328 samples into the run, median |
| Structure recovered | **no** — 41 channels in one source-name family, 11 in another, all 52 given the same class |

Two methodological points that were wrong in the first version and are fixed:
the 15 runs the detector was **fitted and calibrated on** are held out (scoring a
model on its own training data measures nothing), and what was called a
"detection delay" is now "samples into the run", because these labels mark the
whole run as faulty and never record the sample the fault began at.

Question 4 is answered without this script ever naming a column: it reads the
original names S1 recorded, strips the trailing index, and counts the families
that fall out. It measures whether S4 recovered a split that exists without
being told what the split is.

## Why you and your teammates see different channels breaking

`artifacts/drift_events/` held **two different detectors**. 69 runs scored with
17 components and limits 273/112 against a 9600-sample baseline; 14 runs scored
with 0 components, limits 0/0 and a **1-sample** baseline. The 14 are leftovers
from an earlier broken pipeline run, and the current S6 correctly skips those
runs (they are the reference set), so it never overwrote them.

Three things now make this visible instead of mysterious:

- a **fingerprint chip** in the timebar (`k17 · T² 273 · SPE 112 · fit 9600`),
  which turns amber on a degenerate baseline. Comparing notes with a colleague
  is a five-second look at that chip.
- the Check page names the disagreeing group and the files it came from.
- `orchestrator.py --fresh-log` clears `artifacts/drift_events/` before S6 runs,
  so it cannot happen again.

## Other changes

- **`orchestrator.py`**: `--fresh-log` archives the decision log to
  `artifacts/snapshots/` and starts an empty one, and clears previously scored
  runs. Shared file — announce it. The eval check is now the last stage.
- **Decision log, grouped**: 1,300 entries collapse to ~15 rows by stage and
  kind, each expandable. 1,001 of them are `inference`, one per column per
  stage, which is why the raw file is unreadable. Nothing is hidden and the log
  stays append-only. The reader was also widened to the Form B shape
  (`action`/`details`), without which S5 showed as `?` with no text.
- **Sensor report**: "0 actuators, 41 sensors" was not a finding, it was what a
  keyword search for "valve" returned. It now reads *52 channels · 18 distinct
  roles found · N identified by an operator · M corrected by an operator*, and
  each row shows "Channel 21" with `col_021` underneath.
- **New contract** `contracts/unit_state.schema.json` (owner E), and
  `tools/validate.py` now validates `artifacts/machine_context/*.state.json`
  against it. CONTRACTS.md section 6 — add the row to the ownership table.
- **NaN is not JSON.** Python writes a bare `NaN` and every `JSON.parse` rejects
  it, so the Check page rendered "undefined" everywhere with no error to explain
  it. Both the report writer and `Handler._send_json` now emit `null`.

## Running on the full dataset

Not run here — it costs model calls, and that is your key. When you want it:

```bash
export GEMINI_API_KEY=...
.venv/bin/python orchestrator.py --mode prod --fresh-log
.venv/bin/python -m eval.validate_detection
.venv/bin/python ui/server.py
```

Two things to expect. S1 ingests 6 GB through DuckDB, which is the slow part but
streams. S4 makes one model call per column, paced at 15/min — 52 columns is
about four minutes and the column count does not grow with the dataset, so the
model cost is the same as a dev run. Time one `--mode prod` S1 before committing
the night to it.

## Still open

- **The detector catches 15% of injected faults.** That is S6's number, not the
  UI's, and the Check page now reports it whether or not anyone likes it. Faults
  6, 13, 16, 17, 18 and 19 are the ones it does catch.
- **S4 gives all 52 channels the same class.** Every tile reading "manipulated"
  is a faithful rendering of that, not a UI bug.
- **col_051 correlates −0.94 with col_021** and appears in no attribution list.
- The physical-identity answer leans on the unit notes when they contain the
  answer. That is the design — but it means a confident physical class can come
  from what an operator wrote rather than from the statistics. The reasoning
  line says which, and the sources line shows "this unit's notes".

---

# Round three — the assistant was not stupid, we were

Same day, same owner. Three things came out of watching a real person use it.

## The broken record, and whose fault it was

Four turns in a row ended with *"Next: Check the recirculation loop valve on
Channel 21."* — including the answer to *"what's 3+3?"*, which also came back
at **100% confidence**. That looks like a bad model. It was a bug in this UI:

```js
text: answer.answer + (answer.next_step ? "\n\nNext: " + answer.next_step : "")
```

`text` is what gets sent back as conversation history. So every turn, the
assistant was shown its own previous suggestion as part of what it had said, and
did the reasonable thing: kept saying it. We built the loop and then blamed the
model for walking round it.

Fixed by storing the suggestion beside the answer instead of inside it, plus
four instructions that each exist because the opposite was observed:

- an `answer_kind` field (`process` / `meta` / `operator_report` /
  `cannot_answer`), and **a confidence percentage is only shown on `process`**.
  A 100% confidence on "what model are you?" is noise dressed as rigour, and it
  is now dropped server-side rather than merely discouraged.
- `next_step` only when it differs from what was already suggested, omitted when
  in doubt, and stripped entirely on a non-process answer.
- when the operator says they checked or fixed something, that is
  `operator_report`: take it as true, say what it changes, **do not suggest they
  check it again**.
- "the unit notes name specific channels; a statement about one is not evidence
  about another."

The same three questions now:

> **what model are you?** → "I am an analytical assistant designed to help
> monitor unit operations and review event statistics based on provided
> telemetry data." *(no confidence, no valve)*
>
> **what's 3+3?** → "…and I cannot calculate arithmetic for you." *(no
> confidence, no valve)*
>
> **i checked the valve, it was stuck, i fixed it** → "Acknowledged that the
> recirculation loop valve was checked and fixed. Monitor Channel 21 to confirm
> if values return to normal operating range."

## A headline that contradicted its own reasoning

Asked what Channel 2 is, the card printed **"recirculation loop valve"** in bold
and then, in its own reasoning, *"I cannot determine the physical class of
Channel 2 from these statistics alone"* — at 20%. It had lifted the identity the
notes give for Channel 21.

Two fixes. The instruction above stops the transfer. And below 40% confidence,
`determined` is false: the card reads **"Not determined from these statistics"**
with the guess demoted to *Best guess:* underneath, and **the Accept button is
not offered at all** — because pinning a 20% hunch to the unit puts it into every
later answer as though it were settled, which is how a guess becomes a fact
nobody remembers agreeing to. Verified: Channel 2 now returns
`physical_class: "not determined"`, 20%, `uncertain`, with three alternatives
and a note that a P&ID would settle it.

## "If it redraws that fast, it can't be real data"

A fair instinct, and half right — but it points at the wrong thing. The charts
**are** raw data. That was never the claim. The claim is that raw data never
*leaves*, and the speed is a consequence of exactly that: 8.3 MB of Parquet is
read once into the server process and held there, so a redraw is a slice of an
array that is already in memory. Had the data gone somewhere to be looked at,
it would be slower, not faster.

So the Raw room now opens with two counted ledgers, side by side:

| Never leaves this machine | Left this machine |
|---|---|
| **25,974** values read from disk and drawn in this tab | **564.2 KB** across 241 of 244 model calls |
| `data/features/**.parquet → ui/series_reader.py → 127.0.0.1 → inline SVG` | `artifacts/*.json → derived summaries → trust/gateway.py → provider` |
| every source file named, with its size and mtime | **rows of the dataset that left: 0** |

Neither number is asserted. The left one is incremented by
`series_reader.series()` as it serves each request; the right one is read from
the append-only decision log that `trust/gateway.py` writes on every call. The
footer states the ratio and where each number comes from, and the artifact list
names every file the screen read.

That answers the second question too — *where do the statistics come from* — by
naming the files rather than describing them.

## Preflight for the full run: `tools/preflight.py`

Read-only. `--probe` additionally makes one real model call to prove the key
works. On this machine it reports:

| | |
|---|---|
| Source | 5.6 GB, **15,398,375 rows**, 57 columns |
| Parse speed | 149,144 rows/s measured with DuckDB → S1 about **2 min** per pass |
| Disk | 10.3 GB free, run needs about **3.2 GB** (extrapolated from the dev Parquet, not guessed) |
| Model | 15 calls/min → S4's 53 columns in about **4 min**, and *the column count does not grow with the dataset* |

It times an actual DuckDB parse rather than a byte read: the first version timed
raw I/O, measured the page cache, and cheerfully reported that a 5.6 GB CSV
takes four seconds.

**Three warnings worth reading before you start it:**

- **S6 will hold about 6.0 GB of 16 GB RAM.** It rebuilds every run into NumPy
  and keeps them all, so the working set is the whole dataset as float64. It
  should survive; it has no headroom. A `--max-runs` bound belongs in S6
  (owner D).
- **About 16,039 runs → 16,039 JSON files, roughly 392 MB** in
  `artifacts/drift_events/`. The repo has hit this before — there is a commit
  called *"Stop tracking 20k generated drift artifacts"*. `.gitignore` covers
  it (`artifacts/**/*.json`); checked.
- **The shift dropdown is now capped to the 200 most eventful**, with a line
  saying how many quieter ones are not listed. 16,000 options is not a control.

```bash
.venv/bin/python tools/preflight.py --probe
.venv/bin/python orchestrator.py --mode prod --fresh-log
.venv/bin/python -m eval.validate_detection
```

## Still honest about

The physical identity leans on the unit notes when the notes contain the answer.
That is the design — a unit that has been told things should answer better than
one that has not — but it means a confident physical class can come from what an
operator wrote rather than from the statistics. The reasoning line says which,
and the sources line shows "this unit's notes" as one of the inputs. It is worth
saying out loud to a judge rather than letting them find it.
