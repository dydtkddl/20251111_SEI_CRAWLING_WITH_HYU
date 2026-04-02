# 00_diag.py
"""
Diagnostic script for SEI crawling data.
- PDFs: 1-s2.0-S{PII}-main.pdf
- Supplementary: folders named by PII, containing various files
"""

import os
import json
from pathlib import Path
from collections import Counter, defaultdict

# ── 경로 설정 ──────────────────────────────────────────────────────────────────
BASE      = Path("/mnt/d/20251111_SEI_CRAWLING_WITH_HYU/20260105_develope")
PDF_DIR   = BASE / "pdfs"
SUPP_DIR  = BASE / "supplementary_files"
OUT_DIR   = Path("data/diagnostics")
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ══════════════════════════════════════════════════════════════════════════════
# 1. PDF 통계
# ══════════════════════════════════════════════════════════════════════════════
print("=" * 60)
print("[1] PDF 분석 중...")

pdf_files   = sorted(PDF_DIR.glob("*.pdf"))
pdf_piis    = [f.stem.replace("1-s2.0-", "").replace("-main", "") for f in pdf_files]

pdf_stat = {
    "total_count": len(pdf_files),
    "piis": pdf_piis,
}

print(f"  총 PDF 수: {len(pdf_files)}")

# ══════════════════════════════════════════════════════════════════════════════
# 2. Supplementary 통계
# ══════════════════════════════════════════════════════════════════════════════
print("[2] Supplementary 분석 중...")

supp_folders = sorted([d for d in SUPP_DIR.iterdir() if d.is_dir()])

ext_counter       = Counter()   # 전체 확장자 분포
supp_records      = []          # 폴더별 상세
empty_folders     = []          # 빈 폴더 목록
file_count_dist   = Counter()   # 폴더당 파일 수 분포

for folder in supp_folders:
    files = [f for f in folder.iterdir() if f.is_file()]
    exts  = [f.suffix.lower() if f.suffix else "(no_ext)" for f in files]
    ext_counter.update(exts)

    n = len(files)
    file_count_dist[n] += 1

    record = {
        "pii"        : folder.name,
        "file_count" : n,
        "files"      : [
            {"name": f.name, "ext": f.suffix.lower() or "(no_ext)", "size_kb": round(f.stat().st_size / 1024, 2)}
            for f in sorted(files)
        ],
        "ext_summary": dict(Counter(exts)),
    }
    supp_records.append(record)

    if n == 0:
        empty_folders.append(folder.name)

supp_stat = {
    "total_folders"      : len(supp_folders),
    "empty_folder_count" : len(empty_folders),
    "empty_folders"      : empty_folders,
    "ext_distribution"   : dict(ext_counter.most_common()),
    "file_count_distribution": {          # 폴더당 파일 수 → 몇 개 폴더
        str(k): v
        for k, v in sorted(file_count_dist.items())
    },
    "folders"            : supp_records,
}

print(f"  총 Supp 폴더 수 : {len(supp_folders)}")
print(f"  빈 폴더 수      : {len(empty_folders)}")
print(f"  확장자 분포     : {dict(ext_counter.most_common())}")

# ══════════════════════════════════════════════════════════════════════════════
# 3. PDF ↔ Supp 매칭 통계
# ══════════════════════════════════════════════════════════════════════════════
print("[3] PDF ↔ Supp 매칭 분석 중...")

pdf_pii_set  = set(pdf_piis)
supp_pii_set = set(f["pii"] for f in supp_records)

only_in_pdf  = sorted(pdf_pii_set  - supp_pii_set)   # PDF만 있고 Supp 없는
only_in_supp = sorted(supp_pii_set - pdf_pii_set)    # Supp만 있고 PDF 없는
both         = sorted(pdf_pii_set  & supp_pii_set)   # 둘 다 있는

match_stat = {
    "pdf_total"        : len(pdf_pii_set),
    "supp_total"       : len(supp_pii_set),
    "matched_both"     : len(both),
    "only_pdf_count"   : len(only_in_pdf),
    "only_supp_count"  : len(only_in_supp),
    "only_pdf_piis"    : only_in_pdf,
    "only_supp_piis"   : only_in_supp,
}

print(f"  PDF & Supp 모두 있음 : {len(both)}")
print(f"  PDF만 있음           : {len(only_in_pdf)}")
print(f"  Supp만 있음          : {len(only_in_supp)}")

# ══════════════════════════════════════════════════════════════════════════════
# 4. 저장
# ══════════════════════════════════════════════════════════════════════════════
print("[4] JSON 저장 중...")

result = {
    "pdf_stat"   : pdf_stat,
    "supp_stat"  : supp_stat,
    "match_stat" : match_stat,
}

out_path = OUT_DIR / "00_diag.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

print(f"\n✅ 저장 완료 → {out_path}")
print("=" * 60)


