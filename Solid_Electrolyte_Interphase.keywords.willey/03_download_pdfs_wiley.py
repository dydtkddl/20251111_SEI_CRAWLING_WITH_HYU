# -*- coding: utf-8 -*-
"""
03_download_pdfs_wiley_dual_domain.py
──────────────────────────────────────────────
✅ Supports both Wiley main & Advanced domains
   (https://onlinelibrary.wiley.com/ and https://advanced.onlinelibrary.wiley.com/)
✅ Validates PDF magic number (filters out HTML / Cloudflare)
✅ Uses cookies.txt (Netscape format)
✅ Parallel curl download with tqdm
✅ Cloudflare anti-bot headers applied
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

# ─────────────────────────────
# Logging Configuration
# ─────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("wiley_dual_domain.log", mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

# ─────────────────────────────
# Constants
# ─────────────────────────────
UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
REFERER = "https://onlinelibrary.wiley.com/"

# ─────────────────────────────
# Utility Functions
# ─────────────────────────────
def sanitize(text: str) -> str:
    """Remove invalid characters for filenames"""
    return re.sub(r'[\\/*?:"<>|]', "_", text)


def check_pdf_valid(filepath: str) -> bool:
    """Check PDF header (%PDF) and remove HTML fallback"""
    try:
        with open(filepath, "rb") as f:
            head = f.read(200)
        if b"%PDF" in head and b"<html" not in head.lower():
            return True
        os.remove(filepath)
        return False
    except Exception:
        return False


def run_curl(cmd: str) -> bool:
    """Run curl command silently"""
    return subprocess.run(cmd, shell=True, stderr=subprocess.DEVNULL).returncode == 0


def get_pdf_url(doi: str) -> str:
    """
    Automatically determine correct domain:
    - advanced.onlinelibrary.wiley.com for Advanced-series journals
    - onlinelibrary.wiley.com for others
    """
    advanced_prefixes = ["adfm", "adma", "aenm", "small", "anie", "cssc", "advs", "aem", "adpr", "admt"]
    # check if DOI contains any advanced-series keyword
    if any(prefix in doi.lower() for prefix in advanced_prefixes):
        base = "https://advanced.onlinelibrary.wiley.com/doi/pdfdirect/"
    else:
        base = "https://onlinelibrary.wiley.com/doi/pdfdirect/"
    return f"{base}{doi}?download=true"


# ─────────────────────────────
# Download Function
# ─────────────────────────────
def download_one(row):
    idx, doi, cookies, outdir = row
    pdf_url = get_pdf_url(doi)
    outfile = os.path.join(outdir, f"{sanitize(doi)}.pdf")

    # Skip if already valid
    if os.path.exists(outfile) and os.path.getsize(outfile) > 10_000 and check_pdf_valid(outfile):
        return {"idx": idx, "doi": doi, "status": "exists"}

    cmd = (
        f'curl -s -L -b "{cookies}" '
        f'-A "{UA}" '
        f'-e "{REFERER}" '
        f'-H "Accept: application/pdf,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8" '
        f'-H "Sec-Fetch-Site: same-origin" '
        f'-H "Sec-Fetch-Mode: navigate" '
        f'-H "Sec-Fetch-Dest: document" '
        f'-H "Upgrade-Insecure-Requests: 1" '
        f'-H "Accept-Language: en-US,en;q=0.9,ko;q=0.8" '
        f'-o "{outfile}" "{pdf_url}"'
    )

    # Retry up to 3 times
    for attempt in range(3):
        run_curl(cmd)
        if check_pdf_valid(outfile):
            logging.info(f"✅ [{idx}] OK {outfile}")
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
    ap.add_argument("--meta", default="02_articles_metadata.csv", help="CSV file containing DOI list")
    ap.add_argument("--cookies", default="cookies.txt", help="Netscape format cookies file")
    ap.add_argument("--out", default="wiley_pdfs", help="Output folder for PDFs")
    ap.add_argument("--n_cpus", type=int, default=6, help="Number of parallel workers")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    df = pd.read_csv(args.meta)
    dois = [str(d).strip() for d in df["doi"].dropna() if d.startswith("10.")]
    data = [(i + 1, doi, args.cookies, args.out) for i, doi in enumerate(dois)]

    logging.info(f"🚀 Starting {len(data)} Wiley (main+advanced) downloads with {args.n_cpus} workers")

    results = []
    with Pool(args.n_cpus) as pool:
        for res in tqdm(pool.imap_unordered(download_one, data), total=len(data), desc="📥 Wiley PDFs"):
            results.append(res)

    out_csv = "wiley_dual_results.csv"
    pd.DataFrame(results).to_csv(out_csv, index=False)
    logging.info(f"💾 Saved results to {out_csv}")


# ─────────────────────────────
# Entrypoint
# ─────────────────────────────
if __name__ == "__main__":
    main()


