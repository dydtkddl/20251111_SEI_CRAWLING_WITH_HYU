# 01_preprocess_01.py
"""
이미 marker 처리 완료된 80개 PDF를 제외하고
나머지 작업 대상만 01_preprocess_01.csv로 저장

Input:
  - data/diagnostics/01_preprocess_00.csv         (전체 매칭 PII 목록)
  - 01.preprocess.01.already_done80.pdfs_markdowns.json  (완료된 80개)

Output:
  - data/diagnostics/01_preprocess_01.csv          (미처리 대상만)
"""

import csv
import json
from pathlib import Path

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE_DIR   = Path(".")
IN_CSV     = BASE_DIR / "./01_preprocess_00.csv"
DONE_JSON  = BASE_DIR / "01.preprocess.01.already_done80.pdfs_markdowns.json"
OUT_CSV    = BASE_DIR / "01_preprocess_01.csv"

# ── 완료된 80개 PII 추출 ──────────────────────────────────────────────────────
print("=" * 60)
print("[1] 완료된 JSON 로드 중...")

with open(DONE_JSON, encoding="utf-8") as f:
    done_data = json.load(f)

done_piis = set()
for item in done_data["results"]:
    pdf_name = item["pdf"]  # 예: "1-s2.0-S0001868625001101-main.pdf"
    pii = pdf_name.replace("1-s2.0-", "").replace("-main.pdf", "")
    done_piis.add(pii)

print(f"  완료 PII 수 : {len(done_piis)}")
print(f"  예시        : {list(done_piis)[:3]}")

# ── 01_preprocess_00.csv 로드 후 필터링 ───────────────────────────────────────
print("[2] CSV 필터링 중...")

with open(IN_CSV, encoding="utf-8-sig") as f:
    reader     = csv.DictReader(f)
    fieldnames = reader.fieldnames
    all_rows   = list(reader)

print(f"  전체 행 수       : {len(all_rows)}")

remaining_rows = [r for r in all_rows if r["pii"] not in done_piis]
excluded_rows  = [r for r in all_rows if r["pii"] in done_piis]

print(f"  제외(완료) 행 수 : {len(excluded_rows)}")
print(f"  남은(미처리) 행 수: {len(remaining_rows)}")

# ── 혹시 JSON에 있는데 CSV에 없는 PII 체크 ───────────────────────────────────
csv_piis       = set(r["pii"] for r in all_rows)
done_not_in_csv = done_piis - csv_piis
if done_not_in_csv:
    print(f"  ⚠️  JSON엔 있는데 CSV엔 없는 PII : {done_not_in_csv}")
else:
    print(f"  ✅ JSON 완료 PII 전부 CSV에서 확인됨")

# ── 저장 ─────────────────────────────────────────────────────────────────────
print("[3] CSV 저장 중...")

with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(remaining_rows)

print(f"\n✅ 저장 완료 → {OUT_CSV}")
print(f"   전체      : {len(all_rows)}")
print(f"   완료 제외 : {len(excluded_rows)}")
print(f"   미처리    : {len(remaining_rows)}")
print("=" * 60)

