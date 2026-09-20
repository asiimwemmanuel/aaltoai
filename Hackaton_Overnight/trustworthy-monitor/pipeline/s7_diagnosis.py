"""S7 Diagnosis and critique.

The decisions are deterministic: the numbered chain, the critique checks, the refusal to
diagnose on untrusted data, and the confidence level. The model, through
trust.gateway.call_model(), only writes a plain-language narrative and proposes alternative
explanations. It is never asked when S5 refused the data, and if it is unavailable the
deterministic diagnosis is still written -- but marked "degraded": true, logged as a
config_change, and the process exits with DEGRADED_EXIT (3), so a run without a model
cannot pass for a run with one. Only --no-model (asked for) and the stub provider
(built for exactly this) exit 0 without a narrative.

Reads:  artifacts/drift_events.json, optional artifacts/dq_report.json, optional artifacts/semantics.json
Writes: artifacts/diagnosis.json (a list with one diagnosis, the shape contracts/diagnosis.schema.json accepts)

Run from the repository root:  python pipeline/s7_diagnosis.py
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from trust.decision_log import DecisionLog
from trust.gateway import GateViolation, call_model, load_config

STAGE = "S7_diagnosis"
# Same convention as S4: the artifact was written, but no model took part.
DEGRADED_EXIT = 3
LEVELS = ["low", "medium", "high"]
STATUSES = ("inferred", "assumed", "uncertain")
MAX_ALTERNATIVES = 3

NARRATIVE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "alternative_explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "explanation": {"type": "string"},
                    "supported_if": {"type": "string"},
                },
                "required": ["explanation", "supported_if"],
            },
        },
        "epistemic_status": {"enum": list(STATUSES)},
    },
    "required": ["summary", "steps", "alternative_explanations", "epistemic_status"],
}

NARRATIVE_INSTRUCTIONS = (
    "Explain this detected change to a non-specialist in a short summary and two to four plain "
    "sentences. Then propose up to three alternative explanations, each with the observation that "
    "would support it. Use only the facts in this payload: the checks and the confidence were "
    "already decided and must not be contradicted. If a role is missing for a column, describe the "
    "column by its behaviour only. epistemic_status: 'inferred' follows from the payload, 'assumed' "
    "is a working assumption, 'uncertain' means the payload leaves it open."
)

DEFAULTS = {
    "single_signal_share": 0.6,
    "concentration_high": 0.5,
    "concentration_medium": 0.3,
    "top_signals_in_text": 3,
}


def read_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        return None


def read_roles(data):
    if isinstance(data, dict):
        items = [(k, v) for k, v in data.items() if isinstance(v, dict)]
    elif isinstance(data, list):
        items = [(e.get("col_id"), e) for e in data if isinstance(e, dict)]
    else:
        return {}
    roles = {}
    for col_id, entry in items:
        role = entry.get("role") or entry.get("inferred_role") or entry.get("role_hypothesis")
        if col_id and role:
            confidence = entry.get("confidence")
            ids = entry.get("evidence_ids", [])
            roles[col_id] = {
                "role": str(role),
                "confidence": confidence if confidence in LEVELS else "low",
                "evidence_ids": [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else [],
            }
    return roles


def read_gate(report):
    if not report:
        return None
    return {
        "verdict": str(report["trust_verdict"]).lower(),
        "batch_id": report.get("batch_id"),
        "failures": report.get("failures", []),
    }


def pick_event(drift, event_id):
    events = drift["events"]
    if not events:
        raise SystemExit("No events in the drift file: nothing to diagnose.")
    if event_id is None:
        return events[0]
    for event in events:
        if event["event_id"] == event_id:
            return event
    raise SystemExit(f"Event {event_id} not found in the drift file.")


def evidence_of(drift, event, kind):
    wanted = set(event["evidence_ids"])
    for item in drift["evidence"]:
        if item["kind"] == kind and item["evidence_id"] in wanted:
            return item
    return None


def describe_kind(event):
    directions = [s["direction"] for s in event["ranked_signals"]]
    if sum(d == "more_variable" for d in directions) > len(directions) / 2:
        return "variance_increase"
    return {"abrupt": "abrupt_shift", "gradual": "gradual_drift"}.get(event["type"], "unclassified")


KIND_WORDS = {
    "abrupt_shift": "Abrupt shift",
    "gradual_drift": "Gradual drift",
    "variance_increase": "Increase in variability",
    "unclassified": "Unclassified change",
}


def signals_text(ranked, roles, top_n):
    parts = []
    for s in ranked[:top_n]:
        role = roles.get(s["col_id"], {}).get("role")
        label = f"{s['col_id']} ({role})" if role else s["col_id"]
        parts.append(f"{label} {s['share']:.1%} {s['direction'].replace('_', ' ')}")
    return "; ".join(parts)


def lower(level, steps=1):
    return LEVELS[max(0, LEVELS.index(level) - steps)]


def build_critiques(event, gate, params):
    top = event["ranked_signals"][0]
    critiques = []

    if gate is None:
        critiques.append({"check": "top signal fails a data quality check", "outcome": "not_applicable",
                          "effect": "none", "note": "No S5 report was provided."})
    else:
        hit = [f for f in gate["failures"] if f.get("target_col") == top["col_id"]]
        if hit:
            critical = any(f.get("severity") == "CRITICAL" for f in hit)
            critiques.append({"check": "top signal fails a data quality check", "outcome": "raised",
                              "effect": "refused_diagnosis" if critical else "lowered_confidence",
                              "note": f"S5 flagged {top['col_id']}: " + ", ".join(f["check_type"] for f in hit)})
        else:
            critiques.append({"check": "top signal fails a data quality check", "outcome": "passed",
                              "effect": "none"})

    if top["share"] >= params["single_signal_share"]:
        critiques.append({"check": "one signal carries almost all the blame", "outcome": "raised",
                          "effect": "lowered_confidence",
                          "note": f"{top['col_id']} holds {top['share']:.1%}; typical of a sensor problem."})
    else:
        critiques.append({"check": "one signal carries almost all the blame", "outcome": "passed",
                          "effect": "none"})

    critiques.append({"check": "another known fault matches almost as well", "outcome": "not_applicable",
                      "effect": "none", "note": "No signature library is configured (decision D3)."})

    if event["severity"] == "low":
        critiques.append({"check": "weak detection margin", "outcome": "raised",
                          "effect": "lowered_confidence",
                          "note": "The score barely exceeded its limit; nearly invisible faults look like this."})
    else:
        critiques.append({"check": "weak detection margin", "outcome": "passed", "effect": "none"})
    return critiques


def rate_confidence(event, gate, roles, critiques, params):
    ranked = event["ranked_signals"]
    top_n = params["top_signals_in_text"]
    factors = []

    factors.append(("detection margin", event["severity"],
                    f"S6 rated the detection margin as {event['severity']}."))

    top_share = sum(s["share"] for s in ranked[:top_n])
    if top_share >= params["concentration_high"]:
        level = "high"
    elif top_share >= params["concentration_medium"]:
        level = "medium"
    else:
        level = "low"
    factors.append(("blame concentration", level,
                    f"The top {top_n} signals hold {top_share:.1%} of the blame."))

    if gate is None:
        factors.append(("data quality", "medium", "No S5 report was provided, so data trust is not verified."))
    else:
        level = {"trusted": "high", "degraded": "medium", "untrusted": "low"}[gate["verdict"]]
        factors.append(("data quality", level, f"S5 verdict is {gate['verdict']}."))

    known = [roles[s["col_id"]]["confidence"] for s in ranked[:top_n] if s["col_id"] in roles]
    if known:
        factors.append(("signal roles", min(known, key=LEVELS.index),
                        "Role confidence of the top signals, taken from S4."))
    else:
        factors.append(("signal roles", "medium", "Sensor roles are unavailable (no S4 output)."))

    weakest = min(factors, key=lambda f: LEVELS.index(f[1]))
    level, weakest_link = weakest[1], weakest[0]
    reasons = [f[2] for f in factors]

    for c in critiques:
        if c["outcome"] == "raised" and c["effect"] == "lowered_confidence":
            new_level = lower(level)
            if new_level != level:
                level, weakest_link = new_level, f"critique: {c['check']}"
            reasons.append(f"Critique raised: {c['check']}.")
    return {"level": level, "reasons": reasons, "weakest_link": weakest_link}


def change_our_mind(event, roles):
    top = event["ranked_signals"][0]["col_id"]
    items = [
        f"A frozen, missing or saturated reading on {top} in the S5 report would make this a sensor "
        "problem, not a process fault.",
    ]
    if event["end_sample"] is None:
        items.append("If the score returned below its limit and stayed there, this would be a transient "
                     "disturbance rather than a sustained change.")
    else:
        items.append(f"The event ended at sample {event['end_sample']}; a longer event would change the "
                     "assessment of severity.")
    items.append("A signature library built from labelled history could name the closest known fault and "
                 "its runner-up; none is configured, so only the behaviour is described (decision D3).")
    if not roles:
        items.append("Roles for the top signals from S4 (semantics.json, currently unavailable) would show "
                     "whether they belong to the same part of the process.")
    return items


def diagnose(drift, event, gate, roles, params):
    ranked = event["ranked_signals"]
    top_n = params["top_signals_in_text"]
    score = evidence_of(drift, event, "score_exceedance")
    attribution = evidence_of(drift, event, "signal_contribution")
    score_ids = [score["evidence_id"]] if score else []
    attr_ids = [attribution["evidence_id"]] if attribution else []
    limits_ids = [i for i in event["evidence_ids"] if i == "ev_s6_limits"]
    failure_ids = [f["evidence_id"] for f in gate["failures"]] if gate else []

    critiques = build_critiques(event, gate, params)
    refused = any(c["effect"] == "refused_diagnosis" for c in critiques) or (gate and gate["verdict"] == "untrusted")

    if gate is None:
        trust_statement = "No S5 report was provided, so data trust is not verified."
    else:
        trust_statement = f"S5 reports the data as {gate['verdict']} with {len(gate['failures'])} failed check(s)."

    chain = [
        {"step": 1, "statement": trust_statement, "evidence_ids": failure_ids},
        {"step": 2, "statement": f"The first change was at sample {event['start_sample']} and was "
                                 f"confirmed at sample {event['detected_at_sample']} "
                                 f"({event['statistic'].upper()} statistic, severity {event['severity']}).",
         "evidence_ids": score_ids},
        {"step": 3, "statement": "Signals contributing most: " + signals_text(ranked, {}, top_n) + ".",
         "evidence_ids": attr_ids},
    ]

    if refused:
        kind = "sensor_problem"
        bad = sorted({f.get("target_col") for f in (gate["failures"] if gate else []) if f.get("target_col")})
        summary = ("Data quality checks failed" + (f" on {', '.join(bad)}" if bad else "") +
                   ". No process fault is diagnosed: the affected sensors must be checked first.")
        chain.append({"step": 4, "statement": "Diagnosis refused: a broken sensor is not a process fault.",
                      "evidence_ids": failure_ids})
        confidence = {"level": "low", "reasons": [trust_statement], "weakest_link": "data quality"}
    else:
        kind = describe_kind(event)
        summary = (f"{KIND_WORDS[kind]} starting at sample {event['start_sample']} "
                   f"(detected at sample {event['detected_at_sample']}). Most responsible signals: "
                   f"{signals_text(ranked, roles, top_n)}.")
        role_ids = [i for s in ranked[:top_n] for i in roles.get(s["col_id"], {}).get("evidence_ids", [])]
        role_text = ("Signal roles come from S4." if role_ids or any(s["col_id"] in roles for s in ranked)
                     else "Sensor roles are unavailable, so the signals are described by behaviour only.")
        confidence = rate_confidence(event, gate, roles, critiques, params)
        chain += [
            {"step": 4, "statement": role_text, "evidence_ids": role_ids},
            {"step": 5, "statement": f"The change looks like: {KIND_WORDS[kind].lower()}.",
             "evidence_ids": score_ids + limits_ids},
            {"step": 6, "statement": "No signature library is configured, so no known fault is named "
                                     "(decision D3).", "evidence_ids": []},
            {"step": 7, "statement": f"Confidence is {confidence['level']}; weakest link: "
                                     f"{confidence['weakest_link']}.", "evidence_ids": score_ids + limits_ids},
        ]

    result = {
        "event_id": event["event_id"],
        "fault_description": {"kind": kind, "summary": summary},
        "ranked_signals": [
            {"col_id": s["col_id"], "share": s["share"],
             **({"role_hypothesis": roles[s["col_id"]]["role"],
                 "role_confidence": roles[s["col_id"]]["confidence"]} if s["col_id"] in roles else {})}
            for s in ranked
        ],
        "chain": chain,
        "confidence": confidence,
        "critiques": critiques,
        "what_would_change_our_mind": change_our_mind(event, roles),
        "status": "proposed",
    }
    if gate:
        result["data_trust"] = {"verdict": gate["verdict"]}
    return result


def clean_narrative(answer):
    """Keep only what a narrative may contain, or None if the answer is unusable."""
    if not isinstance(answer, dict) or answer.get("_stub") or "_unparsed" in answer:
        return None
    summary = answer.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    steps = answer.get("steps") if isinstance(answer.get("steps"), list) else []
    alternatives = []
    for alt in answer.get("alternative_explanations") or []:
        if isinstance(alt, dict) and isinstance(alt.get("explanation"), str) and alt["explanation"].strip():
            supported_if = alt.get("supported_if")
            alternatives.append({
                "explanation": alt["explanation"].strip(),
                "supported_if": supported_if if isinstance(supported_if, str) else "",
                "epistemic_status": "uncertain",
            })
    status = answer.get("epistemic_status")
    return {
        "summary": summary.strip(),
        "steps": [s.strip() for s in steps if isinstance(s, str) and s.strip()],
        "alternative_explanations": alternatives[:MAX_ALTERNATIVES],
        "epistemic_status": status if status in STATUSES else "uncertain",
        "source": "model",
        "model_call_id": answer.get("_call_id"),
    }


def request_narrative(result, event, roles, llm_config, log):
    """Ask the model to explain a diagnosis that is already decided. Returns (narrative, reason)."""
    top = event["ranked_signals"][:DEFAULTS["top_signals_in_text"]]
    payload = {
        "event_summary": {
            "kind": result["fault_description"]["kind"],
            "change_type": event["type"],
            "start_sample": event["start_sample"],
            "detected_at_sample": event["detected_at_sample"],
            "end_sample": event["end_sample"],
            "severity": event["severity"],
            "statistic": event["statistic"],
            "data_trust": result.get("data_trust", {}).get("verdict", "not_verified"),
            "confidence": result["confidence"]["level"],
        },
        "ranked_contributions": [
            {"col_id": s["col_id"], "share": s["share"], "direction": s["direction"],
             **({"role": roles[s["col_id"]]["role"]} if s["col_id"] in roles else {})}
            for s in top
        ],
        "check_definitions": [
            {"check": c["check"], "outcome": c["outcome"], "effect": c["effect"]}
            for c in result["critiques"]
        ],
        "instructions": NARRATIVE_INSTRUCTIONS,
    }
    try:
        answer = call_model("diagnosis_narrative", payload, NARRATIVE_SCHEMA,
                            stage=STAGE, config=llm_config, log=log)
    except GateViolation as exc:
        return None, f"gate refused the payload: {exc}"
    except OSError as exc:
        return None, f"model endpoint unreachable ({exc})"
    except (ValueError, KeyError) as exc:
        # Missing API key / unknown provider (ValueError) or a malformed provider
        # response (KeyError): "no model", not a crash of this stage.
        return None, f"model not usable ({exc})"
    narrative = clean_narrative(answer)
    return narrative, None if narrative else "the model returned no usable narrative"


def to_contract(result, narrative):
    """Shape one diagnosis the way contracts/diagnosis.schema.json (Form B) accepts it."""
    level = result["confidence"]["level"]
    evidence = list(dict.fromkeys(i for step in result["chain"] for i in step["evidence_ids"]))
    entry = {
        "diagnosis_id": f"diag_{result['event_id']}",
        "drift_event_id": result["event_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "root_cause_hypothesis": result["fault_description"]["summary"],
        "confidence": level,
        "epistemic_status": "uncertain" if level == "low" else "inferred",
        "supporting_evidence": evidence,
        **{k: v for k, v in result.items() if k != "confidence"},
        "confidence_detail": result["confidence"],
    }
    if narrative:
        entry["narrative"] = {k: narrative[k] for k in
                              ("summary", "steps", "epistemic_status", "source", "model_call_id")}
        entry["alternative_explanations"] = narrative["alternative_explanations"]
    return entry


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drift", default="artifacts/drift_events.json")
    ap.add_argument("--semantics", default="artifacts/semantics.json")
    ap.add_argument("--dq-report", default="artifacts/dq_report.json",
                    help="S5 dq_report.json; if missing, data trust is reported as not verified")
    ap.add_argument("--event-id", help="event to diagnose (default: the first one)")
    ap.add_argument("--out", default="artifacts/diagnosis.json")
    ap.add_argument("--log", default="artifacts/decision_log.jsonl")
    ap.add_argument("--no-model", action="store_true", help="skip the model narrative")
    args = ap.parse_args()

    drift = read_json(args.drift)
    if drift is None:
        raise SystemExit(f"Cannot read {args.drift}: run S6 first.")
    roles = read_roles(read_json(args.semantics))
    gate = read_gate(read_json(args.dq_report))
    event = pick_event(drift, args.event_id)
    log = DecisionLog(args.log)

    result = diagnose(drift, event, gate, roles, DEFAULTS)

    narrative = None
    degraded_reason = None
    llm_config = None
    if result["fault_description"]["kind"] == "sensor_problem":
        print("[S7] Diagnosis refused on data quality grounds: the model is not asked to explain it.")
    elif not args.no_model:
        llm_config = load_config(os.environ.get("LLM_CONFIG"))
        narrative, reason = request_narrative(result, event, roles, llm_config, log)
        if reason:
            degraded_reason = reason
            print(f"[S7] No model narrative: {reason}")

    entry = to_contract(result, narrative)
    if degraded_reason:
        # The model was supposed to take part and did not. The deterministic
        # diagnosis stands, but the artifact says so and the run does not exit 0.
        entry["degraded"] = True
        llm = llm_config["llm"]
        log.append(stage=STAGE, kind="config_change",
                   summary=(f"S7 ran DEGRADED for {result['event_id']}: {llm['provider']}/"
                            f"{llm['model']} produced no narrative ({degraded_reason}). "
                            f"The diagnosis is the deterministic chain only; no model "
                            f"explanation and no alternative explanations were obtained."),
                   context={"degraded": True, "provider": llm["provider"],
                            "model": llm["model"], "reason": degraded_reason})
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump([entry], f, indent=2)

    log.append(stage=STAGE, kind="diagnosis",
               summary=f"{result['event_id']}: {result['fault_description']['kind']}, "
                       f"confidence {entry['confidence']}",
               subject=[s["col_id"] for s in result["ranked_signals"][:DEFAULTS["top_signals_in_text"]]],
               evidence_ids=entry["supporting_evidence"],
               confidence=entry["confidence"], epistemic_status=entry["epistemic_status"])

    detail = result["confidence"]
    print(f"{result['event_id']}: {result['fault_description']['kind']}, "
          f"confidence {detail['level']} (weakest link: {detail['weakest_link']}) -> {args.out}")
    print(result["fault_description"]["summary"])

    if degraded_reason:
        print(f"[S7] DEGRADED: no model narrative ({degraded_reason}). "
              f"diagnosis.json is marked degraded and this is in the decision log.",
              file=sys.stderr)
        if llm_config["llm"]["provider"] != "stub":
            return DEGRADED_EXIT
    return 0


if __name__ == "__main__":
    sys.exit(main())
