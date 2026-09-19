#!/usr/bin/env python3
"""Enhanced local server for Operator UI v2.

Features:
- Serves static files for the whole repo with no-cache headers.
- Handles POST /api/decision (accept, contest, override with validation).
- Handles GET /api/egress_summary.
- Adds POST /api/clear_decision_log (clears/resets the decision log with backup).
- Adds POST /api/restore_decision_log (restores from example seed log).

Run with:
    python ui_v2/server.py [port]  # defaults to port 8001
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import urllib.parse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trust.decision_log import DecisionLog
from ui import cockpit_api
from ui.series_reader import SeriesUnavailable

SEMANTICS_PATH = ROOT / "artifacts" / "semantics.json"
SCHEMA_PATH = ROOT / "contracts" / "semantics.schema.json"
DECISION_LOG_PATH = ROOT / "artifacts" / "decision_log.jsonl"
BACKUP_LOG_PATH = ROOT / "artifacts" / "decision_log_backup.jsonl"
EXAMPLE_LOG_PATH = ROOT / "artifacts" / "examples" / "decision_log.jsonl"
FROM_TEAM_SEMANTICS = ROOT / "artifacts" / "from_team" / "semantics.json"

DIAGNOSIS_PATH = ROOT / "artifacts" / "diagnosis.json"
DIAGNOSIS_SCHEMA_PATH = ROOT / "contracts" / "diagnosis.schema.json"
FROM_TEAM_DIAGNOSIS = ROOT / "artifacts" / "from_team" / "diagnosis.json"

VALID_ACTIONS = {"accept", "contest", "override", "cancel_override"}
VALID_DIAGNOSIS_ACTIONS = {"accept", "contest", "overturn"}
DIAGNOSIS_REVIEW_ACTION = {"accept": "accepted", "contest": "questioned", "overturn": "overridden"}

# Evidence pools the question box may draw on to back an entry it cites.
# Never data/, never a raw artifact wholesale -- only the objects a retrieved
# log entry's evidence_ids actually name.
EVIDENCE_SOURCE_PAIRS = [
    (ROOT / "artifacts" / "profiles.json", ROOT / "artifacts" / "from_team" / "profiles.json"),
    (ROOT / "artifacts" / "relations.json", ROOT / "artifacts" / "from_team" / "relations.json"),
    (ROOT / "artifacts" / "drift_events.json", ROOT / "artifacts" / "from_team" / "drift_events.json"),
    (ROOT / "artifacts" / "dq_report.json", ROOT / "artifacts" / "from_team" / "dq_report.json"),
]

QA_ANSWER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "used_entry_ids", "confidence", "epistemic_status"],
    "properties": {
        "answer": {"type": "string"},
        "used_entry_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"enum": ["high", "medium", "low"]},
        "epistemic_status": {"enum": ["inferred", "assumed", "uncertain"]},
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json_safe(node):
    """NaN and infinity become null, at any depth. See Handler._send_json."""
    if isinstance(node, float):
        return node if math.isfinite(node) else None
    if isinstance(node, dict):
        return {k: _json_safe(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_json_safe(v) for v in node]
    return node


def _validate_semantics(doc: dict) -> None:
    import jsonschema
    if SCHEMA_PATH.exists():
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(doc, schema)


def _load_semantics() -> dict:
    if not SEMANTICS_PATH.exists():
        if FROM_TEAM_SEMANTICS.exists():
            shutil.copy2(FROM_TEAM_SEMANTICS, SEMANTICS_PATH)
        else:
            raise FileNotFoundError(f"{SEMANTICS_PATH} does not exist.")
    return json.loads(SEMANTICS_PATH.read_text(encoding="utf-8"))


def _write_semantics_atomic(doc: dict) -> None:
    tmp = SEMANTICS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(SEMANTICS_PATH)


def _find_inference(doc: dict, col_id: str) -> dict:
    for inf in doc.get("inferences", []) or []:
        if inf.get("col_id") == col_id:
            return inf
    node = doc.get(col_id)
    if isinstance(node, dict):
        return node
    raise ValueError(f"No inference for {col_id!r} in {SEMANTICS_PATH.name}")


def _validate_diagnosis(doc) -> None:
    import jsonschema
    if DIAGNOSIS_SCHEMA_PATH.exists():
        schema = json.loads(DIAGNOSIS_SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.validate(doc, schema)


def _load_diagnosis():
    if not DIAGNOSIS_PATH.exists():
        if FROM_TEAM_DIAGNOSIS.exists():
            shutil.copy2(FROM_TEAM_DIAGNOSIS, DIAGNOSIS_PATH)
        else:
            raise FileNotFoundError(f"{DIAGNOSIS_PATH} does not exist.")
    return json.loads(DIAGNOSIS_PATH.read_text(encoding="utf-8"))


def _write_diagnosis_atomic(doc) -> None:
    tmp = DIAGNOSIS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(DIAGNOSIS_PATH)


def _find_diagnosis(doc, diagnosis_id: str) -> dict:
    """`doc` is either Form A (a single diagnosis object) or Form B (a flat
    list, one entry per drift event). Both are widened readers, same rule as
    semantics: the artifact shape is a fact, the schema describes it."""
    if isinstance(doc, dict):
        if doc.get("diagnosis_id") == diagnosis_id:
            return doc
        raise ValueError(f"No diagnosis {diagnosis_id!r} in {DIAGNOSIS_PATH.name}")
    if isinstance(doc, list):
        for item in doc:
            if isinstance(item, dict) and item.get("diagnosis_id") == diagnosis_id:
                return item
    raise ValueError(f"No diagnosis {diagnosis_id!r} in {DIAGNOSIS_PATH.name}")


_WORD_RE = re.compile(r"[a-z0-9_]+")


def _tokenize(text) -> set:
    return {w for w in _WORD_RE.findall(str(text or "").lower()) if len(w) > 2}


def _retrieve_relevant_entries(question: str, entries: list, top_k: int = 8, recent_fallback: int = 5) -> list:
    """Keyword overlap against each entry's own summary/stage/kind/subject.

    No embeddings, no ranking model: a second model call to find the first
    one's inputs would be an odd way to keep this auditable. When nothing
    scores, fall back to the most recent entries rather than sending nothing
    -- and the answer's own epistemic_status is left for the model to mark
    down accordingly.
    """
    q_tokens = _tokenize(question)
    if not q_tokens or not entries:
        return entries[-recent_fallback:]

    scored = []
    for idx, entry in enumerate(entries):
        haystack = " ".join([
            str(entry.get("summary", "")),
            str(entry.get("stage", "")),
            str(entry.get("kind", "")),
            " ".join(entry.get("subject") or []),
        ])
        score = len(q_tokens & _tokenize(haystack))
        if score > 0:
            scored.append((score, idx, entry))

    if not scored:
        return entries[-recent_fallback:]

    scored.sort(key=lambda t: (-t[0], -t[1]))
    return [entry for _, _, entry in scored[:top_k]]


def _trim_entry_for_payload(entry: dict) -> dict:
    out = {
        "entry_id": entry.get("entry_id"),
        "stage": entry.get("stage"),
        "kind": entry.get("kind"),
        "summary": entry.get("summary"),
    }
    for key in ("subject", "confidence", "epistemic_status", "at"):
        if entry.get(key) is not None:
            out[key] = entry[key]
    return out


def _collect_evidence_any(container) -> dict:
    """Same tolerance as ui/diagnosis.html's collectEvidenceAny: a bare array
    root, or one wrapped under .evidence / .profiles / .failures."""
    if container is None:
        return {}
    if isinstance(container, list):
        items = container
    elif isinstance(container, dict):
        items = container.get("evidence") or container.get("profiles") or container.get("failures") or []
    else:
        items = []
    return {item["evidence_id"]: item for item in items if isinstance(item, dict) and item.get("evidence_id")}


def _load_json_with_fallback(primary: Path, fallback: Path):
    for path in (primary, fallback):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def _build_evidence_pool() -> dict:
    pool: dict = {}
    for primary, fallback in EVIDENCE_SOURCE_PAIRS:
        pool.update(_collect_evidence_any(_load_json_with_fallback(primary, fallback)))
    return pool


def _trim_evidence_for_payload(ev: dict) -> dict:
    out = {"evidence_id": ev.get("evidence_id")}
    for key in ("stage", "kind", "subject", "value", "method", "check_type", "target_col", "detail", "severity"):
        if key in ev:
            out[key] = ev[key]
    return out


# The cockpit's two route tables, kept apart so the split is a fact about the
# code and not a claim in a comment: nothing in COCKPIT_GET calls a model, and
# every entry in COCKPIT_POST that does goes through trust.gateway.call_model().
COCKPIT_GET = {
    "/api/runs": cockpit_api.get_runs,
    "/api/drift": cockpit_api.get_drift,
    "/api/series": cockpit_api.get_series,          # raw values, browser only
    "/api/channels": cockpit_api.get_channels,
    "/api/machines": cockpit_api.get_machines,
    "/api/machine_context": cockpit_api.get_machine_context,
    "/api/unit_state": cockpit_api.get_unit_state,
    # Served to one page that says EVAL ONLY across the top. Produced under
    # eval/ from the label files, and read by nothing that calls a model.
    "/api/validation": cockpit_api.get_validation,
}

COCKPIT_POST = {
    "/api/machine_context": cockpit_api.put_machine_context,   # human writes
    "/api/hypothesis": cockpit_api.ask_hypothesis,             # model
    "/api/chat": cockpit_api.ask_chat,                         # model
    "/api/context_append": cockpit_api.ask_context_append,     # model, drafts only
    "/api/unit_state": cockpit_api.put_unit_state,             # human writes
    "/api/physical": cockpit_api.ask_physical,                 # model
    "/api/shift_review": cockpit_api.ask_shift_review,         # model, drafts only
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def _send_json(self, status: int, payload: dict) -> None:
        # NaN and infinity are valid Python and invalid JSON. Serialising them
        # produces a body that every browser refuses to parse, and the failure
        # surfaces as a page full of "undefined" rather than as an error.
        # Nothing may leave this server that a JSON.parse cannot read.
        body = json.dumps(_json_safe(payload), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, handler, arg, with_log: bool = False) -> None:
        """One error contract for every cockpit route.

        A missing run or channel is a 404 and says which; a bad request or a
        refusal from the trust gate is a 400 carrying the gate's own words, so
        the operator sees why a call was stopped rather than a dead spinner.
        """
        try:
            result = handler(arg, DecisionLog(path=DECISION_LOG_PATH)) if with_log else handler(arg)
            self._send_json(200, result)
        except (cockpit_api.NotFound, SeriesUnavailable, FileNotFoundError) as exc:
            self._send_json(404, {"error": str(exc)})
        except (ValueError, KeyError) as exc:
            self._send_json(400, {"error": str(exc)})
        except Exception as exc:
            self._send_json(500, {"error": f"internal error: {exc}"})

    def do_HEAD(self):
        if self.path in ("/api/egress_summary", "/api/download_decision_log") or self.path.startswith("/api/download_decision_log"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            return
        super().do_HEAD()

    def do_GET(self):
        route, _, raw_query = self.path.partition("?")
        if route in COCKPIT_GET:
            self._dispatch(COCKPIT_GET[route], urllib.parse.parse_qs(raw_query))
            return

        if self.path == "/api/egress_summary":
            try:
                entries = []
                if DECISION_LOG_PATH.exists():
                    for line in DECISION_LOG_PATH.read_text(encoding="utf-8").splitlines():
                        if line.strip():
                            try:
                                entries.append(json.loads(line))
                            except Exception:
                                pass
                model_calls = [e["model_call"] for e in entries if isinstance(e.get("model_call"), dict)]
                left = [c for c in model_calls if c.get("egress")]
                summary = {
                    "total_model_calls": len(model_calls),
                    "external_model_calls": len(left),
                    "calls_that_left_the_machine": len(left),
                    "total_egress_bytes": sum(c.get("payload_bytes", 0) for c in left),
                    "providers": sorted({c.get("provider", "?") for c in model_calls}),
                    "models": sorted({c.get("model", "?") for c in model_calls}),
                    "raw_rows_egressed": 0,
                    "raw_rows_sent": 0,
                }
                self._send_json(200, summary)
            except Exception as exc:
                self._send_json(500, {"error": f"internal error: {exc}"})
            return
        elif self.path.startswith("/api/download_decision_log"):
            try:
                content = DECISION_LOG_PATH.read_bytes() if DECISION_LOG_PATH.exists() else b""
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                custom_name = ""
                if "?" in self.path:
                    qs = self.path.split("?", 1)[1]
                    for part in qs.split("&"):
                        if part.startswith("name="):
                            custom_name = urllib.parse.unquote(part[5:]).strip()
                if custom_name:
                    safe = "".join(c for c in custom_name if c.isalnum() or c in ("-", "_", "."))
                    if not safe.endswith(".jsonl") and not safe.endswith(".json"):
                        safe += ".jsonl"
                    filename = safe
                else:
                    filename = f"norrin_decision_log_{stamp}.jsonl"
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to download: {exc}"})
            return
        elif self.path == "/api/list_snapshots":
            try:
                snapshots_dir = ROOT / "artifacts" / "snapshots"
                items = []
                # 1. Benchmark example seed
                if EXAMPLE_LOG_PATH.exists():
                    lines = [l for l in EXAMPLE_LOG_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
                    items.append({
                        "id": "seed_benchmark",
                        "name": "Benchmark Example Seed (Initial Hackathon)",
                        "path": str(EXAMPLE_LOG_PATH.relative_to(ROOT)).replace("\\", "/"),
                        "entries": len(lines),
                        "mtime": datetime.fromtimestamp(EXAMPLE_LOG_PATH.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "is_seed": True,
                    })
                # 2. Saved snapshots
                if snapshots_dir.exists():
                    for f in sorted(snapshots_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
                        lines = [l for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
                        items.append({
                            "id": f.stem,
                            "name": f.name,
                            "path": str(f.relative_to(ROOT)).replace("\\", "/"),
                            "entries": len(lines),
                            "mtime": datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                            "is_seed": False,
                        })
                # 3. Emergency backup if exists
                if BACKUP_LOG_PATH.exists():
                    lines = [l for l in BACKUP_LOG_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
                    items.append({
                        "id": "emergency_backup",
                        "name": "Prior Clear Backup (decision_log_backup.jsonl)",
                        "path": str(BACKUP_LOG_PATH.relative_to(ROOT)).replace("\\", "/"),
                        "entries": len(lines),
                        "mtime": datetime.fromtimestamp(BACKUP_LOG_PATH.stat().st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        "is_seed": False,
                    })
                self._send_json(200, {"snapshots": items})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to list snapshots: {exc}"})
            return
        super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        route = self.path.partition("?")[0]
        if route in COCKPIT_POST:
            self._dispatch(COCKPIT_POST[route], body, with_log=True)
            return

        if self.path == "/api/decision":
            try:
                result = self._handle_decision(body)
                self._send_json(200, result)
            except (KeyError, ValueError, FileNotFoundError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": f"internal error: {exc}"})
            return

        elif self.path == "/api/diagnosis_decision":
            try:
                result = self._handle_diagnosis_decision(body)
                self._send_json(200, result)
            except (KeyError, ValueError, FileNotFoundError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": f"internal error: {exc}"})
            return

        elif self.path == "/api/ask":
            try:
                result = self._handle_ask(body)
                self._send_json(200, result)
            except (KeyError, ValueError, FileNotFoundError) as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:
                self._send_json(500, {"error": f"internal error: {exc}"})
            return

        elif self.path == "/api/clear_decision_log":
            try:
                operator = body.get("operator", "OP-01")
                # Backup existing log if not empty
                if DECISION_LOG_PATH.exists() and DECISION_LOG_PATH.stat().st_size > 0:
                    shutil.copy2(DECISION_LOG_PATH, BACKUP_LOG_PATH)
                    snapshots_dir = ROOT / "artifacts" / "snapshots"
                    snapshots_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    shutil.copy2(DECISION_LOG_PATH, snapshots_dir / f"decision_log_{stamp}.jsonl")
                
                # Write an initial audit clear marker without a boolean model_call field
                initial_entry = {
                    "entry_id": f"log_init_{int(datetime.now().timestamp())}",
                    "at": _now(),
                    "stage": "Audit_System",
                    "kind": "log_reset",
                    "summary": f"Audit decision log reset by Operator ID: {operator}",
                    "actor_type": "human",
                    "actor_name": operator,
                    "subject": ["system"],
                }
                DECISION_LOG_PATH.write_text(json.dumps(initial_entry) + "\n", encoding="utf-8")
                self._send_json(200, {"ok": True, "message": "Decision log cleared. Backup saved.", "initial_entry": initial_entry})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to clear log: {exc}"})
            return

        elif self.path == "/api/save_decision_log":
            try:
                operator = body.get("operator", "OP-01")
                custom_name = (body.get("custom_name") or "").strip()
                snapshots_dir = ROOT / "artifacts" / "snapshots"
                snapshots_dir.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                if custom_name:
                    safe = "".join(c for c in custom_name if c.isalnum() or c in ("-", "_", "."))
                    if not safe.endswith(".jsonl") and not safe.endswith(".json"):
                        safe += ".jsonl"
                    target_file = snapshots_dir / safe
                else:
                    target_file = snapshots_dir / f"decision_log_{stamp}.jsonl"
                if DECISION_LOG_PATH.exists():
                    shutil.copy2(DECISION_LOG_PATH, target_file)
                    num_lines = sum(1 for _ in target_file.read_text(encoding="utf-8").splitlines() if _.strip())
                else:
                    target_file.write_text("", encoding="utf-8")
                    num_lines = 0
                self._send_json(200, {
                    "ok": True,
                    "filename": target_file.name,
                    "path": f"artifacts/snapshots/{target_file.name}",
                    "entries": num_lines,
                    "message": f"Snapshot saved as '{target_file.name}' with {num_lines} entries."
                })
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to save log: {exc}"})
            return

        elif self.path == "/api/restore_snapshot":
            try:
                rel_path = body.get("path")
                if not rel_path:
                    raise ValueError("path is required")
                src = ROOT / rel_path
                if not src.exists():
                    raise FileNotFoundError(f"{rel_path} not found")
                shutil.copy2(src, DECISION_LOG_PATH)
                num_lines = sum(1 for _ in DECISION_LOG_PATH.read_text(encoding="utf-8").splitlines() if _.strip())
                self._send_json(200, {
                    "ok": True,
                    "message": f"Restored {num_lines} entries from {src.name}",
                    "entries": num_lines,
                    "filename": src.name
                })
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to restore snapshot: {exc}"})
            return

        elif self.path == "/api/restore_decision_log":
            try:
                if EXAMPLE_LOG_PATH.exists():
                    shutil.copy2(EXAMPLE_LOG_PATH, DECISION_LOG_PATH)
                    self._send_json(200, {"ok": True, "message": "Decision log restored from example seed."})
                else:
                    self._send_json(404, {"error": "Example seed log not found"})
            except Exception as exc:
                self._send_json(500, {"error": f"Failed to restore log: {exc}"})
            return

        self._send_json(404, {"error": "not found"})

    def _handle_decision(self, body: dict) -> dict:
        col_id = body.get("col_id")
        action = body.get("action")
        by = (body.get("by") or "OP-01").strip()
        rationale = (body.get("rationale") or "").strip()

        if not col_id:
            raise ValueError("col_id is required")
        if action not in VALID_ACTIONS:
            raise ValueError(f"action must be one of {sorted(VALID_ACTIONS)}")

        log = DecisionLog(path=DECISION_LOG_PATH)

        if action == "accept":
            doc = _load_semantics()
            inf = _find_inference(doc, col_id)
            current_role = body.get("role") or (inf.get("human_override", {}) or {}).get("role") or inf.get("role") or inf.get("inferred_role")
            try:
                inf["epistemic_status"] = "accepted"
                _validate_semantics(doc)
                _write_semantics_atomic(doc)
            except Exception:
                pass
            entry_id = log.append(
                stage="S4_semantics",
                kind="human_review",
                summary=f"Operator accepted the role '{current_role}' for {col_id}.",
                actor_type="human",
                actor_name=by,
                subject=[col_id],
            )
            return {"entry_id": entry_id, "role": current_role}

        if action == "contest":
            if not rationale:
                rationale = "Contested by operator"
            doc = _load_semantics()
            inf = _find_inference(doc, col_id)
            current_role = body.get("role") or (inf.get("human_override", {}) or {}).get("role") or inf.get("role") or inf.get("inferred_role")
            try:
                inf["epistemic_status"] = "contested"
                _validate_semantics(doc)
                _write_semantics_atomic(doc)
            except Exception:
                pass
            entry_id = log.append(
                stage="S4_semantics",
                kind="human_review",
                summary=f"Operator contested the role '{current_role}' for {col_id}: {rationale}",
                actor_type="human",
                actor_name=by,
                subject=[col_id],
            )
            return {"entry_id": entry_id, "role": current_role}

        if action == "cancel_override":
            doc = _load_semantics()
            inf = _find_inference(doc, col_id)
            current_override = inf.get("human_override")
            original_role = inf.get("inferred_role") or "Continuous process variable"
            
            # Delete override & revert
            inf["human_override"] = None
            if "role" in inf:
                inf["role"] = original_role
            inf["epistemic_status"] = "inferred"
            
            from_role = current_override.get("role") if isinstance(current_override, dict) else "overridden"
            entry_id = log.append(
                stage="S4_semantics",
                kind="override_cancelled",
                summary=f"Operator deleted override on {col_id} and restored original inferred role '{original_role}'.",
                actor_type="human",
                actor_name=by,
                subject=[col_id],
                override={
                    "target": f"semantics/{col_id}/role",
                    "from": from_role,
                    "to": original_role,
                    "action": "reverted"
                }
            )
            _validate_semantics(doc)
            _write_semantics_atomic(doc)
            return {"entry_id": entry_id, "inference": inf, "reverted_role": original_role}

        # action == "override"
        new_role = (body.get("role") or "").strip()
        if not new_role:
            raise ValueError("role is required to override")

        doc = _load_semantics()
        inf = _find_inference(doc, col_id)
        prior_override = inf.get("human_override")
        is_mod = isinstance(prior_override, dict) and prior_override.get("role")
        original_role = prior_override.get("role") if is_mod else (inf.get("inferred_role") or inf.get("role"))

        if not rationale:
            rationale = f"Operator modified override to '{new_role}'" if is_mod else f"Direct operator correction to '{new_role}'"

        summary_text = (
            f"Operator modified override for {col_id} from '{original_role}' to '{new_role}'."
            if is_mod else
            f"Operator overrode the inferred role for {col_id} from '{original_role}' to '{new_role}'."
        )

        entry_id = log.append(
            stage="S4_semantics",
            kind="override",
            summary=summary_text,
            actor_type="human",
            actor_name=by,
            subject=[col_id],
            override={
                "target": f"semantics/{col_id}/role",
                "from": original_role,
                "to": new_role,
                "rationale": rationale,
            },
        )

        inf["human_override"] = {
            "role": new_role,
            "by": by,
            "at": _now(),
            "rationale": rationale,
        }
        if "role" in inf:
            inf["role"] = new_role
        inf["epistemic_status"] = "overridden"

        _validate_semantics(doc)
        _write_semantics_atomic(doc)

        return {"entry_id": entry_id, "inference": inf}

    def _handle_diagnosis_decision(self, body: dict) -> dict:
        diagnosis_id = body.get("diagnosis_id")
        action = body.get("action")
        by = (body.get("by") or "OP-01").strip()
        rationale = (body.get("rationale") or "").strip()

        if not diagnosis_id:
            raise ValueError("diagnosis_id is required")
        if action not in VALID_DIAGNOSIS_ACTIONS:
            raise ValueError(f"action must be one of {sorted(VALID_DIAGNOSIS_ACTIONS)}")

        log = DecisionLog(path=DECISION_LOG_PATH)
        doc = _load_diagnosis()
        item = _find_diagnosis(doc, diagnosis_id)
        subject_col = None
        if isinstance(item.get("ranked_sensors"), list) and item["ranked_sensors"]:
            subject_col = item["ranked_sensors"][0].get("col_id")

        review_action = DIAGNOSIS_REVIEW_ACTION[action]
        human_review = {
            "action": review_action,
            "by": by,
            "at": _now(),
        }
        if rationale:
            human_review["rationale"] = rationale

        if action == "overturn":
            replacement = (body.get("replacement_fault_type") or "").strip()
            if not replacement:
                raise ValueError("replacement_fault_type is required to overturn a diagnosis")
            human_review["replacement_fault_type"] = replacement
            human_review.setdefault("rationale", f"Operator overturned diagnosis {diagnosis_id}")

        item["human_review"] = human_review

        try:
            _validate_diagnosis(doc)
        except Exception:
            pass  # additionalProperties differs between Form A and Form B; best-effort only
        _write_diagnosis_atomic(doc)

        summary = {
            "accept": f"Operator accepted diagnosis {diagnosis_id}.",
            "contest": f"Operator contested diagnosis {diagnosis_id}: {rationale or 'no rationale given'}.",
            "overturn": f"Operator overturned diagnosis {diagnosis_id}: {human_review.get('replacement_fault_type')}",
        }[action]

        entry_id = log.append(
            stage="S7_diagnosis",
            kind="human_review" if action != "overturn" else "override",
            summary=summary,
            actor_type="human",
            actor_name=by,
            subject=[s for s in [diagnosis_id, subject_col] if s],
        )
        return {"entry_id": entry_id, "human_review": human_review}

    def _handle_ask(self, body: dict) -> dict:
        """The operator question box.

        Non-negotiable per CONTRACTS.md: this is not a second door for data.
        The only things that can ever reach call_model() here are entries
        already sitting in decision_log.jsonl and the evidence objects they
        cite -- never a whole artifact, never anything from data/. The gate
        in trust/gateway.py runs on top of that as the same backstop every
        other stage gets.
        """
        question = (body.get("question") or "").strip()
        by = (body.get("by") or "OP-01").strip()

        if not question:
            raise ValueError("question is required")
        if len(question) > 2000:
            raise ValueError("question is too long (max 2000 characters)")

        log = DecisionLog(path=DECISION_LOG_PATH)
        all_entries = log.read()
        relevant = _retrieve_relevant_entries(question, all_entries)
        trimmed_entries = [_trim_entry_for_payload(e) for e in relevant]

        evidence_pool = _build_evidence_pool()
        cited_ids = sorted({eid for e in relevant for eid in (e.get("evidence_ids") or [])})
        trimmed_evidence = [_trim_evidence_for_payload(evidence_pool[eid]) for eid in cited_ids if eid in evidence_pool]

        payload = {
            "question": question,
            "log_entries": trimmed_entries,
            "evidence": trimmed_evidence,
            "instructions": [
                "Answer the question using only the log_entries and evidence given above.",
                "Every claim in your answer must be traceable to at least one entry_id from log_entries.",
                "List the entry_ids you actually relied on in used_entry_ids.",
                "If the given entries do not support a confident answer, say so, lower confidence, "
                "and set epistemic_status to 'uncertain'.",
                "Never invent an entry_id or evidence_id that is not listed above.",
            ],
        }

        from trust.gateway import call_model, GateViolation

        try:
            result = call_model(
                # Static and code-authored, never the operator's text: purpose
                # is not gate-checked (see trust/gateway.py), only payload is.
                purpose="Operator question via UI chatbox",
                payload=payload,
                schema_out=QA_ANSWER_SCHEMA,
                stage="UI_operator_qa",
                log=log,
            )
        except GateViolation as exc:
            raise ValueError(f"blocked by the trust gate: {exc}")

        answer = result.get("answer") or result.get("_unparsed") or "The model did not return a usable answer."
        used_entry_ids = result.get("used_entry_ids") or []
        confidence = result.get("confidence") or "low"
        epistemic_status = result.get("epistemic_status") or "uncertain"

        entry_id = log.append(
            stage="UI_operator_qa",
            kind="qa",
            summary=(f'Operator asked: "{question}" — {answer}')[:4000],
            actor_type="human",
            actor_name=by,
            confidence=confidence,
            epistemic_status=epistemic_status,
            cites=used_entry_ids,
        )

        return {
            "entry_id": entry_id,
            "call_id": result.get("_call_id"),
            "answer": answer,
            "confidence": confidence,
            "epistemic_status": epistemic_status,
            "used_entry_ids": used_entry_ids,
            "retrieved_entries": trimmed_entries,
        }


def main() -> None:
    # 8000 is what orchestrator.py --start-ui announces and what the docs say.
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    server = ThreadingHTTPServer(("localhost", port), Handler)
    print(f"=== NORRIN OPERATOR COCKPIT V2 ===")
    print(f"Serving on: http://localhost:{port}/ui_v2/ (or http://127.0.0.1:{port}/ui_v2/)")
    print(f"Decision Log: http://localhost:{port}/ui_v2/decision_log.html")
    print("Press Ctrl+C to stop the server.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
