# -*- coding: utf-8 -*-
"""
📦 MDPI PDF 분할 스크립트 (Python + tqdm + logging)
───────────────────────────────────────────────
용상 @ KHU | 2025-11-11
- mdpi_pdfs 내 모든 PDF를 PDF_01~PDF_10 폴더로 균등하게 분할 이동
- tqdm으로 진행률 표시
- logging으로 전체 이동 로그 기록
"""

import os
import math
import shutil
import logging
from tqdm import tqdm

# ─────────────────────────────────────────────
# 설정
SRC_DIR = "mdpi_pdfs"
NUM_PARTS = 10
LOG_FILE = "split_pdf_folders.log"

# ─────────────────────────────────────────────
# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ─────────────────────────────────────────────
# PDF 파일 목록 수집
if not os.path.exists(SRC_DIR):
    logging.error(f"❌ 원본 디렉터리 '{SRC_DIR}' 없음.")
    raise SystemExit(1)

pdf_files = sorted([
    os.path.join(SRC_DIR, f)
    for f in os.listdir(SRC_DIR)
    if f.lower().endswith(".pdf")
])

total = len(pdf_files)
if total == 0:
    logging.error("❌ PDF 파일이 없습니다.")
    raise SystemExit(1)

per_part = math.ceil(total / NUM_PARTS)
logging.info(f"📂 총 {total}개 PDF를 {NUM_PARTS}개 폴더로 분할 (폴더당 약 {per_part}개)")

# ─────────────────────────────────────────────
# 폴더 생성
for i in range(1, NUM_PARTS + 1):
    folder = f"PDF_{i:02d}"
    os.makedirs(folder, exist_ok=True)

# ─────────────────────────────────────────────
# 파일 이동
part = 1
count = 0

for fpath in tqdm(pdf_files, desc="🚚 Moving PDFs", unit="file"):
    folder = f"PDF_{part:02d}"
    fname = os.path.basename(fpath)
    dest = os.path.join(folder, fname)

    try:
        shutil.move(fpath, dest)
        logging.info(f"{fname} → {folder}")
    except Exception as e:
        logging.error(f"⚠️ 이동 실패: {fname} ({e})")

    count += 1
    if count >= per_part and part < NUM_PARTS:
        part += 1
        count = 0

logging.info("✅ 모든 PDF가 PDF_01~PDF_10 폴더로 분할 완료!")
print("\n✅ 완료: PDF_01~PDF_10에 파일 분할 완료.")

