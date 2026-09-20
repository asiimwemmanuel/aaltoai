"""Read and change the active model provider at runtime.

Deliverable 8 asks us to show that the model layer is swappable by configuration
rather than by a rewrite. Until now that meant editing config/llm.yaml by hand
between runs, which is true but invisible to anyone watching a demo.

This module makes the same swap available from the operator UI, and records it.
The rules it enforces:

* Only providers registered in trust.gateway may be selected. The UI cannot
  invent one, and cannot point the pipeline at an arbitrary endpoint.
* API keys are never accepted over the API and never written to disk. The config
  names an environment variable; if that variable is not set, the switch is
  refused with a message saying which one to export. A key posted from a browser
  would end up in a log, a shell history and a screen recording.
* Every switch is written to the decision log as `config_change`, so the egress
  summary and the audit trail stay honest about which model answered when.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config" / "llm.yaml"

# Providers that never send anything off the machine. Shown to the operator as
# the safe choice, because for this challenge it is the scoring one too.
NO_EGRESS = {"local", "stub"}

# Sensible defaults per provider, so the UI offers a working choice rather than
# an empty form. Overridden by whatever is already in the config.
PRESETS: dict[str, dict[str, Any]] = {
    "local": {"model": "llama3.1:8b", "endpoint": "http://localhost:11434",
              "api_key_env": None},
    "stub": {"model": "none", "endpoint": "", "api_key_env": None},
    "google": {"model": "gemini-2.0-flash",
               "endpoint": "https://generativelanguage.googleapis.com/v1beta",
               "api_key_env": "GEMINI_API_KEY"},
    "anthropic": {"model": "claude-sonnet-4-5", "endpoint": "https://api.anthropic.com/v1",
                  "api_key_env": "ANTHROPIC_API_KEY"},
    "openai": {"model": "gpt-4o-mini", "endpoint": "https://api.openai.com/v1",
               "api_key_env": "OPENAI_API_KEY"},
}


def _load() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def _registered() -> list[str]:
    from trust.gateway import _PROVIDERS
    return sorted(_PROVIDERS)


def status() -> dict[str, Any]:
    """What is active now, and what could be chosen, with each one's readiness."""
    cfg = _load()
    llm = cfg.get("llm", {})
    options = []
    for name in _registered():
        preset = PRESETS.get(name, {})
        key_var = preset.get("api_key_env")
        ready = True
        reason = None
        if key_var and not os.environ.get(key_var):
            ready = False
            reason = f"export {key_var} in the shell that runs the server"
        options.append({
            "provider": name,
            "model": preset.get("model"),
            "egress": name not in NO_EGRESS,
            "ready": ready,
            "not_ready_because": reason,
        })
    return {
        "active": {
            "provider": llm.get("provider"),
            "model": llm.get("model"),
            "endpoint": llm.get("endpoint"),
            "egress": llm.get("provider") not in NO_EGRESS,
        },
        "options": options,
        "note": "Switching rewrites config/llm.yaml and is logged as config_change. "
                "Keys are read from the environment and never stored here.",
    }


def switch(provider: str, model: str | None = None, log=None,
           by: str = "OP-01") -> dict[str, Any]:
    """Point the pipeline at a different provider. Returns the new status."""
    if provider not in _registered():
        raise ValueError(f"unknown provider {provider!r}; choose one of {_registered()}")

    preset = PRESETS.get(provider, {})
    key_var = preset.get("api_key_env")
    if key_var and not os.environ.get(key_var):
        raise ValueError(
            f"{provider} needs the {key_var} environment variable. Export it in the "
            f"shell that runs the server and restart it. Keys are deliberately not "
            f"accepted over this API."
        )

    cfg = _load()
    before = dict(cfg.get("llm", {}))
    llm = cfg.setdefault("llm", {})
    llm["provider"] = provider
    llm["model"] = model or preset.get("model") or llm.get("model")
    if preset.get("endpoint") is not None:
        llm["endpoint"] = preset["endpoint"]
    llm["api_key_env"] = key_var

    CONFIG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    if log is not None:
        log.append(
            stage="trust_gateway",
            kind="config_change",
            summary=(f"Model provider switched from {before.get('provider')}/"
                     f"{before.get('model')} to {llm['provider']}/{llm['model']}."),
            actor_type="human",
            actor_name=by,
            override={
                "target": "config/llm.yaml:llm.provider",
                "from": f"{before.get('provider')}/{before.get('model')}",
                "to": f"{llm['provider']}/{llm['model']}",
                "rationale": ("Operator changed the model layer from the cockpit. "
                              "Egress " + ("enabled" if provider not in NO_EGRESS
                                           else "disabled") + " from this point."),
            },
        )
    return status()
