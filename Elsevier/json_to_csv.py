# -*- coding: utf-8 -*-
"""
Stage 2: JSON Folder Parser → CSV 통합
- jsons/ 폴더 내의 모든 JSON 파일 파싱
- entry 목록을 하나의 CSV로 통합
- source_json 컬럼 추가
"""

import os
import json
import csv
import logging
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("json_to_csv.log", encoding="utf-8"),
        logging.StreamHandler()
    ],
)

def parse_entries(data):
    """JSON 구조에서 entry 리스트 추출"""
    entries = data.get("search-results", {}).get("entry", [])
    rows = []
    for e in entries:
        url = e.get("prism:url", "")
        title = e.get("dc:title", "")
        rows.append({"url": url, "title": title})
    return rows


def main():
    JSON_DIR = "jsons"
    OUT_CSV = "combined.csv"

    files = [f for f in os.listdir(JSON_DIR) if f.endswith(".json")]

    all_rows = []

    with tqdm(total=len(files), desc="Parsing JSONs") as pbar:
        for fname in files:
            fpath = os.path.join(JSON_DIR, fname)

            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logging.error(f"Failed to load {fname}: {e}")
                pbar.update(1)
                continue

            entries = parse_entries(data)

            # 🔥 각 row에 source_json 추가
            for row in entries:
                row["source_json"] = fname

            all_rows.extend(entries)
            pbar.update(1)

    # -----------------------------
    # CSV 저장
    # -----------------------------
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "title", "source_json"])
        writer.writeheader()
        writer.writerows(all_rows)

    logging.info(f"Total merged rows = {len(all_rows)}")
    logging.info(f"CSV saved → {OUT_CSV}")


if __name__ == "__main__":
    main()
