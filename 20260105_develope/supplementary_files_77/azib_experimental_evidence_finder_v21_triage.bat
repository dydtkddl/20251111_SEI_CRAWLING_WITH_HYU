@echo off
REM AZIB Paper-Level Triage v21.1 - Two-stage filter (Rule + LLM)
REM ================================================================

echo.
echo ====================================
echo AZIB Paper Triage v21.1
echo ====================================
echo.

cd /d "%~dp0"

REM Run with LLM (Ollama)
python azib_experimental_evidence_finder_v21_triage.py ^
    --input_dir 02_supplementary_md ^
    --output_json out_v21_triage.json ^
    --output_csv out_v21_triage.csv ^
    --top_k 5 ^
    --llm_backend ollama ^
    --llm_model qwen2.5:14b-instruct ^
    --llm_num_ctx 12000 ^
    --log_level DEBUG ^
    --log_file run_v21_triage.log

echo.
echo Done! Check out_v21_triage.json and out_v21_triage.csv
pause
