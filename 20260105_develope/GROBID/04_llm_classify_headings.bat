@echo off
REM LLM 기반 섹션 헤딩 분류
REM Ollama qwen2.5:14b-instruct 사용

python 04_llm_classify_headings.py ^
  --ollama_url "http://localhost:11434" ^
  --llm_model "qwen2.5:14b-instruct" ^
  --min_count 1

pause
