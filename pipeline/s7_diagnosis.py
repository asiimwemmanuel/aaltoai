"""S7 Diagnosis and critique (deterministic template, no model call).

Reads:  contracts/drift_events.json, optional S5 dq_report.json, optional contracts/semantics.json
Writes: contracts/diagnosis.json

Run from the Hackathon/ folder:  python scripts/s7_diagnosis.py
"""
import argparse
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LEVELS = ["low", "medium", "high"]

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
        role = entry.get("role") or entry.get("role_hypothesis")
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
        
        # Build payload for the LLM
        payload = {
            "event_summary": {
                "kind": kind,
                "start_sample": event['start_sample'],
                "severity": event['severity'],
            },
            "ranked_contributions": [
                {
                    "col_id": r["col_id"],
                    "share": r["share"],
                    "direction": r["direction"],
                    "physical_role": roles.get(r["col_id"], {}).get("inferred_role", "Unknown") if roles else "Unknown"
                }
                for r in ranked[:top_n]
            ]
        }
        
        from trust.gateway import call_model
        
        try:
            print("  -> Calling LLM to write the Root Cause Analysis...")
            response = call_model(
                purpose="Write a short, professional Root Cause Analysis summary (1-2 sentences) describing this fault event and the likely physical cause.",
                payload=payload,
                schema_out={"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
                stage="S7_Diagnosis"
            )
            summary = response.get("summary", "LLM failed to generate a summary.")
        except Exception as e:
            print(f"  -> LLM call failed: {e}")
            summary = (f"{kind.capitalize()} starting at sample {event['start_sample']} "
                       f"(detected at sample {event['detected_at_sample']}). "
                       f"Most responsible signals: {signals_text(ranked, roles, top_n)}.")

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


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--drift", default="contracts/drift_events.json")
    ap.add_argument("--semantics", default="contracts/semantics.json")
    ap.add_argument("--dq-report", help="S5 dq_report.json")
    ap.add_argument("--event-id", help="event to diagnose (default: the first one)")
    ap.add_argument("--out", default="contracts/diagnosis.json")
    args = ap.parse_args()

    drift = read_json(args.drift)
    if drift is None:
        raise SystemExit(f"Cannot read {args.drift}: run S6 first.")
    roles = read_roles(read_json(args.semantics))
    gate = read_gate(read_json(args.dq_report))
    event = pick_event(drift, args.event_id)

    result = diagnose(drift, event, gate, roles, DEFAULTS)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"{result['event_id']}: {result['fault_description']['kind']}, "
          f"confidence {result['confidence']['level']} (weakest link: {result['confidence']['weakest_link']}) "
          f"-> {args.out}")
    print(result["fault_description"]["summary"])


if __name__ == "__main__":
    main()
