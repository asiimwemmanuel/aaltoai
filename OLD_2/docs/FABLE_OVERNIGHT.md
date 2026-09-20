# Overnight brief — paste this as your first message to Fable

You are working alone overnight on a hackathon submission that is judged
tomorrow morning. The owner is asleep and has enabled automatic approval. Work
steadily, commit often, and leave the tree in a state that runs.

## Read first, in this order

1. `CONTRACTS.md` — binding. Every rule in it maps to a scoring criterion.
2. `CLAUDE.md` — how to work in this repo.
3. `INTEGRATION.md` — how the two halves of this project were merged.
4. `docs/ROADMAP_TOMMASO.md` — what the owner was doing.
5. `docs/GEMINI_STATUS.md` — the state of the model provider.

Run `make check` before you change anything, so you know what green looks like.
Run it again before every commit. If it fails, you are not done.

## The four rules that fail CI

These are not preferences. Breaking any one of them loses a scoring criterion or
disqualifies the entry.

1. Raw data never leaves the machine. Every model call goes through
   `call_model()` in `trust/gateway.py`. No LLM SDK imported outside `trust/`.
2. No original column names after S1. Only `col_NNN`.
3. `faultNumber` and `fault_status` never appear under `pipeline/`.
4. No line ties an original column name to a physical meaning.

An operator's own words are an accepted input, not a violation. A rule that says
"Reactor pressure must stay below 2900" is data we must handle. Only prompts and
lookup tables are hints.

---

## DO THESE — in this order, autonomously

### 1. Verify the tree actually runs (30 min)

```bash
make setup
make check
python orchestrator.py --mode dev
python ui/server.py    # then stop it
```

`--mode dev` runs on two simulation runs and takes seconds. **Never run the full
6 GB pipeline**: it takes hours and the artifacts already exist.

Fix whatever breaks. Report what you fixed. If the dataset symlink is missing:
`mkdir -p data && ln -sfn ~/Hackaton/te_process.csv data/te_process.csv`.

### 2. Make failure loud (20 min)

`pipeline/s4_semantics.py` currently falls back to a two-branch heuristic when
the model is unreachable, writes a complete `semantics.json`, and exits zero. A
total failure is indistinguishable from success. It produced 52 identical roles
at high confidence and nobody noticed for hours.

If the model was unreachable for **every** column: exit non-zero, write
`"degraded": true` into the artifact, and append a `config_change` entry to the
decision log saying so. Apply the same treatment to S7.

This is the same failure mode the challenge asks us to catch in the data. We
should not have it in our own pipeline.

### 3. Set up Ollama and make local the default (45 min)

```bash
brew install ollama || curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull llama3.1:8b
```

Then switch `config/llm.yaml` to the `llm_local_profile` block already in the
file, re-run S4 in dev mode, and confirm `artifacts/semantics.json` shows varied
roles and mixed confidences rather than one role repeated.

Local is the better end state: it earns the no-egress bonus and removes the API
key dependency entirely. If Ollama cannot be installed, say so and leave the
config on `google`.

### 4. A model toggle in the cockpit UI (1 h)

The backend exists and is tested. `GET /api/model` returns the active provider
and the available options with a `ready` flag and a reason when not ready.
`POST /api/model {"provider": "local"}` switches and logs it.

Add the control to `ui/cockpit.html`: a small selector in the header showing
which model is answering, whether it sends anything off the machine, and letting
the operator switch. Disable options whose `ready` is false and show the reason.

Never accept an API key in the browser. The backend refuses this deliberately.

This is Deliverable 8 performed rather than described: switch from a hosted
model to the local one with the egress panel visible beside it, and the byte
count goes to zero.

### 5. Clean the repository (30 min)

- Delete `artifacts/from_team/` — a merge quarantine, no longer needed.
- Confirm `.gitignore` covers `artifacts/**/*.json`, `artifacts/**/*.jsonl`,
  `*.zip`, `.venv/`, `data/`.
- Remove `online_retail_II.csv.zip` from tracking if still tracked.
- Delete stray `.git/*.lock` files if any remain.
- Do **not** rewrite git history. Do not force push.

### 6. Read the code you did not write (1 h)

Then fix only what is clearly wrong: dead code, a function that cannot be
reached, an exception swallowed silently, a docstring contradicting its code.

Do not refactor for taste. Every change must have a reason you could defend in
one sentence, and that sentence goes in the commit message.

### 7. Write the submission README (45 min)

Replace `README.md` with what a judge reads first:

- What the system does, in three sentences, no jargon.
- How to run it: `make setup && make check && python orchestrator.py --mode dev`.
- The eight deliverables, each with the file or screen that demonstrates it.
- The honest numbers from `eval/validation_report.json`, including the 14.8%
  detection rate. **Do not hide this.** Explain the trade-off with the 4.9%
  false alarm rate, and that three fault types in this dataset are known to be
  undetectable by residual methods. A team that measured itself and can explain
  the result is executing the rubric's own sentence about well-reasoned
  uncertainty scoring above confident-sounding claims.
- What is not done, named plainly.

---

## ASK BEFORE DOING — leave these for the morning

Do the preparation, stop before the irreversible step, and leave a note in
`docs/MORNING_DECISIONS.md` saying exactly what you need.

- **Creating a new GitHub repository under the owner's account.** Prepare
  everything, write the exact commands, do not run them. The current remote
  belongs to a teammate and must not be touched.
- **Pointing the domain `vucumpra.com` at anything.** DNS is irreversible on a
  timescale that matters tomorrow.
- **Exposing anything to the public internet** — tunnels, port forwarding,
  hosting the Ollama endpoint. Write the plan, do not open the door. An
  unauthenticated model endpoint on a public address is somebody else's bill.
- **Force pushing, rewriting history, deleting branches.**
- **Deleting `te_process.csv`** or anything under `data/`.
- **Rotating or committing API keys.** The keys currently in the environment
  should be rotated by the owner in the morning; they have been shared in chat.
- **Any `sudo`.**

## The webapp for judges: prepare, do not deploy

The owner wants judges to reach this from the web tomorrow. Get it ready:

1. Confirm the UI runs cleanly from a fresh clone on `localhost:8000`.
2. Write `docs/DEPLOY.md` with two options, commands included but not run:
   - a tunnel (`cloudflared tunnel --url http://localhost:8000`), fastest, no DNS
   - a static export of the UI reading committed artifacts, which needs no
     server and no model, and is the safer demo if the network misbehaves
3. Note clearly which features need the backend (chat, override, model switch)
   and which work as static files (sensor report, decision log, charts).
4. Flag the obvious risk: the current server has **no authentication**. Anything
   publicly reachable can be written to by anyone. If judges need access, a
   read-only static export is the correct answer, not exposing the write API.

## How to work

- One task per commit. Message says what changed and why, in plain sentences.
- `make check` green before every commit.
- Never run the full 6 GB pipeline. `--mode dev` for everything.
- If a task turns out to be bigger than described, do the part that is safe,
  commit it, and write what remains in `docs/MORNING_DECISIONS.md`.
- If something is ambiguous and getting it wrong would be expensive, stop and
  write it down rather than guessing.

## Leave behind

`docs/MORNING_DECISIONS.md` containing:

- what you did, one line each
- what broke and how you fixed it
- what you did not do, and why
- the decisions waiting for the owner, each with the exact command to run
- anything you found that worries you

Write it as you go, not at the end.
