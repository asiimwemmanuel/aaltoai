"""S4 Semantic inference: what role does each column play?

Reads:  artifacts/profiles.json, artifacts/relations.json
Writes: artifacts/semantics.json

The role of each column is asked of the model through trust.gateway.call_model(),
one call per column, from a payload generated out of the profiles and relations.
When no model answers (provider stub, Ollama down, payload refused by the gate,
unusable answer), that column keeps the statistical pre-analysis and says so:
source "structural_heuristic".

If NO column was answered by a model, the run is degraded and says so loudly:
the artifact carries a top-level "degraded": true, the decision log gets a
config_change entry, and the process exits with DEGRADED_EXIT (3). The artifact
is still written, so downstream stages can run, but nobody can mistake a
two-branch heuristic for 52 model inferences again. The stub provider is the one
configuration where no model answer is the expected outcome, so it is flagged
but exits 0.
"""
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trust.decision_log import DecisionLog
from trust.gateway import GateViolation, call_model, load_config

STAGE = "S4_semantics"
# Exit code for "ran, wrote the artifact, but no model answered a single column".
# Distinct from 1 (crash) so the orchestrator can tell the two apart.
DEGRADED_EXIT = 3
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


def structural_guess(is_leader, has_plateaus, plateau_separates=True):
    """Statistical pre-analysis: returns (structural_class, confidence, basis).

    This function's output is written into the prompt, so its confidence is not
    cosmetic: it is the strength of the hint the model is anchored by. The old
    version answered "manipulated, high" whenever has_plateaus was set, and S2
    set it for all 52 columns, so every column arrived at the model with a
    high-confidence answer already attached. Both a hosted model and a local one
    agreed with it and structural_class collapsed to a single class.

    So each branch now carries the confidence its evidence actually supports,
    and the case where the statistic separates nothing is a branch of its own
    rather than being silently read as "plateaus, therefore manipulated".
    """
    if is_leader:
        # Leading other columns in the lagged correlations is a directional
        # claim about this column specifically. It survives as the strong one.
        return "manipulated", "high", "it leads other columns in the lagged correlations"
    if not plateau_separates:
        return ("undetermined", "low",
                "the plateau statistic takes nearly the same value on every column here, "
                "so it distinguishes none of them, and nothing else in the profile speaks "
                "to this question")
    if has_plateaus:
        # One statistic, above a boundary the cohort itself drew. Suggestive,
        # not conclusive: medium is what one piece of evidence buys.
        return "manipulated", "medium", "it plateaus markedly more than the other columns"
    return "measured", "low", "it neither leads other columns nor plateaus more than its peers"


# Words that are ours, not an answer. On the full run 13 of 52 columns came back
# with `role` set to "structural_class", "semantic_role_inference" or "inference":
# the model echoing the field it was asked to fill, or the purpose of the call,
# instead of naming a role. A hosted model did the same thing in its own way.
# Nothing downstream could tell those from a real answer, so they were written
# into the artifact as though they were one.
#
# The list is built from our own vocabulary -- the keys we put in the payload and
# in ROLE_SCHEMA, plus the purpose string -- so it stays correct if the payload
# changes and carries no assumption about what the data is.
_OUR_OWN_WORDS = frozenset(
    list(ROLE_SCHEMA["properties"])
    + ["column_summaries", "relation_summaries", "candidate_roles", "instructions",
       "semantic_role_inference", "inference", "statistics", "evidence"]
)


def is_echo(role):
    """Did the model name a role, or hand one of our own words back to us?"""
    normalised = role.strip().lower().replace(" ", "_").replace("-", "_")
    return normalised in _OUR_OWN_WORDS


def relations_for(col_id, relations):
    """The strongest relations involving this column, capped so the payload stays a summary."""
    mine = [r for r in relations if col_id in r.get("subject", [])]
    mine.sort(key=lambda r: abs(r.get("value", {}).get("correlation", 0)), reverse=True)
    return mine[:MAX_RELATIONS]


