import duckdb
import pandas as pd
import numpy as np
import json
import os
from itertools import combinations
from datetime import datetime
import warnings
warnings.filterwarnings('ignore') # Ignore pandas warnings for empty slices

def compute_relations():
    print("Loading stratified sample for S3 Relational Analysis...")
    con = duckdb.connect()
    
    # Load schema to get column names
    with open('contracts/schema.json', 'r') as f:
        schema = json.load(f)
    
    cols = [col for col in schema["columns"].keys() if col != "col_time"]
    
    # Since we are using the stratified sample, it's small enough to load entirely into memory
    # This allows us to use Pandas for complex time-series lag math
    df = con.execute("SELECT * FROM 'data/features/*/*.parquet' ORDER BY simulationRun, col_time").df()
    
    relations = []
    
    print("Computing lagged cross-correlations for 1326 pairs... (This takes a few seconds)")
    
    # Group by simulationRun to avoid shifting data across different simulation boundaries
    grouped = [g for _, g in df.groupby('simulationRun')]
    
    pairs = list(combinations(cols, 2))
    evidence_counter = 1
    
    for c1, c2 in pairs:
        # Calculate standard correlation first over the whole sample
        corr = df[c1].corr(df[c2])
        
        # Only dig deeper into lag if the variables are strongly correlated
        if pd.notna(corr) and abs(corr) > 0.7:
            best_lag = 0
            best_corr = corr
            
            # Check up to 5 time steps of lag
            for lag in range(1, 6):
                # c1 leads c2
                lag_corr_fwd = np.mean([g[c1].corr(g[c2].shift(lag)) for g in grouped])
                # c2 leads c1
                lag_corr_bwd = np.mean([g[c1].shift(lag).corr(g[c2]) for g in grouped])
                
                if pd.notna(lag_corr_fwd) and abs(lag_corr_fwd) > abs(best_corr):
                    best_corr = lag_corr_fwd
                    best_lag = lag
                if pd.notna(lag_corr_bwd) and abs(lag_corr_bwd) > abs(best_corr):
                    best_corr = lag_corr_bwd
                    best_lag = -lag # negative lag means c2 leads c1
            
            leader = "concurrent"
            if best_lag > 0:
                leader = c1
            elif best_lag < 0:
                leader = c2
                
            relations.append({
                "evidence_id": f"ev_s3_{evidence_counter:04d}",
                "stage": "S3_relations",
                "kind": "lagged_correlation",
                "subject": [c1, c2],
                "value": {
                    "correlation": float(best_corr),
                    "lag_samples": int(abs(best_lag)),
                    "leader": leader
                },
                "computed_at": datetime.utcnow().isoformat() + "Z",
                "method": "pearson_cross_correlation_max_lag"
            })
            evidence_counter += 1
            
    os.makedirs('contracts', exist_ok=True)
    with open('contracts/relations.json', 'w') as f:
        json.dump(relations, f, indent=2)
        
    print(f"S3 Relational Analysis complete. Found {len(relations)} strong relationships.")
    print("relations.json generated in contracts folder.")

if __name__ == "__main__":
    compute_relations()
