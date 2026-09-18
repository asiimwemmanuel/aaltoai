import json
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.logger import DecisionLogger

class DiagnosisEngine:
    def __init__(self, artifacts_dir='artifacts'):
        self.artifacts_dir = artifacts_dir
        self.logger = DecisionLogger(os.path.join(artifacts_dir, 'decision_log.jsonl'))

    def run(self):
        print("[S7] Starting LLM Diagnosis Engine...")
        
        try:
            with open(os.path.join(self.artifacts_dir, 'drift_event.json'), 'r') as f:
                events = json.load(f)
        except FileNotFoundError:
            print("No drift events found. Run S6 first.")
            return
            
        diagnoses = []
        diag_counter = 1
        
        # Simulating the LLM Gatekeeper response for the Hackathon
        for event in events:
            col = event['col_id']
            ev_id = f"ev_s7_{diag_counter:04d}"
            
            # Here we would normally build a prompt containing relations.json and semantics.json
            # and pass it to call_model(). For now, we mock the JSON.
            diagnosis = {
                "diagnosis_id": ev_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "drift_event_id": event['evidence_id'],
                "root_cause_hypothesis": f"Based on S4 Semantics and S3 Relations, the LLM determines {col} is the root cause.",
                "confidence": "high",
                "supporting_evidence": [event['evidence_id']]
            }
            diagnoses.append(diagnosis)
            
            self.logger.log(
                stage="S7_Diagnosis", 
                action="generate_diagnosis", 
                details=f"Diagnosed root cause related to {col}", 
                evidence_ids=[ev_id]
            )
            diag_counter += 1
            
        with open(os.path.join(self.artifacts_dir, 'diagnosis.json'), 'w') as f:
            json.dump(diagnoses, f, indent=2)
            
        print(f"[S7] Diagnosis complete. Generated {len(diagnoses)} diagnoses. diagnosis.json generated.")

if __name__ == "__main__":
    DiagnosisEngine().run()
