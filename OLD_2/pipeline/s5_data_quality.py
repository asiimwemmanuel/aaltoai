import os
import sys
import json
import duckdb
import argparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.lib.dq_engine import DataQualityEngine
from pipeline.lib.rule_compiler import RuleCompiler

class DataQualityMonitor:
    def __init__(self, artifacts_dir='artifacts', contracts_dir='contracts', data_dir='data'):
        self.artifacts_dir = artifacts_dir
        self.contracts_dir = contracts_dir
        self.data_dir = data_dir

    def run(self, batch_id='simulationRun=2.0'):
        print("[S5] Starting Data Quality Trust Gate...")
        
        schema_path = os.path.join(self.contracts_dir, 'schema.json')
        if not os.path.exists(schema_path):
            schema_path = os.path.join(self.artifacts_dir, 'schema.json')

        engine = DataQualityEngine(
            schema_path=schema_path,
            profiles_path=os.path.join(self.artifacts_dir, 'profiles.json'),
            decision_log_path=os.path.join(self.artifacts_dir, 'decision_log.jsonl')
        )
        
        compiler = RuleCompiler(schema_path=schema_path)
        
        con = duckdb.connect()
        print(f"[S5] Evaluating Data Quality for incoming batch ({batch_id})...")
        
        # Load the data for the given batch
        query = f"SELECT * FROM '{self.data_dir}/features/{batch_id}/*.parquet'"
        try:
            df = con.execute(query).df()
        except Exception as e:
            print(f"[S5] ERROR: Could not load data for batch {batch_id}: {e}")
            return
            
        # Compile some natural language rules
        rules = compiler.compile_rules([
            'col_009 must remain within operational limits',
            'Reactor pressure must stay below 2900'
        ])
        
        report = engine.check_batch(df, batch_id=batch_id, compiled_rules=rules)
        
        # S5 must write to contracts/dq_report.json
        os.makedirs(self.contracts_dir, exist_ok=True)
        with open(os.path.join(self.contracts_dir, 'dq_report.json'), 'w') as f:
            json.dump(report, f, indent=2)
            
        trust_verdict = report['trust_verdict']
        print(f"[S5] Data Quality Gate complete. Verdict: {trust_verdict}.")
        if trust_verdict == "UNTRUSTED":
            print("[S5] PIPELINE STOPPED: Data is not reliable.")
        print("[S5] dq_report.json generated in contracts folder.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="S5 Data Quality Trust Gate")
    parser.add_argument("--batch-id", default="simulationRun=2.0", help="The batch ID (e.g. simulationRun=2.0) to evaluate")
    args = parser.parse_args()
    
    DataQualityMonitor().run(batch_id=args.batch_id)
