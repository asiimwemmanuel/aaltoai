"""Endpoints behind the live cockpit.

Two groups of functions live in this file and they must never be confused with
each other, so they are separated by a banner and named accordingly:

    get_*        read artifacts and Parquet, answer the browser. No model.
    ask_*        build a derived summary and call trust.gateway.call_model().

A `get_` function may return thousands of numbers, because they go to an SVG
renderer over localhost. An `ask_` function may not: everything it sends is an
aggregate produced by series_reader.window_stats() or copied from an artifact
a stage already wrote. The gate in trust/gateway.py re-checks that claim on
every call rather than trusting this docstring, which is the point of having a
gate at all.

`ui/server.py` only dispatches to these; it holds no cockpit logic itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ui import series_reader
from ui.series_reader import SeriesUnavailable
from trust import actions as trust_actions

ROOT = Path(__file__).resolve().parent.parent

ARTIFACTS = ROOT / "artifacts"
DRIFT_RUNS_DIR = ARTIFACTS / "drift_events"
MACHINE_CONTEXT_DIR = ARTIFACTS / "machine_context"

# How much of each thing may reach a model. Every number here is a ceiling on
# the payload, and the gate's own limits sit below them as the real backstop.
MAX_SIGNALS = 5
MAX_CONTEXT_CHARS = 4000
MAX_HISTORY_TURNS = 6
MAX_HISTORY_CHARS = 600
PRE_WINDOW = 30  # samples of lead-in used when summarizing an event window


class NotFound(Exception):
    """A requested run, event or channel does not exist."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(*candidates: Path):
    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
    return None


def _one(query: dict, key: str, default: str | None = None) -> str | None:
    values = query.get(key)
    if not values:
        return default
    return values[0]


def _safe_id(value: str, fallback: str) -> str:
    cleaned = "".join(c for c in (value or "") if c.isalnum() or c in ("-", "_"))
    return cleaned[:64] or fallback


# ==========================================================================
# get_*  --  the data path. Reads disk, answers the browser, calls no model.
# ==========================================================================

def get_runs(query: dict) -> dict:
    """Every scored run, ordered so the ones worth looking at come first."""
    try:
        available = {r["run_id"]: r for r in series_reader.available_runs()}
    except SeriesUnavailable:
        available = {}

    runs = []
    for path in sorted(DRIFT_RUNS_DIR.glob("*.json")):
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        run_id = (data.get("batch") or {}).get("run_id") or path.stem
        events = data.get("events") or []
        severities = {e.get("severity") for e in events}
        worst = "high" if "high" in severities else ("medium" if "medium" in severities else
                 ("low" if severities else "none"))
        runs.append({
            "run_id": run_id,
            "n_samples": (data.get("batch") or {}).get("n_samples")
                          or available.get(run_id, {}).get("n_samples"),
            "n_events": len(events),
            "worst_severity": worst,
            "first_event_sample": min((e.get("detected_at_sample", 0) for e in events), default=None),
            "has_series": run_id in available,
        })

    runs.sort(key=lambda r: (-r["n_events"], r["run_id"]))
    return {"runs": runs, "count": len(runs)}


def get_drift(query: dict) -> dict:
    """One run's detector output: score series, limits, events, evidence.

    The whole run is sent at once and the browser reveals it up to the current
    tick. Fetching per tick would be a request per simulated sample, and the
    replay has to stay smooth on a machine that may be doing real work.
    """
    run_id = _one(query, "run_id")
    if not run_id:
        raise NotFound("run_id is required")
    run_id = _safe_id(run_id, "")
    data = _read_json(DRIFT_RUNS_DIR / f"{run_id}.json")
    if data is None:
        combined = _read_json(ARTIFACTS / "drift_events.json")
        if isinstance(combined, dict) and (combined.get("batch") or {}).get("run_id") == run_id:
            data = combined
    if data is None:
        raise NotFound(f"no scored drift output for run {run_id!r}")
    return data


def get_series(query: dict) -> dict:
    """Raw channel values for the charts. This is the function the gate exists
    to keep away from the model, so it is worth saying plainly: what it returns
    is raw data, it is served only to localhost, and no caller in this codebase
    passes its result to trust.gateway."""
    run_id = _safe_id(_one(query, "run_id", "") or "", "")
    if not run_id:
        raise NotFound("run_id is required")
    cols = [c.strip() for c in (_one(query, "cols", "") or "").split(",") if c.strip()]
    if not cols:
        raise NotFound("cols is required (comma-separated col ids)")
    # The grid draws a trace for every channel at once, so the ceiling is the
    # column count, not a handful. This is local raw data on its way to a local
    # renderer; the limit that matters is the one in trust/gateway.py, and it
    # applies to a different path entirely.
    if len(cols) > 64:
        cols = cols[:64]

    def _int(name: str, default: int | None) -> int | None:
        raw = _one(query, name)
        if raw is None or raw == "":
            return default
        try:
            return int(float(raw))
        except ValueError:
            return default

    return series_reader.series(
        run_id,
        cols,
        to_sample=_int("to", None),
        from_sample=_int("from", 1) or 1,
        max_points=min(2000, _int("max_points", 1200) or 1200),
    )


def get_channels(query: dict) -> dict:
    """One row per channel for the overview grid.

    Merges what three stages wrote about each column into a single object, so
    the grid does not have to fetch and re-join four artifacts in the browser.
    Reader is widened both ways per CLAUDE.md: profiles and semantics may each
    arrive as a map keyed by col_id or as a list of objects carrying one.
    """
    schema = _read_json(ARTIFACTS / "schema.json") or {}
    profiles = _read_json(ARTIFACTS / "profiles.json") or {}
    semantics = _read_json(ARTIFACTS / "semantics.json") or {}

    profiles = _keyed_by_col(profiles)
    semantics = _keyed_by_col(semantics)

    col_ids = sorted(set(profiles) | set(semantics))
    if not col_ids:
        col_ids = [c for c in (schema.get("columns") or {}) if c != "col_time"]

    rows = []
    for col_id in col_ids:
        prof = profiles.get(col_id) or {}
        stats = prof.get("statistics") or {}
        sem = semantics.get(col_id) or {}
        override = sem.get("human_override") or {}
        rows.append({
            "col_id": col_id,
            "role": override.get("role") or sem.get("inferred_role") or sem.get("role") or "unclassified",
            "inferred_role": sem.get("inferred_role") or sem.get("role"),
            "structural_class": sem.get("structural_class"),
            "confidence": sem.get("confidence") or "low",
            "epistemic_status": sem.get("epistemic_status") or "uncertain",
            "overridden": bool(override.get("role")),
            "baseline": {
                "mean": stats.get("mean"),
                "std_dev": stats.get("std_dev"),
                "min_val": stats.get("min_val"),
                "max_val": stats.get("max_val"),
            },
            "evidence_ids": _evidence_id_list(sem.get("evidence_ids")) or _evidence_id_list(prof.get("evidence_ids")),
        })

    return {"channels": rows, "count": len(rows),
            "time_column_id": (schema.get("dataset_metadata") or {}).get("time_column_id", "col_time")}


