# 00_diag.py
"""
Diagnostic script for SEI crawling data.
Outputs:
  - data/diagnostics/00_diag_pdf_meta.csv
  - data/diagnostics/00_diag_supp_meta.csv
  - data/diagnostics/00_diag_supp_detail.csv
  - data/diagnostics/00_diag_summary.json
"""

import os
import json
import csv
from pathlib import Path
from collections import Counter

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE     = Path("/mnt/d/20251111_SEI_CRAWLING_WITH_HYU/20260105_develope")
PDF_DIR  = BASE / "pdfs"
SUPP_DIR = BASE / "supplementary_files"
OUT_DIR  = Path("data/diagnostics")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# 1. PDF meta
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 60)
print("[1] PDF 분석 중...")

pdf_rows = []
for f in sorted(PDF_DIR.glob("*.pdf")):
    pii = f.stem.replace("1-s2.0-", "").replace("-main", "")
    pdf_rows.append({
        "pii"      : pii,
        "filename" : f.name,
        "size_kb"  : round(f.stat().st_size / 1024, 2),
    })

pdf_meta_path = OUT_DIR / "00_diag_pdf_meta.csv"
with open(pdf_meta_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["pii", "filename", "size_kb"])
    writer.writeheader()
    writer.writerows(pdf_rows)

print(f"  총 PDF 수 : {len(pdf_rows)}")
print(f"  저장 → {pdf_meta_path}")

# ══════════════════════════════════════════════════════════════════════════════
# 2. Supplementary meta + detail
# ══════════════════════════════════════════════════════════════════════════════
print("[2] Supplementary 분석 중...")

supp_meta_rows   = []
supp_detail_rows = []
ext_counter      = Counter()
file_count_dist  = Counter()

supp_folders = sorted([d for d in SUPP_DIR.iterdir() if d.is_dir()])

for folder in supp_folders:
    files = sorted([f for f in folder.iterdir() if f.is_file()])
    n     = len(files)
    exts  = [f.suffix.lower() if f.suffix else "(no_ext)" for f in files]

    ext_counter.update(exts)
    file_count_dist[n] += 1

    # supp_meta: 폴더당 1행
    supp_meta_rows.append({
        "pii"           : folder.name,
        "file_count"    : n,
        "is_empty"      : (n == 0),
        "ext_summary"   : ";".join(f"{e}:{c}" for e, c in Counter(exts).most_common()),
    })

    # supp_detail: 파일당 1행
    for f in files:
        supp_detail_rows.append({
            "pii"      : folder.name,
            "filename" : f.name,
            "ext"      : f.suffix.lower() if f.suffix else "(no_ext)",
            "size_kb"  : round(f.stat().st_size / 1024, 2),
        })

supp_meta_path   = OUT_DIR / "00_diag_supp_meta.csv"
supp_detail_path = OUT_DIR / "00_diag_supp_detail.csv"

with open(supp_meta_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["pii", "file_count", "is_empty", "ext_summary"])
    writer.writeheader()
    writer.writerows(supp_meta_rows)

with open(supp_detail_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["pii", "filename", "ext", "size_kb"])
    writer.writeheader()
    writer.writerows(supp_detail_rows)

empty_count = sum(1 for r in supp_meta_rows if r["is_empty"])
print(f"  총 Supp 폴더 수 : {len(supp_folders)}")
print(f"  빈 폴더 수      : {empty_count}")
print(f"  총 Supp 파일 수 : {len(supp_detail_rows)}")
print(f"  확장자 분포     : {dict(ext_counter.most_common())}")
print(f"  저장 → {supp_meta_path}")
print(f"  저장 → {supp_detail_path}")

# ══════════════════════════════════════════════════════════════════════════════
# 3. PDF ↔ Supp 매칭 통계
# ══════════════════════════════════════════════════════════════════════════════
print("[3] PDF ↔ Supp 매칭 분석 중...")

pdf_pii_set  = set(r["pii"] for r in pdf_rows)
supp_pii_set = set(r["pii"] for r in supp_meta_rows)

only_pdf  = sorted(pdf_pii_set  - supp_pii_set)
only_supp = sorted(supp_pii_set - pdf_pii_set)
both      = sorted(pdf_pii_set  & supp_pii_set)

print(f"  PDF & Supp 모두 : {len(both)}")
print(f"  PDF만 있음      : {len(only_pdf)}")
print(f"  Supp만 있음     : {len(only_supp)}")

# ══════════════════════════════════════════════════════════════════════════════
# 4. summary.json 저장
# ══════════════════════════════════════════════════════════════════════════════
print("[4] Summary JSON 저장 중...")

summary = {
    "pdf": {
        "total_count": len(pdf_rows),
    },
    "supp": {
        "total_folders"          : len(supp_folders),
        "empty_folder_count"     : empty_count,
        "total_files"            : len(supp_detail_rows),
        "ext_distribution"       : dict(ext_counter.most_common()),
        "file_count_distribution": {
            str(k): v
            for k, v in sorted(file_count_dist.items())
        },
    },
    "match": {
        "both_count"      : len(both),
        "only_pdf_count"  : len(only_pdf),
        "only_supp_count" : len(only_supp),
        "only_pdf_piis"   : only_pdf,
        "only_supp_piis"  : only_supp,
    },
}

summary_path = OUT_DIR / "00_diag_summary.json"
with open(summary_path, "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print(f"  저장 → {summary_path}")

# ══════════════════════════════════════════════════════════════════════════════
# 5. 최종 요약 출력
# ══════════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print("✅ 완료! 생성된 파일:")
print(f"  {pdf_meta_path}")
print(f"  {supp_meta_path}")
print(f"  {supp_detail_path}")
print(f"  {summary_path}")
print("=" * 60)

