# -*- coding: utf-8 -*-
"""
Stage 2: XML Folder Parser → CSV (Elsevier META_ABS XML Full Parsing)
- 모든 coredata 필드
- authors, subjects
- openaccess 필드 전체
- scopus-id, scopus-eid
- link(rel=self), link(rel=scidir)
"""

import argparse
import os
import csv
import logging
from lxml import etree

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("xml_to_csv.log", encoding="utf-8"),
        logging.StreamHandler()
    ],
)

# -------- NAMESPACE 설정 --------
NS = {
    "prism": "http://prismstandard.org/namespaces/basic/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
    "d": "http://www.elsevier.com/xml/svapi/article/dtd",  # 기본 네임스페이스
}


# -------- Utility 함수 --------
def extract_one(tree, xpath):
    """XPath로 1개 텍스트 추출 (없으면 빈 문자열)"""
    try:
        res = tree.xpath(xpath, namespaces=NS)
        return res[0].text.strip() if res else ""
    except:
        return ""


def extract_multi(tree, xpath):
    """여러 개 텍스트 → ; 로 join"""
    try:
        res = tree.xpath(xpath, namespaces=NS)
        return "; ".join([x.text.strip() for x in res])
    except:
        return ""


# -------- XML 파서 --------
def parse_xml(file_path):
    """단일 XML 파일 파싱 → dict 반환"""

    try:
        tree = etree.parse(file_path)
    except Exception as e:
        logging.error(f"Failed to parse {file_path} | {e}")
        return None

    d = {}

    # --- 기본 coredata ---
    d["prism_url"] = extract_one(tree, "//prism:url")
    d["dc_identifier"] = extract_one(tree, "//dc:identifier")
    d["prism_doi"] = extract_one(tree, "//prism:doi")
    d["pii"] = extract_one(tree, "//d:pii")
    d["dc_title"] = extract_one(tree, "//dc:title")
    d["publicationName"] = extract_one(tree, "//prism:publicationName")
    d["aggregationType"] = extract_one(tree, "//prism:aggregationType")
    d["pubType"] = extract_one(tree, "//d:pubType")
    d["issn"] = extract_one(tree, "//prism:issn")
    d["volume"] = extract_one(tree, "//prism:volume")
    d["startingPage"] = extract_one(tree, "//prism:startingPage")
    d["pageRange"] = extract_one(tree, "//prism:pageRange")
    d["articleNumber"] = extract_one(tree, "//d:articleNumber")
    d["coverDate"] = extract_one(tree, "//prism:coverDate")
    d["coverDisplayDate"] = extract_one(tree, "//prism:coverDisplayDate")
    d["publisher"] = extract_one(tree, "//prism:publisher")
    d["copyright"] = extract_one(tree, "//prism:copyright")
    d["abstract"] = extract_one(tree, "//dc:description")

    # --- authors ---
    d["authors"] = extract_multi(tree, "//dc:creator")

    # --- subjects ---
    d["subjects"] = extract_multi(tree, "//dcterms:subject")

    # --- openaccess meta ---
    d["openaccess"] = extract_one(tree, "//d:openaccess")
    d["openaccessArticle"] = extract_one(tree, "//d:openaccessArticle")
    d["openaccessType"] = extract_one(tree, "//d:openaccessType")
    d["openArchiveArticle"] = extract_one(tree, "//d:openArchiveArticle")
    d["openaccessSponsorName"] = extract_one(tree, "//d:openaccessSponsorName")
    d["openaccessSponsorType"] = extract_one(tree, "//d:openaccessSponsorType")
    d["openaccessUserLicense"] = extract_one(tree, "//d:openaccessUserLicense")

    # --- Scopus info ---
    d["scopus_id"] = extract_one(tree, "//d:scopus-id")
    d["scopus_eid"] = extract_one(tree, "//d:scopus-eid")

    # --- links (기본 namespace) ---
    link_nodes = tree.xpath("//d:link", namespaces=NS)
    link_self, link_scidir = "", ""

    for node in link_nodes:
        rel = node.attrib.get("rel", "")
        if rel == "self":
            link_self = node.attrib.get("href", "")
        elif rel == "scidir":
            link_scidir = node.attrib.get("href", "")

    d["link_self"] = link_self
    d["link_scidir"] = link_scidir

    return d


# -------- Main --------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--xml_dir", type=str, required=True, help="XML 폴더 경로")
    parser.add_argument("--out", type=str, default="articles.csv", help="출력 CSV 파일명")

    args = parser.parse_args()
    xml_dir = args.xml_dir

    files = [f for f in os.listdir(xml_dir) if f.endswith(".xml")]
    logging.info(f"Found XML Files: {len(files)}")

    all_rows = []
    from tqdm import tqdm
    for fname in tqdm(files):
        fpath = os.path.join(xml_dir, fname)
        data = parse_xml(fpath)
        if data:
            data["source_file"] = fname
            all_rows.append(data)

    if not all_rows:
        logging.warning("No data parsed.")
        return

    # CSV 필드 자동 생성
    fieldnames = ["source_file"] + list(all_rows[0].keys())

    # 중복 제거
    fieldnames = list(dict.fromkeys(fieldnames))

    # CSV 저장
    with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    logging.info(f"CSV saved → {args.out}")
    logging.info(f"Total records: {len(all_rows)}")


if __name__ == "__main__":
    main()
