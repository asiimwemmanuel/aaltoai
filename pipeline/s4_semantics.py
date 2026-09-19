import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.lib.logger import DecisionLogger
from trust.gateway import call_model

class SemanticEngine:
    def __init__(self, artifacts_dir='artifacts'):
        self.artifacts_dir = artifacts_dir
        # Note: We don't pass this logger to call_model because Gateway has its own!
        self.logger = DecisionLogger(os.path.join(artifacts_dir, 'decision_log.jsonl'))

    def run(self):
        print("[S4] Starting Semantic Inference Engine via LLM Gateway (Per-Column)...")
        
        with open(os.path.join(self.artifacts_dir, 'profiles.json'), 'r') as f:
            profiles = json.load(f)
        with open(os.path.join(self.artifacts_dir, 'relations.json'), 'r') as f:
            relations = json.load(f)
            
        leaders = {rel['value']['leader'] for rel in relations if rel['value']['leader'] != 'concurrent'}
        
        schema_out = {
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "The specific physical role (e.g. 'Actuator Valve', 'Pressure Sensor', 'Temperature Sensor')"},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "epistemic_status": {"type": "string", "enum": ["inferred", "assumed", "uncertain"]}
            },
            "required": ["role", "confidence", "epistemic_status"]
        }
        
        semantics_output = {}
        
        # We only process the first 5 columns for testing to save time. 
        # Wait, the user wants the full run. Let's process all 52.
        
        print(f"[S4] Calling LLM for {len(profiles)} individual columns (this will take a couple of minutes)...")
        
        for idx, (col_id, profile) in enumerate(profiles.items()):
            is_leader = col_id in leaders
            has_plateaus = profile['statistics'].get('has_plateaus', False)
            ev_ids = list(profile['evidence_ids'].values())
            
            payload = {
                "column_summaries": {
                    col_id: {
                        "std_dev": profile['statistics'].get('std_dev', 0),
                        "has_plateaus": has_plateaus,
                        "is_leader": is_leader,
                    }
                }
            }
            
            purpose = (
                f"Determine the physical role of this single column. "
                f"Rule of thumb: If 'has_plateaus' is true or 'is_leader' is true, it is an 'Actuator Valve'. "
                f"Otherwise, it is a 'Sensor'. Be concise."
            )
            
            print(f"  -> Inferring {col_id} ({idx+1}/{len(profiles)})...", end=" ", flush=True)
            
            try:
                response = call_model(
                    purpose=purpose,
                    payload=payload,
                    schema_out=schema_out,
                    stage="S4_semantics"
                )
                
                semantics_output[col_id] = {
                    "inferred_role": response.get("role", "Unknown"),
                    "confidence": response.get("confidence", "low"),
                    "epistemic_status": response.get("epistemic_status", "uncertain"),
                    "evidence_ids": ev_ids,
                    "human_override": None
                }
                print(f"[{response.get('role')}]")
                
            except Exception as e:
                print(f"[FAILED: {e}]")
                semantics_output[col_id] = {
                    "inferred_role": "Unknown",
                    "confidence": "low",
                    "epistemic_status": "uncertain",
                    "evidence_ids": ev_ids,
                    "human_override": None
                }
                
        with open(os.path.join(self.artifacts_dir, 'semantics.json'), 'w') as f:
            json.dump(semantics_output, f, indent=2)
            
        print("[S4] Semantic Inference complete. semantics.json generated via real LLM!")

if __name__ == "__main__":
    SemanticEngine().run()
