"""Actions the assistant may ask the operator's machine to perform for it.

WHY THIS EXISTS
---------------
An operator types "what about channel 3?" and the assistant has nothing to say,
because the payload it was given describes the channels ranked for the current
event and nothing else. The data exists on disk; the assistant simply cannot
reach it, and it must never reach it directly.

This module is the bridge, and it is deliberately narrow.

The assistant never receives data. It receives a MENU of things it may request,
each one a named action with typed parameters. When it needs something it is not
holding, it returns a `data_request` instead of an answer. The server runs that
request HERE, on the operator's machine, against local files, and returns only
the derived summary the action is defined to produce. Then the assistant answers.

    assistant: "I need window stats for col_003 on this run"
            -> {"action": "channel_window", "col_id": "col_003"}
    server:    runs it locally, against data/features/**
            -> {"mean": 2705.1, "std": 18.4, "slope": 0.02, ...}
    assistant: answers using those eight numbers

Two properties make this safe rather than a hole in the gate:

1. Every action returns AGGREGATES. There is no action that returns a series, a
   row, or a raw value at an index, and adding one would be a gate violation
   that `tools/gate_check.py` should be taught to catch.
2. The assistant chooses the action NAME and its parameters. It never chooses a
   file path, a query, or a column list beyond what the parameters allow, so it
   cannot widen its own access.

Everything that leaves this module still passes through `trust.gateway.call_model`
on the way back out, so the four payload tests apply to action results exactly as
they apply to anything else.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ARTIFACTS = ROOT / "artifacts"

# How many actions one answer may trigger. A model that asks for forty channels
# is not reasoning, it is scanning, and scanning is what the gate exists to stop.
MAX_ACTIONS_PER_TURN = 4


class ActionError(Exception):
    """The action could not be run. Reported to the assistant as a plain reason."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load(name: str) -> Any:
    path = ARTIFACTS / name
    if not path.exists():
        raise ActionError(f"{name} does not exist yet; run the pipeline first")
    return json.loads(path.read_text(encoding="utf-8"))


def _series_reader():
    """Imported lazily: the UI owns it, and actions should work without a browser."""
    sys.path.insert(0, str(ROOT / "ui"))
    import series_reader  # noqa: E402
    return series_reader


def _col_ids() -> set[str]:
    try:
        sem = _load("semantics.json")
    except ActionError:
        return set()
    return {k for k in sem if re.fullmatch(r"col_\d{3}", k)}


