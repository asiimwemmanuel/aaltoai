import duckdb
from core.dq_engine import DataQualityEngine
from core.rule_compiler import RuleCompiler
from scripts.inject_fault import inject_frozen_sensor

con = duckdb.connect()
df = con.execute('SELECT * FROM read_parquet(?) LIMIT 50', ['data/features/simulationRun=1.0/data_0.parquet']).df()

engine = DataQualityEngine(schema_path='contracts/schema.json', profiles_path='artifacts/profiles.json')
compiler = RuleCompiler(schema_path='contracts/schema.json')

print('=== 1. TESTING REAL GIORGIO PARQUET BATCH ===')
rep1 = engine.check_batch(df, batch_id='giorgio_batch_01_clean')
print('Trust Verdict:', rep1['trust_verdict'])
print('Checks Passed:', rep1['checks_passed_count'], '/', rep1['checks_run_count'])
print('Failures:', len(rep1['failures']))

print('\n=== 2. INJECTING DEAD SENSOR ON col_007 (Reactor Pressure) ===')
df_dict = df.to_dict(orient='list')
corrupted, meta = inject_frozen_sensor(df_dict, target_col='col_007', freeze_val=2705.0, samples=20)
rep2 = engine.check_batch(corrupted, batch_id='giorgio_batch_02_frozen')
print('Trust Verdict:', rep2['trust_verdict'])
for f in rep2['failures']:
    print('ALERT: ' + f['check_type'] + ' on ' + f['target_col'] + ' | Severity: ' + f['severity'] + ' | Action: ' + f['action_taken'])

print('\n=== 3. EVALUATING HUMAN RULES ON REAL PARQUET DATA ===')
rules = compiler.compile_rules([
    'Reactor pressure must stay below 2900',
    'col_009 between 100 and 150'
])
rep3 = engine.check_batch(df, batch_id='giorgio_batch_03_rules', compiled_rules=rules)
for r in rep3['compiled_rules_evaluated']:
    print('Rule ' + r['rule_id'] + ': ' + r['raw_text'] + ' -> Target: ' + r['target_col'] + ' -> Status: ' + r['status'])
