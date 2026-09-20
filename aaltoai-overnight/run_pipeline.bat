@echo off
echo Running S1 Ingestion...
python pipeline\s1_ingest.py
if %errorlevel% neq 0 exit /b %errorlevel%

echo Running S2 Profiling...
python pipeline\s2_profiling.py
if %errorlevel% neq 0 exit /b %errorlevel%

echo Running S3 Relations...
python pipeline\s3_relations.py
if %errorlevel% neq 0 exit /b %errorlevel%

echo Running S4 Semantics...
python pipeline\s4_semantics.py
if %errorlevel% neq 0 exit /b %errorlevel%

echo Running S6 Drift (Member D)...
python pipeline\s6_drift.py
if %errorlevel% neq 0 exit /b %errorlevel%

echo Running S7 Diagnosis (Member D)...
python pipeline\s7_diagnosis.py
if %errorlevel% neq 0 exit /b %errorlevel%

echo ALL STAGES COMPLETED SUCCESSFULLY!
