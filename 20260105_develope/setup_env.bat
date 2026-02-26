@echo off
echo [1/4] Creating Conda environment 'crawling' with Python 3.10...
call conda create -n crawling python=3.10 --yes

echo [2/4] Activating environment 'crawling'...
call conda activate crawling

echo [3/4] Installing dependencies from requirements.txt...
if exist requirements.txt (
    pip install -r requirements.txt
) else (
    echo requirements.txt not found! Skipping pip install.
)

echo [4/4] Installing Playwright browsers...
playwright install

echo ========================================================
echo Setup Completed Successfully!
echo To use the environment, run: conda activate crawling
echo ========================================================
pause
