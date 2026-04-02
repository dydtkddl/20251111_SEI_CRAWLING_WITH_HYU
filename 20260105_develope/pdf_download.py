# -*- coding: utf-8 -*-
"""
ScienceDirect PDF Downloader (Playwright + WinAPI) — Stable Version (Auto-click Loop)
- URL 또는 PII 자동 처리
- View PDF → 팝업 → (3초 대기) → 1초마다 WinAPI left click 반복
- 다운로드 신호(Playwright expect_download) 잡히면 즉시 클릭 루프 중단 후 저장
- 논문 1개당 Playwright context 1개 생성하여 안정성 보장
- 탭 누적 및 context 죽음 방지
"""

import argparse
import pandas as pd
from tqdm import tqdm
from playwright.sync_api import sync_playwright
import time, os, ctypes
import logging
import shutil

# ---------------------------------------------------------------
# Windows WinAPI CLICK — Human-like native click
# ---------------------------------------------------------------
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP   = 0x0004

def win_click():
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.01)
    ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    logging.info("[WIN-CLICK] Left click executed at mouse position.")

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
    x = x.split("/")[-1]  # 기사 URL이면 PII만 추출
    return f"https://www.sciencedirect.com/science/article/pii/{x}"

# ---------------------------------------------------------------
# PDF 다운로드 함수 (단일 논문 처리)
# ---------------------------------------------------------------
def download_pdf(context, url, save_dir):
    page = context.new_page()
    logging.info(f"[OPEN] {url}")

    page.goto(url, timeout=90_000)
    time.sleep(2)
    page.mouse.wheel(0, 2000)

    selectors = [
        'li.ViewPDF a[href*="pdfft"]',
        'a[href*="pdfft"]',
        'a[href*="pdf"]',
        '#pdfLink',
        'button[data-aa-name="viewPdf"]'
    ]

    target_selector = None
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=3000)
            target_selector = sel
            break
        except:
            continue

    if not target_selector:
        raise RuntimeError("PDF button not found")

    logging.info(f"[CLICK] PDF button selector = {target_selector}")

    # 팝업 준비
    with page.expect_popup() as pop_ev:
        page.click(target_selector)

    pdf_page = pop_ev.value
    logging.info(f"[POPUP] {pdf_page.url}")

    try:
        pdf_page.wait_for_load_state("networkidle", timeout=60_000)
    except:
        logging.warning("[WARN] networkidle timeout")

    # ✅ 8초 → 3초로 변경
    time.sleep(3)

    logging.info("============================================")
    logging.info("⚠ 다운로드 버튼 위에 마우스를 올려두세요.")
    logging.info("⚠ 다운로드가 시작될 때까지 1초마다 자동 클릭합니다.")
    logging.info("============================================")

    # ✅ 다운로드 감지 + 1초 간격 자동 클릭 루프
    download = None
    max_wait_sec = 90   # 큰 PDF 대비 (필요시 120~180으로 늘리기)
    per_try_timeout_ms = 1200  # 짧게 감시하면서 클릭 반복

    for i in range(max_wait_sec):
        try:
            # 다운로드 이벤트를 짧게 감시 (이 블록 안에서 클릭이 트리거되면 잡힘)
            with pdf_page.expect_download(timeout=per_try_timeout_ms) as d_event:
                win_click()
            download = d_event.value
            logging.info(f"[DOWNLOAD] Detected at t={i+1}s -> stop clicking.")
            break

        except Exception:
            logging.info(f"[RETRY] No download yet... ({i+1}/{max_wait_sec})")
            time.sleep(1)

    if download is None:
        raise RuntimeError("Download event not detected (timeout)")

    # 파일 저장
    tmp_path = download.path()
    dst = os.path.join(save_dir, download.suggested_filename)
    shutil.move(tmp_path, dst)
    logging.info(f"[SUCCESS] Saved → {dst}")

    # context.close()는 main()에서 실행
    return True

# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
# ---------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="ScienceDirect PDF Downloader (Auto-click Loop Ver)")

    parser.add_argument("--csv", required=True, help="Input CSV path (csv/xlsx)")
    parser.add_argument("--col", default="prism_url", help="Column name with URL or PII")
    parser.add_argument("--save_dir", default="./pdfs", help="Output folder for PDFs")

    args = parser.parse_args()

    os.makedirs(args.save_dir, exist_ok=True)

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
        raise ValueError(f"Column '{args.col}' not found in input file")
    
    # 필요하다면 슬라이싱 유지 (전체 다운로드 시에는 제거 추천)
    df = df.iloc[458:,:] 
    
    df["url_norm"] = df[args.col].apply(normalize_url)
    urls = df["url_norm"].dropna().tolist()

    fail_list = []

    logging.info("Connecting to Chrome debug port 9222...")
    
    # [추가] 이미 다운로드된 파일 목록 미리 로드 (속도 향상)
    existing_files = os.listdir(args.save_dir)

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://localhost:9222")

        for url in tqdm(urls, desc="Downloading PDFs"):
            # [추가] 중복 스킵 로직
            # URL 구조가 ".../pii/{PII}" 형태이므로 마지막 부분을 추출
            pii = url.split("/")[-1]
            
            # 저장된 파일명 중에 PII가 포함된 파일이 하나라도 있는지 확인
            is_downloaded = any(pii in filename for filename in existing_files)

            if is_downloaded:
                # tqdm 진행바가 깨지지 않도록 logging 대신 print 사용 혹은 포맷 조정 가능
                # 여기서는 로그만 남기고 넘어갑니다.
                # logging.info(f"[SKIP] Already exists: {pii}")
                continue

            context = None
            try:
                # 논문 1개 처리마다 context 새로 생성
                context = browser.new_context()
                download_pdf(context, url, args.save_dir)
                
                # 성공했다면 현재 세션의 기존 파일 목록에는 없으므로(엄밀히는),
                # 완벽성을 위해 여기서 existing_files를 갱신하거나 넘어가도 무방함.

            except Exception as e:
                logging.error(f"[FAIL] {url} | {e}")
                fail_list.append({"url": url, "error": str(e)})

            finally:
                # context 완전 정리
                if context:
                    try:
                        context.close()
                    except:
                        pass

    # 실패 목록 저장
    if fail_list:
        pd.DataFrame(fail_list).to_csv("fail_list.csv", index=False, encoding="utf-8-sig")
        logging.info(f"FAIL LIST saved → fail_list.csv ({len(fail_list)} fails)")

    logging.info("All downloads completed.")
if __name__ == "__main__":
    main()
