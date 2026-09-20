"""S4 Semantic inference: what role does each column play?

Reads:  artifacts/profiles.json, artifacts/relations.json
Writes: artifacts/semantics.json

The role of each column is asked of the model through trust.gateway.call_model(),
one call per column, from a payload generated out of the profiles and relations.
When no model answers (provider stub, Ollama down, payload refused by the gate,
unusable answer), that column keeps the statistical pre-analysis and says so:
source "structural_heuristic". The pipeline never stops for lack of a model.
"""
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trust.decision_log import DecisionLog
from trust.gateway import GateViolation, call_model, load_config

STAGE = "S4_semantics"
LEVELS = ("high", "medium", "low")
STATUSES = ("inferred", "assumed", "uncertain")
CLASSES = ("measured", "manipulated", "undetermined")
MAX_RELATIONS = 6

STRUCTURAL_LABELS = {
    "manipulated": "Manipulated Variable (Actuator/Valve)",
    "measured": "Measured Variable (Sensor)",
}

ROLE_SCHEMA = {
    "type": "object",
    "properties": {
        "role": {"type": "string"},
        "structural_class": {"enum": list(CLASSES)},
        "confidence": {"enum": list(LEVELS)},
        "epistemic_status": {"enum": list(STATUSES)},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["role", "confidence", "epistemic_status", "evidence_ids", "reasoning"],
}

INSTRUCTIONS = (
    "Infer the functional role of the single column described in column_summaries. "
    "Use only the statistics and relations in this payload. "
    "Choose structural_class from candidate_roles. "
    "Cite evidence_ids exactly as they appear in the payload; never invent an id. "
    "If the evidence does not support a confident answer, use confidence 'low' and "
    "epistemic_status 'assumed' or 'uncertain'. "
    "epistemic_status: 'inferred' follows from the cited evidence, 'assumed' is a working "
    "assumption, 'uncertain' means the evidence is thin or conflicting."
)


def structural_guess(is_leader, has_plateaus):
    """Statistical pre-analysis: returns (structural_class, confidence, basis)."""
    if is_leader:
        return "manipulated", "high", "it leads other columns in the lagged correlations"
    if has_plateaus:
        return "manipulated", "high", "it shows plateaus (discrete steps)"
    return "measured", "low", "it neither leads other columns nor shows plateaus"


def relations_for(col_id, relations):
    """The strongest relations involving this column, capped so the payload stays a summary."""
    mine = [r for r in relations if col_id in r.get("subject", [])]
    mine.sort(key=lambda r: abs(r.get("value", {}).get("correlation", 0)), reverse=True)
    return mine[:MAX_RELATIONS]


def build_payload(col_id, profile, related, guess, basis):
    """Generate the outbound payload from the profile. Nothing here is hand-written per dataset."""
    return {
        "column_summaries": {
            col_id: {"statistics": profile["statistics"], "evidence_ids": profile["evidence_ids"]}
        },
        "relation_summaries": [
            {"evidence_id": r["evidence_id"], "kind": r.get("kind"),
             "subject": r.get("subject"), "value": r.get("value")}
            for r in related
        ],
        "candidate_roles": list(CLASSES),
        "instructions": f"{INSTRUCTIONS} The statistical pre-analysis suggests '{guess}' because {basis}.",
    }


def interpret(answer, allowed_ids, guess):
    """Turn a model answer into a semantics entry, or None if it cannot be used.

    Evidence ids the model cites that were not in the payload are dropped. A claim left
    without evidence is written as assumed with low confidence, as the contract requires.
    """
    if not isinstance(answer, dict) or answer.get("_stub") or "_unparsed" in answer:
        return None
    role = answer.get("role")
    if not isinstance(role, str) or not role.strip():
        return None

    confidence = answer.get("confidence") if answer.get("confidence") in LEVELS else "low"
    status = answer.get("epistemic_status") if answer.get("epistemic_status") in STATUSES else "uncertain"
    cited = answer.get("evidence_ids") if isinstance(answer.get("evidence_ids"), list) else []
    evidence_ids = [i for i in dict.fromkeys(cited) if i in allowed_ids]
    if not evidence_ids:
        confidence = "low"
        status = "assumed"

    structural = answer.get("structural_class")
    reasoning = answer.get("reasoning")
    return {
        "inferred_role": role.strip(),
        "structural_class": structural if structural in CLASSES else guess,
        "confidence": confidence,
        "epistemic_status": status,
        "evidence_ids": evidence_ids,
        "reasoning": reasoning if isinstance(reasoning, str) else "",
    }


class SemanticEngine:
    def __init__(self, artifacts_dir='artifacts', llm_config=None):
        self.artifacts_dir = artifacts_dir
        self.log = DecisionLog(os.path.join(artifacts_dir, 'decision_log.jsonl'))
        self.llm_config = llm_config or load_config(os.environ.get("LLM_CONFIG"))

    def _actor(self):
        llm = self.llm_config["llm"]
        return f"{llm['provider']}/{llm['model']}"

    def _ask_model(self, col_id, payload, allowed_ids, guess):
        """Returns (entry or None, reason). A reason means the model should not be tried again."""
        try:
            answer = call_model("semantic_role_inference", payload, ROLE_SCHEMA,
                                stage=STAGE, config=self.llm_config, log=self.log)
        except GateViolation as exc:
            return None, f"gate refused the payload for {col_id}: {exc}"
        except OSError as exc:
            return None, f"model endpoint unreachable ({exc})"
        if isinstance(answer, dict) and answer.get("_stub"):
            return None, "provider is 'stub', which returns no role"
        entry = interpret(answer, allowed_ids, guess)
        if entry is not None:
            entry["model_call_id"] = answer.get("_call_id")
        return entry, None

    def run(self):
        print("[S4] Starting Semantic Inference Engine...")

        with open(os.path.join(self.artifacts_dir, 'profiles.json'), 'r') as f:
            profiles = json.load(f)
        with open(os.path.join(self.artifacts_dir, 'relations.json'), 'r') as f:
            relations = json.load(f)

        leaders = {rel['value']['leader'] for rel in relations if rel['value']['leader'] != 'concurrent'}
        semantics_output = {}
        use_model = True
        from_model = 0

        for col_id, profile in profiles.items():
            is_leader = col_id in leaders
            has_plateaus = profile['statistics'].get('has_plateaus', False)
            guess, guess_confidence, basis = structural_guess(is_leader, has_plateaus)
            related = relations_for(col_id, relations)
            payload = build_payload(col_id, profile, related, guess, basis)
            allowed_ids = set(profile['evidence_ids'].values()) | {r['evidence_id'] for r in related}

            entry = None
            if use_model:
                entry, reason = self._ask_model(col_id, payload, allowed_ids, guess)
                if reason:
                    use_model = False
                    print(f"[S4] No model for the remaining columns: {reason}")
                    self.log.append(stage=STAGE, kind="flag",
                                    summary=f"Model not used from {col_id} onward: {reason}. "
                                            f"Columns keep the statistical pre-analysis.")

            if entry is not None:
                entry["source"] = "model"
                from_model += 1
            else:
                entry = {
                    "inferred_role": STRUCTURAL_LABELS[guess],
                    "structural_class": guess,
                    "confidence": guess_confidence,
                    "epistemic_status": "inferred",
                    "evidence_ids": list(profile['evidence_ids'].values()),
                    "reasoning": f"Statistical pre-analysis only, no model answered: {basis}.",
                    "source": "structural_heuristic",
                }
            entry["human_override"] = None
            entry["debug_prompt_generated"] = json.dumps(payload, indent=2)
            semantics_output[col_id] = entry

            self.log.append(
                stage=STAGE, kind="inference",
                summary=f"{col_id}: {entry['inferred_role']}",
                actor_type="model" if entry["source"] == "model" else "system",
                actor_name=self._actor() if entry["source"] == "model" else None,
                subject=[col_id],
                evidence_ids=entry["evidence_ids"],
                confidence=entry["confidence"],
                epistemic_status=entry["epistemic_status"],
            )

        with open(os.path.join(self.artifacts_dir, 'semantics.json'), 'w') as f:
            json.dump(semantics_output, f, indent=2)

        print(f"[S4] Semantic Inference complete. semantics.json generated "
              f"({from_model}/{len(semantics_output)} columns answered by the model).")


if __name__ == "__main__":
    SemanticEngine().run()