def build_payload(col_id, profile, related, guess, basis, guess_confidence="low"):
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
        # The hint goes in with its own strength attached. Handing a model a bare
        # "the pre-analysis suggests X" reads as settled and it agrees; saying how
        # much the statistics actually support X leaves it free to disagree, which
        # is the whole point of asking it.
        "instructions": (f"{INSTRUCTIONS} A statistical pre-analysis suggests '{guess}' "
                         f"with {guess_confidence} confidence, because {basis}. Weigh that "
                         f"against the statistics and relations above; it is one input, "
                         f"not the answer, and disagreeing with it is a valid response."),
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
    structural = structural if structural in CLASSES else guess
    reasoning = answer.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, str) else ""

    inferred_role = role.strip()
    if is_echo(inferred_role):
        # An echo is not a wrong answer, it is an absent one. Keeping it at the
        # model's own confidence would put a field name in front of an operator
        # with "high" beside it. The structural class still stands on its own
        # evidence, so the entry survives -- it just stops claiming to name a role.
        reasoning = (f"The model returned '{inferred_role}', which is one of the field names "
                     f"in the request rather than a role, so no role was named here. "
                     f"The structural class below rests on the statistics, not on that answer. "
                     + reasoning).strip()
        inferred_role = STRUCTURAL_LABELS.get(structural, "Undetermined")
        confidence = "low"
        status = "uncertain"

    return {
        "inferred_role": inferred_role,
        "structural_class": structural,
        "confidence": confidence,
        "epistemic_status": status,
        "evidence_ids": evidence_ids,
        "reasoning": reasoning,
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
        except (ValueError, KeyError) as exc:
            # A missing API key or an unknown provider is raised by the gateway
            # as ValueError; a malformed provider response as KeyError. Neither
            # is a bug in this stage, and neither will fix itself on the next
            # column: treat it as "no model", not as a crash.
            return None, f"model not usable ({exc})"
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
        stop_reason = None

        for col_id, profile in profiles.items():
            is_leader = col_id in leaders
            has_plateaus = profile['statistics'].get('has_plateaus', False)
            # Profiles written before S2 recorded this default to True, which is
            # the old behaviour: absent information must not silently become
            # "the statistic separates nothing".
            plateau_separates = profile['statistics'].get('plateau_separates_cohort', True)
            guess, guess_confidence, basis = structural_guess(
                is_leader, has_plateaus, plateau_separates)
            related = relations_for(col_id, relations)
            payload = build_payload(col_id, profile, related, guess, basis, guess_confidence)
            allowed_ids = set(profile['evidence_ids'].values()) | {r['evidence_id'] for r in related}

            entry = None
            if use_model:
                entry, reason = self._ask_model(col_id, payload, allowed_ids, guess)
                if reason:
                    use_model = False
                    stop_reason = reason
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

        total = len(semantics_output)
        degraded = from_model == 0 and total > 0
        if degraded:
            # Top-level marker, deliberately not a column entry: every reader
            # already keeps only col_* keys (or dict values), and the schema
            # names this field explicitly.
            semantics_output["degraded"] = True

        with open(os.path.join(self.artifacts_dir, 'semantics.json'), 'w') as f:
            json.dump(semantics_output, f, indent=2)

        print(f"[S4] Semantic Inference complete. semantics.json generated "
              f"({from_model}/{total} columns answered by the model).")

        if not degraded:
            return 0

        reason = stop_reason or "the model returned no usable answer for any column"
        self.log.append(
            stage=STAGE, kind="config_change",
            summary=(f"S4 ran DEGRADED: {self._actor()} answered 0 of {total} columns "
                     f"({reason}). Every role in semantics.json is the two-branch "
                     f"statistical pre-analysis, not a model inference; its confidences "
                     f"describe that heuristic, not a model's certainty."),
            context={"degraded": True, "provider": self.llm_config["llm"]["provider"],
                     "model": self.llm_config["llm"]["model"], "reason": reason,
                     "columns_total": total, "columns_from_model": 0},
        )
        print(f"[S4] DEGRADED: no model answered any column ({reason}). "
              f"semantics.json is marked degraded and this is in the decision log.",
              file=sys.stderr)
        if self.llm_config["llm"]["provider"] == "stub":
            # The stub exists so a stage can be exercised with no model at all;
            # a degraded result is what it is for. Flagged, not failed.
            return 0
        return DEGRADED_EXIT


if __name__ == "__main__":
    sys.exit(SemanticEngine().run())