def _keyed_by_col(container) -> dict:
    """Both artifact shapes, one reader. See CLAUDE.md: widen the reader."""
    if isinstance(container, dict):
        if any(k.startswith("col_") for k in container):
            return {k: v for k, v in container.items() if isinstance(v, dict)}
        for key in ("inferences", "columns", "profiles"):
            if isinstance(container.get(key), (list, dict)):
                return _keyed_by_col(container[key])
        return {}
    if isinstance(container, list):
        return {item["col_id"]: item for item in container
                if isinstance(item, dict) and item.get("col_id")}
    return {}


def _evidence_id_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value][:12]
    if isinstance(value, dict):
        return [str(v) for v in value.values()][:12]
    return []


# --------------------------------------------------------------------------
# The machine's own notes. A plain file the operator can open in any editor.
# --------------------------------------------------------------------------

CONTEXT_SEED = """# {machine_id}

Written by the operators of this unit, read by the assistant on every question.
Edit it freely: it is a plain Markdown file under artifacts/machine_context/.

## What this unit is

_Not yet described. Write the unit's name and what it does here._

## Normal behaviour

_What a good day looks like. Add notes as you learn them._

## Known history

_Past events, what they turned out to be, and what fixed them._
"""


def machine_context_path(machine_id: str) -> Path:
    return MACHINE_CONTEXT_DIR / f"{_safe_id(machine_id, 'machine_01')}.md"


def get_machine_context(query: dict) -> dict:
    machine_id = _safe_id(_one(query, "machine_id", "machine_01") or "machine_01", "machine_01")
    path = machine_context_path(machine_id)
    exists = path.exists()
    text = path.read_text(encoding="utf-8") if exists else CONTEXT_SEED.format(machine_id=machine_id)
    return {
        "machine_id": machine_id,
        "markdown": text,
        "exists": exists,
        "path": str(path.relative_to(ROOT)).replace("\\", "/"),
        "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                      .strftime("%Y-%m-%d %H:%M:%S UTC") if exists else None,
        "bytes": path.stat().st_size if exists else 0,
    }


def get_machines(query: dict) -> dict:
    items = []
    if MACHINE_CONTEXT_DIR.exists():
        for path in sorted(MACHINE_CONTEXT_DIR.glob("*.md")):
            items.append({
                "machine_id": path.stem,
                "bytes": path.stat().st_size,
                "updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                              .strftime("%Y-%m-%d %H:%M:%S UTC"),
            })
    return {"machines": items, "count": len(items)}


def put_machine_context(body: dict, log) -> dict:
    """Operator edit. The model never reaches this function: proposals come
    back as text for a human to approve, and the approval is what writes."""
    machine_id = _safe_id(body.get("machine_id") or "machine_01", "machine_01")
    markdown = body.get("markdown")
    by = (body.get("by") or "OP-01").strip()
    if not isinstance(markdown, str):
        raise ValueError("markdown is required")
    if len(markdown) > 200_000:
        raise ValueError("markdown is too large (max 200 KB)")

    MACHINE_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    path = machine_context_path(machine_id)
    previous_bytes = path.stat().st_size if path.exists() else 0
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(markdown, encoding="utf-8")
    tmp.replace(path)

    entry_id = log.append(
        stage="UI_machine_context",
        kind="context_write",
        summary=(f"Operator saved the context notes for {machine_id} "
                 f"({previous_bytes} -> {len(markdown.encode('utf-8'))} bytes)."),
        actor_type="human",
        actor_name=by,
        context={"machine_id": machine_id},
    )
    return {"ok": True, "entry_id": entry_id, "machine_id": machine_id,
            "bytes": len(markdown.encode("utf-8")), "updated_at": _now()}


# ==========================================================================
# ask_*  --  the model path. Builds derived summaries, calls the one gateway.
# ==========================================================================

HYPOTHESIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hypothesis", "confidence_pct", "confidence", "epistemic_status", "evidence_ids"],
    "properties": {
        "hypothesis": {"type": "string", "description": "At most two sentences."},
        "confidence_pct": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"enum": ["high", "medium", "low"]},
        "epistemic_status": {"enum": ["inferred", "assumed", "uncertain"]},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "leading_channel": {"type": "string"},
        "suggested_checks": {"type": "array", "items": {"type": "string"}},
        "what_would_change_my_mind": {"type": "string"},
    },
}

CHAT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "confidence_pct", "epistemic_status", "answer_kind"],
    "properties": {
        "answer": {"type": "string"},
        "answer_kind": {
            "enum": ["process", "meta", "operator_report", "cannot_answer"],
            "description": ("process = a claim about the plant, the only kind a confidence "
                            "percentage means anything on. meta = a question about the "
                            "assistant itself. operator_report = the operator is telling you "
                            "what they found. cannot_answer = outside what the evidence covers."),
        },
        "confidence_pct": {"type": "integer", "minimum": 0, "maximum": 100},
        "confidence": {"enum": ["high", "medium", "low"]},
        "epistemic_status": {"enum": ["inferred", "assumed", "uncertain"]},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "next_step": {"type": "string"},
        # The assistant cannot read the plant. When the operator asks about
        # something outside the payload it was handed, it says so HERE instead of
        # guessing, and the server runs the request locally and asks again. See
        # trust/actions.py for why this does not widen what a model can reach.
        "data_request": {
            "type": "array",
            "maxItems": 4,
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "col_id": {"type": "string"},
                    "run_id": {"type": "string"},
                    "start": {"type": "integer"},
                    "end": {"type": "integer"},
                },
                "required": ["action"],
            },
        },
    },
}

CONTEXT_APPEND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["markdown_append", "epistemic_status"],
    "properties": {
        "markdown_append": {"type": "string", "description": "Markdown to append. No preamble."},
        "epistemic_status": {"enum": ["inferred", "assumed", "uncertain"]},
        "confidence_pct": {"type": "integer", "minimum": 0, "maximum": 100},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
}

# Said once, reused by all three calls. Written as a list of plain sentences so
# it is obvious at a glance that nothing here describes what the process is:
# the model is told how to answer, never what it is looking at.
BASE_INSTRUCTIONS = [
    "You are given aggregate statistics only. You have never seen the underlying records.",
    "Ground every claim in an evidence_id listed in the payload, and list the ones you used.",
    "Never invent an evidence_id, a channel id or a number that is not given above.",
    "Channel ids are opaque. A prior_role_inference is an earlier guess by a model, not a fact.",
    "If the evidence does not support a confident answer, say that plainly, lower "
    "confidence_pct and set epistemic_status to 'uncertain'. An honest 'I cannot tell "
    "from this, which channel do you mean?' is a better answer than a guess.",
    "Write for a plant operator under time pressure: two sentences, no hedging prose, "
    "no restating the numbers back.",
]


