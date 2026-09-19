import argparse
import subprocess
import sys
import os

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
    args = parser.parse_args()

    print(f"Starting AaltoAI Orchestrator in [{args.mode.upper()}] mode.")

    # Define the sequential stages of the architecture
    stages = [
        ("S1: Ingestion & Normalization", f"{sys.executable} pipeline/s1_ingest.py" + (" --dev" if args.mode == "dev" else "")),
        ("S2: Statistical Profiling", f"{sys.executable} pipeline/s2_profiling.py"),
        ("S3: Relational Analysis", f"{sys.executable} pipeline/s3_relations.py"),
        ("S4: Semantic Inference", f"{sys.executable} pipeline/s4_semantics.py"),
        ("S6 (Pre-req): Manifest Generator", f"{sys.executable} make_manifest.py"),
        ("S6: Statistical Drift Monitor (PCA)", f"{sys.executable} pipeline/s6_drift.py"),
        # We explicitly point S7 to the generated output folders
        ("S7: LLM Diagnosis", f"{sys.executable} pipeline/s7_diagnosis.py --drift artifacts/drift_events.json --semantics artifacts/semantics.json --out artifacts/diagnosis.json"),
    ]

    for name, cmd in stages:
        run_stage(name, cmd)

    print(f"\n{'='*60}\n🎉 ENTIRE PIPELINE COMPLETED SUCCESSFULLY! 🎉\n{'='*60}")
    print("All JSON artifacts have been safely generated in the artifacts/ folder.")
    
    if args.start_ui:
        print("\nStarting the Operator UI Server...")
        print("Automatically opening http://localhost:8000/ui/ in your browser...")
        
        import threading
        import webbrowser
        import time
        
        def open_browser():
            time.sleep(2)
            webbrowser.open("http://localhost:8000/ui/")
            
        threading.Thread(target=open_browser, daemon=True).start()
        
        try:
            subprocess.run(f"{sys.executable} ui/server.py", shell=True)
        except KeyboardInterrupt:
            print("\nShutting down UI Server.")

if __name__ == "__main__":
    main()