def resolve_channel_mentions(text: str) -> list[str]:
    """Turn what an operator typed into column ids that actually exist.

    Accepts `col_003`, `col 3`, `channel 3`, `canale 3`, `sensor 3`, `#3`.
    An operator says "channel 3"; the artifacts say "col_003"; without this the
    assistant is handed a question about something it has no record of.
    """
    known = _col_ids()
    found: list[str] = []
    patterns = [
        r"\bcol[_\s]?(\d{1,3})\b",
        r"\bchannel\s*(\d{1,3})\b",
        r"\bcanale\s*(\d{1,3})\b",
        r"\bsensor[e]?\s*(\d{1,3})\b",
        r"#(\d{1,3})\b",
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            cid = f"col_{int(m.group(1)):03d}"
            if cid in known and cid not in found:
                found.append(cid)
    return found[:MAX_ACTIONS_PER_TURN]


# ---------------------------------------------------------------------------
# the actions themselves -- each returns aggregates, never a series
# ---------------------------------------------------------------------------

def act_channel_profile(col_id: str = "", **_: Any) -> dict[str, Any]:
    """Everything already inferred about one channel, with no new computation."""
    sem = _load("semantics.json")
    node = sem.get(col_id)
    if not isinstance(node, dict):
        raise ActionError(f"{col_id} is not a known channel")
    out: dict[str, Any] = {
        "col_id": col_id,
        "inferred_role": node.get("human_override", {}).get("role")
        if isinstance(node.get("human_override"), dict) else node.get("inferred_role"),
        "confidence": node.get("confidence"),
        "epistemic_status": node.get("epistemic_status", "not stated"),
        "overridden_by_operator": bool(node.get("human_override")),
        "evidence_ids": (node.get("evidence_ids") or [])[:6],
    }
    try:
        prof = _load("profiles.json").get(col_id) or {}
        stats = prof.get("statistics") or {}
        out["baseline_statistics"] = {
            k: v for k, v in stats.items()
            if isinstance(v, (int, float)) and k in {
                "mean", "std_dev", "stddev", "min", "max", "noise_level",
                "autocorr_lag1", "longest_plateau", "step_count", "n_distinct"}
        }
    except ActionError:
        out["baseline_statistics"] = None
    return out


def act_channel_window(col_id: str = "", run_id: str = "", start: int = 1,
                       end: int = 0, **_: Any) -> dict[str, Any]:
    """Aggregates for one channel over one window. Eight numbers, no series."""
    sr = _series_reader()
    try:
        stats = sr.window_stats(run_id, col_id, int(start), int(end or start + 1))
    except Exception as exc:  # SeriesUnavailable and friends
        raise ActionError(f"no series available for {col_id} on run {run_id}: {exc}")
    return {"col_id": col_id, "run_id": run_id,
            "window": {"start": int(start), "end": int(end)}, "stats": stats}


def act_channel_neighbours(col_id: str = "", **_: Any) -> dict[str, Any]:
    """Which channels move with this one, and which leads, from relations.json."""
    rel = _load("relations.json")
    rows = rel if isinstance(rel, list) else rel.get("pairs", [])
    out = []
    for r in rows:
        subj = r.get("subject") or [r.get("a"), r.get("b")]
        if col_id not in (subj or []):
            continue
        val = r.get("value") or {}
        other = [c for c in subj if c != col_id]
        out.append({
            "other": other[0] if other else None,
            "corr": val.get("corr") or r.get("corr"),
            "lag_samples": val.get("lag") or val.get("lag_samples") or r.get("best_lag"),
            "leader": val.get("leader") or r.get("direction"),
            "evidence_id": r.get("evidence_id"),
        })
    out.sort(key=lambda d: abs(d.get("corr") or 0), reverse=True)
    return {"col_id": col_id, "related_channels": out[:8]}


def act_data_quality_for_channel(col_id: str = "", **_: Any) -> dict[str, Any]:
    """Whether the data for this channel can be trusted at all, before any process talk."""
    dq = _load("dq_report.json")
    checks = dq.get("checks_log") or dq.get("checks") or []
    mine = [c for c in checks if col_id in (c.get("subject") or [c.get("target_col")])]
    return {
        "col_id": col_id,
        "batch_trust_verdict": dq.get("trust_verdict"),
        "checks": [{k: c.get(k) for k in
                    ("check_id", "dimension", "status", "detail", "fault_class")}
                   for c in mine[:8]],
        "note": "If the batch verdict is UNTRUSTED, nothing below it is a process "
                "conclusion. Say that before anything else.",
    }


ACTIONS: dict[str, dict[str, Any]] = {
    "channel_profile": {
        "fn": act_channel_profile,
        "params": ["col_id"],
        "returns": "inferred role, confidence, evidence ids, baseline statistics",
        "use_when": "the operator asks about a channel you were not given",
    },
    "channel_window": {
        "fn": act_channel_window,
        "params": ["col_id", "run_id", "start", "end"],
        "returns": "mean, std, slope, min, max over that window -- aggregates only",
        "use_when": "you need how a channel behaved during a specific event",
    },
    "channel_neighbours": {
        "fn": act_channel_neighbours,
        "params": ["col_id"],
        "returns": "correlated channels with lag and which one leads",
        "use_when": "the operator asks what a channel is connected to",
    },
    "data_quality_for_channel": {
        "fn": act_data_quality_for_channel,
        "params": ["col_id"],
        "returns": "the batch trust verdict and the checks touching this channel",
        "use_when": "before drawing any process conclusion about a channel",
    },
}


def manifest() -> list[dict[str, Any]]:
    """The menu handed to the assistant. Names and shapes, never data."""
    return [{"action": name, "params": spec["params"],
             "returns": spec["returns"], "use_when": spec["use_when"]}
            for name, spec in ACTIONS.items()]


def run(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Execute the assistant's requests locally. Returns one result per request.

    A failure is returned as a `reason`, not raised: the assistant is expected to
    say "I could not retrieve that" rather than invent the answer, and it can only
    do that if it is told.
    """
    results: list[dict[str, Any]] = []
    for req in (requests or [])[:MAX_ACTIONS_PER_TURN]:
        if not isinstance(req, dict):
            continue
        name = req.get("action")
        spec = ACTIONS.get(name)
        if spec is None:
            results.append({"action": name, "ok": False,
                            "reason": f"unknown action; choose one of {sorted(ACTIONS)}"})
            continue
        params = {k: req.get(k) for k in spec["params"] if req.get(k) is not None}
        try:
            results.append({"action": name, "params": params, "ok": True,
                            "result": spec["fn"](**params)})
        except ActionError as exc:
            results.append({"action": name, "params": params, "ok": False,
                            "reason": str(exc)})
        except Exception as exc:  # never let one bad request kill the answer
            results.append({"action": name, "params": params, "ok": False,
                            "reason": f"{type(exc).__name__}: {exc}"})
    return results
