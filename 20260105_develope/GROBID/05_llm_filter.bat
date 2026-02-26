@echo off
REM LLM 분류 결과 기반 필터링
REM Abstract 제거, 신뢰도 0.6 이상만 유지

python 05_llm_filter.py ^
  --min_confidence 0.6 ^
  --remove_abstract

pause
