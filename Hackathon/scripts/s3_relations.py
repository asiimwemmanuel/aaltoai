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
from core.logger import DecisionLogger

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
        df = con.execute(f"SELECT * FROM read_parquet({features_path}) ORDER BY simulationRun, col_time").df()
        
        grouped = [g for _, g in df.groupby('simulationRun')]
        pairs = list(combinations(cols, 2))
        relations = []
        evidence_counter = 1
        
        for c1, c2 in pairs:
            corr = df[c1].corr(df[c2])
            if pd.notna(corr) and abs(corr) > 0.7:
                best_lag, best_corr = 0, corr
                for lag in range(1, 6):
                    lag_corr_fwd = np.mean([g[c1].corr(g[c2].shift(lag)) for g in grouped])
                    lag_corr_bwd = np.mean([g[c1].shift(lag).corr(g[c2]) for g in grouped])
                    
                    if pd.notna(lag_corr_fwd) and abs(lag_corr_fwd) > abs(best_corr):
                        best_corr, best_lag = lag_corr_fwd, lag
                    if pd.notna(lag_corr_bwd) and abs(lag_corr_bwd) > abs(best_corr):
                        best_corr, best_lag = lag_corr_bwd, -lag
                        
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
