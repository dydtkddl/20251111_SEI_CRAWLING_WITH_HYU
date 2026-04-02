# -*- coding: utf-8 -*-
"""
Stage 1: Elsevier PII XML Downloader (Improved)
----------------------------------------------
- PII & view를 argparse로 입력받아
- Elsevier Article API에서 XML을 다운로드
- xmls/<pii>__<view>.xml 형태로 저장

Features
- HTTP status check + 예외 처리
- 429 / 5xx 응답에 대해 재시도 (retry + backoff)
- 환경변수 ELSEVIER_API_KEY 우선 사용, 없으면 코드 내 기본값 사용
- 이미 존재하는 파일은 기본적으로 스킵, --overwrite 옵션으로 강제 덮어쓰기
- 상세 logging
"""

import argparse
import logging
import os
import time
from typing import Optional

import requests

# -----------------------------
# 기본 설정
# -----------------------------

# 1) 환경변수 우선, 없으면 fallback
_DEFAULT_API_KEY = "3c271c9aec7337d30416c170817761ad"
API_KEY = os.getenv("ELSEVIER_API_KEY", _DEFAULT_API_KEY)

BASE_URL = "https://api.elsevier.com/content/article/pii"

# 재시도 설정
MAX_RETRIES = 3
BACKOFF_SECONDS = 3.0  # 1차 3초, 2차 6초, 3차 9초 ...


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("download_xml.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger(__name__)


# -----------------------------
# 유틸 함수
# -----------------------------


def build_request_url(pii: str) -> str:
    """PII 기반으로 base URL 구성."""
    return f"{BASE_URL}/{pii}"


def save_xml(content: bytes, path: str) -> None:
    """바이너리 XML 내용을 지정 경로에 저장."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    logger.info(f"Saved XML → {path}")


def fetch_xml(
    pii: str,
    view: str,
    api_key: str,
    max_retries: int = MAX_RETRIES,
    backoff: float = BACKOFF_SECONDS,
    timeout: int = 15,
) -> Optional[bytes]:
    """
    Elsevier Article API에서 XML을 가져오는 함수.
    - 429, 5xx에 대해 재시도
    - 성공 시 raw bytes 반환, 실패 시 None
    """
    url = build_request_url(pii)
    params = {
        "apiKey": api_key,
        "view": view,
    }

    for attempt in range(1, max_retries + 1):
        logger.info(f"[Attempt {attempt}/{max_retries}] Requesting: {url} | view={view}")
        try:
            resp = requests.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            logger.error(f"Request failed with exception: {e}")
            # 재시도 여지가 있으면 backoff 후 계속
            if attempt < max_retries:
                sleep_time = backoff * attempt
                logger.info(f"Retrying after {sleep_time:.1f} seconds...")
                time.sleep(sleep_time)
                continue
            else:
                return None

        # HTTP status code 체크
        if resp.status_code == 200:
            logger.info(f"Successfully fetched XML for PII={pii}, view={view}")
            return resp.content

        # 429 / 5xx → 재시도 대상
        if resp.status_code in (429, 500, 502, 503, 504):
            logger.warning(
                f"Server responded with {resp.status_code}. "
                f"Reason: {resp.reason}. Attempt {attempt}/{max_retries}"
            )
            if attempt < max_retries:
                sleep_time = backoff * attempt
                logger.info(f"Retrying after {sleep_time:.1f} seconds...")
                time.sleep(sleep_time)
                continue
            else:
                logger.error(
                    f"Max retries reached for PII={pii}. "
                    f"Last response code: {resp.status_code}"
                )
                return None
        else:
            # 재시도 안 하는 에러
            snippet = resp.text[:300].replace("\n", " ")
            logger.error(
                f"Non-retryable HTTP error {resp.status_code} for PII={pii}, view={view}. "
                f"Response snippet: {snippet}"
            )
            return None

    # 여기 도달하면 실패
    return None


def get_output_path(pii: str, view: str, out_dir: str = "xmls") -> str:
    """저장할 파일 경로 생성."""
    filename = f"{pii}__{view}.xml"
    return os.path.join(out_dir, filename)


# -----------------------------
# 메인 함수 (CLI)
# -----------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Elsevier Article XML by PII."
    )
    parser.add_argument(
        "--pii",
        type=str,
        required=True,
        help="Elsevier PII (e.g., S0016-2361(23)00555-7)",
    )
    parser.add_argument(
        "--view",
        type=str,
        default="META_ABS",
        help="Elsevier view type (default: META_ABS)",
    )
    parser.add_argument(
        "--api_key",
        type=str,
        default=None,
        help="Elsevier API key (optional, overrides ENV/DEFAULT if given)",
    )
    parser.add_argument(
        "--out_dir",
        type=str,
        default="xmls",
        help="Output directory for XML files (default: xmls)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing XML file if it already exists.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    pii = args.pii.strip()
    view = args.view.strip()
    out_dir = args.out_dir

    # API 키 결정 우선순위: CLI 인자 > ENV > DEFAULT
    api_key = args.api_key.strip() if args.api_key else API_KEY
    if not api_key:
        logger.error(
            "API key is missing. Set --api_key or ENV ELSEVIER_API_KEY "
            "or fill _DEFAULT_API_KEY."
        )
        return 1

    output_path = get_output_path(pii, view, out_dir)

    # 기존 파일 존재 시 처리
    if os.path.exists(output_path) and not args.overwrite:
        logger.info(
            f"File already exists and overwrite=False. Skip. → {output_path}"
        )
        return 0

    logger.info(f"Target PII : {pii}")
    logger.info(f"View       : {view}")
    logger.info(f"Output Path: {output_path}")

    content = fetch_xml(pii=pii, view=view, api_key=api_key)

    if content is None:
        logger.error(f"Failed to download XML for PII={pii}, view={view}")
        return 1

    save_xml(content, output_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
