@echo off
:: 한글 출력을 위한 인코딩 설정 (UTF-8)
chcp 65001 > nul
setlocal

:: ----------------------------------------------------------------
:: 1. 환경 변수 설정
:: ----------------------------------------------------------------
echo [INFO] 환경 변수를 설정합니다 (Hybrid Strategy 활성화)...
set MODEL_STRATEGY=hybrid

:: ----------------------------------------------------------------
:: 2. 파이프라인 실행 (새로운 run_full_pipeline.py 사용)
:: ----------------------------------------------------------------
echo [INFO] 메인 파이프라인을 시작합니다...
echo [INFO] Using: run_full_pipeline.py

python scripts/run_full_pipeline.py ^
    --paper-list paper_list.txt ^
    --data-root data ^
    --no-cache

:: ----------------------------------------------------------------
:: 3. 종료 처리 (에러 핸들링 포함)
:: ----------------------------------------------------------------
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] 스크립트 실행 중 오류가 발생했습니다. (Exit Code: %errorlevel%)
    exit /b %errorlevel%
)

echo.
echo [INFO] 모든 작업이 성공적으로 완료되었습니다.
endlocal