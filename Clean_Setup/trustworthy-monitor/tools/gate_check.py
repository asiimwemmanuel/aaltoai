#!/usr/bin/env python3
"""Static scan for the rules that cost points if broken.

Run before every commit: `make gate`. It reads source, not data, so it is instant.

REFINED AFTER INTEGRATION (19 Sep)
----------------------------------
The first version of this scanner flagged any chemistry word in any string
literal. That produced false positives on legitimate code, because the challenge
explicitly ASKS us to accept plain-language operating rules written by an
operator -- and an operator writes "Reactor pressure must stay below 2900".
The rule text is an input we must handle, not knowledge we injected.

A scanner that cries wolf gets ignored, so the domain rule is now split in two:

  GROUNDTRUTH (fatal)  a line that maps an ORIGINAL column name to a physical
                       meaning. This is hand-fed Tennessee Eastman knowledge and
                       it forfeits evaluation criterion 1.
  DOMAIN      (fatal)  a chemistry term inside a file that actually calls a
                       model, i.e. text that could reach the LLM as a hint.

Operator rule strings in test and evaluation code are neither, and pass.

Rules enforced, mapped to CONTRACTS.md:
  §0 gate          no LLM SDK imported outside trust/
  §1 isolation     no stage imports another stage
  §2 col identity  no original column names after S1
  §3 labels        no faultNumber / fault_status under pipeline/, except the splitter
  §5 no hints      see GROUNDTRUTH / DOMAIN above
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RED, GREEN, YELLOW, DIM, OFF = "\033[31m", "\033[32m", "\033[33m", "\033[2m", "\033[0m"

# Files allowed to break a rule, because handling it is their job.
EXEMPT = {
    # trust/gateway.py IS the door; the scanner names the SDKs it looks for.
    "gate": {"trust/gateway.py", "tools/gate_check.py", "config/llm.yaml"},
    # S1 is the only place original names may appear: it builds the col_NNN map.
    "colnames": {"pipeline/s1_ingest.py", "tools/gate_check.py", "config/llm.yaml"},
    # S1 is also the quarantine boundary: it must name the label columns in order
    # to write them to a SEPARATE parquet set that pipeline/ never reads.
    "labels": {"pipeline/s1_ingest.py", "tools/gate_check.py", "config/llm.yaml"},
    "groundtruth": {"tools/gate_check.py"},
    # The scanner itself spells out the vocabulary it searches for.
    "domain": {"tools/gate_check.py"},
}

LLM_SDKS = re.compile(
    r"^\s*(?:import|from)\s+(anthropic|openai|google\.generativeai|google\.genai"
    r"|mistralai|cohere|litellm|ollama|transformers)\b",
    re.MULTILINE,
)
COLNAMES = re.compile(r"\b(xmeas|xmv)\b", re.IGNORECASE)
ORIGINAL_COL = re.compile(r"\b(xmeas_\d+|xmv_\d+)\b", re.IGNORECASE)
LABELS = re.compile(r"\b(faultNumber|fault_status)\b")
DOMAIN = re.compile(
    r"\b(reactor|stripper|condenser|separator|compressor|catalyst|distillation"
    r"|tennessee\s+eastman)\b",
    re.IGNORECASE,
)
STRING_LITERAL = re.compile(r'"""(?:.|\n)*?"""|\'\'\'(?:.|\n)*?\'\'\'|"[^"\n]*"|\'[^\'\n]*\'')
STAGE_IMPORT = re.compile(r"^\s*(?:import|from)\s+pipeline\.(s[1-7]_\w+)", re.MULTILINE)
CALLS_MODEL = re.compile(r"\bcall_model\s*\(")


def scan() -> list[tuple[str, str, int, str]]:
    """Returns (rule, relpath, lineno, detail)."""
    out: list[tuple[str, str, int, str]] = []
    py_files = [p for p in ROOT.rglob("*.py")
                if ".venv" not in p.parts and "__pycache__" not in p.parts]

    for path in py_files:
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()

        def lineno(idx: int) -> int:
            return text.count("\n", 0, idx) + 1

        # §0 -- the gate
        if rel not in EXEMPT["gate"]:
            for m in LLM_SDKS.finditer(text):
                out.append(("GATE", rel, lineno(m.start()),
                            f"imports {m.group(1)} directly. Every model call goes "
                            f"through trust.gateway.call_model()."))

        # §2 -- column identity
        if rel not in EXEMPT["colnames"]:
            for m in COLNAMES.finditer(text):
                out.append(("COLNAMES", rel, lineno(m.start()),
                            f"contains original column name {m.group(1)!r}. Use col_NNN."))

        # §3 -- label quarantine
        if rel.startswith("pipeline/") and rel not in EXEMPT["labels"]:
            for m in LABELS.finditer(text):
                out.append(("LABELS", rel, lineno(m.start()),
                            f"references {m.group(1)!r} inside pipeline/. "
                            f"Labels live in eval/."))

        # §1 -- stage isolation
        if rel.startswith("pipeline/") and not rel.startswith("pipeline/lib/"):
            me = Path(rel).stem
            for m in STAGE_IMPORT.finditer(text):
                if m.group(1) != me:
                    out.append(("ISOLATION", rel, lineno(m.start()),
                                f"imports {m.group(1)}. Stages talk through "
                                f"artifacts/, never imports."))

        # §5a -- GROUNDTRUTH: an original column name tied to a physical meaning on
        # the same line. This is the hand-fed lookup the challenge forbids.
        if rel not in EXEMPT["groundtruth"]:
            for i, line in enumerate(lines, start=1):
                if ORIGINAL_COL.search(line) and DOMAIN.search(line):
                    out.append(("GROUNDTRUTH", rel, i,
                                f"maps an original column name to a physical meaning: "
                                f"{line.strip()[:70]}"))

        # §5b -- DOMAIN: chemistry vocabulary in a file that reaches a model.
        # Operator rule text elsewhere is an accepted INPUT, not an injected hint.
        if CALLS_MODEL.search(text) and rel not in EXEMPT["domain"]:
            for lit in STRING_LITERAL.finditer(text):
                body = lit.group(0)
                if body.startswith(('"""', "'''")):
                    continue  # docstrings explain the rules; they are not prompts
                for m in DOMAIN.finditer(body):
                    out.append(("DOMAIN", rel, lineno(lit.start()),
                                f"prompt literal in a model-calling file contains "
                                f"{m.group(0)!r}. Prompts are generated from profiles."))
    return out


