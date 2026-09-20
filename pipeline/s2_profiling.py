import duckdb
import json
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.lib.logger import DecisionLogger


# How much bigger than the typical step between neighbours the largest step has
# to be before we call it a separation rather than a ripple. Not tuned against
# any answer: it is the point where one gap dominates the ordered sequence.
_SEPARATION_FACTOR = 10.0

# A column that almost never repeats a value is not plateauing, however it
# compares with its peers. This is the original absolute intent, kept as a
# necessary condition rather than a sufficient one.
_PLATEAU_FLOOR = 0.05


def cohort_split(ratios):
    """Where does an ordered set of ratios actually separate, if at all?

    The previous rule was `plateau_ratio > 0.05`, a constant. On the full
    dataset every one of the 52 columns scored between 0.072 and 0.287, so the
    flag was True for all of them: a constant wearing the costume of a
    measurement. S4 turned that into "manipulated, high confidence" for every
    column and fed the sentence to the model, which agreed. Both a hosted model
    and a local one collapsed structural_class to a single class because of it.

    So ask the cohort instead of a constant. Sort the ratios, find the largest
    step between neighbours, and compare it with the median step. If one step
    dominates, that is a real boundary and it becomes the threshold. If no step
    stands out, the statistic does not separate these columns, and saying so is
    the honest answer -- downstream must not be handed a split that is not there.

    Returns (threshold, separates). threshold is None when separates is False.
    """
    values = sorted(r for r in ratios if r is not None)
    if len(values) < 4:
        return None, False
    gaps = [(values[i + 1] - values[i], i) for i in range(len(values) - 1)]
    widest, at = max(gaps)
    if widest <= 0:
        # Every column scored the same. Nothing to separate.
        return None, False
    ordered = sorted(g for g, _ in gaps)
    median_gap = ordered[len(ordered) // 2]
    # A median gap of zero means most columns sit on top of each other while one
    # step is real: two tight groups, which is the cleanest separation there is.
    # Requiring `median_gap > 0` rejected exactly that case -- 20 columns at 0.01
    # against 11 at 0.9 came back "does not separate" -- so zero is handled first
    # rather than folded into the ratio below.
    if median_gap > 0 and widest < median_gap * _SEPARATION_FACTOR:
        return None, False
    # The boundary sits between the two columns the gap separates.
    threshold = (values[at] + values[at + 1]) / 2.0
    if threshold < _PLATEAU_FLOOR:
        return None, False
    return threshold, True

class ProfilingEngine:
    def __init__(self, artifacts_dir='artifacts', data_dir='data'):
        self.artifacts_dir = artifacts_dir
        self.data_dir = data_dir
        self.logger = DecisionLogger(os.path.join(artifacts_dir, 'decision_log.jsonl'))

    def run(self):
        print("[S2] Starting Profiling Engine...")
        con = duckdb.connect()
        
        schema_path = os.path.join(self.artifacts_dir, 'schema.json')
        with open(schema_path, 'r') as f:
            schema = json.load(f)
            
        cols = [col for col in schema["columns"].keys() if col != "col_time"]
        profiles = {}
        import glob
        features_path = glob.glob(os.path.join(self.data_dir, 'features', '**', '*.parquet'), recursive=True)
        
        for col in cols:
            query = f"""
            WITH lagged AS (
                SELECT {col} as val, {col} - lag({col}) OVER (PARTITION BY simulationRun ORDER BY col_time) as diff
                FROM read_parquet({features_path})
            )
            SELECT avg(val) as mean, stddev_pop(val) as std_dev, min(val) as min_val, max(val) as max_val,
                   approx_quantile(val, 0.25) as p25, approx_quantile(val, 0.50) as p50, approx_quantile(val, 0.75) as p75,
                   count(distinct val) as distinct_vals, kurtosis(val) as kurtosis, skewness(val) as skewness,
                   stddev_pop(diff) as noise_level, (sum(CASE WHEN diff = 0 THEN 1 ELSE 0 END)::DOUBLE / count(*)) as plateau_ratio
            FROM lagged
            """
            
            cursor = con.execute(query)
            col_names = [d[0] for d in cursor.description]
            row = dict(zip(col_names, cursor.fetchone()))

            profiles[col] = {
                "statistics": {
                    "mean": float(row["mean"]) if row["mean"] is not None else 0.0,
                    "std_dev": float(row["std_dev"]) if row["std_dev"] is not None else 0.0,
                    "min_val": float(row["min_val"]) if row["min_val"] is not None else 0.0,
                    "max_val": float(row["max_val"]) if row["max_val"] is not None else 0.0,
                    "quantiles": {"p25": float(row["p25"]) if row["p25"] is not None else 0.0, "p50": float(row["p50"]) if row["p50"] is not None else 0.0, "p75": float(row["p75"]) if row["p75"] is not None else 0.0},
                    "skewness": float(row["skewness"]) if row["skewness"] is not None else 0.0,
                    "distinct_values": int(row["distinct_vals"]),
                    "noise_level": float(row["noise_level"]) if row["noise_level"] is not None else 0.0,
                    "plateau_ratio": float(row["plateau_ratio"]) if row["plateau_ratio"] is not None else 0.0,
                    # Filled in below, once every column has been profiled: this
                    # flag is a statement about one column's position among the
                    # others, and cannot be decided one column at a time.
                    "has_plateaus": False
                },
                "evidence_ids": {"mean": f"ev_s2_{col}_mean", "std_dev": f"ev_s2_{col}_stddev", "distribution": f"ev_s2_{col}_dist", "dynamics": f"ev_s2_{col}_dyn"}
            }
            self.logger.log(stage="S2_Profile", action="profile_column", details=f"Profiled {col}", evidence_ids=list(profiles[col]["evidence_ids"].values()))
            
        # --- second pass: does plateau_ratio separate this cohort at all? ---
        ratios = [pr["statistics"]["plateau_ratio"] for pr in profiles.values()]
        threshold, separates = cohort_split(ratios)
        for col, profile in profiles.items():
            stats = profile["statistics"]
            stats["has_plateaus"] = bool(separates and stats["plateau_ratio"] > threshold)
            # Recorded so the next stage can tell "this column does not plateau"
            # from "this statistic cannot tell anyone apart here". They are very
            # different things and only one of them justifies a confident claim.
            stats["plateau_separates_cohort"] = separates
            stats["plateau_threshold"] = threshold

        n_flagged = sum(1 for pr in profiles.values() if pr["statistics"]["has_plateaus"])
        if separates:
            detail = (f"plateau_ratio separates the cohort at {threshold:.4f}: "
                      f"{n_flagged} of {len(profiles)} columns above it")
        else:
            detail = (f"plateau_ratio does not separate the cohort of {len(profiles)} "
                      f"columns; no column is flagged and downstream is told so")
        print(f"[S2] {detail}")
        self.logger.log(stage="S2_Profile", action="plateau_cohort_split", details=detail,
                        evidence_ids=[f"ev_s2_{c}_dyn" for c in profiles])

        with open(os.path.join(self.artifacts_dir, 'profiles.json'), 'w') as f:
            json.dump(profiles, f, indent=2)
            
        print("[S2] Profiling complete. profiles.json generated.")

if __name__ == "__main__":
    ProfilingEngine().run()
