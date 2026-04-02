# -*- coding: utf-8 -*-
"""
03_pdf_download_mdpi_named_v2.py — Stable version (fixed headless download)
─────────────────────────────────────────────
✅ metadata.csv 기반 병렬 다운로드
✅ Headless Chrome download 경로 문제 해결
✅ PDF 자동 저장 (rename 안 함)
✅ 다운로드된 파일명 + 링크 매핑 CSV 생성
"""

import os
import re
import time
import argparse
import logging
import pandas as pd
from tqdm import tqdm
from multiprocessing import Pool
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options


# ─────────────────────────────
# Logging setup
# ─────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("mdpi_pdf_download_parallel.log"),
        logging.StreamHandler(),
    ],
)


# ─────────────────────────────
# Utility
# ─────────────────────────────
def parse_mdpi_pdf_info(url: str):
    """Extract (volume, issue, article_id) from MDPI PDF URL."""
    m = re.search(r"/(\d{1,3})/(\d{1,3})/(\d{1,5})/pdf", url)
    return m.groups() if m else ("NA", "NA", "NA")


def init_driver(download_dir):
    """Initialize Chrome headless driver with working download directory."""
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.6943.141 Safari/537.36"
    )

    prefs = {
        "download.default_directory": os.path.abspath(download_dir),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)

    logging.info(f"🧩 Launching Chrome with download dir: {download_dir}")
    return webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=options)


def wait_for_download_complete(folder, timeout=600):
    """Wait until no .crdownload files remain."""
    waited = 0
    while waited < timeout:
        downloading = [f for f in os.listdir(folder) if f.endswith(".crdownload")]
        if not downloading:
            return True
        time.sleep(1)
        waited += 1
    return False


# ─────────────────────────────
# PDF downloader
# ─────────────────────────────
def download_pdf(row):
    idx, pdf_url, outdir = row
    vol, iss, aid = parse_mdpi_pdf_info(pdf_url)
    result = {"idx": idx, "pdf_url": pdf_url, "volume": vol, "issue": iss, "article_id": aid, "downloaded_file": None}

    # 각 프로세스 전용 임시폴더 생성
    temp_dir = os.path.join(outdir, f"tmp_{idx}")
    os.makedirs(temp_dir, exist_ok=True)

    try:
        driver = init_driver(temp_dir)

        page_url = pdf_url.split("/pdf")[0]
        logging.info(f"🌐 [{idx}] Opening article page: {page_url}")
        driver.get(page_url)
        time.sleep(5)

        logging.info(f"⬇️  [{idx}] Downloading: {pdf_url}")
        driver.get(pdf_url)
        time.sleep(3)

        success = wait_for_download_complete(temp_dir, timeout=600)
        pdf_files = [f for f in os.listdir(temp_dir) if f.endswith(".pdf")]

        if success and pdf_files:
            latest = max([os.path.join(temp_dir, f) for f in pdf_files], key=os.path.getctime)
            dest = os.path.join(outdir, os.path.basename(latest))
            os.rename(latest, dest)
            result["downloaded_file"] = os.path.basename(dest)
            logging.info(f"✅ [{idx}] Download success → {os.path.basename(dest)}")
        else:
            logging.warning(f"⚠️ [{idx}] No .pdf file found after {pdf_url}")

        driver.quit()

    except Exception as e:
        logging.error(f"❌ [{idx}] {pdf_url} failed: {e}")

    finally:
        # temp 폴더 정리
        try:
            for f in os.listdir(temp_dir):
                os.remove(os.path.join(temp_dir, f))
            os.rmdir(temp_dir)
        except Exception:
            pass

    return result

# ─────────────────────────────
# Main
# ─────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta", default="metadata.csv")
    parser.add_argument("--out", default="mdpi_pdfs")
    parser.add_argument("--n_cpus", type=int, default=4)
    parser.add_argument("--map_csv", default="mdpi_download_map.csv")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)
    df = pd.read_csv(args.meta)
    if "pdf_url" not in df.columns:
        raise ValueError("metadata.csv must contain column 'pdf_url'")

    urls = [u for u in df["pdf_url"].dropna() if str(u).startswith("https")]
    data = [(i + 1, url, args.out) for i, url in enumerate(urls)]

    logging.info(f"🚀 Starting {len(data)} downloads using {args.n_cpus} CPUs")

    results = []
    with Pool(args.n_cpus) as pool:
        for res in tqdm(pool.imap_unordered(download_pdf, data), total=len(data), desc="📥 Downloading PDFs"):
            results.append(res)

    pd.DataFrame(results).to_csv(args.map_csv, index=False, encoding="utf-8")
    logging.info(f"🧾 Mapping CSV saved → {args.map_csv}")


if __name__ == "__main__":
    main()


