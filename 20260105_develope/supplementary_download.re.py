# -*- coding: utf-8 -*-
"""
ScienceDirect Supplementary Data Downloader - RETRY Mode
- [기능] 기존 로그 파일(supplementary_status_log.csv)을 읽어 분석
- [기능] Status가 'NO_LINKS'인 항목만 필터링하여 재다운로드 시도
- [기능] 중복 URL 제거 및 재시도 전용 로그 별도 기록
"""

import argparse
import pandas as pd
from tqdm import tqdm
from playwright.sync_api import sync_playwright
import time, os
import logging
import csv
from datetime import datetime

# ---------------------------------------------------------------
# Helper: Real-time CSV Logging (재시도용)
# ---------------------------------------------------------------
def log_to_csv(csv_path, pii, url, status, filename, message):
    """
    실시간으로 상태를 CSV에 기록
    """
    try:
        file_exists = os.path.exists(csv_path)
        
        with open(csv_path, mode='a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            # 헤더가 없으면 작성
            if not file_exists:
                writer.writerow(['Timestamp', 'PII', 'URL', 'Status', 'Filename', 'Message'])
            
            writer.writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                pii,
                url,
                status,
                filename,
                message
            ])
    except Exception as e:
        logging.error(f"[CSV LOG ERROR] {e}")

# ---------------------------------------------------------------
# Helper: File Size Formatter
# ---------------------------------------------------------------
def get_file_size_str(filepath):
    try:
        size = os.path.getsize(filepath)
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024.0:
                return f"{size:.2f} {unit}"
            size /= 1024.0
        return f"{size:.2f} TB"
    except:
        return "Unknown Size"

# ---------------------------------------------------------------
# Downloader Function (기존 로직 유지)
# ---------------------------------------------------------------
def download_supplementary(context, url, save_dir, csv_path):

    pii = url.split("/")[-1]
    
    # [폴더 생성] 다운로드 시도 전, 무조건 폴더부터 생성
    article_dir = os.path.join(save_dir, pii)
    os.makedirs(article_dir, exist_ok=True)

    page = context.new_page()
    logging.info(f"[RETRY OPEN] {url}")

    try:
        page.goto(url, timeout=90_000)
    except Exception as e:
        logging.error(f"[LOAD ERROR] {url}: {e}")
        log_to_csv(csv_path, pii, url, "FAIL_LOAD", "", str(e))
        return False

    time.sleep(4) # 재시도이므로 대기 시간을 1초 늘림
    # 동적 로딩 유도 (스크롤)
    page.mouse.wheel(0, 15000) 
    time.sleep(3)

    # [선택자 전략]
    potential_selectors = [
        'div.Appendices a.download-link',
        '//section[.//h2[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "supplementary")]]//a[contains(@class, "download-link")]',
        'a[href*="mmc"]'
    ]

    target_links = None

    for sel in potential_selectors:
        try:
            try:
                page.wait_for_selector(sel, timeout=3000) # 대기 시간 소폭 증가
            except:
                pass 
            
            count = page.locator(sel).count()
            if count > 0:
                target_links = page.locator(sel)
                logging.info(f"[MATCH] Found {count} links using selector: {sel}")
                break
        except:
            continue
    
    # Fallback search
    if not target_links:
        time.sleep(2)
        fallback_sel = 'a[href*="mmc"]'
        if page.locator(fallback_sel).count() > 0:
            target_links = page.locator(fallback_sel)
            logging.info(f"[MATCH] Found links using fallback selector: {fallback_sel}")

    if not target_links or target_links.count() == 0:
        logging.warning("[INFO] Still no supplementary download links found.")
        log_to_csv(csv_path, pii, url, "NO_LINKS_AGAIN", "", "Retry failed: No links")
        return False

    count = target_links.count()
    logging.info(f"[FOUND] {count} candidate file(s). Filtering images...")
    
    downloaded_count = 0
    
    for i in range(count):
        link = target_links.nth(i)
        
        try:
            href = link.get_attribute("href") or ""
            title = link.get_attribute("title") or ""
            
            # [이미지 필터링]
            is_image = False
            if "gr" in href and "mmc" not in href: is_image = True 
            if "high-res" in title.lower() or "full-size" in title.lower(): is_image = True
            
            if is_image:
                logging.info(f"[SKIP] Ignoring image link: {title} ({href})")
                continue
            
            link.scroll_into_view_if_needed()
            
            try:
                with page.expect_download(timeout=10000) as d_event: # 타임아웃 2배 증가
                    link.click()
            except Exception:
                logging.warning(f"[SKIP] Index {i} has no direct download response.")
                log_to_csv(csv_path, pii, url, "SKIP_TIMEOUT", "", f"Index {i} download timeout")
                continue
            
            download = d_event.value
            original_filename = download.suggested_filename
            
            safe_filename = f"{pii}_{original_filename}"
            dst = os.path.join(article_dir, safe_filename)
            
            download.save_as(dst)
            
            # [추가] 파일 크기 확인
            file_size_str = get_file_size_str(dst)
            
            logging.info(f"[SUCCESS] Saved → {dst} ({file_size_str})")
            log_to_csv(csv_path, pii, url, "SUCCESS_RETRY", safe_filename, f"Downloaded successfully ({file_size_str})")
            downloaded_count += 1
            
        except Exception as e:
            logging.error(f"[DOWNLOAD FAIL] Index {i} | {e}")
            log_to_csv(csv_path, pii, url, "FAIL_DOWNLOAD", "", str(e))
            continue

    if downloaded_count == 0:
         log_to_csv(csv_path, pii, url, "NO_FILES_SAVED", "", "Links found but filtered (images) or failed")

    return downloaded_count > 0

