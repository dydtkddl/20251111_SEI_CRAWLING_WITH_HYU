# -*- coding: utf-8 -*-
"""
RSC capsule parser (multi-page)
html/ 폴더 내 모든 HTML을 순회하여
drawer-control 내부 capsule 제외 후 CSV로 저장
"""

import os
import glob
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
import re 
import glob 
OUTPUT_CSV = "rsc_capsules_all_pages.csv"


HTML_DIR = "html"

# 모든 html 수집
html_files = glob.glob(os.path.join(HTML_DIR, "*.html"))

# page_{num}.html 의 num 기준 정렬
def extract_number(path):
    m = re.search(r"page_(\d+)\.html", path)
    return int(m.group(1)) if m else 999999

# ------------------------------
# drawer-control 내부인지 확인
# ------------------------------
def inside_drawer(tag):
    p = tag.parent
    while p:
        if p.has_attr("class") and "drawer-control" in p.get("class", []):
            return True
        p = p.parent
    return False


# ------------------------------
# 날짜 파싱
# ------------------------------
def parse_date(raw):
    try:
        raw = raw.replace("The article was first published on", "").strip()
        d = datetime.strptime(raw, "%d %b %Y")
        return d.strftime("%Y-%m-%d")
    except:
        return raw


# ------------------------------
# capsule extractor (단일 HTML)
# ------------------------------
def extract_capsules(soup):
    BASE = "https://pubs.rsc.org"

    capsules = soup.find_all("div", class_="capsule capsule--article")
    rows = []
    print("###################################")
    print("###################################")
    print(len(capsules))
    print("###################################")
    print("###################################")
    for cap in capsules:

        # drawer-control 내부 제외
        if inside_drawer(cap):
            print("###########PASSSSSSSS")
            continue

        # --- article type ---
        ctx = cap.find("span", class_="capsule__context")
        article_type = ctx.get_text(strip=True) if ctx else ""

        # --- title ---
        title_tag = cap.find("h3", class_="capsule__title")
        title = title_tag.get_text(strip=True) if title_tag else ""

        # --- authors ---
        authors_tag = cap.find("div", class_="article__authors")
        authors = authors_tag.get_text(" ", strip=True) if authors_tag else ""

        # --- abstract short ---
        abs_tag = cap.find("div", class_="capsule__text")
        abstract_short = abs_tag.get_text(" ", strip=True) if abs_tag else ""

        # --- published date ---
        date_tag = cap.find("span", class_="block fixpadv--xs")
        published_date = parse_date(date_tag.get_text(strip=True)) if date_tag else ""

        # --- journal info + DOI ---
        journal_info = ""
        doi = ""

        footer = cap.find("div", class_="text--small")
        if footer:
            spans = footer.find_all("span")
            if len(spans) >= 2:
                journal_info = spans[1].get_text(strip=True)

            doi_tag = footer.find("a")
            if doi_tag:
                doi = doi_tag["href"]

        # --- Article landing link ---
        a_tag = cap.find("a", class_="capsule__action")
        article_link = BASE + a_tag["href"] if a_tag else ""

        # --- PDF link ---
        pdf_link = ""
        pdf_btn = cap.find("a", class_="btn btn--primary btn--tiny")
        if pdf_btn:
            pdf_link = BASE + pdf_btn["href"]

        # --- HTML link ---
        html_link = ""
        html_btn = cap.find_all("a", class_="btn btn--tiny")
        if html_btn:
            html_link = BASE + html_btn[-1]["href"]

        rows.append({
            "article_type": article_type,
            "title": title,
            "authors": authors,
            "abstract_short": abstract_short,
            "published_date": published_date,
            "journal_info": journal_info,
            "doi": doi,
            "article_link": article_link,
            "pdf_link": pdf_link,
            "html_link": html_link
        })

    return rows


# ------------------------------
# Main: html/ 폴더 순회
# ------------------------------
if __name__ == "__main__":

    all_rows = []
    html_files = sorted(html_files, key=extract_number)

    print(f"📄 Found {len(html_files)} HTML files in '{HTML_DIR}'")

    for i, html_file in enumerate(html_files, start=1):
        print(f"▶ Parsing ({i}/{len(html_files)}): {html_file}")

        html = open(html_file, "r", encoding="utf-8").read()
        soup = BeautifulSoup(html, "lxml")

        rows = extract_capsules(soup)
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    print("✔ CSV saved:", OUTPUT_CSV)
    print("✔ Total capsules extracted:", len(df))
    print(df.head())
