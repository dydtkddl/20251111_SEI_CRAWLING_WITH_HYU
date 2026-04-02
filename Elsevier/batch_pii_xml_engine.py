# -*- coding: utf-8 -*-
"""
Batch PII XML Downloader Engine (with success/failure logs, dedup, options)
---------------------------------------------------------------------------
- 입력: PII를 포함한 URL 컬럼이 있는 CSV
- 처리:
  1) URL에서 PII 추출
  2) 유효한 PII만 필터링
  3) PII 중복 제거 후, 각 PII에 대해 다운로더 스크립트(pi i_xml_downloader.py) 호출
  4) 병렬(subprocess + multiprocessing.Pool) 다운로드
- 출력:
  - engine_logs/engine_full.log  : 엔진 전체 진행 로그
  - engine_logs/engine_fail.log  : 실패한 PII만 모은 로그
  - download_results.csv         : PII별 성공/실패/리턴코드/메시지
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime
from multiprocessing import Pool, cpu_count
from typing import List, Optional, Tuple

import pandas as pd
import subprocess
from tqdm import tqdm

# ------------------------------------------------------------
# Logging setup (Engine-level)
# ------------------------------------------------------------

os.makedirs("engine_logs", exist_ok=True)

FULL_LOG_PATH = os.path.join("engine_logs", "engine_full.log")
FAIL_LOG_PATH = os.path.join("engine_logs", "engine_fail.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(FULL_LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("engine")

fail_logger = logging.getLogger("fail_logger")
fail_logger.setLevel(logging.WARNING)
_fail_handler = logging.FileHandler(FAIL_LOG_PATH, encoding="utf-8")
_fail_handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))
fail_logger.addHandler(_fail_handler)


# ------------------------------------------------------------
# Utility: Extract PII from URL
# ------------------------------------------------------------

_PII_PATTERN = re.compile(r"/pii/([A-Za-z0-9().-]+)")


def extract_pii(url: str) -> Optional[str]:
    """Extract Elsevier PII from ScienceDirect/Elsevier URL."""
    if not isinstance(url, str):
        return None
    m = _PII_PATTERN.search(url)
    if m:
        return m.group(1).strip()
    return None


# ------------------------------------------------------------
# Worker: run downloader via subprocess
# ------------------------------------------------------------

def run_downloader(task: Tuple[str, str, str, Optional[str], Optional[str], bool]) -> Tuple[str, str, int]:
    """
    Subprocess로 단일 PII에 대해 downloader 실행.
    반환: (pii, status_msg, returncode)
    """
    pii, view, script_path, api_key, out_dir, overwrite = task

    cmd: List[str] = [
        sys.executable,
        script_path,
        "--pii",
        pii,
        "--view",
        view,
    ]

    # 옵션 전달: api_key / out_dir / overwrite
    if api_key:
        cmd.extend(["--api_key", api_key])
    if out_dir:
        cmd.extend(["--out_dir", out_dir])
    if overwrite:
        cmd.append("--overwrite")

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        rc = result.returncode

        if rc == 0:
            status = "OK"
        else:
            # stderr가 있으면 에러 메시지로 활용
            err_msg = (result.stderr or "").strip()
            # 너무 길면 앞/뒤만
            if len(err_msg) > 500:
                err_msg = err_msg[:400] + " ... " + err_msg[-80:]
            status = f"FAIL (rc={rc}): {err_msg}"

        return pii, status, rc

    except Exception as e:
        # subprocess 자체가 터진 경우
        return pii, f"FAIL (EXCEPTION): {e}", -999


# ------------------------------------------------------------
# Main engine
# ------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Batch PII XML Downloader Engine"
    )
    parser.add_argument(
        "--csv",
        required=True,
        help="CSV file containing URLs (must include a URL column)",
    )
    parser.add_argument(
        "--url_col",
        default="url",
        help="Name of the URL column in CSV (default: 'url')",
    )
    parser.add_argument(
        "--view",
        default="META_ABS",
        help="Elsevier API view type (default: META_ABS)",
    )
    parser.add_argument(
        "--script",
        default="pii_xml_downloader.py",
        help="Downloader script path (default: pii_xml_downloader.py)",
    )
    parser.add_argument(
        "--n_cpus",
        type=int,
        default=4,
        help="Number of parallel workers (default: 4)",
    )
    parser.add_argument(
        "--api_key",
        type=str,
        default=None,
        help="API key to pass to downloader (optional). "
             "If omitted, downloader 내부 로직(ENV/DEFAULT)을 사용.",
    )
    parser.add_argument(
        "--out_dir",
        type=str,
        default="xmls",
        help="Output directory to pass to downloader (default: xmls)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="If set, pass --overwrite to downloader (force re-download).",
    )
    parser.add_argument(
        "--results_csv",
        type=str,
        default="download_results.csv",
        help="Path to save result CSV (default: download_results.csv)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    logger.info("===========================================")
    logger.info(" Batch PII XML Downloader Engine START")
    logger.info("===========================================")
    logger.info(f"CSV file   : {args.csv}")
    logger.info(f"URL column : {args.url_col}")
    logger.info(f"View       : {args.view}")
    logger.info(f"Script path: {args.script}")
    logger.info(f"n_cpus     : {args.n_cpus}")
    logger.info(f"out_dir    : {args.out_dir}")
    logger.info(f"overwrite  : {args.overwrite}")

    # --------------------------------------------------------
    # 1. CSV 로드
    # --------------------------------------------------------
    if not os.path.exists(args.csv):
        logger.error(f"CSV file not found: {args.csv}")
        return 1

    df = pd.read_csv(args.csv)

    if args.url_col not in df.columns:
        logger.error(f"CSV must contain column: '{args.url_col}'")
        return 1

    total_rows = len(df)
    logger.info(f"Loaded CSV rows: {total_rows}")

    # --------------------------------------------------------
    # 2. PII 추출
    # --------------------------------------------------------
    df["pii"] = df[args.url_col].apply(extract_pii)
    df_valid = df[df["pii"].notna()].copy()

    valid_rows = len(df_valid)
    logger.info(f"Rows with valid PII       : {valid_rows}")

    # PII 중복 제거
    # 순서를 유지한 채 unique하게
    unique_pii_list: List[str] = list(pd.Index(df_valid["pii"]).drop_duplicates())
    logger.info(f"Unique PII count (deduped): {len(unique_pii_list)}")

    if len(unique_pii_list) == 0:
        logger.warning("No valid PII found. Nothing to download.")
        return 0

    # --------------------------------------------------------
    # 3. Task 리스트 구성
    # --------------------------------------------------------
    tasks: List[Tuple[str, str, str, Optional[str], Optional[str], bool]] = [
        (pii, args.view, args.script, args.api_key, args.out_dir, args.overwrite)
        for pii in unique_pii_list
    ]

    # --------------------------------------------------------
    # 4. 병렬 실행
    # --------------------------------------------------------
    n_workers = min(args.n_cpus, cpu_count())
    if n_workers < args.n_cpus:
        logger.info(f"Requested n_cpus={args.n_cpus}, "
                    f"but limited by system to n_workers={n_workers}")

    logger.info(f"Starting parallel downloads (workers={n_workers})")

    results: List[Tuple[str, str, int]] = []

    try:
        with Pool(n_workers) as pool:
            for pii, status, rc in tqdm(
                pool.imap_unordered(run_downloader, tasks, chunksize=5),
                total=len(tasks),
                desc="Downloading XMLs",
            ):
                results.append((pii, status, rc))
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt received. Terminating pool...")
        return 1

    # --------------------------------------------------------
    # 5. 결과 정리 & CSV 저장
    # --------------------------------------------------------
    res_df = pd.DataFrame(results, columns=["pii", "status", "returncode"])
    res_df.to_csv(args.results_csv, index=False, encoding="utf-8-sig")
    logger.info(f"Result CSV saved → {args.results_csv}")

    # 실패 항목만 로그로 남기기
    failures = res_df[res_df["status"].str.startswith("FAIL")]
    n_fail = len(failures)
    n_ok = len(res_df) - n_fail

    if n_fail > 0:
        logger.warning(f"Failed downloads: {n_fail} / {len(res_df)}")
        for _, row in failures.iterrows():
            fail_logger.warning(f"{row['pii']} | {row['status']}")
    else:
        logger.info("No failed downloads.")

    logger.info("===========================================")
    logger.info(" Batch PII XML Downloader Engine DONE")
    logger.info(f"  Success: {n_ok}")
    logger.info(f"  Failed : {n_fail}")
    logger.info(f"Engine full log → {FULL_LOG_PATH}")
    logger.info(f"Engine fail log → {FAIL_LOG_PATH}")
    logger.info("===========================================")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