_EVIDENCE_IDS: set[str] = set()


def known_evidence_ids(refresh: bool = False) -> set[str]:
    """Every evidence id that actually exists in the artifacts.

    Built once per process from the four artifacts that carry evidence objects.
    Used to check the model's citations rather than trusting them.
    """
    if _EVIDENCE_IDS and not refresh:
        return _EVIDENCE_IDS

    def _harvest(container):
        if isinstance(container, list):
            for item in container:
                _harvest(item)
        elif isinstance(container, dict):
            if isinstance(container.get("evidence_id"), str):
                _EVIDENCE_IDS.add(container["evidence_id"])
            for key, value in container.items():
                if key == "evidence_ids":
                    if isinstance(value, list):
                        _EVIDENCE_IDS.update(v for v in value if isinstance(v, str))
                    elif isinstance(value, dict):
                        _EVIDENCE_IDS.update(v for v in value.values() if isinstance(v, str))
                elif isinstance(value, (dict, list)):
                    _harvest(value)

    for name in ("profiles.json", "relations.json", "dq_report.json", "drift_events.json"):
        _harvest(_read_json(ARTIFACTS / name))
    for path in sorted(DRIFT_RUNS_DIR.glob("*.json"))[:200]:
        _harvest(_read_json(path))
    return _EVIDENCE_IDS


def check_citations(ids) -> dict:
    """Split what a model cited into what exists and what it made up.

    A model that invents an evidence id has produced an unsourced claim, and the
    operator is entitled to see that rather than a citation that looks fine
    until someone goes looking for it. Observed in testing: an answer cited
    "machine_context" and a bare column id as if they were evidence objects.
    """
    cited = [str(i) for i in (ids or []) if isinstance(i, str)]
    pool = known_evidence_ids()
    verified = [i for i in cited if i in pool]
    return {
        "verified": verified,
        "unverifiable": [i for i in cited if i not in pool],
        "all_verified": bool(cited) and len(verified) == len(cited),
    }


def _word_confidence(pct) -> str:
    try:
        value = int(pct)
    except (TypeError, ValueError):
        return "low"
    return "high" if value >= 75 else ("medium" if value >= 45 else "low")


def _find_event(drift: dict, event_id: str) -> dict:
    """`latest` resolves to the most recent detection of the shift.

    The home screen has a chat box but no event selected, and making the
    operator pick one before they can ask a question would be exactly the kind
    of ceremony this screen exists to remove.
    """
    events = drift.get("events") or []
    if event_id in ("", "latest", None):
        if not events:
            raise NotFound("this shift has no detected events to talk about")
        return max(events, key=lambda e: e.get("detected_at_sample") or e.get("start_sample") or 0)
    for event in events:
        if event.get("event_id") == event_id:
            return event
    raise NotFound(f"no event {event_id!r} in this run")


def _event_context(drift: dict, event: dict) -> dict:
    """Scalars describing one detection. Every field is already in the artifact
    S6 wrote; nothing is recomputed from the series here."""
    detector = drift.get("detector") or {}
    limits = detector.get("limits") or {}
    statistic = event.get("statistic") or "t2"
    series = drift.get("score_series") or {}
    values = series.get(statistic) or []
    start = int(event.get("start_sample") or 1)
    end = int(event.get("end_sample") or start)
    window = values[max(0, start - 1):max(start, end)] or [0.0]
    limit = limits.get(statistic) or limits.get(f"limit_{statistic}") or 0.0

    return {
        "event_id": event.get("event_id"),
        "run_id": (drift.get("batch") or {}).get("run_id"),
        "detector_family": detector.get("family"),
        "statistic": statistic,
        "shape": event.get("type"),
        "severity": event.get("severity"),
        "start_sample": start,
        "detected_at_sample": event.get("detected_at_sample"),
        "end_sample": end,
        "duration_samples": max(1, end - start + 1),
        "control_limit": round(float(limit), 4),
        "peak_over_limit_ratio": round(max(window) / float(limit), 3) if limit else None,
        "evidence_ids": (event.get("evidence_ids") or [])[:8],
    }


def _signal_summaries(run_id: str, drift: dict, event: dict, channels: dict) -> list[dict]:
    """The top contributing channels, each described by aggregates only.

    Two sources, deliberately: the attribution share S6 already computed, and a
    window summary from series_reader.window_stats(). The second reads the
    Parquet, which is why this function returns eight numbers per channel and
    not the window itself.
    """
    out = []
    start = int(event.get("start_sample") or 1)
    end = int(event.get("end_sample") or start)

    for signal in (event.get("ranked_signals") or [])[:MAX_SIGNALS]:
        col_id = signal.get("col_id")
        if not col_id:
            continue
        row = channels.get(col_id) or {}
        entry = {
            "col_id": col_id,
            "attribution_share": signal.get("share"),
            "direction": signal.get("direction"),
            "prior_role_inference": row.get("inferred_role"),
            "prior_role_confidence": row.get("confidence"),
            "baseline": {k: v for k, v in (row.get("baseline") or {}).items() if v is not None},
            "evidence_ids": (row.get("evidence_ids") or [])[:4],
        }
        try:
            entry["during_event"] = series_reader.window_stats(run_id, col_id, start, end)
            entry["before_event"] = series_reader.window_stats(
                run_id, col_id, max(1, start - PRE_WINDOW), max(2, start - 1))
        except SeriesUnavailable:
            entry["during_event"] = None
            entry["before_event"] = None
        out.append(entry)

    # The attribution shares alone would let a model build a story out of four
    # channels that have nothing to do with each other. Telling it which of them
    # actually co-moves with the leader is the difference between one suspect
    # and five, and it costs one aggregate per channel.
    if out:
        leader = out[0]["col_id"]
        try:
            moves = {m["col_id"]: m for m in series_reader.co_movement(
                run_id, leader, [e["col_id"] for e in out[1:]],
                start=max(1, start - PRE_WINDOW), end=end)}
        except SeriesUnavailable:
            moves = {}
        for entry in out[1:]:
            entry["co_movement_with_leader"] = moves.get(entry["col_id"])
    return out


def _machine_context_text(machine_id: str) -> str:
    path = machine_context_path(machine_id)
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if len(text) <= MAX_CONTEXT_CHARS:
        return text
    # Keep the tail: the notes grow by appending, so the recent history is the
    # part most likely to matter, and the operator is told it was cut.
    return "...[earlier notes omitted]...\n" + text[-MAX_CONTEXT_CHARS:]


