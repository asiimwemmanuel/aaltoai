import duckdb
import json
import os
import pandas as pd
from datetime import datetime

def run_s5_data_quality():
    print("Loading S2 profiles and S4 semantics...")
    
    with open('contracts/schema.json', 'r') as f:
        schema = json.load(f)
    
    with open('contracts/profiles.json', 'r') as f:
        profiles = json.load(f)
        
    # We simulate receiving a new batch of data. We'll use simulationRun=2 as the "incoming batch"
    con = duckdb.connect()
    print("Evaluating Data Quality for incoming batch (simulationRun=2.0)...")
    
    query = """
    SELECT * FROM 'data/features/simulationRun=2.0/*.parquet'
    """
    df = con.execute(query).df()
    
    checks_log = []
    failed_checks = 0
    
    cols = [col for col in schema["columns"].keys() if col != "col_time"]
    
    for col in cols:
        profile = profiles[col]['statistics']
        col_data = df[col]
        
        # Check 1: Completeness (missing values)
        missing_count = col_data.isna().sum()
        if missing_count > 0:
            checks_log.append({"col": col, "check": "completeness", "status": "FAIL", "msg": f"{missing_count} missing values"})
            failed_checks += 1
            
        # Check 2: Validity (out of historical bounds)
        min_val = profile.get('min_val', -9999)
        max_val = profile.get('max_val', 9999)
        range_margin = (max_val - min_val) * 0.1
        out_of_bounds = col_data[(col_data < (min_val - range_margin)) | (col_data > (max_val + range_margin))]
        
        if len(out_of_bounds) > 0:
            checks_log.append({"col": col, "check": "validity", "status": "WARN", "msg": f"{len(out_of_bounds)} readings out of historical bounds"})
            
        # Check 3: Frozen/Stuck Sensor
        # If the standard deviation of this batch is 0 but historical std_dev > 0.01
        batch_std = col_data.std()
        if pd.notna(batch_std) and batch_std == 0 and profile.get('std_dev', 0) > 0.01:
            checks_log.append({"col": col, "check": "frozen_sensor", "status": "FAIL", "msg": "Sensor value is frozen/stuck"})
            failed_checks += 1

    # Check 4: Timeliness (duplicate timestamps)
    duplicate_times = df['col_time'].duplicated().sum()
    if duplicate_times > 0:
        checks_log.append({"col": "col_time", "check": "timeliness", "status": "FAIL", "msg": f"{duplicate_times} duplicate timestamps"})
        failed_checks += 1
        
    # Check 5: Mocking Compiled Natural Language Rules
    mock_llm_rule = {"rule_text": "col_009 must remain within operational limits unless col_001 is closed", "status": "PASS"}
    checks_log.append({"col": "col_009", "check": "human_rule", "status": mock_llm_rule["status"], "msg": mock_llm_rule["rule_text"]})
    
    # Determine Trust Verdict
    trust_verdict = "reliable"
    if failed_checks > 0:
        trust_verdict = "untrustworthy"
    elif any(c["status"] == "WARN" for c in checks_log):
        trust_verdict = "degraded"
        
    dq_report = {
        "batch_id": "simulationRun=2.0",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "trust_verdict": trust_verdict,
        "checks_log": checks_log,
        "stop_pipeline": trust_verdict == "untrustworthy"
    }

    os.makedirs('contracts', exist_ok=True)
    with open('contracts/dq_report.json', 'w') as f:
        json.dump(dq_report, f, indent=2)
        
    print(f"S5 Data Quality Gate complete. Verdict: {trust_verdict.upper()}.")
    if trust_verdict == "untrustworthy":
        print("PIPELINE STOPPED: Data is not reliable.")
    print("dq_report.json generated in contracts folder.")

if __name__ == "__main__":
    run_s5_data_quality()
