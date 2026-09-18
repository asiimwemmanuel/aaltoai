import duckdb
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.logger import DecisionLogger

class ProfilingEngine:
    def __init__(self, artifacts_dir='artifacts', data_dir='data'):
        self.artifacts_dir = artifacts_dir
        self.data_dir = data_dir
        self.logger = DecisionLogger(os.path.join(artifacts_dir, 'decision_log.jsonl'))

    def run(self):
        print("[S2] Starting Profiling Engine...")
        con = duckdb.connect()
        
        schema_path = os.path.join(self.artifacts_dir, 'schema.json')
        with open(schema_path, 'r') as f:
            schema = json.load(f)
            
        cols = [col for col in schema["columns"].keys() if col != "col_time"]
        profiles = {}
        import glob
        features_path = glob.glob(os.path.join(self.data_dir, 'features', '**', '*.parquet'), recursive=True)
        
        for col in cols:
            query = f"""
            WITH lagged AS (
                SELECT {col} as val, {col} - lag({col}) OVER (PARTITION BY simulationRun ORDER BY col_time) as diff
                FROM read_parquet({features_path})
            )
            SELECT avg(val) as mean, stddev_pop(val) as std_dev, min(val) as min_val, max(val) as max_val,
                   approx_quantile(val, 0.25) as p25, approx_quantile(val, 0.50) as p50, approx_quantile(val, 0.75) as p75,
                   count(distinct val) as distinct_vals, kurtosis(val) as kurtosis, skewness(val) as skewness,
                   stddev_pop(diff) as noise_level, (sum(CASE WHEN diff = 0 THEN 1 ELSE 0 END)::DOUBLE / count(*)) as plateau_ratio
            FROM lagged
            """
            
            df = con.execute(query).df()
            row = df.iloc[0]
            
            profiles[col] = {
                "statistics": {
                    "mean": float(row["mean"]) if not df["mean"].isnull().iloc[0] else 0.0,
                    "std_dev": float(row["std_dev"]) if not df["std_dev"].isnull().iloc[0] else 0.0,
                    "min_val": float(row["min_val"]) if not df["min_val"].isnull().iloc[0] else 0.0,
                    "max_val": float(row["max_val"]) if not df["max_val"].isnull().iloc[0] else 0.0,
                    "quantiles": {"p25": float(row["p25"]) if not df["p25"].isnull().iloc[0] else 0.0, "p50": float(row["p50"]) if not df["p50"].isnull().iloc[0] else 0.0, "p75": float(row["p75"]) if not df["p75"].isnull().iloc[0] else 0.0},
                    "skewness": float(row["skewness"]) if not df["skewness"].isnull().iloc[0] else 0.0,
                    "distinct_values": int(row["distinct_vals"]),
                    "noise_level": float(row["noise_level"]) if not df["noise_level"].isnull().iloc[0] else 0.0,
                    "plateau_ratio": float(row["plateau_ratio"]) if not df["plateau_ratio"].isnull().iloc[0] else 0.0,
                    "has_plateaus": bool(row["plateau_ratio"] > 0.05) if not df["plateau_ratio"].isnull().iloc[0] else False
                },
                "evidence_ids": {"mean": f"ev_s2_{col}_mean", "std_dev": f"ev_s2_{col}_stddev", "distribution": f"ev_s2_{col}_dist", "dynamics": f"ev_s2_{col}_dyn"}
            }
            self.logger.log(stage="S2_Profile", action="profile_column", details=f"Profiled {col}", evidence_ids=list(profiles[col]["evidence_ids"].values()))
            
        with open(os.path.join(self.artifacts_dir, 'profiles.json'), 'w') as f:
            json.dump(profiles, f, indent=2)
            
        print("[S2] Profiling complete. profiles.json generated.")

if __name__ == "__main__":
    ProfilingEngine().run()
