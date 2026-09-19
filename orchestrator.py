import argparse
import shutil
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DECISION_LOG = ROOT / "artifacts" / "decision_log.jsonl"
SCORED_DIR = ROOT / "artifacts" / "drift_events"


def fresh_log():
    """Archive the decision log and start an empty one.

    Without this, a second run appends to the first one's log and the two are
    impossible to tell apart: the same column gets inferred twice, the egress
    counter double-counts, and reading the trail means guessing where one run
    stopped. The old log is never deleted, only moved.
    """
    if DECISION_LOG.exists() and DECISION_LOG.stat().st_size:
        snapshots = ROOT / "artifacts" / "snapshots"
        snapshots.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = snapshots / f"decision_log_{stamp}.jsonl"
        shutil.move(str(DECISION_LOG), target)
        print(f"Archived the previous decision log to {target.relative_to(ROOT)}")
    DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
    DECISION_LOG.write_text("", encoding="utf-8")


def clear_scored_runs():
    """Remove previously scored runs before S6 writes new ones.

    S6 skips the reference runs, so their files survive a re-run and end up
    mixed in with fresh output scored against a different baseline. That is
    exactly how 14 stale single-sample files ended up being compared against 69
    good ones -- and why two people on the same data saw different channels
    breaking. eval/validate_detection.py reports the disagreement; this prevents
    it.
    """
    if not SCORED_DIR.exists():
        return
    removed = 0
    for path in SCORED_DIR.glob("*.json"):
        path.unlink()
        removed += 1
    if removed:
        print(f"Cleared {removed} previously scored run(s) from {SCORED_DIR.relative_to(ROOT)}")

def run_stage(name, command):
    print(f"\n{'='*60}\n🚀 Launching {name}...\n{'='*60}")
    # We use subprocess.Popen to stream the output in real-time
    process = subprocess.Popen(command, shell=True, stdout=sys.stdout, stderr=sys.stderr)
    process.wait()
    
    if process.returncode != 0:
        print(f"\n❌ ERROR: {name} failed with exit code {process.returncode}.")
        print("Aborting the rest of the pipeline.")
        sys.exit(process.returncode)
    print(f"\n✅ {name} completed successfully.")

def main():
    parser = argparse.ArgumentParser(description="AaltoAI Data Pipeline Orchestrator")
    parser.add_argument("--mode", choices=["dev", "prod"], default="prod",
                        help="Run mode: 'dev' runs on a tiny subset of data. 'prod' crunches the full 6GB dataset.")
    parser.add_argument("--start-ui", action="store_true", help="Start the UI server immediately after the pipeline finishes.")
    parser.add_argument("--skip-to", type=str, default=None, help="Skip directly to a specific stage (e.g. 'S4')")
    parser.add_argument("--fresh-log", action="store_true",
                        help="Archive the current decision log and start an empty one, and clear "
                             "previously scored runs. Use this on every re-run: mixing two runs' "
                             "output in one log and one folder is what makes them unreadable.")
    args = parser.parse_args()

    print(f"Starting AaltoAI Orchestrator in [{args.mode.upper()}] mode.")

    if args.fresh_log:
        fresh_log()
        clear_scored_runs()

    # Define the sequential stages of the architecture
    stages = [
        ("S1: Ingestion & Normalization", f"{sys.executable} pipeline/s1_ingest.py" + (" --dev" if args.mode == "dev" else "")),
        ("S2: Statistical Profiling", f"{sys.executable} pipeline/s2_profiling.py"),
        ("S3: Relational Analysis", f"{sys.executable} pipeline/s3_relations.py"),
        ("S4: Semantic Inference", f"{sys.executable} pipeline/s4_semantics.py"),
        ("S6 (Pre-req): Manifest Generator", f"{sys.executable} make_manifest.py"),
        ("S6: Statistical Drift Monitor (PCA)", f"{sys.executable} pipeline/s6_drift.py"),
        # s6_drift.py (run with no --run-id) writes one file per scored run
        # under artifacts/drift_events/. S7 and the UI read a single combined
        # artifacts/drift_events.json instead, so bridge the two here.
        ("S6 (Post): Select run for diagnosis", f"{sys.executable} select_drift_events.py"),
        # We explicitly point S7 to the generated output folders
        ("S7: LLM Diagnosis", f"{sys.executable} pipeline/s7_diagnosis.py --drift artifacts/drift_events.json --semantics artifacts/semantics.json --out artifacts/diagnosis.json"),
        # Scores the run against data/labels/. Lives in eval/ and touches nothing
        # the pipeline reads; it exists so the numbers can be checked rather than
        # believed. Safe to run every time: no model calls, seconds to finish.
        ("EVAL: Ground truth check", f"{sys.executable} -m eval.validate_detection"),
    ]

    skip_active = bool(args.skip_to)
    for name, cmd in stages:
        if skip_active:
            if name.startswith(args.skip_to):
                skip_active = False
            else:
                print(f"Skipping {name}...")
                continue
        run_stage(name, cmd)

    print(f"\n{'='*60}\n🎉 ENTIRE PIPELINE COMPLETED SUCCESSFULLY! 🎉\n{'='*60}")
    print("All JSON artifacts have been safely generated in the artifacts/ folder.")
    
    if args.start_ui:
        print("\nStarting the Operator UI Server...")
        print("Navigate to http://localhost:8000/ui/ in your browser.")
        try:
            subprocess.run(f"{sys.executable} ui/server.py", shell=True)
        except KeyboardInterrupt:
            print("\nShutting down UI Server.")

if __name__ == "__main__":
    main()
