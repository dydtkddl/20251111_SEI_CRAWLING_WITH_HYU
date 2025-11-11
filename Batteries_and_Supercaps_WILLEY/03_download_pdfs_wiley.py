# -*- coding: utf-8 -*-
"""
03_download_pdfs_wiley.py
─────────────────────────────
articles_metadata.csv 기반 PDF 다운로드
PDF URL 형식:
https://chemistry-europe.onlinelibrary.wiley.com/doi/pdfdirect/{DOI}?download=true
"""

import os, time, logging, pandas as pd, requests
from tqdm import tqdm

META_CSV = "articles_metadata.csv"
OUT_DIR = "wiley_pdfs"
MAP_CSV = "wiley_download_map.csv"
os.makedirs(OUT_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def download_pdf(row):
    doi = row["doi"]
    if not isinstance(doi, str) or not doi.startswith("10."):
        return None
    pdf_url = f"https://chemistry-europe.onlinelibrary.wiley.com/doi/pdfdirect/{doi}?download=true"
    fname = doi.replace("/", "_") + ".pdf"
    fpath = os.path.join(OUT_DIR, fname)

    try:
        r = requests.get(pdf_url, timeout=60)
        if r.status_code == 200 and r.headers.get("content-type","").startswith("application/pdf"):
            with open(fpath, "wb") as f: f.write(r.content)
            logging.info(f"✅ {doi}")
            return {"doi": doi, "pdf_url": pdf_url, "saved_as": fname, "status": "ok"}
        else:
            logging.warning(f"⚠️ {doi} bad status {r.status_code}")
            return {"doi": doi, "pdf_url": pdf_url, "saved_as": None, "status": f"http {r.status_code}"}
    except Exception as e:
        logging.error(f"❌ {doi}: {e}")
        return {"doi": doi, "pdf_url": pdf_url, "saved_as": None, "status": "error"}

def main():
    df = pd.read_csv(META_CSV)
    results = []
    for _, row in tqdm(df.iterrows(), total=len(df), desc="📥 Downloading PDFs"):
        res = download_pdf(row)
        if res: results.append(res)
        time.sleep(1.2)   # polite delay
    pd.DataFrame(results).to_csv(MAP_CSV, index=False, encoding="utf-8")
    logging.info(f"🧾 Map saved → {MAP_CSV}")

if __name__ == "__main__":
    main()

