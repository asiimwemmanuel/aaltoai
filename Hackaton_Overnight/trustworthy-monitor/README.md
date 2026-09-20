# Trustworthy Process Monitor

Norrin challenge, AaltoAI Hackathon, September 2026.

The system reads an undocumented industrial sensor stream and works out, from
the numbers alone, what each of the 52 columns is likely to be. It then checks
whether the data itself can be trusted, watches for the moment the process
starts to drift away from normal, and hands the operator a step-by-step
diagnosis that they can accept, question or overturn. No raw reading ever
leaves the operator's machine: a model only ever sees statistics, correlations
and the operator's own words, and every such call is logged with its byte count.

## Run it

```bash
make setup                                   # virtualenv, dependencies, example artifacts
make check                                   # the two contract checks; must be green
.venv/bin/python orchestrator.py --mode dev  # the whole pipeline on 2 simulation runs, ~90 s
.venv/bin/python ui/server.py                # then open http://localhost:8000/ui/
```

`--mode dev` needs the dataset at `data/te_process.csv` (a symlink is fine) and
a model. Without one, the run finishes but says so: S4 and S7 mark their
artifacts `"degraded": true`, the decision log gets a `config_change` entry, and
the orchestrator exits 3 under a DEGRADED banner instead of the green one. The
model is chosen in `config/llm.yaml`; `tools/setup_ollama.sh` installs a local
one and switches to it in one command.

Two things to know before running it in a checkout that already holds a full
run: S1 rebuilds `data/features/` and every stage rewrites `artifacts/*.json`,
and the eval step rewrites `eval/validation_report.json`. Run from a fresh clone
or copy those aside first. Never run `--mode prod` casually: it reads 6 GB and
takes hours.

## The eight deliverables, and where each one is

| # | Deliverable | Where to look |
|---|---|---|
| 1 | Sensor understanding report: a role per column, with evidence and confidence | `artifacts/semantics.json`, written by `pipeline/s4_semantics.py` from `profiles.json` and `relations.json`. Screen: `ui/index.html`. Each entry carries `evidence_ids`, `confidence`, `epistemic_status` and the model's `reasoning`; the exact prompt generated from the statistics (`debug_prompt_generated`) is shown behind the technical-evidence toggle in `ui/diagnosis.html`, as proof the prompt is derived, not written. |
| 2 | Automated data-quality checks, baseline and rule-derived, with pass/fail and traceability | `artifacts/dq_report.json`, written by `pipeline/s5_data_quality.py` through `pipeline/lib/dq_engine.py`. Completeness, frozen sensor, out-of-range and timeliness on every batch; plain-language rules compiled by `pipeline/lib/rule_compiler.py`, each reported under `compiled_rules_evaluated` with its `rule_id`, text, target column and status. A rule the compiler cannot tie to a column comes back as `NEEDS_OPERATOR_INPUT` with a question, never as a data failure. |
| 3 | Drift and anomaly detection, attributed to signals | `artifacts/drift_events/` (one file per scored run) and `artifacts/drift_events.json`, from `pipeline/s6_drift.py`: PCA T²/SPE against a baseline of normal runs, each event with `ranked_signals` and their share of the deviation. Screen: the channel grid, per-channel series and shift replay in `ui/cockpit.html`. |
| 4 | Root-cause diagnosis for a non-specialist | `artifacts/diagnosis.json`, from `pipeline/s7_diagnosis.py`: kind of change, ranked sensors with their inferred roles, a numbered `chain` of statements each pointing at evidence, a confidence level with its weakest link, a `critiques` list where the system argues against itself, and a model-written `narrative` when a model is available. Screen: `ui/diagnosis.html`, operator view by default. |
| 5 | Human-in-the-loop controls | Accept / contest / override on every role in `ui/index.html`; accept / question / overturn on every diagnosis in `ui/diagnosis.html`; both write to the decision log (`POST /api/decision`, `/api/diagnosis_decision` in `ui/server.py`). An override on a role is what the rule compiler reads next. |
| 6 | Decision log | `artifacts/decision_log.jsonl`, append-only, one entry per inference, check, flag, diagnosis, model call, human review, override and config change, each with `evidence_ids`. Schema: `contracts/decision_log_entry.schema.json`. Screen: `ui/decision_log.html`, with a question box that answers "why?" from the log entries and their evidence, through the same gateway as everything else. |
| 7 | Adaptability | Architectural, not a second-domain run (see "Not done"). After S1 no code names an original column: everything downstream speaks `col_NNN`, and `tools/gate_check.py` fails the build on the strings `xmeas` or `xmv` outside S1 and `eval/`. Rules are matched to columns through inferred roles, not a lookup table. The stages talk only through JSON artifacts with schemas in `contracts/`, so a different S1 is the whole change for a different dataset. |
| 8 | Data-flow record and a swappable model layer | `GET /api/provenance` and the "Raw room" screen in `ui/cockpit.html` count, from the log, what left the machine, to which model, and in how many bytes; `trust/decision_log.py:egress_summary()` computes the same. `trust/gateway.py:call_model()` is the only door out, and it refuses anything that looks like records before any network call. The provider is a line in `config/llm.yaml`; the selector in the cockpit header switches it live (`POST /api/model`), logs it, and shows whether the new one sends anything off the machine. |

