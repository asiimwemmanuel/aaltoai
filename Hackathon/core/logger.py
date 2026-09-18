import json
import os
from datetime import datetime, timezone

class DecisionLogger:
    def __init__(self, log_path='artifacts/decision_log.jsonl'):
        self.log_path = log_path
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)

    def log(self, stage, action, details, evidence_ids=None, confidence=None, model=None):
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "action": action,
            "details": details,
            "evidence_ids": evidence_ids or [],
            "confidence": confidence,
            "model_used": model
        }
        with open(self.log_path, 'a') as f:
            f.write(json.dumps(record) + '\n')
