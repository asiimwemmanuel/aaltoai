import duckdb
import json
import os
import shutil

def ingest_data(csv_path):
    print("Connecting to DuckDB...")
    con = duckdb.connect()
    
    print("Creating stratified sample (simulationRun <= 2) and exporting to Parquet...")
    
    # Clean up old data if exists
    if os.path.exists('data/features'):
        shutil.rmtree('data/features')
    if os.path.exists('data/labels'):
        shutil.rmtree('data/labels')
        
    os.makedirs('data/features', exist_ok=True)
    os.makedirs('data/labels', exist_ok=True)
    
    # Get all column names to create opaque col_ids
    print("Extracting schema...")
    columns = con.execute(f"SELECT * FROM read_csv_auto('{csv_path}') LIMIT 1").df().columns.tolist()
    
    xmeas_cols = [c for c in columns if c.startswith('xmeas_')]
    xmv_cols = [c for c in columns if c.startswith('xmv_')]
    
    # Create opaque IDs
    schema = {
        "dataset_metadata": {
            "total_columns_processed": len(xmeas_cols) + len(xmv_cols),
            "time_column_id": "col_time"
        },
        "columns": {}
    }
    
    col_mapping = {}
    col_counter = 1
    
    schema["columns"]["col_time"] = {
        "original_name": "sample",
        "data_type": "int64",
        "cardinality": "incremental"
    }
    col_mapping["sample"] = "col_time"
    
    for c in xmeas_cols + xmv_cols:
        col_id = f"col_{col_counter:03d}"
        schema["columns"][col_id] = {
            "original_name": c, 
            "data_type": "float64",
            "cardinality": "continuous"
        }
        col_mapping[c] = col_id
        col_counter += 1
        
    # Write schema to contracts
    os.makedirs('contracts', exist_ok=True)
    with open('contracts/schema.json', 'w') as f:
        json.dump(schema, f, indent=2)
    print("schema.json generated in contracts folder.")
    
    # Export queries
    features_select = "simulationRun, sample as col_time, " + ", ".join([f"{c} as {col_mapping[c]}" for c in xmeas_cols + xmv_cols])
    labels_select = "simulationRun, sample as col_time, faultNumber, fault_status, source"
    
    # Export FEATURES
    print("Exporting Features... (This prevents faultNumber from leaking into the detection path)")
    con.execute(f"""
        COPY (
            SELECT {features_select}
            FROM read_csv_auto('{csv_path}')
            WHERE simulationRun <= 2
        ) TO 'data/features' (FORMAT PARQUET, PARTITION_BY (simulationRun));
    """)
    
    # Export LABELS
    print("Exporting Labels... (Kept separately for evaluation only)")
    con.execute(f"""
        COPY (
            SELECT {labels_select}
            FROM read_csv_auto('{csv_path}')
            WHERE simulationRun <= 2
        ) TO 'data/labels' (FORMAT PARQUET, PARTITION_BY (simulationRun));
    """)
    
    print("Ingestion complete. Stratified sample saved to data/features and data/labels.")

if __name__ == "__main__":
    ingest_data("te_process.csv")
