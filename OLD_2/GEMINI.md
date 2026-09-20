# Agent instructions

Read `CONTRACTS.md` first and treat it as binding. The same rules apply to every
tool used on this repo — Antigravity, Gemini CLI, Cursor, Claude Code, Codex.

## Hard rules (enforced by CI, `make check`)

1. Raw data never leaves the machine. Every language-model call goes through
   `call_model()` in `trust/gateway.py`. Importing an LLM SDK outside `trust/`
   fails the build.
2. After stage S1, no original column names in code. Only `col_NNN`. The strings
   `xmeas` and `xmv` are banned outside `pipeline/s1_ingest.py` and `eval/`.
3. `faultNumber` and `fault_status` are banned under `pipeline/`. Labels live in
   `eval/` and are for evaluation only.
4. No chemistry vocabulary in prompt string literals. Prompts are generated from
   statistical profiles by a function.

Run `make check` before considering any task finished.

## Architecture

Seven stages, communicating only via JSON artifacts in `artifacts/`. No stage
imports another stage. Every artifact has a schema in `contracts/` and a
conforming example in `artifacts/examples/`. Build against the examples.

## Evidence

Every inference carries `evidence_ids`. See `CONTRACTS.md` §4.

## Stack

DuckDB over Parquet. Never pandas on the full CSV.
