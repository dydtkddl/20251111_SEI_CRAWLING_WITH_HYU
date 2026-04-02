# -*- coding: utf-8 -*-
"""
keyword_runner.py (Rate-limit + Retry 강화 버전)
─────────────────────────────────────────────
- 키워드 파일(JSON 또는 TXT) 입력
- 각 키워드에 대해 json_downloader.py 실행
- 병렬 실행 (n_cpus 지정 가능)
- 전역 Rate-limit (QPS 제한)
- 재시도 라운드 구조 (max_retries)
- tqdm + logging
"""

import argparse
import json
import subprocess
import logging
from multiprocessing import Pool, cpu_count, Manager
from tqdm import tqdm
import os
import time
import random

# ==========================
# LOGGING
# ==========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("keyword_runner.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# ==========================
# RATE LIMIT 설정
# ==========================

# 두 요청 사이 최소 간격 (초)
# Elsevier가 빡세게 막으니까 0.7~1.0 정도가 안전 영역
MIN_INTERVAL_BETWEEN_REQUESTS = 0.8

# 라운드 간 쿨다운 (429 많이 터졌으면 이 시간 동안 API 쉬게 함)
COOLDOWN_BETWEEN_ROUNDS = 10.0  # seconds


# Manager로 공유될 전역 객체들 (worker에서 접근)
_global_lock = None
_global_last_call = None


def init_worker(lock, last_call):
    """
    각 worker 프로세스가 시작될 때 실행되는 초기화 함수.
    전역 rate-limit용 lock / last_call 공유 객체를 바인딩.
    """
    global _global_lock, _global_last_call
    _global_lock = lock
    _global_last_call = last_call


# ---------------------------------------------------------
# Load keywords
# ---------------------------------------------------------
def load_keywords(path):
    ext = path.split(".")[-1].lower()

    if ext == "json":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [str(x).strip() for x in data if str(x).strip()]
    else:
        with open(path, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]


