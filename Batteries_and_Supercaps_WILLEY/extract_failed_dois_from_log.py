# -*- coding: utf-8 -*-
"""
extract_failed_dois_from_log.py — Wiley 다운로드 로그에서 실패 DOI 추출
─────────────────────────────────────────────
Usage:
  python extract_failed_dois_from_log.py --log wiley_curl_final.log --out failed_dois.txt
"""

import re
import argparse

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="wiley_curl_final.log")
    parser.add_argument("--out", default="failed_dois.txt")
    args = parser.parse_args()

    pattern = re.compile(r"(10\.1002/[^\s]+)")
    failed_keywords = ("invalid", "403", "fail", "error", "denied")

    dois = set()
    with open(args.log, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if any(k in line.lower() for k in failed_keywords):
                match = pattern.search(line)
                if match:
                    dois.add(match.group(1).strip())

    if dois:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(dois)))
        print(f"✅ {len(dois)} failed DOIs saved → {args.out}")
    else:
        print("✅ No failed DOIs found in log.")

if __name__ == "__main__":
    main()

