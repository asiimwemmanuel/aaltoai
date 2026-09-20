# Model provider status — for Ezequiel

19 Sep, written by Tommaso (role E, owns `trust/`). Short handover on why S4
still produces no real inferences.

## Symptom

`python pipeline/s4_semantics.py` prints:

```
[S4] No model for the remaining columns: model endpoint unreachable (HTTP Error 404: Not Found)
```

and then falls back to the deterministic guess for every column.

The result looks plausible but is degenerate:

- 52 of 52 columns get the identical role `Manipulated Variable (Actuator/Valve)`
- all 52 at `confidence: high`
- zero columns have a `reasoning` field
- zero `model_call` entries in `artifacts/decision_log.jsonl`

The dataset is 41 measured and 11 manipulated variables, so a single role for
everything is wrong on its face. **Deliverable 1 is currently not met**, and the
egress panel correctly reports 0 because nothing ever left.

Your S4/S7 wiring is fine. The fallback did exactly what it should. There was
simply no model answering.

## What changed in `trust/` (my side, already committed)

`trust/gateway.py` now registers six providers behind the same `call_model()`:

| provider | egress | notes |
| --- | --- | --- |
| `local` | no | Ollama-compatible, `/api/generate` |
| `stub` | no | returns valid low-confidence JSON, no model |
| `google` | yes | Gemini via AI Studio |
| `anthropic` | yes | Messages API |
| `openai` | yes | any OpenAI-shaped `/chat/completions` |
| `eu_hosted` | yes | same shape, EU endpoint in config |

Adding one is a function plus a line in `_PROVIDERS`. No stage changes — that
swappability is Deliverable 8.

Keys are never written into `config/llm.yaml`, which is committed. The config
names an environment variable and `_api_key()` reads it.

Current config:

```yaml
llm:
  provider: google
  model: gemini-2.0-flash
  endpoint: https://generativelanguage.googleapis.com/v1beta
  api_key_env: GEMINI_API_KEY
```

The URL built by `_call_google()` is:

```
{endpoint}/models/{model}:generateContent?key={key}
```

## Most likely cause of the 404

The key we tried starts with `AQ.Ab8R...`. Google AI Studio API keys start with
`AIza...`. `AQ.` looks like an OAuth token from a different Google flow, which
would not authenticate against `?key=`.

Second candidate: the model name. `gemini-2.0-flash` may not be available for
that key or API version.

## Fastest way to tell them apart

```bash
export GEMINI_API_KEY="..."
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY" | head -40
```

- **List of models** → key is fine, the model name is wrong. Pick one from the
  list and set it in `config/llm.yaml`.
- **`API key not valid` / 401 / 403** → wrong key type. Create a new one at
  aistudio.google.com/apikey, it must start with `AIza`.

## One change worth making in S4

Right now a total failure is indistinguishable from a successful run: S4 exits 0,
writes a complete `semantics.json`, and the orchestrator reports success. We only
noticed because the roles were all identical.

Suggestion: if the model was unreachable for **every** column, S4 should say so
loudly — a non-zero exit, or at minimum a `degraded: true` marker in the output
and a decision-log entry of kind `config_change`. Silent degradation to a
two-branch heuristic is exactly the failure mode the challenge asks us to catch
in the data; we should not have it in our own pipeline.

## Fallback if Gemini stays blocked

Giorgio is close to shipping Ollama. `config/llm.yaml` already carries the local
profile under `llm_local_profile` — copy those four lines over `llm:` and rerun.
That also earns the no-egress bonus, so it is the better end state anyway.

Either way, once one real call lands, `semantics.json` should show varied roles,
mixed confidences and a `reasoning` field, and the decision log should show
`model_call` entries with byte counts and payload hashes.

## What I am doing meanwhile

Operator UX: plain-language diagnosis, the audit view behind a toggle, and the
trust indicator reading the real `trust_verdict`. That work reads the existing
artifacts and does not depend on the model, so it proceeds in parallel.
