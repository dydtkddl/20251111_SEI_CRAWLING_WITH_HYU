# -*- coding: utf-8 -*-
"""
ScienceDirect Supplementary Data Downloader (Playwright)
- URL 또는 PII 자동 처리
- 논문 상세 페이지에서 Supplementary data 섹션의 파일 다운로드
- 논문 1개당 Playwright context 1개 생성하여 안정성 보장
- 실시간 다운로드 현황 CSV 저장 기능 추가
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
    실시간으로 상태를 CSV에 기록 (Thread-safe하지 않으나, 여기서는 단일 프로세스라 무방)
    """
    try:
        # 파일이 없으면 헤더 작성
        file_exists = os.path.exists(csv_path)
        
        with open(csv_path, mode='a', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
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
# URL Normalizer
# ---------------------------------------------------------------
def normalize_url(x):
    """
    prism_url, link_self, pii 등 어떤 형태가 들어와도
    정규화하여 SD PII URL로 변환
    """
    if isinstance(x, float):
        return None

    x = str(x).strip()
    x = x.split("/")[-1]    # 기사 URL이면 PII만 추출
    return f"https://www.sciencedirect.com/science/article/pii/{x}"

def download_supplementary(context, url, save_dir, status_csv):
    pii = url.split("/")[-1]
    page = context.new_page()
    logging.info(f"[OPEN] {url}")

    try:
        page.goto(url, timeout=90_000)
    except Exception as e:
        logging.error(f"[LOAD ERROR] {url}: {e}")
        log_to_csv(status_csv, pii, url, "LOAD_ERROR", "", str(e))
        return False

    time.sleep(3)
    # 페이지 하단으로 스크롤하여 동적 로딩 유도
    page.mouse.wheel(0, 15000) 
    time.sleep(2)

    potential_selectors = [
        'div.Appendices a.download-link',
        '//section[.//h2[contains(translate(., "ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"), "supplementary")]]//a[contains(@class, "download-link")]',
        'a.download-link' 
    ]

    target_links = None

    for sel in potential_selectors:
        try:
            page.wait_for_selector(sel, timeout=2000)
            count = page.locator(sel).count()
            if count > 0:
                target_links = page.locator(sel)
                logging.info(f"[MATCH] Found {count} links using selector: {sel}")
                break
        except:
            continue
    
    if not target_links:
        time.sleep(2)
        try:
            fallback_sel = 'a[href*="mmc"]'
            if page.locator(fallback_sel).count() > 0:
                target_links = page.locator(fallback_sel)
                logging.info(f"[MATCH] Found links using fallback selector: {fallback_sel}")
        except:
            pass

    if not target_links or target_links.count() == 0:
        logging.warning("[INFO] No supplementary download links found.")
        log_to_csv(status_csv, pii, url, "NO_LINKS", "", "No eligible selectors matched")
        return False

    count = target_links.count()
    logging.info(f"[FOUND] {count} supplementary file(s).")
    
    downloaded_count = 0
    
    for i in range(count):
        link = target_links.nth(i)
        
        try:
            link.scroll_into_view_if_needed()
            
            try:
                with page.expect_download(timeout=5000) as d_event:
                    link.click()
            except Exception:
                logging.warning(f"[SKIP] Index {i} has no direct download response. Skipping...")
                log_to_csv(status_csv, pii, url, "SKIP", f"Index_{i}", "Timeout or no download event")
                continue
            
            download = d_event.value
            original_filename = download.suggested_filename
            
            article_dir = os.path.join(save_dir, pii)
            os.makedirs(article_dir, exist_ok=True)
            
            safe_filename = f"{pii}_{original_filename}"
            dst = os.path.join(article_dir, safe_filename)
            
            download.save_as(dst)
            logging.info(f"[SUCCESS] Saved → {dst}")
            
            log_to_csv(status_csv, pii, url, "SUCCESS", safe_filename, "Saved successfully")
            downloaded_count += 1
            
        except Exception as e:
            logging.error(f"[DOWNLOAD FAIL] Index {i} | {e}")
            log_to_csv(status_csv, pii, url, "DOWNLOAD_FAIL", f"Index_{i}", str(e))
            continue

    if downloaded_count == 0:
        # 링크는 찾았으나 하나도 성공 못한 경우
        log_to_csv(status_csv, pii, url, "PARTIAL_FAIL", "", "Links found but 0 downloaded")

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

    # Initialize Log CSV with Header if not exists
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
    
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
        except Exception as e:
            logging.error("Chrome 디버깅 포트(9222)에 연결할 수 없습니다. Chrome이 디버깅 모드로 실행 중인지 확인하세요.")
            raise e

        # tqdm에 현재 상태 표시
        pbar = tqdm(urls, desc="Downloading")
        for url in pbar:
            context = None
            try:
                pbar.set_postfix({"current": url.split("/")[-1]})
                context = browser.new_context()
                download_supplementary(context, url, args.save_dir, args.status_log)

            except Exception as e:
                logging.error(f"[FAIL] {url} | {e}")
                pii = url.split("/")[-1] if url else "UNKNOWN"
                log_to_csv(args.status_log, pii, url, "CRITICAL_ERROR", "", str(e))

            finally:
                if context:
                    try:
                        context.close()
                    except:
                        pass

    logging.info(f"All tasks completed. Status log saved at: {args.status_log}")


if __name__ == "__main__":
    main()