def _prepare(body: dict) -> tuple[str, dict, dict, list[dict], str]:
    run_id = _safe_id(body.get("run_id") or "", "")
    event_id = str(body.get("event_id") or "")
    machine_id = _safe_id(body.get("machine_id") or "machine_01", "machine_01")
    if not run_id:
        raise ValueError("run_id is required")

    drift = get_drift({"run_id": [run_id]})
    event = _find_event(drift, event_id)
    channels = {row["col_id"]: row for row in get_channels({})["channels"]}
    return (run_id,
            _event_context(drift, event),
            event,
            _signal_summaries(run_id, drift, event, channels),
            machine_id)


def _answer_of(result: dict, *keys: str) -> str:
    for key in keys:
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    unparsed = result.get("_unparsed")
    if isinstance(unparsed, str) and unparsed.strip():
        return unparsed.strip()
    return "The model did not return a usable answer."


def _call(purpose: str, payload: dict, schema: dict, stage: str, log):
    from trust.gateway import call_model, GateViolation
    try:
        return call_model(purpose=purpose, payload=payload, schema_out=schema,
                          stage=stage, log=log)
    except GateViolation as exc:
        raise ValueError(f"blocked by the trust gate: {exc}")


def ask_hypothesis(body: dict, log) -> dict:
    """One synthetic hypothesis for one detected event, with a confidence."""
    run_id, event_context, event, signals, machine_id = _prepare(body)

    payload = {
        "event_context": event_context,
        "sensor_summary": signals,
        "machine_context": _machine_context_text(machine_id),
        "instructions": BASE_INSTRUCTIONS + [
            "Name the most likely origin of this event and say why, in at most two sentences.",
            "leading_channel must be one of the col ids in sensor_summary, or omitted.",
            "suggested_checks: at most three concrete things an operator could verify now.",
            "what_would_change_my_mind: the one observation that would overturn this.",
        ],
    }

    result = _call("Hypothesis for one detected event", payload,
                   HYPOTHESIS_SCHEMA, "UI_cockpit_hypothesis", log)

    hypothesis = _answer_of(result, "hypothesis")
    pct = result.get("confidence_pct")
    if not isinstance(pct, int):
        pct = {"high": 80, "medium": 55, "low": 25}.get(result.get("confidence"), 25)
    citations = check_citations(result.get("evidence_ids"))
    cited = citations["verified"]

    entry_id = log.append(
        stage="UI_cockpit_hypothesis",
        kind="hypothesis",
        summary=f"Hypothesis for {event_context['event_id']} ({pct}%): {hypothesis}"[:4000],
        actor_type="model",
        actor_name="assistant",
        subject=[s["col_id"] for s in signals],
        confidence=_word_confidence(pct),
        epistemic_status=result.get("epistemic_status") or "uncertain",
        evidence_ids=cited or event_context.get("evidence_ids") or [],
        context={"run_id": run_id, "event_id": event_context["event_id"],
                 "machine_id": machine_id},
    )

    return {
        "entry_id": entry_id,
        "call_id": result.get("_call_id"),
        "event_id": event_context["event_id"],
        "hypothesis": hypothesis,
        "confidence_pct": pct,
        "confidence": _word_confidence(pct),
        "epistemic_status": result.get("epistemic_status") or "uncertain",
        "evidence_ids": cited,
        "citations": citations,
        "leading_channel": result.get("leading_channel"),
        "suggested_checks": (result.get("suggested_checks") or [])[:3],
        "what_would_change_my_mind": result.get("what_would_change_my_mind"),
        "payload_sent": payload,
    }


def get_model_status(_params: dict) -> dict:
    """Which model is answering, and what else could. Reads config, calls nothing."""
    from trust import model_switch
    return model_switch.status()


def put_model_switch(body: dict, log) -> dict:
    """Change the model layer from the cockpit.

    Deliverable 8 asks us to prove the model is swappable by configuration. Doing
    it from the operator screen, with the egress panel visible beside it, is the
    same proof performed rather than described.

    No key is accepted here. If the chosen provider needs one, the switch is
    refused with the name of the environment variable to export: a key posted
    from a browser ends up in a server log and a screen recording.
    """
    from trust import model_switch
    provider = (body.get("provider") or "").strip()
    if not provider:
        raise ValueError("provider is required")
    return model_switch.switch(
        provider=provider,
        model=(body.get("model") or None),
        log=log,
        by=(body.get("by") or "OP-01").strip(),
    )


