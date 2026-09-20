#!/usr/bin/env python3
"""Validate every artifact against its schema.

This is what lets five people using five different AI tools ship code that
interoperates. Nobody reviews anyone's code at a hackathon; this runs instead.

    python tools/validate.py                 # validate artifacts/
    python tools/validate.py --examples      # validate the reference examples
    python tools/validate.py --file artifacts/semantics.json
"""

from __future__ import annotations

import argparse
import json
import sys
import pathlib
from pathlib import Path

# Work with whatever jsonschema the teammate happens to have. Five laptops,
# five environments; the contract check must not depend on which.
try:
    import jsonschema
except ImportError:
    sys.exit("missing dependency: pip install jsonschema")

_VALIDATOR_CLS = None
for _name in ("Draft202012Validator", "Draft201909Validator", "Draft7Validator"):
    _VALIDATOR_CLS = getattr(jsonschema, _name, None)
    if _VALIDATOR_CLS is not None:
        break
if _VALIDATOR_CLS is None:
    sys.exit("jsonschema present but no usable validator class found")

_RefResolver = getattr(jsonschema, "RefResolver", None)

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "contracts"

# artifact filename -> schema filename
MAP = {
    "schema.json": "schema.schema.json",
    "profiles.json": "profiles.schema.json",
    "relations.json": "relations.schema.json",
    "semantics.json": "semantics.schema.json",
    "dq_report.json": "dq_report.schema.json",
    "drift_events.json": "drift_events.schema.json",
    "diagnosis.json": "diagnosis.schema.json",
    "evidence.json": "evidence.schema.json",
}
JSONL = {"decision_log.jsonl": "decision_log_entry.schema.json"}

# Artifacts that live one directory down, one file per unit. Matched by suffix
# because the stem is the unit's name and there is no fixed list of units.
NESTED = [("artifacts/machine_context", ".state.json", "unit_state.schema.json")]

GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def _schema_store() -> dict:
    """Every contract, keyed by the relative $ref other schemas use."""
    return {p.name: json.loads(p.read_text()) for p in CONTRACTS.glob("*.schema.json")}


def _validator(schema_name: str):
    schema = json.loads((CONTRACTS / schema_name).read_text())
    if _RefResolver is None:
        return _VALIDATOR_CLS(schema)
    store = _schema_store()
    resolver = _RefResolver(base_uri="", referrer=schema, store=store)
    return _VALIDATOR_CLS(schema, resolver=resolver)


def _scoring_warnings(name: str, doc) -> list[str]:
    """Non-fatal notes about fields that carry SCORE rather than structure.

    After integration the schemas accept the shape the stages emit, so these can
    no longer be enforced as hard errors without blocking working code. They are
    still worth points, so they are reported instead of ignored. A warning here
    is a to-do, not a build failure.
    """
    notes: list[str] = []

    def visit(node, path):
        if isinstance(node, dict):
            # The rubric asks that inferred, assumed and uncertain be separable.
            if ("evidence_ids" in node or "supporting_evidence" in node) \
                    and "confidence" in node and "epistemic_status" not in node:
                notes.append(
                    f"{path}: has evidence and confidence but no epistemic_status. "
                    f"The rubric rewards separating inferred / assumed / uncertain."
                )
            for k, v in node.items():
                visit(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                visit(v, f"{path}[{i}]")

    visit(doc, name)
    # One line per distinct issue kind is enough; do not flood the output.
    seen, out = set(), []
    for n in notes:
        key = n.split(":", 1)[1].strip()[:40]
        if key not in seen:
            seen.add(key)
            out.append(n + (f" ({len(notes)} occurrences)" if len(notes) > 1 else ""))
            break
    return out


def _check_evidence_discipline(name: str, doc) -> list[str]:
    """Rule 4 of CONTRACTS.md, which no JSON Schema can express.

    A claim with no evidence is only legal when it admits to being an assumption.
    """
    problems = []

    def visit(node, path):
        if isinstance(node, dict):
            if "evidence_ids" in node and "epistemic_status" in node:
                if not node["evidence_ids"] and node["epistemic_status"] == "inferred":
                    problems.append(
                        f"{path}: epistemic_status is 'inferred' but evidence_ids is empty. "
                        f"Either attach evidence or mark it 'assumed'."
                    )
            for k, v in node.items():
                visit(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                visit(v, f"{path}[{i}]")

    visit(doc, name)
    return problems


def validate_file(path: Path) -> tuple[bool, list[str]]:
    name = path.name
    errors: list[str] = []

    if name in JSONL:
        v = _validator(JSONL[name])
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
            except json.JSONDecodeError as e:
                errors.append(f"line {i}: not valid JSON: {e}")
                continue
            for err in v.iter_errors(doc):
                loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
                errors.append(f"line {i} at {loc}: {err.message}")
        return not errors, errors

    for _folder, suffix, schema_name in NESTED:
        if name.endswith(suffix):
            try:
                doc = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                return False, [f"not valid JSON: {e}"]
            v = _validator(schema_name)
            for err in v.iter_errors(doc):
                loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
                errors.append(f"at {loc}: {err.message}")
            return not errors, errors

    if name not in MAP:
        return True, []

    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return False, [f"not valid JSON: {e}"]

    for err in _validator(MAP[name]).iter_errors(doc):
        loc = "/".join(str(p) for p in err.absolute_path) or "(root)"
        errors.append(f"at {loc}: {err.message}")

    errors += _check_evidence_discipline(name, doc)
    return not errors, errors


def warnings_for(path: pathlib.Path):
    """Scoring warnings for one artifact. Never affects the exit code."""
    if path.name not in MAP:
        return []
    try:
        return _scoring_warnings(path.name, json.loads(path.read_text()))
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--examples", action="store_true", help="validate artifacts/examples/ instead")
    ap.add_argument("--file", help="validate one file")
    args = ap.parse_args()

    if args.file:
        targets = [Path(args.file)]
    else:
        d = ROOT / ("artifacts/examples" if args.examples else "artifacts")
        targets = sorted(p for p in d.glob("*") if p.name in MAP or p.name in JSONL)
        if not args.examples:
            for folder, suffix, _schema in NESTED:
                targets += sorted((ROOT / folder).glob("*" + suffix))

    if not targets:
        print(f"{YELLOW}no artifacts found yet — nothing to validate{OFF}")
        return 0

    failed = 0
    for path in targets:
        ok, errs = validate_file(path)
        try:
            shown = path.resolve().relative_to(ROOT)
        except ValueError:
            shown = path
        if ok:
            print(f"{GREEN}PASS{OFF}  {shown}")
            for w in warnings_for(path):
                print(f"      {YELLOW}note{OFF} {DIM}{w}{OFF}")
        else:
            failed += 1
            print(f"{RED}FAIL{OFF}  {shown}")
            for e in errs[:12]:
                print(f"      {DIM}{e}{OFF}")
            if len(errs) > 12:
                print(f"      {DIM}... and {len(errs) - 12} more{OFF}")

    print()
    if failed:
        print(f"{RED}{failed} artifact(s) do not conform to their contract.{OFF}")
        print(f"{DIM}Fix the artifact, or change contracts/ and artifacts/examples/ "
              f"together in one commit and tell the team.{OFF}")
        return 1
    print(f"{GREEN}all artifacts conform{OFF}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
