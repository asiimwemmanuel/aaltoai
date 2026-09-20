# Running this on Windows, from zero — for Giorgio

Everything below assumes a clean Windows machine and PowerShell. It takes about
twenty minutes, most of it downloads.

Read the warning in step 6 before you run anything. It is the one that costs
2.4 GB of work if you skip it.

---

## 1. Python

Install Python 3.11 or 3.12 from python.org — **not** from the Microsoft Store,
which sandboxes paths and breaks virtual environments in confusing ways.

During the installer, tick **"Add python.exe to PATH"**. It is off by default
and everything after this depends on it.

```powershell
python --version
```

Must print `Python 3.11.x` or `3.12.x`. If it opens the Store instead, the PATH
tick was missed: re-run the installer and choose Modify.

## 2. Git

```powershell
winget install --id Git.Git -e
```

Close and reopen PowerShell, then:

```powershell
git --version
```

## 3. Get the repository

```powershell
cd $HOME
git clone https://github.com/asiimwemmanuel/aaltoai.git trustworthy-monitor
cd trustworthy-monitor
git checkout overnight
```

`overnight` is the current working branch. `integrazione` is the shared one and
may be behind.

## 4. The virtual environment

Windows puts the interpreter in a different place from macOS and Linux:
`.venv\Scripts\python.exe`, not `.venv/bin/python`. Every command below uses the
Windows path. `make` does not exist on Windows and the Makefile assumes the Unix
layout, so ignore it entirely.

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install jsonschema pyyaml duckdb polars pandas numpy pyarrow
```

Check it took:

```powershell
.venv\Scripts\python.exe -c "import duckdb, polars, pandas, numpy, yaml, jsonschema; print('all imports fine')"
```

**Use `.venv\Scripts\python.exe` for everything from here.** Plain `python` is
the system interpreter and does not have these packages. Most of the confusion
on this project has come from running the wrong interpreter.

## 5. The dataset

The pipeline expects `data\te_process.csv`. The file is 5.6 GB, so do not copy
it if you already have it somewhere — link it.

If you already have the CSV:

```powershell
mkdir data -Force
# PowerShell as Administrator, or with Developer Mode enabled:
New-Item -ItemType SymbolicLink -Path "data\te_process.csv" -Target "C:\path\to\te_process.csv"
```

If symbolic links are refused, a hard link works on the same drive and needs no
special permissions:

```powershell
cmd /c mklink /H "data\te_process.csv" "C:\path\to\te_process.csv"
```

Last resort, copy it. It costs 5.6 GB of disk and several minutes.

If you do not have the CSV: Additional Tennessee Eastman Process Simulation Data
for Anomaly Detection Evaluation, on Harvard Dataverse.

## 6. Read this before running the pipeline

`orchestrator.py` deletes `data\features` and `data\labels` at stage S1, then
every stage overwrites `artifacts\*.json`, and the evaluation step overwrites
`eval\validation_report.json`.

On Tommaso's machine those files hold the full 6 GB run: 20,985 scored runs and
the real detection numbers. `artifacts\*.json` is in `.gitignore`, so **git has
no copy**. A fresh clone on your machine starts empty, so there is nothing to
lose — but if you ever work in someone else's checkout, back up first:

```powershell
mkdir $HOME\DEMO_BACKUP -Force
Copy-Item artifacts\*.json, artifacts\*.jsonl, eval\validation_report.json $HOME\DEMO_BACKUP\
```

## 7. Check the tree before changing anything

```powershell
.venv\Scripts\python.exe tools\gate_check.py
.venv\Scripts\python.exe tools\validate.py
```

The first must say `gate check passed`. The second must say `all artifacts
conform`, or report that there is nothing to validate yet on a fresh clone.

These two are what `make check` runs on macOS. Run them before every commit.

## 8. Run the pipeline

Development mode, two simulation runs, about ninety seconds:

```powershell
.venv\Scripts\python.exe orchestrator.py --mode dev
```

Full run, the whole 5.6 GB, hours:

```powershell
.venv\Scripts\python.exe orchestrator.py --mode prod
```

There is also `run_pipeline.bat` in the repository root if you prefer a
double-click, but check which interpreter it calls before trusting it.

Stages in order: S1 ingest, S2 profiling, S3 relations, S4 semantics,
S5 data quality, manifest, S6 drift, run selection, S7 diagnosis.

**S4 and S7 call a language model.** Without one configured they exit with code
3 and write `"degraded": true` into their artifacts. That is intentional: a
silent fallback used to produce 52 identical roles at high confidence and
nobody noticed for hours. A degraded exit is the system telling you the truth.

## 9. Choose a model

Open `config\llm.yaml`. The `provider` line is the only thing that decides which
model answers.

**No model at all**, for testing the plumbing:

```yaml
provider: stub
```

Every stage completes, roles come back marked low confidence and assumed. Exit
code 0. Good for checking the pipeline runs.

**Gemini**, needs a key:

```powershell
$env:GEMINI_API_KEY = "your-key-here"
.venv\Scripts\python.exe orchestrator.py --mode dev
```

with `provider: google` in the config. The key lives in the environment and is
never written into the file, which is committed.

**Ollama, locally**, no egress and the better end state:

```powershell
winget install Ollama.Ollama
ollama serve
ollama pull llama3.1:8b
```

Then in `config\llm.yaml` use the `llm_local_profile` block already sitting
below the main one — copy those four lines over `llm:`. Set
`rate_limit_per_min: 0` while you are there: it is tuned for a free API tier and
makes a local run four times slower for no reason.

`tools\setup_ollama.sh` does all of this on macOS. On Windows do it by hand; the
steps are the same.

## 10. The operator interface

```powershell
.venv\Scripts\python.exe ui\server.py
```

Then open `http://localhost:8000/ui/`.

If a page looks stale after you change a file, hard-reload with Ctrl+Shift+R.
The server sends no-cache headers, but a browser that already cached a page
before that change keeps serving it.

Screens: sensor report with inferred roles and evidence, decision log with the
egress panel, diagnosis, and the cockpit.

---

## If something breaks

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ModuleNotFoundError: duckdb` | Wrong interpreter | Use `.venv\Scripts\python.exe` |
| `No files found that match data/te_process.csv` | Link missing | Step 5 |
| S4 exits 3, `degraded: true` | No model answered | Step 9 |
| `gate check` fails | A rule in `CONTRACTS.md` broke | The message names file, line and rule |
| UI table empty | Artifacts missing or stale browser cache | Run the pipeline, then Ctrl+Shift+R |
| Pipeline is very slow with a local model | `rate_limit_per_min` still at 15 | Set it to 0 in `config\llm.yaml` |

## The four rules

Read `CONTRACTS.md` in full — it is short. The four that fail CI:

1. Raw data never leaves. Every model call goes through `call_model()` in
   `trust\gateway.py`. No LLM SDK imported outside `trust\`.
2. No original column names after S1. Only `col_NNN`.
3. `faultNumber` and `fault_status` never appear under `pipeline\`.
4. No line ties an original column name to a physical meaning.

A rule an operator typed, like "Reactor pressure must stay below 2900", is an
accepted input, not a violation. Only prompts and lookup tables are.
