@echo off
:: 한글 출력을 위한 인코딩 설정 (UTF-8)
chcp 65001 > nul
setlocal

:: ----------------------------------------------------------------
:: Enterprise Grade Pipeline Execution Script
:: ----------------------------------------------------------------

:: 실행할 런 이름 설정 (이 부분을 수정하여 새로운 런 생성 가능)
set RUN_NAME=runs/run_enterprise_01

echo [INFO] 엔터프라이즈 파이프라인(%RUN_NAME%)을 시작합니다...

:: 1. 이전 결과 정리 (선택 사항: 주석 처리하면 덮어쓰거나 이어함)
:: if exist "%RUN_NAME%" (
::     echo [WARN] 기존 실행 디렉토리가 존재합니다. 내용을 정리하지 않고 진행합니다.
:: )

:: 2. 파이프라인 실행
:: --skip-grobid: 이미 GROBID 변환된 파일이 있다면 사용하여 속도 향상
python scripts/main_pipeline.py ^
    --pii-list paper_list.txt ^
    --pdf-dir ../pdfs ^
    --supp-dir ../supplementary_files ^
    --run-dir %RUN_NAME% ^
    --skip-grobid

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] 스크립트 실행 중 오류가 발생했습니다.
    exit /b %errorlevel%
)

echo.
echo [INFO] 모든 작업이 성공적으로 완료되었습니다.
echo [INFO] 결과 위치: %RUN_NAME%/derived/10_measurements_final.jsonl
pause
endlocal