def ask_chat(body: dict, log) -> dict:
    """The operator's own question, answered against the same summaries."""
    question = (body.get("question") or "").strip()
    if not question:
        raise ValueError("question is required")
    if len(question) > 2000:
        raise ValueError("question is too long (max 2000 characters)")

    run_id, event_context, event, signals, machine_id = _prepare(body)
    by = (body.get("by") or "OP-01").strip()

    focus_col = body.get("col_id")
    if focus_col and focus_col not in {s["col_id"] for s in signals}:
        channels = {row["col_id"]: row for row in get_channels({})["channels"]}
        row = channels.get(focus_col)
        if row:
            extra = {
                "col_id": focus_col,
                "attribution_share": None,
                "direction": "not ranked for this event",
                "prior_role_inference": row.get("inferred_role"),
                "prior_role_confidence": row.get("confidence"),
                "baseline": {k: v for k, v in (row.get("baseline") or {}).items() if v is not None},
                "evidence_ids": (row.get("evidence_ids") or [])[:4],
            }
            try:
                extra["during_event"] = series_reader.window_stats(
                    run_id, focus_col,
                    int(event.get("start_sample") or 1), int(event.get("end_sample") or 1))
            except SeriesUnavailable:
                extra["during_event"] = None
            signals = signals[:MAX_SIGNALS - 1] + [extra]

    history = []
    for turn in (body.get("history") or [])[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        role = "operator" if turn.get("role") == "operator" else "assistant"
        text = str(turn.get("text") or "")[:MAX_HISTORY_CHARS]
        if text:
            history.append({"role": role, "text": text})

    payload = {
        "question": question,
        "chat_history": history,
        "event_context": event_context,
        "sensor_summary": signals,
        "machine_context": _machine_context_text(machine_id),
        "instructions": BASE_INSTRUCTIONS + [
            "Answer the operator's question directly, in at most four sentences.",
            "If they ask what to do, give steps they can carry out at the panel.",
            # Every one of these exists because the assistant did the opposite in
            # testing. It answered "what is 3+3" with a confidence of 100% and a
            # suggestion to go check a valve, four turns running.
            "Set answer_kind honestly. A question about you, or about anything other than "
            "this plant, is not a 'process' answer: say so plainly in one sentence, do not "
            "attach plant advice to it, and omit next_step entirely.",
            "confidence_pct describes how sure you are of a claim about the plant. On a "
            "meta or off-topic answer there is no such claim -- return 0 and set "
            "epistemic_status to 'assumed'. A confidence of 100 is almost never honest.",
            "If the operator tells you they checked something, or fixed it, that is new "
            "information and answer_kind is 'operator_report'. Take it as true, say what it "
            "changes, and do NOT suggest they check the same thing again.",
            "next_step: the single most useful thing to do next, and only when it differs "
            "from what you have already suggested in this conversation. Repeating the same "
            "instruction every turn is worse than saying nothing. Omit it when in doubt.",
            "The unit notes describe specific channels by name. A statement about one "
            "channel is not evidence about a different one.",
        ],
    }

    # ------------------------------------------------------------------
    # Language into action, part 1: resolve what the operator NAMED.
    #
    # An operator types "channel 3". The artifacts say "col_003". Without this
    # the assistant is handed a question about something it has no record of,
    # and answers "I have no information on that" while the data sits on disk.
    # Whatever they named is fetched locally and attached before the first call.
    # ------------------------------------------------------------------
    mentioned = [c for c in trust_actions.resolve_channel_mentions(question)
                 if c not in {s_["col_id"] for s_ in signals}]
    fetched = trust_actions.run(
        [{"action": "channel_profile", "col_id": c} for c in mentioned])
    if fetched:
        payload["channels_the_operator_named"] = fetched

    # The menu of what it may ask for when it still lacks something. Names and
    # shapes only: no data crosses here, and the assistant cannot widen its own
    # access because it only ever picks an action name and its parameters.
    payload["available_actions"] = trust_actions.manifest()
    payload["instructions"] = payload["instructions"] + [
        "You cannot read the plant. If the operator asks about something not in "
        "this payload, do NOT guess and do NOT say you have no information: return "
        "data_request naming one of available_actions, and you will be asked again "
        "with the result attached.",
        "Only request what the question actually needs. Asking for many channels "
        "at once is scanning, not reasoning.",
    ]

    result = _call("Operator question in the cockpit chat", payload,
                   CHAT_SCHEMA, "UI_cockpit_chat", log)

    # ------------------------------------------------------------------
    # Language into action, part 2: fulfil what it asked for, once.
    #
    # One extra round trip, never a loop: a second request after the results are
    # in hand means the assistant is exploring rather than answering, and the
    # operator is waiting.
    # ------------------------------------------------------------------
    requested = result.get("data_request") if isinstance(result, dict) else None
    if requested:
        payload["requested_data"] = trust_actions.run(requested)
        payload["instructions"] = payload["instructions"] + [
            "The data you asked for is in requested_data. Answer from it now. "
            "If an entry has ok=false, say plainly that you could not retrieve it "
            "and what the operator could check instead. Do not request anything further.",
        ]
        result = _call("Operator question, second pass with retrieved data", payload,
                       CHAT_SCHEMA, "UI_cockpit_chat", log)

    answer = _answer_of(result, "answer")
    kind = result.get("answer_kind") or "process"
    pct = result.get("confidence_pct")
    if not isinstance(pct, int):
        pct = {"high": 80, "medium": 55, "low": 25}.get(result.get("confidence"), 25)
    # A percentage on an answer that makes no claim about the plant is noise, and
    # a suggestion attached to one is a non sequitur. Both are dropped here as
    # well as discouraged in the instructions: the model does not get to decide
    # whether its own confidence is meaningful.
    next_step = result.get("next_step")
    if kind != "process":
        pct = 0
        next_step = None
    citations = check_citations(result.get("evidence_ids"))
    cited = citations["verified"]

    entry_id = log.append(
        stage="UI_cockpit_chat",
        kind="qa",
        summary=f'Operator asked about {event_context["event_id"]}: "{question}" - {answer}'[:4000],
        actor_type="human",
        actor_name=by,
        subject=[s["col_id"] for s in signals],
        confidence=_word_confidence(pct),
        epistemic_status=result.get("epistemic_status") or "uncertain",
        evidence_ids=cited,
        context={"run_id": run_id, "event_id": event_context["event_id"],
                 "machine_id": machine_id},
    )

    return {
        "entry_id": entry_id,
        "call_id": result.get("_call_id"),
        "answer": answer,
        "confidence_pct": pct,
        "confidence": _word_confidence(pct),
        "epistemic_status": result.get("epistemic_status") or "uncertain",
        "evidence_ids": cited,
        "citations": citations,
        "next_step": next_step,
        "answer_kind": kind,
        "payload_sent": payload,
    }


def ask_context_append(body: dict, log) -> dict:
    """Draft a note for the machine's own file. Returns text; writes nothing.

    The write happens only when a human posts it back to put_machine_context(),
    which is what keeps the file something an operator owns rather than
    something that accumulates behind their back.
    """
    run_id, event_context, event, signals, machine_id = _prepare(body)
    outcome = str(body.get("outcome") or "").strip()[:1000]
    accepted = str(body.get("accepted_hypothesis") or "").strip()[:1000]

    payload = {
        "event_context": event_context,
        "sensor_summary": signals,
        "machine_context": _machine_context_text(machine_id),
        "chat_history": ([{"role": "assistant", "text": accepted}] if accepted else [])
                        + ([{"role": "operator", "text": outcome}] if outcome else []),
        "instructions": BASE_INSTRUCTIONS + [
            "Draft a short Markdown note recording this event for the unit's own notes file.",
            "Three lines at most: what was seen, which channels led it, what the operator concluded.",
            "Start with '### ' and the event id. Do not repeat what the notes already say.",
            "markdown_append must contain the note only, with no preamble and no code fence.",
        ],
    }

    result = _call("Draft a note for the machine context file", payload,
                   CONTEXT_APPEND_SCHEMA, "UI_machine_context", log)

    draft = _answer_of(result, "markdown_append")
    return {
        "call_id": result.get("_call_id"),
        "machine_id": machine_id,
        "markdown_append": draft,
        "epistemic_status": result.get("epistemic_status") or "uncertain",
        "confidence_pct": result.get("confidence_pct"),
        "evidence_ids": result.get("evidence_ids") or [],
        "written": False,
        "payload_sent": payload,
    }


# ==========================================================================
# Unit state: what this machine has been told, and what it expects next.
# Structured half of the memory. `<id>.md` stays the half a model reads.
# ==========================================================================

VALIDATION_REPORT = ROOT / "eval" / "validation_report.json"
ROUTE_DESKS = ["Instrumentation", "Mechanical", "Control room", "Process engineering"]


def unit_state_path(machine_id: str) -> Path:
    return MACHINE_CONTEXT_DIR / f"{_safe_id(machine_id, 'machine_01')}.state.json"


def _empty_state(machine_id: str) -> dict:
    return {"machine_id": machine_id, "updated_at": _now(),
            "channel_hypotheses": {}, "tickets": [], "carried_predictions": []}


def _load_state(machine_id: str) -> dict:
    path = unit_state_path(machine_id)
    if not path.exists():
        return _empty_state(machine_id)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _empty_state(machine_id)
    state.setdefault("channel_hypotheses", {})
    state.setdefault("tickets", [])
    state.setdefault("carried_predictions", [])
    state["machine_id"] = machine_id
    return state


def _prune(node):
    """Drop keys whose value is None, at any depth.

    An optional field that was not filled in should be absent, not present and
    null: `contracts/unit_state.schema.json` types these as strings, and writing
    null puts the artifact out of contract on the very first ticket that omits
    an event id.
    """
    if isinstance(node, dict):
        return {k: _prune(v) for k, v in node.items() if v is not None}
    if isinstance(node, list):
        return [_prune(v) for v in node]
    return node


def _save_state(state: dict) -> None:
    MACHINE_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    path = unit_state_path(state["machine_id"])
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(_prune(state), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _next_id(items: list, key: str, prefix: str) -> str:
    return f"{prefix}{len(items) + 1:04d}"


def get_unit_state(query: dict) -> dict:
    machine_id = _safe_id(_one(query, "machine_id", "machine_01") or "machine_01", "machine_01")
    state = _load_state(machine_id)
    state["route_desks"] = ROUTE_DESKS
    state["path"] = str(unit_state_path(machine_id).relative_to(ROOT)).replace("\\", "/")
    return state


def get_validation(query: dict) -> dict:
    """The ground truth report, for the one page that is allowed to show it.

    Served, never sent: this dict is returned to the browser and nothing in
    `ask_*` reads it. It is produced under `eval/` by `eval/validate_detection.py`,
    which is the only code in the repository permitted to open the label files.
    """
    if not VALIDATION_REPORT.exists():
        raise NotFound("eval/validation_report.json does not exist yet. Run: "
                       "python -m eval.validate_detection")
    report = json.loads(VALIDATION_REPORT.read_text(encoding="utf-8"))
    report["_source"] = "eval/validation_report.json"
    return report


def put_unit_state(body: dict, log) -> dict:
    """Every write to a unit's structured memory. Human-initiated, always.

    `op` says which of the three things is being written. A model can draft the
    text for any of them, but only this function writes, and it only runs
    because someone pressed a button.
    """
    machine_id = _safe_id(body.get("machine_id") or "machine_01", "machine_01")
    op = body.get("op")
    by = (body.get("by") or "OP-01").strip()
    state = _load_state(machine_id)

    if op == "channel_hypothesis":
        col_id = str(body.get("col_id") or "")
        physical_class = (body.get("physical_class") or "").strip()
        if not col_id or not physical_class:
            raise ValueError("col_id and physical_class are required")
        entry_id = log.append(
            stage="UI_unit_state", kind="human_review",
            summary=f"Operator accepted '{physical_class}' as the physical class of {col_id}.",
            actor_type="human", actor_name=by, subject=[col_id],
            confidence=_word_confidence(body.get("confidence_pct")),
            epistemic_status=body.get("epistemic_status") or "assumed",
            evidence_ids=body.get("evidence_ids") or [],
            context={"machine_id": machine_id},
        )
        state["channel_hypotheses"][col_id] = {
            "physical_class": physical_class,
            "reasoning": body.get("reasoning") or "",
            "confidence_pct": body.get("confidence_pct"),
            "epistemic_status": body.get("epistemic_status") or "assumed",
            "evidence_ids": body.get("evidence_ids") or [],
            "accepted_by": by, "accepted_at": _now(),
            "call_id": body.get("call_id"), "entry_id": entry_id,
        }
        _save_state(state)
        return {"ok": True, "entry_id": entry_id, "state": state}

    if op == "ticket":
        col_id = str(body.get("col_id") or "")
        reason = (body.get("reason") or "").strip()
        if not col_id or not reason:
            raise ValueError("col_id and reason are required")
        ticket_id = body.get("ticket_id")
        existing = next((t for t in state["tickets"] if t.get("ticket_id") == ticket_id), None)
        status = body.get("status") or "draft"
        if status not in ("draft", "forwarded", "closed"):
            raise ValueError("status must be draft, forwarded or closed")

        ticket = existing or {"ticket_id": _next_id(state["tickets"], "ticket_id", "tkt_"),
                              "created_at": _now()}
        ticket.update({
            "machine_id": machine_id, "col_id": col_id, "reason": reason,
            "run_id": body.get("run_id"), "event_id": body.get("event_id"),
            "physical_class": body.get("physical_class"),
            "evidence_ids": body.get("evidence_ids") or [],
            "route_to": body.get("route_to") or ROUTE_DESKS[0],
            "status": status, "by": by,
        })
        if body.get("closed_note"):
            ticket["closed_note"] = body["closed_note"]

        verb = {"draft": "drafted", "forwarded": "forwarded", "closed": "closed"}[status]
        ticket["entry_id"] = log.append(
            stage="UI_unit_state", kind="human_review",
            summary=(f"Operator {verb} ticket {ticket['ticket_id']} for {col_id} "
                     f"to {ticket['route_to']}: {reason}")[:4000],
            actor_type="human", actor_name=by, subject=[col_id],
            evidence_ids=ticket["evidence_ids"],
            context={"machine_id": machine_id, "run_id": ticket.get("run_id") or "",
                     "event_id": ticket.get("event_id") or ""},
        )
        if not existing:
            state["tickets"].append(ticket)
        _save_state(state)
        return {"ok": True, "ticket": ticket, "entry_id": ticket["entry_id"], "state": state}

    if op == "prediction":
        text = (body.get("text") or "").strip()
        if not text:
            raise ValueError("text is required")
        prediction = {
            "prediction_id": _next_id(state["carried_predictions"], "prediction_id", "pred_"),
            "created_at": _now(),
            "col_id": body.get("col_id"),
            "text": text,
            "from_run_id": body.get("run_id"),
            "confidence_pct": body.get("confidence_pct"),
            "epistemic_status": body.get("epistemic_status") or "assumed",
            "evidence_ids": body.get("evidence_ids") or [],
            "expires_after_shifts": int(body.get("expires_after_shifts") or 3),
            "shifts_seen": 0, "confirmed_times": 0, "accepted_by": by,
        }
        prediction["entry_id"] = log.append(
            stage="UI_unit_state", kind="context_write",
            summary=f"Operator carried a prediction to the next shift: {text}"[:4000],
            actor_type="human", actor_name=by,
            subject=[prediction["col_id"]] if prediction.get("col_id") else None,
            evidence_ids=prediction["evidence_ids"],
            confidence=_word_confidence(prediction["confidence_pct"]),
            epistemic_status=prediction["epistemic_status"],
            context={"machine_id": machine_id, "run_id": body.get("run_id") or ""},
        )
        state["carried_predictions"].append(prediction)
        _save_state(state)
        return {"ok": True, "prediction": prediction, "entry_id": prediction["entry_id"], "state": state}

    if op == "confirm_prediction":
        prediction_id = body.get("prediction_id")
        for prediction in state["carried_predictions"]:
            if prediction.get("prediction_id") == prediction_id:
                prediction["confirmed_times"] = int(prediction.get("confirmed_times") or 0) + 1
                entry_id = log.append(
                    stage="UI_unit_state", kind="human_review",
                    summary=(f"A carried prediction came true again "
                             f"({prediction['confirmed_times']}x): {prediction['text']}")[:4000],
                    actor_type="human", actor_name=by,
                    context={"machine_id": machine_id, "run_id": body.get("run_id") or ""},
                )
                prediction["last_entry_id"] = entry_id
                _save_state(state)
                return {"ok": True, "entry_id": entry_id, "state": state}
        raise NotFound(f"no prediction {prediction_id!r}")

    if op == "drop_prediction":
        prediction_id = body.get("prediction_id")
        before = len(state["carried_predictions"])
        state["carried_predictions"] = [p for p in state["carried_predictions"]
                                        if p.get("prediction_id") != prediction_id]
        if len(state["carried_predictions"]) == before:
            raise NotFound(f"no prediction {prediction_id!r}")
        entry_id = log.append(
            stage="UI_unit_state", kind="human_review",
            summary=f"Operator dropped carried prediction {prediction_id}.",
            actor_type="human", actor_name=by, context={"machine_id": machine_id},
        )
        _save_state(state)
        return {"ok": True, "entry_id": entry_id, "state": state}

    raise ValueError("op must be one of: channel_hypothesis, ticket, prediction, "
                     "confirm_prediction, drop_prediction")


PHYSICAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["physical_class", "confidence_pct", "epistemic_status", "reasoning"],
    "properties": {
        "physical_class": {"type": "string", "description": "Two or three words."},
        "reasoning": {"type": "string", "description": "One sentence, from the statistics only."},
        "confidence_pct": {"type": "integer", "minimum": 0, "maximum": 100},
        "epistemic_status": {"enum": ["inferred", "assumed", "uncertain"]},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "alternatives": {"type": "array", "items": {"type": "string"}},
        "what_would_settle_it": {"type": "string"},
    },
}

SHIFT_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["predictions"],
    "properties": {
        "predictions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "confidence_pct"],
                "properties": {
                    "col_id": {"type": "string"},
                    "text": {"type": "string"},
                    "confidence_pct": {"type": "integer", "minimum": 0, "maximum": 100},
                    "epistemic_status": {"enum": ["inferred", "assumed", "uncertain"]},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "shift_summary": {"type": "string"},
    },
}


def ask_physical(body: dict, log) -> dict:
    """What kind of thing behaves like this channel?

    The step from a statistical class to a physical one is the model's to make,
    from statistics, out loud, with a confidence and a way to be wrong. There is
    no table in this repository mapping a statistical class to a component, and
    there must never be: a lookup like that is exactly the hand-fed knowledge
    the challenge forbids, and it would forfeit the autonomy criterion outright.
    Nothing is written anywhere unless an operator accepts the answer.
    """
    run_id = _safe_id(body.get("run_id") or "", "")
    col_id = str(body.get("col_id") or "")
    machine_id = _safe_id(body.get("machine_id") or "machine_01", "machine_01")
    if not col_id:
        raise ValueError("col_id is required")

    channels = {row["col_id"]: row for row in get_channels({})["channels"]}
    row = channels.get(col_id)
    if row is None:
        raise NotFound(f"unknown channel {col_id!r}")

    summary = {
        "col_id": col_id,
        "prior_role_inference": row.get("inferred_role"),
        "prior_role_confidence": row.get("confidence"),
        "structural_class": row.get("structural_class"),
        "baseline": {k: v for k, v in (row.get("baseline") or {}).items() if v is not None},
        "evidence_ids": (row.get("evidence_ids") or [])[:4],
    }

    neighbours: list[dict] = []
    if run_id:
        try:
            summary["whole_run"] = series_reader.window_stats(run_id, col_id, 1, 10 ** 9)
        except SeriesUnavailable:
            pass
        others = [c for c in list(channels)[:60] if c != col_id]
        try:
            moves = series_reader.co_movement(run_id, col_id, others)
            neighbours = [m for m in moves if abs(m["correlation"]) >= 0.4][:6]
        except SeriesUnavailable:
            neighbours = []

    payload = {
        "sensor_summary": [summary],
        "relation_summaries": neighbours,
        "machine_context": _machine_context_text(machine_id),
        "instructions": BASE_INSTRUCTIONS + [
            "From these statistics alone, name the kind of equipment that behaves this way.",
            "physical_class is two or three words, the sort of thing an operator would say.",
            "You have no plant diagram and no channel names. If the statistics do not "
            "distinguish between two kinds of equipment, say the more general one and "
            "lower confidence_pct rather than picking.",
            # Observed: asked about one channel, the assistant answered with the
            # equipment the notes name for a different channel, then admitted in
            # its own reasoning that it could not tell. The class and the
            # reasoning have to agree or the answer is worthless.
            "The unit notes name specific channels. If they describe a different channel "
            "from the one you are asked about, that tells you nothing about this one: do "
            "not carry the identity across.",
            "If you cannot tell, physical_class must be exactly 'not determined' and "
            "confidence_pct below 40. Never return a specific class while your own "
            "reasoning says you cannot determine it.",
            "alternatives: other kinds it could equally be, at most three.",
            "what_would_settle_it: the one measurement that would decide between them.",
        ],
    }

    result = _call("Physical class for one channel", payload,
                   PHYSICAL_SCHEMA, "UI_physical_class", log)

    physical = _answer_of(result, "physical_class")
    pct = result.get("confidence_pct")
    if not isinstance(pct, int):
        pct = 25

    # Below this, the answer is a guess and the UI must not print it as a
    # headline. The information is not thrown away -- it moves to "best guess" --
    # because a 20% hunch is still worth more to an operator than a blank, as
    # long as nobody mistakes it for a finding.
    determined = pct >= 40 and physical.strip().lower() not in (
        "not determined", "unknown", "unclear", "indeterminate")

    entry_id = log.append(
        stage="UI_physical_class", kind="hypothesis",
        summary=f"Proposed physical class for {col_id} ({pct}%): {physical}"[:4000],
        actor_type="model", actor_name="assistant", subject=[col_id],
        confidence=_word_confidence(pct),
        epistemic_status=result.get("epistemic_status") or "uncertain",
        evidence_ids=result.get("evidence_ids") or summary["evidence_ids"],
        context={"machine_id": machine_id, "run_id": run_id},
    )

    return {
        "entry_id": entry_id, "call_id": result.get("_call_id"), "col_id": col_id,
        "physical_class": physical,
        "determined": determined,
        "reasoning": _answer_of(result, "reasoning"),
        "confidence_pct": pct, "confidence": _word_confidence(pct),
        "epistemic_status": result.get("epistemic_status") or "uncertain",
        "evidence_ids": check_citations(result.get("evidence_ids"))["verified"],
        "citations": check_citations(result.get("evidence_ids")),
        "alternatives": (result.get("alternatives") or [])[:3],
        "what_would_settle_it": result.get("what_would_settle_it"),
        "co_movers": neighbours,
        "accepted": False,
        "payload_sent": payload,
    }


def ask_shift_review(body: dict, log) -> dict:
    """End of shift: what should the next shift be warned about?

    Drafts only. Each prediction becomes real when an operator accepts it, and
    from then on it is shown beside the alert it predicted -- which is also how
    it gets found out when it was wrong.
    """
    run_id = _safe_id(body.get("run_id") or "", "")
    machine_id = _safe_id(body.get("machine_id") or "machine_01", "machine_01")
    if not run_id:
        raise ValueError("run_id is required")

    drift = get_drift({"run_id": [run_id]})
    events = drift.get("events") or []
    if not events:
        raise NotFound("this shift has no detected events to review")

    leaders: dict[str, dict] = {}
    for event in events:
        lead = (event.get("ranked_signals") or [{}])[0]
        col_id = lead.get("col_id")
        if not col_id:
            continue
        node = leaders.setdefault(col_id, {"col_id": col_id, "times_led": 0,
                                           "severities": [], "starts": []})
        node["times_led"] += 1
        node["severities"].append(event.get("severity"))
        node["starts"].append(event.get("start_sample"))

    ranked = sorted(leaders.values(), key=lambda d: -d["times_led"])[:3]
    for node in ranked:
        starts = sorted(s for s in node["starts"] if s)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        node["mean_gap_samples"] = round(sum(gaps) / len(gaps), 1) if gaps else None
        node["worst_severity"] = ("high" if "high" in node["severities"]
                                  else "medium" if "medium" in node["severities"] else "low")
        node["severities"] = dict(_count(node["severities"]))
        node["starts"] = starts[:12]

    state = _load_state(machine_id)
    payload = {
        "event_summary": {
            "run_id": run_id,
            "n_events": len(events),
            "n_samples": (drift.get("batch") or {}).get("n_samples"),
            "distinct_leading_channels": len(leaders),
            "already_carried": [p.get("text") for p in state.get("carried_predictions", [])][:5],
        },
        "sensor_summary": ranked,
        "machine_context": _machine_context_text(machine_id),
        "instructions": BASE_INSTRUCTIONS + [
            "This shift is over. Write what the next shift should be warned to expect.",
            "At most three predictions, each one sentence, each about a specific channel.",
            "A prediction that cannot be checked next shift is not worth writing: say what "
            "would be seen, and roughly when.",
            "Do not repeat anything already in already_carried.",
            "If nothing in this shift justifies a prediction, return an empty list.",
        ],
    }

    result = _call("End of shift review", payload, SHIFT_REVIEW_SCHEMA, "UI_shift_review", log)

    drafts = []
    for item in (result.get("predictions") or [])[:3]:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        pct = item.get("confidence_pct")
        citations = check_citations(item.get("evidence_ids"))
        drafts.append({
            "col_id": item.get("col_id"),
            "text": str(item["text"]),
            "confidence_pct": pct if isinstance(pct, int) else 25,
            "epistemic_status": item.get("epistemic_status") or "assumed",
            "evidence_ids": citations["verified"],
            "citations": citations,
        })

    return {
        "call_id": result.get("_call_id"), "run_id": run_id, "machine_id": machine_id,
        "shift_summary": result.get("shift_summary") or "",
        "predictions": drafts, "written": False,
        "payload_sent": payload,
    }


def _count(values):
    from collections import Counter
    return Counter(v for v in values if v)


# ==========================================================================
# Provenance. Where what you are looking at came from, and what left.
# ==========================================================================

def get_provenance(query: dict) -> dict:
    """Two ledgers, side by side, both counted rather than asserted.

    Written because of a fair objection: *if the charts redraw that fast, you
    cannot be looking at real data.* The speed is the point -- the values are
    read from Parquet on this machine, held in this process, and sent over
    loopback to a renderer in the same browser. Nothing crosses a network, so
    there is no network latency to pay. The opposite arrangement, where the data
    went somewhere to be looked at, is the slow one.

    So the page shows both numbers: how many values were served locally, and how
    many bytes left the machine. The second is read from the decision log, which
    is append-only and written by trust/gateway.py on every call, including the
    ones the gate refused.
    """
    log_path = ARTIFACTS / "decision_log.jsonl"
    calls, egress_bytes, refusals = [], 0, 0
    providers, models = set(), set()

    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            call = entry.get("model_call")
            if isinstance(call, dict):
                calls.append(call)
                providers.add(call.get("provider", "?"))
                models.add(call.get("model", "?"))
                if call.get("egress"):
                    egress_bytes += int(call.get("payload_bytes") or 0)
            if "gate" in str(entry.get("summary", "")).lower() and "block" in str(entry.get("summary", "")).lower():
                refusals += 1

    artifacts = []
    for name in ("schema.json", "profiles.json", "semantics.json", "relations.json",
                 "dq_report.json", "drift_events.json", "diagnosis.json"):
        path = ARTIFACTS / name
        if path.exists():
            artifacts.append({
                "path": f"artifacts/{name}",
                "bytes": path.stat().st_size,
                "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                            .strftime("%Y-%m-%d %H:%M:%S UTC"),
            })

    try:
        sources = series_reader.source_files()
        served = series_reader.served_counters()
        n_runs = len(series_reader.available_runs())
        n_cols = len(series_reader.column_ids())
    except SeriesUnavailable:
        sources, served, n_runs, n_cols = [], {"values": 0, "requests": 0, "columns": 0}, 0, 0

    outbound_calls = len([c for c in calls if c.get("egress")])
    return {
        "stays_here": {
            "source_files": sources,
            "source_bytes": sum(f["bytes"] for f in sources),
            "runs": n_runs,
            "columns": n_cols,
            "values_served_to_this_browser": served["values"],
            "chart_requests": served["requests"],
            "path": "data/features/**.parquet  ->  ui/series_reader.py  ->  127.0.0.1  ->  inline SVG",
            "note": ("Read once into this process and held in memory. A chart redraw is a "
                     "slice of an array that is already here, which is why it is instant."),
        },
        "leaves_here": {
            "model_calls": len(calls),
            "calls_that_left": outbound_calls,
            "bytes_that_left": egress_bytes,
            "kb_that_left": round(egress_bytes / 1024, 1),
            "providers": sorted(providers),
            "models": sorted(models),
            "rows_that_left": 0,
            "gate_refusals": refusals,
            "path": "artifacts/*.json  ->  derived summaries  ->  trust/gateway.py  ->  provider",
            "note": ("Every one of these went through call_model() in trust/gateway.py, which "
                     "refuses a payload containing a numeric array longer than 32, an original "
                     "column name, a label field, or more than 64 KB. The audit view prints the "
                     "exact payload of the last call."),
        },
        "artifacts_read": artifacts,
    }
