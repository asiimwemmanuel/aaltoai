# CONTRACTS — the single source of truth

Read this before writing any code. It is tool-agnostic on purpose: Claude Code,
Antigravity, Cursor and a human with a text editor all obey the same rules here.
`CLAUDE.md` and `AGENTS.md` are thin pointers to this file.

Nothing in this file is a style preference. Every rule below maps to a scoring
criterion or to the gate condition of the challenge.

---

## 0. The gate (pass/fail, not scored)

Raw data never leaves the operator's environment.

- No raw rows, numeric arrays, samples, `df.head()`, `to_string()`, `to_csv()`
  or record dumps in any prompt or any outbound payload.
- Every call to any language model goes through `call_model()` in
  `trust/gateway.py`. There is no second door. Importing an LLM SDK anywhere
  outside `trust/` fails CI.
- What may leave: aggregate statistics, correlation coefficients, lag values,
  cluster memberships, rule text written by a human, column ids.

## 1. Stage isolation

Seven stages. Each reads artifacts from disk and writes artifacts to disk.

    S1 ingest -> S2 profiling -> S3 relations -> S4 semantics
    S1 ingest -> S5 data quality -> S6 drift -> S7 diagnosis

- No stage imports another stage. Ever. If you need S2's output, read
  `artifacts/profiles.json`.
- Every stage runs standalone: `python -m pipeline.s2_profiling --sample`.
- A stage that cannot run because an upstream artifact is missing exits with a
  clear message naming the missing file. It does not crash.

Rationale: five people on five branches with different AI tools. Disk is the
only interface that cannot drift.

## 2. Column identity

After S1, no code anywhere uses an original column name.

- S1 assigns `col_000` ... `col_0NN` and records the mapping in
  `artifacts/schema.json` under `columns[].source_name`.
- Downstream code that contains the string `xmeas` or `xmv` fails CI.

Rationale: the day the pipeline runs on the business dataset the names change
and the code must not. This single rule carries half of the adaptability
criterion.

## 3. Label quarantine

`faultNumber` and `fault_status` exist to evaluate, never to detect.

- S1 writes two separate Parquet sets: `data/` and `labels/`.
- `pipeline/` never receives the path to `labels/`. Code under `pipeline/`
  containing `faultNumber` or `fault_status` fails CI.
- All label use lives under `eval/`.

## 4. Evidence is the atom

Every inference, flag and diagnosis carries `evidence_ids`. A claim with an
empty `evidence_ids` list is not written to an artifact — it is dropped, or
recorded with `confidence: "low"` and an explicit `assumption` field.

The three-way split the challenge asks for is a field, not a tone of voice:

    "epistemic_status": "inferred" | "assumed" | "uncertain"

## 5. No domain terms in prompts

Prompts are generated from statistical profiles by a function. If a prompt
string literal contains a chemistry word (reactor, stripper, condenser,
separator, compressor, catalyst) the build fails. Domain vocabulary may appear
in the *model's output* as a hypothesis; it may not appear in *our input* as a
hint.

## 6. Artifact ownership

One person owns each artifact. Nobody writes to an artifact they do not own.

| Artifact | Owner | Consumers |
|---|---|---|
| `artifacts/schema.json` | A | S2, S5 |
| `artifacts/profiles.json` | A | S3, S4, S5, S6 |
| `artifacts/relations.json` | B | S4, S6 |
| `artifacts/semantics.json` | B | S7, UI |
| `artifacts/dq_report.json` | C | S6, S7, UI |
| `artifacts/drift_events.json` | D | S7, UI |
| `artifacts/diagnosis.json` | D | UI |
| `artifacts/decision_log.jsonl` | E (format) / all (append) | UI, judges |

## 7. The contract is machine-checked

Two commands. They are the whole quality process — there is no code review at a
hackathon, so this is what replaces it.

    make validate    # every artifact in artifacts/ conforms to its schema
    make gate        # static scan: gate violations, label leaks, name leaks

Both run in CI on every push. Both must pass before merging to main.

Working against the schema means: your module can be written before the upstream
module exists. `artifacts/examples/` holds a conforming fake of every artifact.
Symlink or copy them into `artifacts/` and build against those on day one.

## 8. Branch discipline

- One branch per person: `a-data`, `b-semantics`, `c-quality`, `d-drift`,
  `e-trust`.
- Merge to `main` only at the three integration checkpoints.
- After checkpoint 2, `main` always runs. Whoever breaks it fixes it.
- No review. The schemas are the review.

## 9. Adding a field

You may add fields to an artifact you own. You may not remove or rename one.

To add: edit `contracts/<artifact>.schema.json`, update
`artifacts/examples/<artifact>.json`, push both in the same commit, and say so
in the team channel. Consumers keep working because they read by key.
