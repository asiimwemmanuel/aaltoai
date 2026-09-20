# Trustworthy Process Monitor

Norrin challenge, AaltoAI Hackathon 2026.

An autonomous agent that ingests an undocumented industrial sensor stream,
infers what each signal measures, separates broken data from a broken process,
and hands an operator a diagnosis they can question and overturn — without raw
data ever leaving their environment.

## Start here, in this order

1. Read `CONTRACTS.md`. All of it. It is short and it is binding.
2. `make setup` — installs deps, links the example artifacts into `artifacts/`.
3. `make check` — proves your environment is sane. It should pass on a fresh clone.
4. Build your stage against `artifacts/examples/`. Do not wait for upstream.

## Layout

    contracts/          JSON Schema for every artifact. The interface.
    artifacts/          Runtime artifacts. Gitignored except examples/.
    artifacts/examples/ Conforming fakes. Build against these from hour one.
    pipeline/           The seven stages. No stage imports another.
    trust/              call_model gateway, decision log. The only door out.
    eval/               Everything that touches fault labels. Quarantined.
    tools/              validate.py, gate_check.py. The contract enforcers.
    ui/                 Operator interface.
    config/             llm.yaml — swap the model provider here, not in code.

## Commands

    make check      validate + gate. Run before every commit.
    make validate   artifacts conform to their schemas
    make gate       static scan for gate violations and label leaks
    make run        full pipeline, config-driven

## Roles

| | Role | Owns |
|---|---|---|
| A | Data & Infra | schema.json, profiles.json, Parquet conversion |
| B | Relations & Semantics | relations.json, semantics.json |
| C | Data Quality | dq_report.json, the rule compiler |
| D | Drift & Diagnosis | drift_events.json, diagnosis.json |
| E | Trust Layer & UI | gateway, decision log, dashboard, integration |
