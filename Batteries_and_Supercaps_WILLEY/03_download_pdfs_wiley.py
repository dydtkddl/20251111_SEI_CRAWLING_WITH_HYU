# -*- coding: utf-8 -*-
"""
03_download_pdfs_wiley_cookie_requests.py
─────────────────────────────────────────────
✅ Wiley PDF 다운로드 (requests 기반, cookies.json 활용)
✅ Playwright로 CAPTCHA 통과 후 저장된 쿠키 재사용
✅ 403 차단 우회 / 병렬 다운로드 / 재시도 / 실패 HTML 저장
"""

import os
import re
import time
import random
import argparse
import logging
import json
import pandas as pd
import requests
from tqdm import tqdm
from multiprocessing import Pool
from urllib.parse import quote

# ─────────────────────────────
# Logging setup
# ─────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("wiley_pdf_download_cookie.log"),
        logging.StreamHandler(),
    ],
)

# ─────────────────────────────
BASE_PDF_URL = "https://chemistry-europe.onlinelibrary.wiley.com/doi/pdfdirect/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Referer": "https://chemistry-europe.onlinelibrary.wiley.com/",
    "Accept": "application/pdf,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─────────────────────────────
def sanitize_filename(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", text)

def load_cookies(cookie_file="cookies.json"):
    """Playwright 등으로 저장한 cookies.json을 requests용 dict로 변환"""
    if not os.path.exists(cookie_file):
        logging.warning("⚠️ cookies.json not found. Proceeding without cookies.")
        return {}
    with open(cookie_file, "r", encoding="utf-8") as f:
        cookies_raw = json.load(f)
    return {c["name"]: c["value"] for c in cookies_raw if "name" in c and "value" in c}

# ─────────────────────────────
def download_one(args):
    idx, doi, outdir, cookies = args
    result = {"idx": idx, "doi": doi, "status": "failed", "file": None}

    pdf_url = f"{BASE_PDF_URL}{quote(doi)}?download=true"
    safe_name = sanitize_filename(f"{doi}.pdf")
    out_path = os.path.join(outdir, safe_name)
    fail_path = os.path.join(outdir, f"fail_{safe_name}.html")

    if os.path.exists(out_path) and os.path.getsize(out_path) > 5000:
        result["status"] = "exists"
        result["file"] = out_path
        return result

    s = requests.Session()
    s.headers.update(HEADERS)
    s.cookies.update(cookies)

    try:
        for attempt in range(3):
            r = s.get(pdf_url, stream=True, timeout=60)
            ctype = r.headers.get("Content-Type", "").lower()

            if r.status_code == 200 and "pdf" in ctype:
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                result["status"] = "success"
                result["file"] = out_path
                logging.info(f"✅ [{idx}] {doi} → OK")
                break
            else:
                logging.warning(f"⚠️ [{idx}] {doi} failed ({r.status_code}) retry {attempt+1}/3")
                with open(fail_path, "wb") as f:
                    f.write(r.content)
                time.sleep(random.uniform(2, 4))
    except Exception as e:
        logging.error(f"❌ [{idx}] {doi} error: {e}")
    return result

# ─────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta", default="02_articles_metadata.csv")
    parser.add_argument("--out", default="wiley_pdfs")
    parser.add_argument("--cookies", default="cookies.json")
    parser.add_argument("--n_cpus", type=int, default=4)
    parser.add_argument("--map_csv", default="wiley_download_map.csv")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    df = pd.read_csv(args.meta)
    if "doi" not in df.columns:
        raise ValueError("metadata.csv must contain a 'doi' column")

    dois = [str(d).strip() for d in df["doi"].dropna() if d.startswith("10.")]
    cookies = load_cookies(args.cookies)
    data = [(i + 1, doi, args.out, cookies) for i, doi in enumerate(dois)]

    logging.info(f"🚀 Starting {len(data)} downloads with {args.n_cpus} CPUs (cookies loaded={len(cookies) > 0})")

    results = []
    with Pool(args.n_cpus) as pool:
        for res in tqdm(pool.imap_unordered(download_one, data), total=len(data), desc="📥 Downloading PDFs"):
            results.append(res)

    pd.DataFrame(results).to_csv(args.map_csv, index=False, encoding="utf-8")
    logging.info(f"🧾 Mapping CSV saved → {args.map_csv}")

# ─────────────────────────────
if __name__ == "__main__":
    main()