# ---------------------------------------------------------
# Worker: 실행 함수
# ---------------------------------------------------------
def run_single_keyword(keyword):
    """
    한 개 키워드에 대해 json_downloader.py 실행
    - 전역 rate-limit 적용
    - timeout + 예외 처리
    - json_downloader 로그 내용까지 보고 성공/실패 판정
    """
    global _global_lock, _global_last_call

    # 전역 rate-limit: 모든 프로세스가 이 구간을 공유
    if _global_lock is not None and _global_last_call is not None:
        with _global_lock:
            now = time.time()
            elapsed = now - _global_last_call.value
            if elapsed < MIN_INTERVAL_BETWEEN_REQUESTS:
                time.sleep(MIN_INTERVAL_BETWEEN_REQUESTS - elapsed)
            _global_last_call.value = time.time()

    # 아주 약간의 랜덤 지터 추가 (동일 타이밍 요청 방지)
    time.sleep(random.uniform(0.0, 0.3))

    cmd = [
        "python",
        "json_downloader.py",
        "--query", keyword,
        "--field", "title,abstract",
        "--start", "0",
        "--count", "100",
        "--sort", "relevance"
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30  # fail-safe timeout
        )

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        text_all = stdout + "\n" + stderr

        # json_downloader.py 안에서 logging.error("Download failed: ...") 쓰므로
        # 그 문자열을 기준으로 실패 판정
        if "Download failed" in text_all:
            success = False
        else:
            success = True

        return (keyword, success, stdout, stderr)

    except subprocess.TimeoutExpired:
        # 타임아웃도 실패로 간주하고, 나중에 재시도 대상에 포함
        return (keyword, False, "", "ERROR: TIMEOUT")

    except Exception as e:
        # 기타 예외도 실패로 간주
        return (keyword, False, "", f"ERROR: {e}")


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keywords_file", required=True,
                        help="Path to keywords JSON or TXT")
    parser.add_argument("--n_cpus", type=int, default=8,
                        help="Parallel processes (default=8)")
    parser.add_argument("--max_retries", type=int, default=5,
                        help="전체 라운드 수 (1 + 재시도 횟수, default=5)")

    args = parser.parse_args()

    keywords = load_keywords(args.keywords_file)
    keywords = list(dict.fromkeys(keywords))  # 중복 제거
    logging.info(f"Loaded {len(keywords)} unique keywords from {args.keywords_file}")

    n_workers = min(args.n_cpus, cpu_count())
    logging.info(f"Using {n_workers} CPU workers (logical cores: {cpu_count()})")

    os.makedirs("output", exist_ok=True)

    # 키워드별 시도 횟수 기록
    attempts = {kw: 0 for kw in keywords}

    # 아직 처리(또는 성공)되지 않은 키워드 목록
    remaining = keywords[:]

    # 전체 결과 로그 저장용
    all_results = []

    # -----------------------------
    # Retry 라운드 루프
    # -----------------------------
    for round_idx in range(1, args.max_retries + 1):
        if not remaining:
            logging.info(f"Nothing left to process at round {round_idx}.")
            break

        logging.info("=" * 80)
        logging.info(f"=== Round {round_idx} / {args.max_retries} 시작: {len(remaining)} keywords ===")

        # Manager로 전역 rate-limit 상태 공유
        manager = Manager()
        lock = manager.Lock()
        last_call = manager.Value('d', 0.0)

        round_results = []

        # 병렬 실행
        with Pool(
            processes=n_workers,
            initializer=init_worker,
            initargs=(lock, last_call)
        ) as pool:
            for kw, success, out, err in tqdm(
                pool.imap_unordered(run_single_keyword, remaining, chunksize=1),
                total=len(remaining),
                desc=f"Round {round_idx}"
            ):
                attempts[kw] += 1
                round_results.append((kw, success, out, err))
                all_results.append((kw, success, out, err))

        # 이번 라운드에서 실패한 키워드만 모음
        failed_keywords = [kw for (kw, success, _, _) in round_results if not success]
        success_count = len(remaining) - len(failed_keywords)

        logging.info(
            f"Round {round_idx} 완료 - 성공: {success_count}, 실패: {len(failed_keywords)}"
        )

        if not failed_keywords:
            logging.info("모든 키워드가 성공적으로 처리되었습니다. 더 이상 재시도하지 않습니다.")
            remaining = []
            break

        # 다음 라운드를 위해 실패 키워드만 재시도 대상에 남김
        remaining = failed_keywords

        if round_idx < args.max_retries:
            logging.info(
                f"{len(remaining)} 개 키워드가 실패하여 다음 라운드로 넘깁니다. "
                f"쿨다운 {COOLDOWN_BETWEEN_ROUNDS}초 대기 후 재시작."
            )
            time.sleep(COOLDOWN_BETWEEN_ROUNDS)

    # -----------------------------
    # 로그 파일 기록
    # -----------------------------
    log_path = "output/keyword_runner_output.log"
    with open(log_path, "w", encoding="utf-8") as f:
        for kw, success, out, err in all_results:
            f.write("=" * 80 + "\n")
            f.write(f"KEYWORD: {kw}\n")
            f.write(f"SUCCESS: {success}\n")
            f.write(f"ATTEMPTS: {attempts.get(kw, 0)}\n")
            f.write("- STDOUT -\n")
            f.write((out or "") + "\n\n")
            f.write("- STDERR -\n")
            f.write((err or "") + "\n\n")

    logging.info(f"Detailed run log saved to: {log_path}")

    # 최종 실패 키워드 정리
    if remaining:
        failed_path = "output/failed_keywords.txt"
        with open(failed_path, "w", encoding="utf-8") as f:
            for kw in remaining:
                f.write(f"{kw}\n")
        logging.warning(
            f"{len(remaining)} 개 키워드가 모든 라운드에서 실패했습니다. "
            f"목록: {failed_path}"
        )
    else:
        logging.info("✅ 모든 키워드가 최소 한 번 이상 성공적으로 다운로드 되었습니다.")


if __name__ == "__main__":
    main()
