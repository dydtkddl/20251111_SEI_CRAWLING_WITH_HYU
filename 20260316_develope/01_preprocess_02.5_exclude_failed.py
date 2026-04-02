# 01_preprocess_02.5_exclude_failed.py
"""
변환 실패 PII 제외한 CSV 저장
01_preprocess_01.csv → 01_preprocess_02.5.csv
"""

import csv
from pathlib import Path

IN_CSV   = Path("./01_preprocess_01.csv")
OUT_CSV  = Path("./01_preprocess_02.5.csv")
REPORT   = Path("./01_preprocess_02_convert_report.csv")

# 실패 PII 추출
failed_piis = set()
with open(REPORT, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        if row["status"] in ("FAIL", "ERROR", "TIMEOUT"):
            failed_piis.add(row["pii"])

print(f"실패 PII : {failed_piis}")

# 필터링 후 저장
with open(IN_CSV, encoding="utf-8-sig") as f:
    reader     = csv.DictReader(f)
    fieldnames = reader.fieldnames
    rows       = [r for r in reader if r["pii"] not in failed_piis]

with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

print(f"✅ {IN_CSV.name} ({len(rows) + len(failed_piis)}행) → {OUT_CSV.name} ({len(rows)}행)")
print(f"   제외 : {len(failed_piis)}개")


