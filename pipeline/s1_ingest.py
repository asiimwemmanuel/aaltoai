import duckdb
import json
import os
import shutil
import sys

# Add parent directory to path to import core
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.lib.logger import DecisionLogger

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
        
        columns = con.sql(f"SELECT * FROM read_csv_auto('{self.csv_path}') LIMIT 0").columns
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
        
        # Row order in data/features is not cosmetic: it is load-bearing.
        # S6 rebuilds sub-run boundaries from it, by looking for col_time
        # resetting to 1. DuckDB's default parallel writer splits one partition
        # across threads and writes the pieces in completion order, so the same
        # input produced four files on 19 Sep and five on 20 Sep, with a
        # partition that began at col_time=206 instead of 1. Two hundred and
        # ninety-five orphan rows were then glued onto the first sub-run, which
        # is a reference run, which is the PCA baseline. Every control limit
        # moved and the blamed signal changed. Nothing in the data was wrong;
        # only the order was, and nothing said so.
        #
        # One writer thread with insertion order preserved: same input, same
        # bytes, same boundaries, every time. The ingest is slower and runs
        # once. S6 now also refuses to score a partition that does not begin
        # at col_time=1, so if this ever regresses it stops instead of lying.
        con.execute("SET preserve_insertion_order = true;")
        con.execute("SET threads TO 1;")

        con.execute(f"COPY (SELECT {features_select} FROM read_csv_auto('{self.csv_path}') {where_clause}) TO '{features_dir}' (FORMAT PARQUET, PARTITION_BY (simulationRun));")
        con.execute(f"COPY (SELECT {labels_select} FROM read_csv_auto('{self.csv_path}') {where_clause}) TO '{labels_dir}' (FORMAT PARQUET, PARTITION_BY (simulationRun));")
        
        self.logger.log(stage="S1_Ingest", action="schema_and_parquet_generation", details=f"Processed {len(schema['columns'])-1} features. Limit: {limit_runs}")
        print("[S1] Ingestion complete. Data partitioned securely.")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", action="store_true", help="Run in dev mode (subset of data)")
    args = parser.parse_args()
    
    limit = 2 if args.dev else None
    csv_path = 'docs/te_process.csv' if os.path.exists('docs/te_process.csv') else 'data/te_process.csv'
    IngestionEngine(csv_path=csv_path).run(limit_runs=limit)
