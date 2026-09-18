# Role C: Data Quality and Trust Gatekeeper (Stage 5)
**Author:** Zoe (Role C)  
**Challenge:** Norrin - Trustworthy Process Monitor  
**Core Objective:** Enforce Criterion 2 (A dead sensor is not a process fault) and compile plain-language operating rules.

---

## 1. Directory Structure

`	ext
Role C - Zoe/
|-- core/
|   |-- dq_engine.py          # Stage 5 (S5) Trust Gate bouncer
|   +-- rule_compiler.py      # Plain-language operating rule compiler
|-- contracts/
|   +-- dq_report.json        # Official S5 output JSON Schema
|-- artifacts/
|   |-- dq_report.json        # Conforming sample output artifact
|   +-- decision_log.jsonl    # Append-only audit ledger (with S5 decision records)
|-- scripts/
|   +-- inject_fault.py       # Fault injector for Sunday live demo (Criterion 2)
|-- tests/
|   +-- test_dq_engine.py     # Automated unit test suite (6 tests, 100% pass)
|-- test_giorgio_integration.py # Integration script running on Giorgio real Parquet data
+-- README.md                 # This integration guide
`

---

## 2. Integration Guide for Teammates

### A. For Role D (Drift and Diagnosis):
Before your drift and anomaly models run on an incoming batch, check our trust verdict:

`python
from core.dq_engine import DataQualityEngine

engine = DataQualityEngine(
    schema_path=contracts/schema.json,
    profiles_path=artifacts/profiles.json
)

report = engine.check_batch(batch_df, batch_id=batch_001, compiled_rules=compiled_rules)

if report[trust_verdict] == UNTRUSTED:
    # HARDWARE FAULT DETECTED (dead/frozen sensor or missing data)
    # HALT downstream process models: do NOT trigger a chemical fault!
    print(Halt: Broken data detected!, report[failures])
elif report[trust_verdict] == DEGRADED:
    # Run drift models with caution
    pass
else:
    # TRUSTED: Proceed to Stage 6 Drift and Fault Attribution
    pass
`

### B. For Role E (Trust Layer and UI):
In the Streamlit cockpit:
1. **Trust Verdict Banner:** Display report[trust_verdict]:
   - TRUSTED -> Green badge (All data integrity checks passed)
   - DEGRADED -> Amber badge (Minor range/timing anomalies detected)
   - UNTRUSTED -> Red alert (Hardware sensor flatline/missing data - Data Untrusted)
2. **Rule Compiler Widget:**
   - Operator types: Reactor pressure must stay below 2900
   - Run: compiler.compile_rule(user_text)
   - Pass compiled rule to check_batch()
   - Display PASS / FAIL status in the UI table with traceability to the raw rule text.
3. **Audit Ledger:**
   - S5 automatically appends every evaluation to artifacts/decision_log.jsonl with timestamps, verdicts, and evidence_ids.

---

## 3. How to Run the Tests

### Run Automated Unit Tests:
`ash
python -m unittest tests/test_dq_engine.py
`
*(6 tests, tests completeness, frozen sensors, range checks, rule compilation, and decision logging).*

### Run the Real Parquet Integration Demo:
`ash
python test_giorgio_integration.py
`
*(Demonstrates: Clean batch -> TRUSTED, Injected frozen sensor -> UNTRUSTED with CRITICAL alert, Human rules evaluation).*

---

## 4. Sunday Demo Script for Criterion 2 (45 Seconds)
1. Show a clean incoming batch passing Stage 5 (TRUSTED).
2. Run scripts/inject_fault.py to inject a flatline on col_007 (Reactor Pressure).
3. Stage 5 intercepts the batch:
   ALERT: FROZEN_SENSOR on col_007 | Severity: CRITICAL | Action: ISOLATE_SENSOR_AND_HALT_PROCESS_REASONING
4. Point out to the judges: A naive model would diagnose Fault 4. Our system identifies that the data itself is broken, halts process reasoning, and alerts maintenance to the dead sensor.