## The honest numbers

`eval/validation_report.json` is produced by `eval/validate_detection.py` from
the fault labels, which nothing under `pipeline/` ever reads. On the full
dataset (20,985 runs scored, 15 reference runs held out):

| | |
|---|---|
| Runs with a fault detected | **14.8 %** (3,013 of 20,328) |
| False alarm rate on fault-free runs | **4.9 %** (32 of 657) |
| Median position of the first detection | sample 317 of the run |

14.8 % is low, and we are not hiding it. It comes with a false alarm rate under
5 %, which was the choice: the detector's limits are set at the 99th percentile
of held-out normal data, so it stays quiet on normal operation at the price of
missing subtle faults. Three fault types in this dataset (3, 9 and 15) are
known in the literature to be undetectable by residual methods of this kind;
our rates on them are 6.4 %, 6.3 % and 7.3 %, in line with that. The rest of
the shortfall is ours: the per-fault breakdown in the report shows faults 1, 2
and 6, which are usually easy, at 6 %, 6 % and 31 %, which points at the
baseline and limits, not at the data. We measured it, we can explain it, and
the report says exactly which runs were compared and which were excluded. The
rubric asks for a well-reasoned, appropriately uncertain result over a
confident-sounding one, and this is what that looks like when the number is bad.

The same report also grades S4 against the one structural fact the source
naming gives away (41 measured variables, 11 manipulated). The run recorded
there put every column in one class. A model has since answered for all 52
columns with varied roles and reasoning, but the measured/manipulated split is
still not cleanly recovered; the roles are honest hypotheses with evidence, not
a solved problem.

## Not done

- No second-domain run. The retail dataset was downloaded and never used.
  Deliverable 7 rests on the architecture argument above.
- The PCA baseline in S6 is refitted on every run and never saved. In batch
  operation a slow drift would be absorbed into "normal", which is the failure
  the challenge is about.
- The detector calibration above. The numbers are reported, not fixed.
- The UI server has no authentication. It is a demo server for one machine;
  anything reachable from outside can write to the decision log. See
  `docs/DEPLOY.md` before exposing it.
- No drift-over-time chart on the diagnosis screen; the cockpit shows the
  series per channel, the diagnosis page shows the numbers.

## Layout

    contracts/          JSON Schema for every artifact. The interface between stages.
    artifacts/          Runtime artifacts, gitignored except examples/.
    pipeline/           The seven stages, s1 to s7. No stage imports another.
    pipeline/lib/       Shared libraries: dq_engine, rule_compiler, logger adapter.
    trust/              call_model() gateway, decision log, model switch. The only door out.
    eval/               Everything that touches fault labels. Never read by pipeline/.
    tools/              validate.py, gate_check.py, preflight.py, setup_ollama.sh.
    ui/                 Plain HTML + inline JS, no build step. server.py serves it.
    config/llm.yaml     The model provider. Swap it here, never in code.
    docs/               Working notes, including MORNING_DECISIONS.md and DEPLOY.md.

`CONTRACTS.md` is the binding document; `make check` is what enforces it.
