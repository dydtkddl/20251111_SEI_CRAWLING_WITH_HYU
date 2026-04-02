# -*- coding: utf-8 -*-
"""
Stage 1: JSON Downloader (Argument-based URL builder)
- query, field, start, count, sort 를 argparse로 받고
- API key + base URL 은 코드에서 자동 조립
- 결과 JSON 파일을 jsons/<auto_name>.json 으로 저장
"""

import argparse
import requests
import json
import os
import logging

API_KEY = "3c271c9aec7337d30416c170817761ad"
BASE_URL = "https://api.elsevier.com/content/search/sciencedirect"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("json_downloader.log", encoding="utf-8"),
        logging.StreamHandler()
    ],
)

def save_json(data, name):
    os.makedirs("jsons", exist_ok=True)
    path = os.path.join("jsons", f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logging.info(f"Saved JSON → {path}")


def build_url(query, field, start, count, sort):
    """ScienceDirect Search API URL 조립"""
    return (
        f"{BASE_URL}"
        f"?apiKey={API_KEY}"
        f"&query={query.replace(' ', '%20')}"
        f"&field={field}"
        f"&start={start}"
        f"&count={count}"
        f"&sort={sort}"
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument("--query", type=str, required=True)
    parser.add_argument("--field", type=str, default="title,abstract")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--sort", type=str, default="relevance")

    args = parser.parse_args()

    url = build_url(args.query, args.field, args.start, args.count, args.sort)
    
    logging.info("Final built URL:")
    logging.info(url)

    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        logging.error(f"Download failed: {e}")
        return

    # 파일명 자동 생성
    safe_name = (
        f"{args.query.replace(' ', '_')}__"
        f"{args.start}_{args.count}_{args.sort}"
    )[:150]

    save_json(data, safe_name)


if __name__ == "__main__":
    main()
