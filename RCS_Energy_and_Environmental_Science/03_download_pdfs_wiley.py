# -*- coding: utf-8 -*-
"""
RSC PDF auto-downloader (parallel version)
- multiprocessing.Pool 병렬 다운로드
- PDF magic number 검증
- curl 사용
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
from urllib.parse import urlparse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("rsc_pdf_download.log"),
        logging.StreamHandler(),
    ],
)

CSV_FILE = "rsc_capsules_all_pages.csv"
SAVE_DIR = "pdfs"
os.makedirs(SAVE_DIR, exist_ok=True)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"


def sanitize(text):
    return re.sub(r'[\\/*?:"<>|]', "_", text)


def safe_filename(url):
    path = urlparse(url).path
    fname = os.path.basename(path)

    if not fname:
        fname = url.split("/")[-1].split("?")[0]

    if not fname.endswith(".pdf"):
        fname += ".pdf"

    return sanitize(fname)


def check_pdf_valid(filepath):
    """PDF magic number 검증"""
    try:
        with open(filepath, "rb") as f:
            head = f.read(100)

        if b"%PDF" in head:
            return True

        os.remove(filepath)
        return False
    except:
        return False


def run_curl(cmd):
    """curl 실행"""
    return subprocess.run(cmd, shell=True, stderr=subprocess.DEVNULL).returncode == 0


def download_one(args):
    idx, url = args
    fname = safe_filename(url)
    outpath = os.path.join(SAVE_DIR, fname)

    # 이미 존재하면 스킵
    if os.path.exists(outpath) and os.path.getsize(outpath) > 5000:
        if check_pdf_valid(outpath):
            return {"idx": idx, "url": url, "status": "exists"}

    # curl 명령어
    cmd = (
        f'curl -s -L -A "{UA}" '
        f'-H "Accept: application/pdf" '
        f'-o "{outpath}" "{url}"'
    )

    # 3번 retry
    for attempt in range(3):
        run_curl(cmd)
        if check_pdf_valid(outpath):
            return {"idx": idx, "url": url, "status": "success"}

        time.sleep(1)

    return {"idx": idx, "url": url, "status": "failed"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", default=CSV_FILE)
    parser.add_argument("--out", default=SAVE_DIR)
    parser.add_argument("--n_cpus", type=int, default=6)
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    df = pd.read_csv(args.csv)
    pdf_links = df[df["pdf_link"].notna() & (df["pdf_link"] != "")]["pdf_link"].tolist()

    tasks = [(i + 1, url) for i, url in enumerate(pdf_links)]

    logging.info(f"🚀 Starting {len(tasks)} RSC PDF downloads with {args.n_cpus} workers")

    results = []
    with Pool(args.n_cpus) as pool:
        for r in tqdm(pool.imap_unordered(download_one, tasks), total=len(tasks)):
            results.append(r)

    pd.DataFrame(results).to_csv("rsc_pdf_results.csv", index=False)
    logging.info("✔ Saved results → rsc_pdf_results.csv")


if __name__ == "__main__":
    main()
