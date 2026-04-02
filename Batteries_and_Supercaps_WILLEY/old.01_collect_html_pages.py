# -*- coding: utf-8 -*-
"""
01_collect_html_pages_selenium.py
─────────────────────────────
✅ Wiley Cloudflare 대응 완전 안정 버전
✅ 실제 Chrome 구동 (headless=False)
✅ 각 페이지의 <ul id="search-result">만 html_pages/page_XXX.html 로 저장
"""

import os, time, logging
from tqdm import tqdm
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

SAVE_DIR = "html_pages"
os.makedirs(SAVE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

def init_driver():
    """GUI Chrome (또는 headless 전환 가능)"""
    options = Options()
    # options.add_argument("--headless=new")   # headless로 돌리려면 주석 해제
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "--user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/133.0.6943.141 Safari/537.36"
    )
    return webdriver.Chrome(service=Service("/usr/bin/chromedriver"), options=options)

def fetch_page(driver, a):
    """페이지 진입 후 <ul id='search-result'> 추출"""
    url = f"https://chemistry-europe.onlinelibrary.wiley.com/action/doSearch?SeriesKey=25666223&sortBy=Earliest&startPage={a}&pageSize=20"
    driver.get(url)
    time.sleep(8)  # JS 렌더링 대기

    try:
        ul = driver.find_element(By.CSS_SELECTOR, "ul#search-result")
        html_block = ul.get_attribute("outerHTML")
        path = os.path.join(SAVE_DIR, f"page_{a:03d}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_block)
        logging.info(f"[{a}] saved {path}")
    except Exception as e:
        logging.warning(f"[{a}] failed: {e}")

def main():
    driver = init_driver()
    for a in tqdm(range(92), desc="📄 Collecting Wiley pages"):
        fetch_page(driver, a)
    driver.quit()

if __name__ == "__main__":
    main()


