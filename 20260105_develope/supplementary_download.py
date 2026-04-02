# -*- coding: utf-8 -*-
"""
ScienceDirect Supplementary Data Downloader (Playwright)
- URL 또는 PII 자동 처리
- 논문 상세 페이지에서 Supplementary data 섹션의 파일 다운로드
- 논문 1개당 Playwright context 1개 생성하여 안정성 보장
- [기능] 실시간 다운로드 현황 및 파일 용량 CSV 저장
- [기능] 파일이 없어도 빈 폴더 생성 (폴더 구조 유지)
- [기능] 종료 시 최종 통계 요약 출력
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
# Helper: Real-time CSV Logging
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
# URL Normalizer
# ---------------------------------------------------------------
def normalize_url(x):
    if isinstance(x, float):
        return None
    x = str(x).strip()
    x = x.split("/")[-1]
    return f"https://www.sciencedirect.com/science/article/pii/{x}"

# ---------------------------------------------------------------
# Downloader Function
# ---------------------------------------------------------------
def download_supplementary(context, url, save_dir, csv_path):

    pii = url.split("/")[-1]
    
    # [폴더 생성] 다운로드 시도 전, 무조건 폴더부터 생성 (빈 폴더 보장)
    article_dir = os.path.join(save_dir, pii)
    os.makedirs(article_dir, exist_ok=True)

    page = context.new_page()
    logging.info(f"[OPEN] {url}")

    try:
        page.goto(url, timeout=90_000)
    except Exception as e:
        logging.error(f"[LOAD ERROR] {url}: {e}")
        log_to_csv(csv_path, pii, url, "FAIL_LOAD", "", str(e))
        return False

    time.sleep(3)
    # 동적 로딩 유도 (스크롤)
    page.mouse.wheel(0, 15000) 
    time.sleep(2)

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
                page.wait_for_selector(sel, timeout=2000)
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
        logging.warning("[INFO] No supplementary download links found.")
        log_to_csv(csv_path, pii, url, "NO_LINKS", "", "No supplementary links found")
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
                with page.expect_download(timeout=5000) as d_event:
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
            log_to_csv(csv_path, pii, url, "SUCCESS", safe_filename, f"Downloaded successfully ({file_size_str})")
            downloaded_count += 1
            
        except Exception as e:
            logging.error(f"[DOWNLOAD FAIL] Index {i} | {e}")
            log_to_csv(csv_path, pii, url, "FAIL_DOWNLOAD", "", str(e))
            continue

    if downloaded_count == 0:
         log_to_csv(csv_path, pii, url, "NO_FILES_SAVED", "", "Links found but filtered (images) or failed")

    return downloaded_count > 0


# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ScienceDirect Supplementary Downloader")

    parser.add_argument("--csv", required=True, help="Input CSV path")
    parser.add_argument("--col", default="prism_url", help="Column name with URL or PII")
    parser.add_argument("--save_dir", default="./supplementary_files", help="Output folder")
    parser.add_argument("--status_log", default="./supplementary_status_log.csv", help="Real-time status CSV path")

    args = parser.parse_args()

    os.makedirs(args.save_dir, exist_ok=True)

    # 로그 파일 초기화
    if not os.path.exists(args.status_log):
        try:
            with open(args.status_log, mode='w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(['Timestamp', 'PII', 'URL', 'Status', 'Filename', 'Message'])
        except Exception as e:
            print(f"Warning: Could not create status log file: {e}")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )

    if not os.path.exists(args.csv):
        raise FileNotFoundError(f"Input file not found: {args.csv}")

    if args.csv.lower().endswith(('.xlsx', '.xls')):
        logging.info(f"Loading Excel file: {args.csv}")
        df = pd.read_excel(args.csv)
    else:
        logging.info(f"Loading CSV file: {args.csv}")
        df = pd.read_csv(args.csv)
        
    if args.col not in df.columns:
        logging.warning(f"Column '{args.col}' not found. Using the first column as default.")
        term_col = df.columns[0]
    else:
        term_col = args.col

    df["url_norm"] = df[term_col].apply(normalize_url)
    urls = df["url_norm"].dropna().tolist()

    logging.info("Connecting to Chrome debug port 9222...")
    
    # [통계용 변수]
    total_papers = len(urls)
    success_papers = 0
    failed_papers = 0
    
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            logging.error("Chrome 디버깅 포트(9222)에 연결할 수 없습니다. Chrome이 디버깅 모드로 실행 중인지 확인하세요.")
            raise e

        pbar = tqdm(urls, desc="Downloading")
        for url in pbar:
            context = None
            try:
                pbar.set_postfix({"current": url.split("/")[-1]})
                context = browser.new_context()
                
                # 다운로드 수행 (성공 여부 반환)
                is_success = download_supplementary(context, url, args.save_dir, args.status_log)
                
                if is_success:
                    success_papers += 1
                else:
                    failed_papers += 1

            except Exception as e:
                logging.error(f"[FAIL] {url} | {e}")
                pii = url.split("/")[-1] if url else "UNKNOWN"
                log_to_csv(args.status_log, pii, url, "CRITICAL_ERROR", "", str(e))
                failed_papers += 1

            finally:
                if context:
                    try:
                        context.close()
                    except:
                        pass

    # [최종 요약 출력]
    summary_msg = (
        f"\n{'='*40}\n"
        f"        [Summary Report]        \n"
        f"{'='*40}\n"
        f" Total Papers  : {total_papers}\n"
        f" Success (Files): {success_papers}\n"
        f" No Files/Fail : {failed_papers}\n"
        f"{'='*40}\n"
        f" Detailed log saved to: {args.status_log}\n"
        f"{'='*40}\n"
    )
    logging.info(summary_msg)
    print(summary_msg)

if __name__ == "__main__":
    main()