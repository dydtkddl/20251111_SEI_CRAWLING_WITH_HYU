#!/bin/bash
# install_deps.sh
# LibreOffice headless 변환에 필요한 폰트/환경 세팅

set -e
echo "=========================================="
echo "[1] 시스템 업데이트"
echo "=========================================="
sudo apt-get update -y

echo "=========================================="
echo "[2] LibreOffice 한글/CJK 폰트 설치 (깨짐 방지)"
echo "=========================================="
sudo apt-get install -y \
    fonts-nanum \
    fonts-nanum-coding \
    fonts-nanum-extra \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
    fonts-liberation \
    fontconfig

echo "=========================================="
echo "[3] 폰트 캐시 갱신"
echo "=========================================="
fc-cache -fv

echo "=========================================="
echo "[4] LibreOffice headless 의존성 확인"
echo "=========================================="
sudo apt-get install -y \
    libreoffice-writer \
    libreoffice-common \
    python3-uno

echo "=========================================="
echo "[5] 버전 확인"
echo "=========================================="
libreoffice --version
python3 -c "import subprocess; print('subprocess OK')"

echo ""
echo "✅ 설치 완료!"

