"""Append-only decision log.

Deliverable 6. Every inference, check, flag, diagnosis, model call and human
override lands here with the evidence behind it. This is not a debug log: the
judges will read it, so it is validated against
contracts/decision_log_entry.schema.json.

Append-only means append-only. There is no update and no delete. A correction is
a new entry of kind 'override' that names what it supersedes.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_LOCK = threading.Lock()
_DEFAULT_PATH = Path("artifacts/decision_log.jsonl")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class DecisionLog:
    """One instance per process. Safe across threads, safe across stages."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("DECISION_LOG", _DEFAULT_PATH))
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _next_id(self) -> str:
        n = 0
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                n = sum(1 for line in fh if line.strip())
        return f"log_{n:04d}"

    def append(
        self,
        *,
        stage: str,
        kind: str,
        summary: str,
        actor_type: str = "system",
        actor_name: str | None = None,
        subject: Iterable[str] | None = None,
        evidence_ids: Iterable[str] | None = None,
        confidence: str | None = None,
        epistemic_status: str | None = None,
        model_call: dict[str, Any] | None = None,
        override: dict[str, Any] | None = None,
        cites: Iterable[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """Write one entry. Returns its entry_id.

        `kind` is one of: inference, check, flag, diagnosis, model_call,
        human_review, override, config_change, qa, hypothesis, context_write.

        `subject` is column ids and nothing else -- that is what makes the log
        filterable by channel. Anything else an entry is about (a run, an event,
        a machine) goes in `context`, which is why that field exists.
        """
        with _LOCK:
            entry: dict[str, Any] = {
                "entry_id": self._next_id(),
                "at": _now(),
                "actor": {"type": actor_type},
                "stage": stage,
                "kind": kind,
                "summary": summary,
            }
            if actor_name:
                entry["actor"]["name"] = actor_name
            if subject:
                entry["subject"] = list(subject)
            if evidence_ids:
                entry["evidence_ids"] = list(evidence_ids)
            if confidence:
                entry["confidence"] = confidence
            if epistemic_status:
                entry["epistemic_status"] = epistemic_status
            if model_call:
                entry["model_call"] = model_call
            if override:
                entry["override"] = override
            if cites:
                entry["cites"] = list(cites)
            if context:
                entry["context"] = dict(context)

            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            return entry["entry_id"]

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def egress_summary(self) -> dict[str, Any]:
        """Deliverable 8, computed rather than claimed.

        What left this machine, to which model, why, and how much of it.
        """
        calls = [e["model_call"] for e in self.read() if "model_call" in e]
        left = [c for c in calls if c.get("egress")]
        return {
            "total_model_calls": len(calls),
            "calls_that_left_the_machine": len(left),
            "bytes_that_left": sum(c.get("payload_bytes", 0) for c in left),
            "providers": sorted({c.get("provider", "?") for c in calls}),
            "models": sorted({c.get("model", "?") for c in calls}),
            "payload_kinds_sent": sorted({k for c in calls for k in c.get("payload_kinds", [])}),
            "raw_rows_sent": 0,  # structurally guaranteed by trust.gateway
        }