def main() -> int:
    findings = scan()
    if not findings:
        print(f"{GREEN}gate check passed{OFF}  "
              f"{DIM}no leaks, no label bleed, no hand-fed ground truth{OFF}")
        return 0

    by_rule: dict[str, list] = {}
    for rule, rel, line, detail in findings:
        by_rule.setdefault(rule, []).append((rel, line, detail))

    explain = {
        "GATE": "Raw data must never leave. This is the challenge's pass/fail condition.",
        "COLNAMES": "Original names after S1 break portability to a second dataset.",
        "LABELS": "Fault labels are for evaluation only. Using them to detect is cheating.",
        "ISOLATION": "Stage imports couple modules and break parallel work.",
        "GROUNDTRUTH": "Hand-fed column meanings forfeit criterion 1, 'no manual labelling'.",
        "DOMAIN": "Domain vocabulary reaching the model is a hint we are not allowed to give.",
    }

    for rule in ("GROUNDTRUTH", "GATE", "LABELS", "COLNAMES", "ISOLATION", "DOMAIN"):
        items = by_rule.get(rule)
        if not items:
            continue
        print(f"{RED}{rule}{OFF}  {DIM}{explain[rule]}{OFF}")
        for rel, line, detail in items:
            print(f"   {rel}:{line}  {detail}")
        print()

    print(f"{RED}{len(findings)} violation(s).{OFF} "
          f"{DIM}Fix before committing; CI runs this too.{OFF}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
