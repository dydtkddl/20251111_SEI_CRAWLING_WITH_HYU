# -*- coding: utf-8 -*-
"""
RSC 검색 페이지를 눈으로 보면서(page visible)
284 페이지를 자동으로 넘기며 tab-content 저장하는 Playwright 스크립트

- headless=False (브라우저 실제로 열림)
- slow_mo=700 (버튼 클릭하는 게 눈에 보임)
- DOM 재생성에도 안전한 next 페이지 클릭
"""

import os
import logging
from tqdm import tqdm
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


URL = "https://pubs.rsc.org/en/results/journals?Category=Journal&AllText=Solid%20Electrolyte%20Interphase&IncludeReference=false&SelectJournal=True&DateRange=false&SelectDate=false&PriceCode=False&OpenAccess=false"

SAVE_DIR = "html"
os.makedirs(SAVE_DIR, exist_ok=True)

logging.basicConfig(
    filename="rsc_crawl.log",
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    encoding="utf-8"
)


def get_total_pages(page):
    page.wait_for_selector("div.pagination-summary")
    text = page.inner_text("div.pagination-summary")
    # 예: Showing page 1 of 284
    total = int(text.split("of")[-1].strip())
    return total
# tabArticles

def save_tab_content(page, idx):
    page.wait_for_selector("div#tabArticles")
    # page.wait_for_selector("div.tab-content[style*='display: block']")
    # soup = BeautifulSoup(page.content(), "lxml")
    # tab = soup.find("div", class_="tab-content",
    #                 attrs={"style": lambda s: s and "display: block" in s})
    soup = BeautifulSoup(page.content(), "lxml")

    # id 로 직접 찾기
    tab = soup.find("div", id="tabArticles")

    if tab:
        inner = tab.decode_contents()
        out_path = f"{SAVE_DIR}/page_{idx}.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(inner)
        logging.info(f"Saved page {idx}")
    else:
        logging.warning(f"tab-content not found on page {idx}")


def go_next(page):
    """DOM 재생성에도 안전한 방식으로 next 버튼 클릭."""
    selector = "a.paging__btn--next"

    # 버튼 기다리기
    try:
        page.wait_for_selector(selector, timeout=15000)
    except:
        return False

    # disabled 확인
    disabled = page.get_attribute(selector, "aria-disabled")
    if disabled == "true":
        return False

    # 버튼 클릭 (DOM stale 문제 없음)
    page.click(selector)

    # AJAX 렌더링 대기
    page.wait_for_load_state("networkidle")

    # tab-content 로딩 대기
    page.wait_for_selector("div.tab-content[style*='display: block']")

    return True


def main():
    with sync_playwright() as p:
        # 👇 headless=False 로 브라우저가 실제로 열린다.
        browser = p.chromium.launch(headless=False, slow_mo=700)
        ctx = browser.new_context()
        page = ctx.new_page()

        print("▶ Opening RSC search results page…")
        page.goto(URL, wait_until="networkidle", timeout=120000)

        total_pages = get_total_pages(page)
        print(f"📄 Total pages detected: {total_pages}")

        # 페이지 1부터 total_pages까지 순회
        for i in tqdm(range(1, total_pages + 1), desc="Crawling"):

            # 1) 현재 페이지 저장
            save_tab_content(page, i)

            # 마지막 페이지이면 끝
            if i == total_pages:
                print("✔ Finished all pages.")
                break

            # 2) 다음 페이지 이동
            ok = go_next(page)
            if not ok:
                print(f"❌ Could not click next on page {i}")
                break

        browser.close()


if __name__ == "__main__":
    main()
