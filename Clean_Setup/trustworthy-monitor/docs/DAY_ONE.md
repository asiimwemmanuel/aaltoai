# Day one, in order

Ninety minutes from clone to five people building in parallel.

## Everyone, together (30 min)

1. Clone the repo. Run `make setup` then `make check`. It must pass on a fresh
   clone before anyone writes anything.
2. Read `CONTRACTS.md` out loud, together. Not skimmed alone — out loud. It takes
   six minutes and it is the only shared context that survives the night.
3. Assign A-E. Write the names in `README.md` and push.
4. Agree the second dataset for adaptability. Put its URL in `docs/DAY_ONE.md`
   under "Second dataset" below. Today, not later.
5. Each person creates their branch: `git checkout -b <letter>-<role>`.

## Each person, alone (30 min)

6. Open `contracts/<your artifact>.schema.json` and
   `artifacts/examples/<your artifact>.json` side by side. That pair is your
   specification. Everything else is detail.
7. Start your tool with the kickoff prompt from `docs/KICKOFF.md`. Make it plan
   before it writes.

## Everyone, together again (30 min)

8. First push. Everyone pushes a branch that passes `make check`, even if it
   only contains a stub that writes the example artifact unchanged.
9. Confirm CI is green.

After this, nobody is blocked on anybody until checkpoint 1.

## Second dataset

TBD — decide on day one and record it here.

## Local model

Ollama on at least one machine: `ollama pull llama3.1:8b`, then set
`provider: local` in `config/llm.yaml`. Until then everyone develops with
`provider: stub`, which returns valid low-confidence answers without any model.
