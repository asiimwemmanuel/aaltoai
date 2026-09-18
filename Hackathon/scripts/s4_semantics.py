import json
import os

def run_s4_semantics():
    print("Loading S2 profiles and S3 relations...")
    with open('contracts/profiles.json', 'r') as f:
        profiles = json.load(f)
    
    with open('contracts/relations.json', 'r') as f:
        relations = json.load(f)
        
    print("Performing Structural First Pass (Actuator vs Sensor)...")
    
    # Identify which columns act as 'leaders' in time-lagged relationships
    leaders = set()
    for rel in relations:
        if rel['value']['leader'] != 'concurrent':
            leaders.add(rel['value']['leader'])
            
    semantics_output = {}
    
    print("Generating automated LLM prompts and building semantics.json...")
    
    for col_id, profile in profiles.items():
        # 1. Structural First Pass Logic
        is_leader = col_id in leaders
        has_plateaus = profile['statistics'].get('has_plateaus', False)
        
        # Rule of thumb: Actuators (valves) lead processes and often hold steady (plateau) at a setpoint.
        if is_leader or has_plateaus:
            structural_guess = "Manipulated Variable (Actuator/Valve)"
            confidence = "high"
        else:
            structural_guess = "Measured Variable (Sensor)"
            confidence = "low" # We force low confidence because a passive sensor is harder to identify without the LLM context
            
        # 2. Build the exact prompt that will be sent to the LLM
        prompt = f"""
System: You are an autonomous data agent analyzing an undocumented industrial process.
Task: Infer the physical role of column '{col_id}'.

Statistical Evidence:
- Standard Deviation: {profile['statistics'].get('std_dev', 0)} (Evidence ID: {profile['evidence_ids'].get('std_dev', '')})
- Has Plateaus: {has_plateaus} (Evidence ID: {profile['evidence_ids'].get('dynamics', '')})
- Is a leading indicator for other sensors: {is_leader}

Structural Pre-Analysis: Based on the math, this behaves like a {structural_guess}.

Instructions:
1. Validate or refine the Structural Pre-Analysis.
2. Propose a specific functional role.
3. If you lack evidence, you MUST declare 'low' confidence.

Output strictly as JSON: {{"role": string, "confidence": "high"|"low", "evidence_ids": [string]}}
"""
        
        # In the real pipeline, we would call: llm_response = call_model(prompt)
        # For now, we mock the LLM output to keep the team unblocked, but we save the generated prompt so you can read it!
        
        semantics_output[col_id] = {
            "inferred_role": structural_guess, 
            "confidence": confidence,
            "evidence_ids": list(profile['evidence_ids'].values()),
            "human_override": None,
            "debug_prompt_generated": prompt.strip() 
        }

    os.makedirs('contracts', exist_ok=True)
    with open('contracts/semantics.json', 'w') as f:
        json.dump(semantics_output, f, indent=2)
        
    print("S4 complete. contracts/semantics.json generated successfully.")

if __name__ == "__main__":
    run_s4_semantics()
