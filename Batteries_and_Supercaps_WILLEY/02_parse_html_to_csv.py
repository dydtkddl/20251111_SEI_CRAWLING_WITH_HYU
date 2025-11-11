# -*- coding: utf-8 -*-
"""
02_parse_html_to_csv.py
─────────────────────────────
html_pages/*.html → articles_metadata.csv
"""

import os, re, pandas as pd
from bs4 import BeautifulSoup
from tqdm import tqdm

SRC_DIR = "html_pages"
OUT_CSV = "articles_metadata.csv"

records = []

def parse_single(file):
    with open(file, encoding="utf-8") as f:
        soup = BeautifulSoup(f, "html.parser")
    lis = soup.find_all("li", class_=re.compile("search__item"))
    for li in lis:
        title_tag = li.select_one(".hlFld-Title a.publication_title")
        title = title_tag.get_text(strip=True) if title_tag else None
        href = title_tag["href"] if title_tag else None
        doi = href.replace("/doi/", "").strip() if href else None

        authors = ", ".join(a.get_text(strip=True) for a in li.select(".hlFld-ContribAuthor span"))
        access = li.select_one(".doi-access")
        access = access.get_text(strip=True) if access else None
        date = li.select_one(".meta__epubDate")
        date = date.get_text(" ", strip=True) if date else None
        journal = li.select_one(".publication_meta_serial")
        journal = journal.get_text(strip=True) if journal else None
        vol_issue = li.select_one(".publication_meta_volume_issue")
        vol_issue = vol_issue.get_text(strip=True) if vol_issue else None

        records.append({
            "title": title,
            "doi": doi,
            "href": href,
            "authors": authors,
            "journal": journal,
            "volume_issue": vol_issue,
            "date": date,
            "access": access,
            "source_file": os.path.basename(file)
        })

def main():
    files = sorted([os.path.join(SRC_DIR, f) for f in os.listdir(SRC_DIR) if f.endswith(".html")])
    for file in tqdm(files, desc="🔍 Parsing HTML"):
        parse_single(file)
    df = pd.DataFrame(records)
    df.drop_duplicates(subset=["doi"], inplace=True)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8")
    print(f"✅ Saved {len(df)} records → {OUT_CSV}")

if __name__ == "__main__":
    main()

