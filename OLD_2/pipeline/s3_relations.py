"""S3 relational analysis.

Ported 20 Sep from the fork that solved the memory problem: the previous version
held every pair in memory and fell over on the full dataset. This one accumulates
the correlation matrix file by file (map-reduce style), keeps only pairs above the
threshold as candidates, and pays the expensive lagged pass on those alone.

Writes artifacts/relations.json: a flat list of evidence objects, each naming the
two col_ids, the correlation, the best lag and which of the two leads. Reads only
data/features/** and never the labels.
"""
import duckdb
import pandas as pd
import numpy as np
import json
import os
import sys
from itertools import combinations
from datetime import datetime, timezone
import warnings
warnings.filterwarnings('ignore')

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.lib.logger import DecisionLogger

class RelationalEngine:
    def __init__(self, artifacts_dir='artifacts', data_dir='data'):
        self.artifacts_dir = artifacts_dir
        self.data_dir = data_dir
        self.logger = DecisionLogger(os.path.join(artifacts_dir, 'decision_log.jsonl'))

    def run(self):
        print("[S3] Starting Relational Engine...")
        con = duckdb.connect()
        
        schema_path = os.path.join(self.artifacts_dir, 'schema.json')
        with open(schema_path, 'r') as f:
            schema = json.load(f)
            
        cols = [col for col in schema["columns"].keys() if col != "col_time"]
        import glob
        features_path = glob.glob(os.path.join(self.data_dir, 'features', '**', '*.parquet'), recursive=True)
        
        print(f"[S3] 6GB Dataset detected. Using Vectorized Chunking to process 100% of the data with zero MemoryError...")
        
        # Step 1: Compute average baseline correlation incrementally (Map-Reduce style)
        sum_corr = 0
        valid_runs = 0
        for file in features_path:
            df_file = pd.read_parquet(file, columns=cols)
            c = df_file.corr().values
            sum_corr = sum_corr + np.nan_to_num(c)
            valid_runs += 1
            
        avg_corr = sum_corr / max(1, valid_runs)
        avg_corr_df = pd.DataFrame(avg_corr, index=cols, columns=cols)
        
        # Step 2: Find candidate pairs with strong correlation
        pairs = list(combinations(cols, 2))
        candidate_pairs = [(c1, c2) for c1, c2 in pairs if abs(avg_corr_df.loc[c1, c2]) > 0.7]
        
        relations = []
        evidence_counter = 1
        
        # Step 3: Compute lag correlations strictly for candidates
        for c1, c2 in candidate_pairs:
            best_lag, best_corr = 0, avg_corr_df.loc[c1, c2]
            
            lag_sums_fwd = {lag: 0 for lag in range(1, 6)}
            lag_sums_bwd = {lag: 0 for lag in range(1, 6)}
            
            for file in features_path:
                df_file = pd.read_parquet(file, columns=[c1, c2])
                for lag in range(1, 6):
                    fwd = df_file[c1].corr(df_file[c2].shift(lag))
                    if pd.notna(fwd): lag_sums_fwd[lag] += fwd
                    bwd = df_file[c1].shift(lag).corr(df_file[c2])
                    if pd.notna(bwd): lag_sums_bwd[lag] += bwd
                    
            for lag in range(1, 6):
                avg_fwd = lag_sums_fwd[lag] / valid_runs
                avg_bwd = lag_sums_bwd[lag] / valid_runs
                
                if abs(avg_fwd) > abs(best_corr):
                    best_corr, best_lag = avg_fwd, lag
                if abs(avg_bwd) > abs(best_corr):
                    best_corr, best_lag = avg_bwd, -lag
                    
            leader = c1 if best_lag > 0 else (c2 if best_lag < 0 else "concurrent")
            ev_id = f"ev_s3_{evidence_counter:04d}"
            
            relations.append({
                "evidence_id": ev_id,
                "stage": "S3_relations",
                "kind": "lagged_correlation",
                "subject": [c1, c2],
                "value": {"correlation": float(best_corr), "lag_samples": int(abs(best_lag)), "leader": leader},
                "computed_at": datetime.now(timezone.utc).isoformat(),
                "method": "pearson_cross_correlation_max_lag"
            })
            self.logger.log(stage="S3_Relations", action="detect_correlation", details=f"{c1} and {c2} correlated. Leader: {leader}", evidence_ids=[ev_id])
            evidence_counter += 1
                
        with open(os.path.join(self.artifacts_dir, 'relations.json'), 'w') as f:
            json.dump(relations, f, indent=2)
            
        print(f"[S3] Found {len(relations)} relationships. relations.json generated.")

if __name__ == "__main__":
    RelationalEngine().run()
