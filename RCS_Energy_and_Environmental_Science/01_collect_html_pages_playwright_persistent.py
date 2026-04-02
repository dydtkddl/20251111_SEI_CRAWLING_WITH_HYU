# -*- coding: utf-8 -*-
"""
📄 RSC EES: 여러 Volume/Issue 페이지에서
<div class="tab-content" style="display: block;"> 내부 HTML 자동 추출 스크립트

- tqdm 진행 상태 표시
- logging 기록
- html/vol{VOL}_iss{ISS}.html 로 저장
"""

import os
import logging
from tqdm import tqdm
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ─────────────────────────────────────────────
# ⚙️ Logging 설정
logging.basicConfig(
    filename="rsc_tab_extract.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8"
)

# ─────────────────────────────────────────────
# 📁 저장 폴더 생성
SAVE_DIR = "html"
os.makedirs(SAVE_DIR, exist_ok=True)


def extract_single_issue(page, url, out_path):
    """하나의 URL에서 tab-content HTML만 추출"""
    try:
        logging.info(f"Navigating to: {url}")
        page.goto(url, timeout=120000, wait_until="networkidle")

        # tab-content 렌더링 대기
        page.wait_for_selector("div.tab-content[style*='display: block']", timeout=60000)

        soup = BeautifulSoup(page.content(), "lxml")

        tab = soup.find("div", class_="tab-content",
                        attrs={"style": lambda s: s and "display: block" in s})

        if not tab:
            logging.warning(f"No tab-content found: {url}")
            return False

        inner_html = tab.decode_contents()

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(inner_html)

        logging.info(f"Saved: {out_path}")
        return True

    except Exception as e:
        logging.error(f"Error on {url}: {e}")
        return False


# ─────────────────────────────────────────────
# 📌 여러 Volume/Issue 순회 설정
# (원하는 만큼 자유롭게 확장)
VOLUMES = [1, 2, 3, 4, 5]        # 예시
ISSUES = [1, 2, 3, 4, 5, 6]      # 예시

BASE = "https://pubs.rsc.org/en/journals/journalissues/ee#!issueid=ee{VOL:03d}{ISS:03d}&type=current"


# ─────────────────────────────────────────────
# 🚀 메인 실행
def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        tasks = []
        for vol in VOLUMES:
            for iss in ISSUES:
                tasks.append((vol, iss))

        print(f"📚 Total tasks: {len(tasks)}")

        for vol, iss in tqdm(tasks, desc="Collecting Issues"):
            url = BASE.format(VOL=vol, ISS=iss)
            out_path = f"{SAVE_DIR}/vol{vol}_iss{iss}.html"

            extract_single_issue(page, url, out_path)

        browser.close()


# ─────────────────────────────────────────────
if __name__ == "__main__":
    main()
