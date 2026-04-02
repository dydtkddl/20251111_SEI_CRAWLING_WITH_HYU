# 01_preprocess_00.py
"""
PDF ↔ Supp 매칭된 PII만 추출하여 통합 메타 CSV 생성
- only_pdf / only_supp PII 제외
- 각 PII별 1행
- supp 내 파일목록/확장자 컬럼은 리스트형태로 저장 ("|" 구분)

Output:
  data/diagnostics/01_preprocess_00.csv
"""

import csv
import json
from pathlib import Path
from collections import Counter

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE     = Path("/mnt/d/20251111_SEI_CRAWLING_WITH_HYU/20260105_develope")
PDF_DIR  = BASE / "pdfs"
SUPP_DIR = BASE / "supplementary_files"
OUT_DIR  = Path("data/diagnostics")
SUMMARY  = OUT_DIR / "00_diag_summary.json"
OUT_CSV  = OUT_DIR / "01_preprocess_00.csv"

# ── 매칭 제외 PII 로드 ────────────────────────────────────────────────────────
with open(SUMMARY, encoding="utf-8") as f:
    summary = json.load(f)

exclude_piis = set(summary["match"]["only_pdf_piis"]) | set(summary["match"]["only_supp_piis"])
print(f"제외 PII 수 : {len(exclude_piis)}")

# ── PDF 정보 수집 ─────────────────────────────────────────────────────────────
print("[1] PDF 수집 중...")

pdf_map = {}  # pii → {filename, size_kb}
for f in sorted(PDF_DIR.glob("*.pdf")):
    pii = f.stem.replace("1-s2.0-", "").replace("-main", "")
    if pii in exclude_piis:
        continue
    pdf_map[pii] = {
        "pdf_filename" : f.name,
        "pdf_size_kb"  : round(f.stat().st_size / 1024, 2),
    }

print(f"  유효 PDF 수 : {len(pdf_map)}")

# ── Supp 정보 수집 ────────────────────────────────────────────────────────────
print("[2] Supp 수집 중...")

supp_map = {}  # pii → {folder_name, file_count, filenames, exts, sizes}
for folder in sorted([d for d in SUPP_DIR.iterdir() if d.is_dir()]):
    pii = folder.name
    if pii in exclude_piis:
        continue
    if pii not in pdf_map:
        continue

    files = sorted([f for f in folder.iterdir() if f.is_file()])

    filenames = [f.name for f in files]
    exts      = [f.suffix.lower() if f.suffix else "(no_ext)" for f in files]
    sizes     = [round(f.stat().st_size / 1024, 2) for f in files]
    ext_dist  = Counter(exts)

    supp_map[pii] = {
        "supp_folder_name"    : folder.name,
        "supp_file_count"     : len(files),
        "supp_is_empty"       : (len(files) == 0),
        "supp_filenames"      : "|".join(filenames),       # 리스트 → "|" 구분
        "supp_exts"           : "|".join(exts),            # 리스트 → "|" 구분
        "supp_sizes_kb"       : "|".join(map(str, sizes)), # 리스트 → "|" 구분
        # 확장자별 컬럼 (동적 생성용으로 별도 dict에 담아둠)
        "ext_dist"            : ext_dist,
    }

print(f"  유효 Supp 폴더 수 : {len(supp_map)}")

# ── 확장자 전체 목록 수집 (컬럼 동적 생성용) ──────────────────────────────────
all_exts = set()
for v in supp_map.values():
    all_exts.update(v["ext_dist"].keys())
all_exts = sorted(all_exts)  # 예: ['.csv', '.doc', '.docx', '.pptx', ...]
print(f"  확장자 종류 : {all_exts}")

# ── 통합 행 생성 ──────────────────────────────────────────────────────────────
print("[3] 통합 행 생성 중...")

# 컬럼 순서 정의
fieldnames = (
    ["pii"]
    + ["pdf_filename", "pdf_size_kb"]
    + ["supp_folder_name", "supp_file_count", "supp_is_empty"]
    + ["supp_filenames", "supp_exts", "supp_sizes_kb"]
    + [f"supp_ext_{e.lstrip('.')}" for e in all_exts]   # .docx → supp_ext_docx
)

rows = []
for pii in sorted(pdf_map.keys()):
    if pii not in supp_map:
        continue

    pdf  = pdf_map[pii]
    supp = supp_map[pii]

    row = {
        "pii"               : pii,
        "pdf_filename"      : pdf["pdf_filename"],
        "pdf_size_kb"       : pdf["pdf_size_kb"],
        "supp_folder_name"  : supp["supp_folder_name"],
        "supp_file_count"   : supp["supp_file_count"],
        "supp_is_empty"     : supp["supp_is_empty"],
        "supp_filenames"    : supp["supp_filenames"],
        "supp_exts"         : supp["supp_exts"],
        "supp_sizes_kb"     : supp["supp_sizes_kb"],
    }

    # 확장자별 카운트 컬럼
    for e in all_exts:
        col = f"supp_ext_{e.lstrip('.')}"
        row[col] = supp["ext_dist"].get(e, 0)

    rows.append(row)

print(f"  최종 행 수 : {len(rows)}")

# ── CSV 저장 ──────────────────────────────────────────────────────────────────
print("[4] CSV 저장 중...")

with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig: 엑셀 한글 깨짐 방지
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

print(f"\n✅ 저장 완료 → {OUT_CSV}")
print(f"   행 수    : {len(rows)}")
print(f"   컬럼 수  : {len(fieldnames)}")
print(f"   컬럼 목록: {fieldnames}")
print("=" * 60)


