# AaltoAI: The Deterministic Industrial AI Pipeline

Welcome to our submission for the Industrial Fault Monitor Hackathon. 

## The Story: How It Works
The core philosophy of our architecture is simple: **AI hallucinates, but math does not.** 
The challenge demanded that we use an LLM to analyze industrial data, but strictly forbade the LLM from ever seeing the raw data (to prevent data leaks and hallucinations). 

To solve this, we decoupled the architecture into two distinct halves: **The Math Engines** and **The AI Interpreters**, separated by an impassable firewall.

1. **The Math Engines (S1, S2, S3, S6):** 
   We take 6GB of raw, undocumented sensor data and crush it using DuckDB and Polars. We extract standard deviations, causal relationships, leading indicators, and multivariate PCA drift anomalies. We save these indisputable mathematical facts as highly compressed JSON files (`profiles.json`, `relations.json`, `drift_events.json`). The raw data legally stops here.
2. **The Firewall (`trust/gateway.py`):** 
   Before any prompt is sent to the LLM, it must pass through the Gateway. The Gateway mathematically audits the payload, rejecting any array over 32 numbers, blocking forbidden words, and logging the exact SHA-256 hash of the prompt for the judges.
3. **The AI Interpreters (S4, S7):** 
   We feed the mathematical JSON summaries through the Gateway to a local `llama3.1` model. The AI acts strictly as a translator: it infers that a mathematical plateau means the sensor is an "Actuator Valve" (S4), and writes a human-readable Root Cause Analysis describing the PCA drift (S7). 
4. **The Human-in-the-Loop UI:** 
   The Operator Dashboard displays the AI's inferences alongside the exact mathematical "evidence cards" it used to make them. If the AI is wrong, the human operator can click "Override" to permanently overrule the machine.

---

## Step-by-Step Guide: How to Run the System

You do not need to manually run 7 different Python scripts. We have built a dedicated **Orchestrator** that securely manages the entire pipeline end-to-end.

### Step 1: Prerequisites
Ensure you have the required Python dependencies installed:
```bash
uv pip install duckdb polars pandas numpy jsonschema pyyaml fastapi uvicorn pydantic
```

### Step 2: Start the Local LLM
Ensure Ollama is installed and running in the background. Pull the required model if you haven't already:
```bash
ollama pull llama3.1:latest
```

### Step 3: Run the Orchestrator
To execute the pipeline and automatically spin up the UI, run the following command in your terminal from the root `aaltoai` directory:

**For a full production run (Crunches the massive 6GB dataset):**
```bash
python orchestrator.py --mode prod --start-ui
```

*(Note: If you just want to do a rapid test on a tiny subset of data, you can use `python orchestrator.py --mode dev --start-ui`)*

### Step 4: Interact with the UI
Once the orchestrator prints `Starting the Operator UI Server...`:
1. Open your web browser and navigate to: `http://localhost:8000/ui/`
2. Enter your name in the **Operator** field.
3. Click on any row to expand the mathematical evidence.
4. Click **Override** on any row to test the Human-in-the-Loop override functionality.
5. Click **Decision Log** at the top to view the chronological audit trail of the entire pipeline, including the secure LLM hashes.