# ---------------------------------------------------------------
# MAIN (Retry Logic)
# ---------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ScienceDirect Supplementary Downloader - RETRY MODE")

    parser.add_argument("--input_log", required=True, help="Path to the existing status log (e.g., supplementary_status_log.csv)")
    parser.add_argument("--save_dir", default="./supplementary_files", help="Output folder")
    parser.add_argument("--retry_log", default="./supplementary_retry_log.csv", help="New log file for retry results")

    args = parser.parse_args()

    # 1. 기존 로그 파일 로드
    if not os.path.exists(args.input_log):
        raise FileNotFoundError(f"Input log file not found: {args.input_log}")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    logging.info(f"Loading log file: {args.input_log}")
    try:
        df = pd.read_csv(args.input_log)
    except Exception as e:
        logging.error(f"Failed to read CSV: {e}")
        return

    # 2. 'NO_LINKS' 상태 필터링
    if 'Status' not in df.columns:
        logging.error("The input CSV does not have a 'Status' column.")
        return

    # Status가 NO_LINKS인 행만 추출
    retry_targets = df[df['Status'] == 'NO_LINKS']
    
    # URL 기준으로 중복 제거 (같은 논문이 여러 번 실패했을 수 있음)
    unique_retry_urls = retry_targets['URL'].dropna().unique().tolist()
    
    total_retry_count = len(unique_retry_urls)
    logging.info(f"Found {total_retry_count} unique URLs with 'NO_LINKS' status.")

    if total_retry_count == 0:
        logging.info("No failed items to retry. Exiting.")
        return

    # 3. Playwright 연결 및 재다운로드 수행
    logging.info("Connecting to Chrome debug port 9222...")
    
    success_count = 0
    fail_count = 0

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            logging.error("Chrome 디버깅 포트(9222)에 연결할 수 없습니다. Chrome이 디버깅 모드로 실행 중인지 확인하세요.")
            raise e

        # tqdm으로 진행 상황 표시
        pbar = tqdm(unique_retry_urls, desc="Retrying")
        
        for url in pbar:
            context = None
            try:
                pii = url.split("/")[-1]
                pbar.set_postfix({"PII": pii})
                
                context = browser.new_context()
                
                # 재다운로드 실행 (결과는 retry_log에 저장)
                is_success = download_supplementary(context, url, args.save_dir, args.retry_log)
                
                if is_success:
                    success_count += 1
                else:
                    fail_count += 1

            except Exception as e:
                logging.error(f"[RETRY CRITICAL FAIL] {url} | {e}")
                log_to_csv(args.retry_log, url.split("/")[-1], url, "CRITICAL_ERROR", "", str(e))
                fail_count += 1

            finally:
                if context:
                    try:
                        context.close()
                    except:
                        pass

    # 4. 최종 결과 요약
    summary_msg = (
        f"\n{'='*40}\n"
        f"        [Retry Summary Report]        \n"
        f"{'='*40}\n"
        f" Total Retried : {total_retry_count}\n"
        f" Success       : {success_count}\n"
        f" Still Failed  : {fail_count}\n"
        f"{'='*40}\n"
        f" Retry log saved to: {args.retry_log}\n"
        f"{'='*40}\n"
    )
    logging.info(summary_msg)
    print(summary_msg)

if __name__ == "__main__":
    main()