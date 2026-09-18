"""Dev-only: score S6 against the answer key. Never imported by the pipeline.

Reads artifacts/drift_events/*.json (from S6) and eval/run_index.json (from eval/dev_manifest.py).

Run from the Hackathon/ folder:  python eval/eval_s6.py
"""
import json
import os
import statistics


def alarm_samples(result):
    total = 0
    last = result["batch"]["last_sample"]
    for ev in result["events"]:
        end = ev["end_sample"] if ev["end_sample"] is not None else last
        total += end - ev["start_sample"] + 1
    return total


def main():
    with open("eval/run_index.json") as f:
        index = json.load(f)
    with open("contracts/reference_manifest.dev.json") as f:
        manifest = json.load(f)
    reference = set(manifest["fit"]) | set(manifest["calibration"])

    results = {}
    for run_id in index:
        path = f"artifacts/drift_events/{run_id}.json"
        if run_id not in reference and os.path.exists(path):
            with open(path) as f:
                results[run_id] = json.load(f)

    normal = [r for r in results if index[r]["faultNumber"] == 0]
    print("== False alarms on held-out normal runs ==")
    for r in normal:
        n = index[r]["n_samples"]
        print(f"{r}: {len(results[r]['events'])} event(s), {alarm_samples(results[r]) / n:.1%} of samples in alarm")

    print("\n== Detection per fault (4 runs each: train/test x 2 partitions) ==")
    print(f"{'fault':>5} {'found':>6} {'delay(med)':>10} {'pre-onset events':>17}  first post-onset event: type, statistic, top signals")
    for fault in range(1, 21):
        found, delays, pre, first = 0, [], 0, None
        for r, res in results.items():
            meta = index[r]
            if meta["faultNumber"] != fault:
                continue
            onset = meta["onset"]
            post = [e for e in res["events"] if e["end_sample"] is None or e["end_sample"] >= onset]
            pre += sum(1 for e in res["events"] if e["detected_at_sample"] < onset)
            if post:
                found += 1
                delays.append(max(post[0]["detected_at_sample"], onset) - onset)
                first = first or post[0]
        runs = sum(1 for r in results if index[r]["faultNumber"] == fault)
        top = ",".join(s["col_id"] for s in first["ranked_signals"][:3]) if first else "-"
        desc = f"{first['type']}, {first['statistic']}, {top}" if first else "-"
        delay = f"{statistics.median(delays):.0f}" if delays else "-"
        print(f"{fault:>5} {found:>3}/{runs:<2} {delay:>10} {pre:>17}  {desc}")


if __name__ == "__main__":
    main()
