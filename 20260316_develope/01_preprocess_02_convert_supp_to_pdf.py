# 01_preprocess_02_convert_supp_to_pdf.py
"""
Supp 폴더 내 .docx / .doc / .rtf 파일을 LibreOffice headless로 PDF 변환

Input:
  - data/diagnostics/01_preprocess_01.csv  (미처리 대상 PII 목록)
  - SUPP_DIR 내 각 PII 폴더

Output:
  - 각 PII 폴더 내에 원본파일명.pdf 생성
  - data/diagnostics/01_preprocess_02_convert_report.csv  (변환 결과 리포트)

변환 대상 확장자: .docx, .doc, .rtf
제외 확장자:      .zip, .csv, .xlsx, .pptx, .pdf (이미 pdf인것도 스킵)
"""

import csv
import subprocess
import shutil
import time
from pathlib import Path
from datetime import datetime

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
SUPP_DIR   = Path("/mnt/d/20251111_SEI_CRAWLING_WITH_HYU/20260105_develope/supplementary_files")
IN_CSV     = Path("01_preprocess_01.csv")
OUT_DIR    = Path(".")
REPORT_CSV = OUT_DIR / "01_preprocess_02_convert_report.csv"

# 변환 대상 확장자
TARGET_EXTS = {".docx", ".doc", ".rtf"}

# LibreOffice 실행 경로
SOFFICE = shutil.which("libreoffice") or shutil.which("soffice")

# ── 전수조사: 변환 대상 파일 목록 수집 ───────────────────────────────────────
print("=" * 60)
print("[1] 전수조사 중...")

if not SOFFICE:
    raise RuntimeError("❌ LibreOffice를 찾을 수 없습니다. install_deps.sh 먼저 실행하세요.")
print(f"  LibreOffice 경로 : {SOFFICE}")

# 대상 PII 목록 로드
with open(IN_CSV, encoding="utf-8-sig") as f:
    target_piis = set(row["pii"] for row in csv.DictReader(f))
print(f"  대상 PII 수      : {len(target_piis)}")

# 변환 대상 파일 전수 수집
to_convert = []   # (pii, src_path, dst_path)
skip_rows  = []   # 제외 파일 로그

for pii in sorted(target_piis):
    folder = SUPP_DIR / pii
    if not folder.exists():
        continue

    for src in sorted(folder.iterdir()):
        if not src.is_file():
            continue

        ext = src.suffix.lower()
        dst = src.with_suffix(".pdf")

        if ext in TARGET_EXTS:
            if dst.exists():
                # 이미 변환된 경우 스킵
                skip_rows.append({
                    "pii"    : pii,
                    "src"    : src.name,
                    "status" : "SKIP_ALREADY_EXISTS",
                    "detail" : str(dst.name),
                    "elapsed": 0,
                })
            else:
                to_convert.append((pii, src, dst))
        else:
            skip_rows.append({
                "pii"    : pii,
                "src"    : src.name,
                "status" : "SKIP_EXT",
                "detail" : ext,
                "elapsed": 0,
            })

print(f"  변환 대상        : {len(to_convert)}개")
print(f"  스킵 대상        : {len(skip_rows)}개")
print()

# ── 변환 실행 ─────────────────────────────────────────────────────────────────
print("[2] PDF 변환 중...")
print(f"  시작 시각 : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("-" * 60)

results = []
ok_count   = 0
fail_count = 0

for idx, (pii, src, dst) in enumerate(to_convert, 1):
    t_start = time.time()
    try:
        proc = subprocess.run(
            [
                SOFFICE,
                "--headless",
                "--norestore",
                "--nofirststartwizard",
                "--convert-to", "pdf",
                "--outdir", str(src.parent),
                str(src),
            ],
            capture_output=True,
            text=True,
            timeout=120,   # 파일당 최대 2분
        )
        elapsed = round(time.time() - t_start, 2)

        if proc.returncode == 0 and dst.exists():
            status = "OK"
            detail = f"size={round(dst.stat().st_size/1024,1)}KB"
            ok_count += 1
            marker = "✅"
        else:
            # returncode 0인데 dst 없는 케이스 대비
            status = "FAIL"
            detail = proc.stderr.strip()[:200] if proc.stderr else "dst not created"
            fail_count += 1
            marker = "❌"

    except subprocess.TimeoutExpired:
        elapsed = 120
        status  = "TIMEOUT"
        detail  = "exceeded 120s"
        fail_count += 1
        marker  = "⏰"

    except Exception as e:
        elapsed = round(time.time() - t_start, 2)
        status  = "ERROR"
        detail  = str(e)[:200]
        fail_count += 1
        marker  = "💥"

    results.append({
        "pii"    : pii,
        "src"    : src.name,
        "status" : status,
        "detail" : detail,
        "elapsed": elapsed,
    })

    # 진행상황 출력 (50개마다 + 실패시 즉시)
    if idx % 50 == 0 or status != "OK":
        print(f"  [{idx:4d}/{len(to_convert)}] {marker} {pii} | {src.name} | {status} | {elapsed}s")

print("-" * 60)
print(f"  완료 시각 : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# ── 리포트 저장 ───────────────────────────────────────────────────────────────
print("[3] 리포트 저장 중...")

all_rows   = results + skip_rows
fieldnames = ["pii", "src", "status", "detail", "elapsed"]

with open(REPORT_CSV, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(all_rows)

# ── 최종 요약 ─────────────────────────────────────────────────────────────────
print(f"\n{'=' * 60}")
print("✅ 변환 완료 요약")
print(f"  변환 성공  : {ok_count}")
print(f"  변환 실패  : {fail_count}")
print(f"  스킵       : {len(skip_rows)}")
print(f"  리포트     → {REPORT_CSV}")
print("=" * 60)

# 실패 목록 있으면 출력
fails = [r for r in results if r["status"] != "OK"]
if fails:
    print("\n⚠️  실패 목록:")
    for r in fails:
        print(f"  {r['pii']} | {r['src']} | {r['status']} | {r['detail']}")

