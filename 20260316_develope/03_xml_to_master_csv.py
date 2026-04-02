#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
00_xml_to_master_csv.py
=======================
Elsevier XML 디렉토리를 스캔하여 모든 메타데이터를 단일 CSV로 추출.

사용법:
    python 00_xml_to_master_csv.py \
        --xml_dir /mnt/d/20251111_SEI_CRAWLING_WITH_HYU/Elsevier/xmls \
        --output master_meta.csv

출력 컬럼:
    pii, pii_clean, title, abstract, journal, pub_type, aggregation_type,
    year, cover_date, doi, issn, volume, start_page, page_range,
    article_number, publisher, copyright, openaccess,
    authors, subjects, scopus_id, scopus_eid, pubmed_id,
    link_scidir, abstract_length, xml_filename
"""

import argparse
import csv
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {
    "default": "http://www.elsevier.com/xml/svapi/article/dtd",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "prism": "http://prismstandard.org/namespaces/basic/2.0/",
    "xocs": "http://www.elsevier.com/xml/xocs/dtd",
}

COLUMNS = [
    "pii", "pii_clean", "title", "abstract", "journal",
    "pub_type", "aggregation_type", "year", "cover_date",
    "doi", "issn", "isbn", "volume", "start_page", "page_range",
    "article_number", "publisher", "copyright", "openaccess",
    "authors", "subjects", "scopus_id", "scopus_eid", "pubmed_id",
    "link_scidir", "abstract_length", "xml_filename",
]


def text_or(root: ET.Element, *paths: str) -> str:
    """여러 XPath 경로를 시도하여 첫 번째 텍스트 반환."""
    for p in paths:
        el = root.find(p, NS)
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    return ""


def all_texts(root: ET.Element, *paths: str) -> list:
    """여러 XPath 경로에서 모든 텍스트 수집."""
    results = []
    for p in paths:
        for el in root.findall(p, NS):
            if el.text and el.text.strip():
                results.append(el.text.strip())
    return results


def parse_xml(xml_path: Path) -> dict:
    """단일 Elsevier XML → dict 변환."""
    row = {c: "" for c in COLUMNS}
    row["xml_filename"] = xml_path.name

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return row

    root = tree.getroot()
    core = root.find("default:coredata", NS)
    if core is None:
        core = root.find("coredata")
    if core is None:
        core = root  # fallback: 루트에서 직접 검색

    # PII
    pii_raw = text_or(core, "default:pii", "pii")
    row["pii"] = pii_raw
    row["pii_clean"] = re.sub(r"[^a-zA-Z0-9]", "", pii_raw)

    # 제목
    row["title"] = text_or(core, "dc:title")

    # 초록 — dc:description 내 전체 텍스트
    desc_el = core.find("dc:description", NS)
    if desc_el is not None:
        abstract = " ".join(desc_el.itertext()).strip()
        # 다중 공백 정리
        abstract = re.sub(r"\s+", " ", abstract)
        row["abstract"] = abstract
    row["abstract_length"] = str(len(row["abstract"]))

    # 저널
    row["journal"] = text_or(core, "prism:publicationName")

    # pubType, aggregationType
    row["pub_type"] = text_or(core, "default:pubType", "pubType")
    row["aggregation_type"] = text_or(core, "prism:aggregationType")

    # 날짜
    cover_date = text_or(core, "prism:coverDate")
    row["cover_date"] = cover_date
    ym = re.search(r"(19|20)\d{2}", cover_date)
    row["year"] = ym.group(0) if ym else ""

    # 식별자
    row["doi"] = text_or(core, "prism:doi")
    row["issn"] = text_or(core, "prism:issn")
    row["isbn"] = text_or(core, "prism:isbn")
    row["volume"] = text_or(core, "prism:volume")
    row["start_page"] = text_or(core, "prism:startingPage")
    row["page_range"] = text_or(core, "prism:pageRange")
    row["article_number"] = text_or(core, "default:articleNumber", "articleNumber")

    # 출판사, 저작권
    row["publisher"] = text_or(core, "prism:publisher")
    row["copyright"] = text_or(core, "prism:copyright")
    row["openaccess"] = text_or(core, "default:openaccess", "openaccess")

    # 저자 (세미콜론 구분)
    authors = all_texts(core, "dc:creator")
    row["authors"] = "; ".join(authors)

    # 키워드 (세미콜론 구분)
    subjects = all_texts(core, "dcterms:subject")
    row["subjects"] = "; ".join(subjects)

    # Scopus
    row["scopus_id"] = text_or(root, "default:scopus-id", "scopus-id")
    row["scopus_eid"] = text_or(root, "default:scopus-eid", "scopus-eid")
    row["pubmed_id"] = text_or(root, "default:pubmed-id", "pubmed-id")

    # ScienceDirect 링크
    for link in core.findall("default:link", NS) + core.findall("link"):
        rel = link.get("rel", "")
        href = link.get("href", "")
        if rel == "scidir":
            row["link_scidir"] = href

    return row


def main():
    parser = argparse.ArgumentParser(description="Elsevier XML → master CSV")
    parser.add_argument("--xml_dir", required=True, help="XML 디렉토리 경로")
    parser.add_argument("--output", default="master_meta.csv", help="출력 CSV 경로")
    args = parser.parse_args()

    xml_dir = Path(args.xml_dir)
    if not xml_dir.exists():
        print(f"ERROR: {xml_dir} not found")
        sys.exit(1)

    xml_files = sorted(xml_dir.glob("*.xml"))
    print(f"Found {len(xml_files)} XML files in {xml_dir}")

    out_path = Path(args.output)
    parsed = 0
    empty_abstract = 0
    errors = 0

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()

        for xf in xml_files:
            row = parse_xml(xf)
            writer.writerow(row)
            parsed += 1
            if not row["abstract"]:
                empty_abstract += 1
            if not row["pii"]:
                errors += 1

    print(f"Done: {parsed} parsed → {out_path}")
    print(f"  Empty abstracts: {empty_abstract}")
    print(f"  Missing PII: {errors}")
    print(f"  pub_type breakdown:")

    # 간단한 통계 출력
    import pandas as pd
    df = pd.read_csv(out_path)
    print(df["pub_type"].value_counts().to_string(header=False))
    print(f"\n  aggregation_type breakdown:")
    print(df["aggregation_type"].value_counts().to_string(header=False))
    print(f"\n  year range: {df['year'].min()} ~ {df['year'].max()}")
    print(f"  abstract length: mean={df['abstract_length'].astype(int).mean():.0f}, "
          f"median={df['abstract_length'].astype(int).median():.0f}")


if __name__ == "__main__":
    main()
