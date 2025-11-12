# -*- coding: utf-8 -*-
"""
03_download_pdfs_wiley_retry_from_txt.py
──────────────────────────────────────────────
✅ failed_dois.txt 기반 재시도 다운로드
✅ PDF magic number + HTML fallback 감지
✅ cookies.txt 기반 (Netscape format)
✅ curl 사용 (TLS/브라우저 호환)
✅ 병렬 다운로드 + tqdm
"""

import os
import re
import time
import argparse
import logging
import subprocess
from multiprocessing import Pool
from tqdm import tqdm

# ─────────────────────────────
# Logging setup
# ─────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("wiley_retry.log"),
        logging.StreamHandler(),
    ],
)

# ─────────────────────────────
# Constants
# ─────────────────────────────
BASE = "https://chemistry-europe.onlinelibrary.wiley.com/doi/pdfdirect/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
REFERER = "https://chemistry-europe.onlinelibrary.wiley.com/"

# ─────────────────────────────
# Helpers
# ─────────────────────────────
def sanitize(text: str) -> str:
    """파일 이름에 사용할 수 없는 문자 제거"""
    return re.sub(r'[\\/*?:"<>|]', "_", text)

def check_pdf_valid(filepath):
    """PDF magic number 및 HTML fallback 감지"""
    try:
        with open(filepath, "rb") as f:
            head = f.read(200)
        if b"%PDF" in head and b"<html" not in head.lower():
            return True
        os.remove(filepath)
        return False
    except Exception:
        return False

def run_curl(cmd):
    return subprocess.run(cmd, shell=True, stderr=subprocess.DEVNULL).returncode == 0

# ─────────────────────────────
# Worker
# ─────────────────────────────
def download_one(row):
    idx, doi, cookies, outdir = row
    pdf_url = f"{BASE}{doi}?download=true"
    outfile = os.path.join(outdir, f"{sanitize(doi)}.pdf")

    # 이미 성공한 파일 존재 시 skip
    if os.path.exists(outfile) and os.path.getsize(outfile) > 10_000 and check_pdf_valid(outfile):
        return {"idx": idx, "doi": doi, "status": "exists"}

    cmd = (
        f'curl -s -L -b "{cookies}" '
        f'-A "{UA}" '
        f'-e "{REFERER}" '
        f'-H "Accept: application/pdf,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" '
        f'-o "{outfile}" "{pdf_url}"'
    )

    for attempt in range(3):
        run_curl(cmd)
        if check_pdf_valid(outfile):
            logging.info(f"✅ [{idx}] OK → {outfile}")
            return {"idx": idx, "doi": doi, "status": "success"}
        else:
            logging.warning(f"⚠️ [{idx}] {doi} invalid/403 (attempt {attempt+1}/3)")
            time.sleep(2)

    return {"idx": idx, "doi": doi, "status": "failed"}

# ─────────────────────────────
# Main
# ─────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--txt", default="failed_dois.txt", help="TXT file containing DOIs (one per line)")
    ap.add_argument("--cookies", default="cookies.txt", help="cookies.txt (Netscape format)")
    ap.add_argument("--out", default="wiley_pdfs_retry", help="Output directory for retried PDFs")
    ap.add_argument("--n_cpus", type=int, default=6, help="Number of parallel workers")
    args = ap.parse_args()

    if not os.path.exists(args.txt):
        raise FileNotFoundError(f"DOI list file not found: {args.txt}")
    if not os.path.exists(args.cookies):
        raise FileNotFoundError(f"Cookies file not found: {args.cookies}")

    os.makedirs(args.out, exist_ok=True)

    # TXT 읽기
    with open(args.txt, "r", encoding="utf-8") as f:
        dois = [line.strip() for line in f if line.strip().startswith("10.")]

    data = [(i + 1, doi, args.cookies, args.out) for i, doi in enumerate(dois)]

    logging.info(f"🚀 Starting {len(data)} verified re-downloads with {args.n_cpus} workers")
    results = []
    with Pool(args.n_cpus) as pool:
        for res in tqdm(pool.imap_unordered(download_one, data), total=len(data), desc="📥 Wiley Retry PDFs"):
            results.append(res)

    # 결과 저장
    import pandas as pd
    pd.DataFrame(results).to_csv("wiley_retry_results.csv", index=False)
    logging.info("🧾 Saved → wiley_retry_results.csv")

# ─────────────────────────────
if __name__ == "__main__":
    main()

