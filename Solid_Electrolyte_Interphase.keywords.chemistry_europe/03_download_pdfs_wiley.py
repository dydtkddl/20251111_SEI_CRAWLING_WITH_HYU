# -*- coding: utf-8 -*-
"""
03_download_pdfs_wiley_final_verified.py
──────────────────────────────────────────────
✅ 완전 검증: PDF magic number + Cloudflare HTML 감지
✅ cookies.txt 기반 (Netscape format)
✅ curl 사용 (브라우저와 TLS 동일)
✅ 병렬 다운로드 + tqdm
"""

import os
import re
import time
import argparse
import logging
import pandas as pd
import subprocess
from multiprocessing import Pool
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("wiley_curl_final.log"),
        logging.StreamHandler(),
    ],
)

BASE = "https://chemistry-europe.onlinelibrary.wiley.com/doi/pdfdirect/"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
REFERER = "https://chemistry-europe.onlinelibrary.wiley.com/"

def sanitize(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", text)

def check_pdf_valid(filepath):
    """PDF magic number 및 HTML fallback 감지"""
    try:
        with open(filepath, "rb") as f:
            head = f.read(100)
        if b"%PDF" in head and b"<html" not in head.lower():
            return True
        os.remove(filepath)
        return False
    except Exception:
        return False

def run_curl(cmd):
    return subprocess.run(cmd, shell=True, stderr=subprocess.DEVNULL).returncode == 0

def download_one(row):
    idx, doi, cookies, outdir = row
    pdf_url = f"{BASE}{doi}?download=true"
    outfile = os.path.join(outdir, f"{sanitize(doi)}.pdf")

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

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", default="02_articles_metadata.csv")
    ap.add_argument("--cookies", default="cookies.txt")
    ap.add_argument("--out", default="wiley_pdfs")
    ap.add_argument("--n_cpus", type=int, default=6)
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    df = pd.read_csv(args.meta)
    dois = [str(d).strip() for d in df["doi"].dropna() if d.startswith("10.")]
    data = [(i + 1, doi, args.cookies, args.out) for i, doi in enumerate(dois)]

    logging.info(f"🚀 Starting {len(data)} verified downloads with {args.n_cpus} workers")
    results = []
    with Pool(args.n_cpus) as pool:
        for res in tqdm(pool.imap_unordered(download_one, data), total=len(data), desc="📥 Wiley PDFs"):
            results.append(res)

    pd.DataFrame(results).to_csv("wiley_final_results.csv", index=False)
    logging.info("🧾 Saved → wiley_final_results.csv")

if __name__ == "__main__":
    main()



