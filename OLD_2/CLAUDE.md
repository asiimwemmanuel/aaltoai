@CONTRACTS.md

# Trustworthy Process Monitor

The import above loads the binding contract into every session. This file adds
what is specific to working here, and what the project has learned the hard way.

Norrin challenge, AaltoAI Hackathon 2026. Branch `integrazione`.

## The four rules that fail CI

Run `make check` before saying any task is done. If it fails, you are not done.

1. **Raw data never leaves.** Every model call goes through `call_model()` in
   `trust/gateway.py`. No LLM SDK import outside `trust/`. This is the
   challenge's disqualification condition, not a preference.
2. **No original column names after S1.** Only `col_NNN`. The strings `xmeas`
   and `xmv` are banned outside `pipeline/s1_ingest.py` and `eval/`.
3. **`faultNumber` and `fault_status` are banned under `pipeline/`.** They live
   in `eval/`. Using labels to detect is cheating and it is written in the rules.
4. **No column tied to a physical meaning in code.** A line containing both an
   original column name and a domain word fails the GROUNDTRUTH check. Roles are
   inferred from statistics, never looked up.

Operator rule text is different: a rule an operator wrote, such as
"Reactor pressure must stay below 2900", is an INPUT we must accept. It is not
a violation. Only prompts and lookup tables are.

## Architecture in one paragraph

Seven stages, `pipeline/s1..s7`, communicating only through JSON artifacts in
`artifacts/`. No stage imports another stage. Every artifact has a schema in
`contracts/` and a conforming example in `artifacts/examples/`. Shared helpers
live in `pipeline/lib/`, which are libraries and not stages. `trust/` is the only
code that talks to a model. `eval/` holds everything that touches labels.
`tools/` holds the two checks. `orchestrator.py` runs the stages in order.

## Artifacts come in two shapes

The contracts were written before any stage existed, so each schema now accepts
two forms under `anyOf`: the original contract shape, and the shape the stages
actually emit (usually a map keyed by `col_id` rather than an array).

**Anything that reads an artifact must handle both.** See `normaliseInferences()`
and `collectEvidence()` in `ui/index.html`, and `_find_inference()` in
`ui/server.py`. When you write a new reader, follow that pattern.

Do not "fix" an artifact to match a schema. Widen the reader instead.

## Evidence is the product

Every inference, check and flag carries `evidence_ids` pointing at objects that
match `contracts/evidence.schema.json`. A claim you cannot attach evidence to is
written with low confidence and an honest status, or not written.

The rubric states that a lower-confidence inference with visible reasoning
scores higher than a confident label with none. Build for that. An honest
"I cannot resolve this, which column do you mean?" is worth more than a guess.

## Two audiences, one screen

The operator view is the default: plain language, no `col_NNN`, no evidence ids,
confidence in words. The audit view sits behind a toggle and holds column ids,
evidence objects, statistics and the generated prompt.

The audit view is the proof of autonomy for the judges. The operator view is
what the challenge is scored on. Never merge them, never drop either.

## Stack and constraints

- DuckDB over Parquet for anything touching the dataset. Never pandas on the
  6 GB CSV. `orchestrator.py --mode dev` runs on 2 simulation runs.
- The UI is plain HTML with inline JS and SVG. No build step, no CDN, no
  framework. It reads JSON over `fetch` and nothing else.
- The dev server sends no-store headers. If a page looks stale anyway, the
  browser cached it: hard-reload before debugging anything else.

## Debugging order

When a change seems not to take effect, check in this order. It is the order
that has actually saved time on this project:

1. Is the file on disk changed? `git status`, `git diff`.
2. Is the server serving the changed file? Restart it.
3. Is the browser showing the served file? Hard-reload, check the Network tab.
4. Does the logic work on the real data? Run the function outside the browser
   against the actual artifact.

Step 4 first when the symptom is "empty table" or "wrong numbers". A UI bug here
is almost always artifact shape or caching, and both are provable in a terminal
in under a minute.

## Working here

- Plan before implementing anything larger than one file. Say the plan in under
  ten lines, then stop and wait.
- One module per session. Do not build two screens in one conversation.
- Start from the JSON schema in `contracts/`, not from prose.
- Touch only files the current role owns. Ask before editing `pipeline/`.

## Who owns what

| Area | Owner |
| --- | --- |
| `pipeline/s1`, `s2`, profiling, ingest | A |
| `pipeline/s3`, `s4`, semantics | B / Ezequiel |
| `pipeline/lib/dq_engine`, `rule_compiler`, `s5` | C |
| `pipeline/s6`, `s7`, drift and diagnosis | D / Ezequiel |
| `trust/`, `ui/`, `contracts/`, `tools/` | E (Tommaso) |
| UI design and charts | Zoe |

## Known open items

- S5 is missing from `orchestrator.py`. Data quality never runs in the main
  path, which silently skips scoring criterion 2.
- The PCA baseline in `s6_drift.py` is refitted on every run and never saved.
  In batch operation this absorbs slow drift into the definition of normal,
  which is precisely the failure the challenge is about.
- `epistemic_status` is missing from `semantics.json` and `diagnosis.json`.
  `make validate` reports it as a non-fatal note.
- `artifacts/**` and `*.zip` are tracked in git, which put the repository near
  900 MB. `.gitignore` only covered the top level.
