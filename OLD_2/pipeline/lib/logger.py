"""Compatibility adapter for the original core/logger.py.

WHY THIS FILE EXISTS
--------------------
During parallel development two decision loggers were written independently:

  * core/logger.py      (pipeline side) -> {timestamp, stage, action, details, model_used}
  * trust/decision_log.py (trust side)  -> {entry_id, at, actor, kind, summary, model_call}

The decision log is Deliverable 6 and is read by the judges, so the project can
only have one format. We kept trust/decision_log.py because it carries the
`model_call` record that Deliverable 8 (data-flow / egress evidence) depends on,
and the `egress_summary()` function that computes it.

Rather than ask four people to rewrite their logging calls, this adapter keeps
the original `DecisionLogger(...).log(...)` signature and translates each call
into the contract format. No stage had to change a line.

Mapping applied here:
  action + details -> summary
  model (if any)   -> actor_name, and kind becomes "model_call"
  evidence_ids, confidence pass through unchanged
"""

from trust.decision_log import DecisionLog


class DecisionLogger:
    """Drop-in replacement for the original logger, writing the contract format."""

    def __init__(self, log_path='artifacts/decision_log.jsonl'):
        self._log = DecisionLog(log_path)

    def log(self, stage, action, details, evidence_ids=None, confidence=None, model=None):
        return self._log.append(
            stage=stage,
            kind="model_call" if model else "inference",
            summary=f"{action}: {details}",
            actor_type="model" if model else "system",
            actor_name=model,
            evidence_ids=evidence_ids,
            confidence=confidence,
        )
