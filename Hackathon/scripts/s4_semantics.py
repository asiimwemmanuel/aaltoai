import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.logger import DecisionLogger

class SemanticEngine:
    def __init__(self, artifacts_dir='artifacts'):
        self.artifacts_dir = artifacts_dir
        self.logger = DecisionLogger(os.path.join(artifacts_dir, 'decision_log.jsonl'))

    def run(self):
        print("[S4] Starting Semantic Inference Engine...")
        
        with open(os.path.join(self.artifacts_dir, 'profiles.json'), 'r') as f:
            profiles = json.load(f)
        with open(os.path.join(self.artifacts_dir, 'relations.json'), 'r') as f:
            relations = json.load(f)
            
        leaders = {rel['value']['leader'] for rel in relations if rel['value']['leader'] != 'concurrent'}
        semantics_output = {}
        
        for col_id, profile in profiles.items():
            is_leader = col_id in leaders
            has_plateaus = profile['statistics'].get('has_plateaus', False)
            
            if is_leader or has_plateaus:
                structural_guess = "Manipulated Variable (Actuator/Valve)"
                confidence = "high"
            else:
                structural_guess = "Measured Variable (Sensor)"
                confidence = "low"
                
            prompt = f"""
System: You are an autonomous data agent analyzing an undocumented industrial process.
Task: Infer the physical role of column '{col_id}'.

Statistical Evidence:
- Standard Deviation: {profile['statistics'].get('std_dev', 0)} (Evidence ID: {profile['evidence_ids'].get('std_dev', '')})
- Has Plateaus: {has_plateaus} (Evidence ID: {profile['evidence_ids'].get('dynamics', '')})
- Is a leading indicator for other sensors: {is_leader}

Structural Pre-Analysis: Based on the math, this behaves like a {structural_guess}.

Instructions:
1. Validate or refine the Structural Pre-Analysis.
2. Propose a specific functional role.
3. If you lack evidence, you MUST declare 'low' confidence.

Output strictly as JSON: {{"role": string, "confidence": "high"|"low", "evidence_ids": [string]}}
"""
            ev_ids = list(profile['evidence_ids'].values())
            semantics_output[col_id] = {
                "inferred_role": structural_guess, 
                "confidence": confidence,
                "evidence_ids": ev_ids,
                "human_override": None,
                "debug_prompt_generated": prompt.strip()
            }
            
            self.logger.log(
                stage="S4_Semantics", 
                action="infer_role", 
                details=f"Inferred {structural_guess} for {col_id}", 
                evidence_ids=ev_ids,
                confidence=confidence,
                model="Structural_Pre_Analysis_Mock"
            )
            
        with open(os.path.join(self.artifacts_dir, 'semantics.json'), 'w') as f:
            json.dump(semantics_output, f, indent=2)
            
        print("[S4] Semantic Inference complete. semantics.json generated.")

if __name__ == "__main__":
    SemanticEngine().run()
