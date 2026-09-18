import duckdb
import json
import os
import shutil
import sys

# Add parent directory to path to import core
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.logger import DecisionLogger

class IngestionEngine:
    def __init__(self, csv_path='te_process.csv', artifacts_dir='artifacts', data_dir='data'):
        self.csv_path = csv_path
        self.artifacts_dir = artifacts_dir
        self.data_dir = data_dir
        self.logger = DecisionLogger(os.path.join(artifacts_dir, 'decision_log.jsonl'))

    def run(self, limit_runs=2):
        print(f"[S1] Starting Ingestion Engine. Limit runs: {limit_runs if limit_runs else 'FULL 6GB RUN'}")
        con = duckdb.connect()
        
        features_dir = os.path.join(self.data_dir, 'features')
        labels_dir = os.path.join(self.data_dir, 'labels')
        
        if os.path.exists(features_dir): shutil.rmtree(features_dir)
        if os.path.exists(labels_dir): shutil.rmtree(labels_dir)
        os.makedirs(features_dir, exist_ok=True)
        os.makedirs(labels_dir, exist_ok=True)
        os.makedirs(self.artifacts_dir, exist_ok=True)
        
        columns = con.execute(f"SELECT * FROM read_csv_auto('{self.csv_path}') LIMIT 1").df().columns.tolist()
        xmeas_cols = [c for c in columns if c.startswith('xmeas_')]
        xmv_cols = [c for c in columns if c.startswith('xmv_')]
        
        schema = {
            "dataset_metadata": {
                "total_columns_processed": len(xmeas_cols) + len(xmv_cols),
                "time_column_id": "col_time"
            },
            "columns": {"col_time": {"original_name": "sample", "data_type": "int64", "cardinality": "incremental"}}
        }
        
        col_mapping = {"sample": "col_time"}
        col_counter = 1
        
        for c in xmeas_cols + xmv_cols:
            col_id = f"col_{col_counter:03d}"
            schema["columns"][col_id] = {"original_name": c, "data_type": "float64", "cardinality": "continuous"}
            col_mapping[c] = col_id
            col_counter += 1
            
        schema_path = os.path.join(self.artifacts_dir, 'schema.json')
        with open(schema_path, 'w') as f:
            json.dump(schema, f, indent=2)
            
        where_clause = f"WHERE simulationRun <= {limit_runs}" if limit_runs else ""
        features_select = "simulationRun, sample as col_time, " + ", ".join([f"{c} as {col_mapping[c]}" for c in xmeas_cols + xmv_cols])
        labels_select = "simulationRun, sample as col_time, faultNumber, fault_status, source"
        
        con.execute(f"COPY (SELECT {features_select} FROM read_csv_auto('{self.csv_path}') {where_clause}) TO '{features_dir}' (FORMAT PARQUET, PARTITION_BY (simulationRun));")
        con.execute(f"COPY (SELECT {labels_select} FROM read_csv_auto('{self.csv_path}') {where_clause}) TO '{labels_dir}' (FORMAT PARQUET, PARTITION_BY (simulationRun));")
        
        self.logger.log(stage="S1_Ingest", action="schema_and_parquet_generation", details=f"Processed {len(schema['columns'])-1} features. Limit: {limit_runs}")
        print("[S1] Ingestion complete. Data partitioned securely.")

if __name__ == "__main__":
    # For development, we keep limit_runs=2. For production demo, pass None.
    IngestionEngine(csv_path='docs/te_process.csv' if os.path.exists('docs/te_process.csv') else 'data/te_process.csv').run(limit_runs=2)
