# -*- coding: utf-8 -*-
"""
02_extract_metadata.py
──────────────────────────────────────────────
MDPI Batteries HTML → 논문 메타데이터 추출
──────────────────────────────────────────────
입력: ./mdpi_batteries_output/html_raw/*.html
출력:
 ├─ metadata.csv
 └─ metadata.json
──────────────────────────────────────────────
"""

import os
import re
import glob
import json
import logging
import pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm

# ─────────────────────────────────────────────
HTML_DIR = "mdpi_batteries_output/html_raw"
OUT_CSV = "mdpi_batteries_output/metadata.csv"
OUT_JSON = "mdpi_batteries_output/metadata.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("MDPI_PARSE")

# ─────────────────────────────────────────────
def parse_article(article_div):
    """단일 <div class='article-content'> 블록 파싱"""
    try:
        title_tag = article_div.select_one("a.title-link")
        title = title_tag.get_text(strip=True) if title_tag else None
        url = f"https://www.mdpi.com{title_tag['href']}" if title_tag else None

        authors = ", ".join(
            [a.get_text(strip=True) for a in article_div.select("div.authors strong")]
        ) or None

        info = article_div.select_one("div.color-grey-dark")
        year = None
        doi = None
        if info:
            text = info.get_text(" ", strip=True)
            year_match = re.search(r"(\d{4})", text)
            year = year_match.group(1) if year_match else None
            doi_tag = info.find("a", href=re.compile(r"doi\.org"))
            doi = doi_tag["href"] if doi_tag else None

        pdf_tag = article_div.select_one("a.UD_Listings_ArticlePDF")
        pdf_url = f"https://www.mdpi.com{pdf_tag['href']}" if pdf_tag else None

        cited = None
        cite_tag = article_div.find("a", string=re.compile("Cited by"))
        if cite_tag:
            m = re.search(r"(\d+)", cite_tag.text)
            cited = m.group(1) if m else None

        abstract_tag = article_div.select_one("div.abstract-full")
        abstract = abstract_tag.get_text(" ", strip=True) if abstract_tag else None

        return {
            "title": title,
            "authors": authors,
            "year": year,
            "doi": doi,
            "pdf_url": pdf_url,
            "article_url": url,
            "cited_by": cited,
            "abstract": abstract,
        }
    except Exception as e:
        logger.warning(f"⚠️ parse error: {e}")
        return None

# ─────────────────────────────────────────────
def parse_file(html_path):
    """단일 HTML 파일 파싱"""
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    soup = BeautifulSoup(html, "html.parser")
    articles = soup.select("div.article-content")
    results = [parse_article(div) for div in articles if div]
    results = [r for r in results if r and r["title"]]
    return results

# ─────────────────────────────────────────────
def main():
    html_files = sorted(glob.glob(os.path.join(HTML_DIR, "*.html")))
    all_records = []

    logger.info(f"📂 Found {len(html_files)} HTML files in {HTML_DIR}")

    for html_path in tqdm(html_files, desc="🔍 Parsing HTML files"):
        records = parse_file(html_path)
        all_records.extend(records)

    df = pd.DataFrame(all_records)
    df.drop_duplicates(subset=["doi"], inplace=True)

    # CSV 저장
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    logger.info(f"✅ Metadata CSV saved → {OUT_CSV}")

    # JSON 저장
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_records, f, ensure_ascii=False, indent=2)
    logger.info(f"✅ Metadata JSON saved → {OUT_JSON}")

    logger.info(f"📑 Total articles parsed: {len(df)}")

# ─────────────────────────────────────────────
if __name__ == "__main__":
    main()


