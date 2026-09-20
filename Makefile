# Works on a bare machine: `make setup` builds its own virtualenv, so a locked-down
# system python (common on macOS) cannot block you on day one.

PY    ?= python3
VENV  := .venv
VPY   := $(VENV)/bin/python
RUNPY := $(shell [ -x $(VENV)/bin/python ] && echo $(VENV)/bin/python || echo $(PY))

.PHONY: setup check validate gate examples doctor run run-prod detect refit ui clean

setup:
	@echo "→ creating virtualenv in $(VENV)"
	@$(PY) -m venv $(VENV)
	@$(VPY) -m pip install -q --upgrade pip
	@echo "→ installing dependencies"
	# Every package here is imported somewhere in the tree. polars and pandas
	# were missing and a fresh clone could not start the UI: series_reader.py
	# imports polars, s3_relations.py imports pandas.
	@$(VPY) -m pip install -q jsonschema pyyaml duckdb polars pandas numpy pyarrow
	@mkdir -p artifacts
	@cp -n artifacts/examples/*.json artifacts/ 2>/dev/null || true
	@cp -n artifacts/examples/decision_log.jsonl artifacts/ 2>/dev/null || true
	@echo "→ example artifacts copied into artifacts/ — build against those"
	@echo "→ done. Now run: make check"

check: gate validate

validate:
	@$(RUNPY) tools/validate.py

examples:
	@$(RUNPY) tools/validate.py --examples

gate:
	@$(RUNPY) tools/gate_check.py

doctor:
	@echo "python:   $$($(RUNPY) -V 2>&1)"
	@echo "using:    $(RUNPY)"
	@$(RUNPY) -c "import jsonschema; print('jsonschema:', jsonschema.__version__)" 2>/dev/null || echo "jsonschema: MISSING — run make setup"
	@$(RUNPY) -c "import yaml; print('pyyaml:   ok')" 2>/dev/null || echo "pyyaml:    MISSING — run make setup"
	@echo "artifacts: $$(ls artifacts/*.json 2>/dev/null | wc -l | tr -d ' ') file(s)"
	@echo "examples:  $$(ls artifacts/examples/*.json 2>/dev/null | wc -l | tr -d ' ') file(s)"

# --- running the thing -------------------------------------------------------
# run       2 simulation runs, seconds, for checking that the wiring holds.
# run-prod  the whole dataset, every stage, including the model calls in S4/S7.
# detect    the S6 half only: no model calls, no S1 re-ingest. ~1 minute.
# refit     detect, but decide a new PCA baseline. Moves every control limit,
#           so it is a separate verb you have to type on purpose.
# ui        serve the cockpit against whatever is in artifacts/ right now.

run:
	$(RUNPY) orchestrator.py --mode dev

run-prod:
	$(RUNPY) orchestrator.py --mode prod --fresh-log

detect:
	$(RUNPY) make_manifest.py
	$(RUNPY) pipeline/s6_drift.py --dq-report artifacts/dq_report.json
	$(RUNPY) select_drift_events.py
	$(RUNPY) -m eval.validate_detection

refit:
	$(RUNPY) make_manifest.py
	$(RUNPY) pipeline/s6_drift.py --dq-report artifacts/dq_report.json --refit
	$(RUNPY) select_drift_events.py
	$(RUNPY) -m eval.validate_detection

ui:
	$(RUNPY) ui/server.py

clean:
	@rm -f artifacts/*.json artifacts/*.jsonl
	@find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	@echo "artifacts cleared"
