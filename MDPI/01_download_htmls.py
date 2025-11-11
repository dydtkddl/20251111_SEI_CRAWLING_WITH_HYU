# -*- coding: utf-8 -*-
"""
MDPI Batteries HTML Fetcher (Playwright + Multiprocessing)
──────────────────────────────────────────────
✔ Volume-Issue 단위 HTML만 빠르게 저장
✔ PDF / Metadata / Parsing 없음
✔ n_cpus 병렬 처리
──────────────────────────────────────────────
"""

import os
import time
import random
import logging
import argparse
import asyncio
from tqdm import tqdm
from multiprocessing import Pool, cpu_count
from playwright.async_api import async_playwright

# ─────────────────────────────────────────────
BASE = "https://www.mdpi.com"
JOURNAL = "2313-0105"
OUTDIR = "mdpi_batteries_output/html_raw"
os.makedirs(OUTDIR, exist_ok=True)

# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger("MDPI_HTML")

# ─────────────────────────────────────────────
async def fetch_and_save(vol, iss):
    """단일 volume-issue 페이지를 가져와 저장"""
    url = f"{BASE}/{JOURNAL}/{vol}/{iss}"
    html_path = os.path.join(OUTDIR, f"vol{vol}_iss{iss}.html")

    # 이미 저장되어 있다면 스킵
    if os.path.exists(html_path):
        logger.debug(f"⏭️ Skip existing {html_path}")
        return

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=random.choice([
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
                ])
            )
            page = await context.new_page()

            await page.goto(url, timeout=60000)
            await page.wait_for_load_state("networkidle")

            # lazy load 방지용 스크롤
            await page.evaluate("""
                async () => {
                    window.scrollTo(0, document.body.scrollHeight);
                    await new Promise(r => setTimeout(r, 1500));
                }
            """)

            html = await page.content()
            await browser.close()

            with open(html_path, "w", encoding="utf-8") as f:
                f.write(html)

            logger.info(f"✅ Saved {html_path}")
    except Exception as e:
        logger.warning(f"⚠️ Failed V{vol} I{iss}: {e}")

# ─────────────────────────────────────────────
def fetch_sync(args):
    """멀티프로세스용 동기 wrapper"""
    vol, iss = args
    asyncio.run(fetch_and_save(vol, iss))

# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="MDPI HTML Downloader (Parallel)")
    parser.add_argument("--start_vol", type=int, default=1, help="Start volume")
    parser.add_argument("--end_vol", type=int, default=11, help="End volume")
    parser.add_argument("--max_issue", type=int, default=12, help="Max issue number to check per volume")
    parser.add_argument("--n_cpus", type=int, default=min(8, cpu_count()), help="Number of CPU cores")
    args = parser.parse_args()

    logger.info(f"🚀 Start HTML download: Vol {args.start_vol}-{args.end_vol}, "
                f"Issues ≤ {args.max_issue}, n_cpus={args.n_cpus}")

    # Volume-Issue 조합 생성
    tasks = [(v, i) for v in range(args.start_vol, args.end_vol + 1)
                    for i in range(1, args.max_issue + 1)]

    with Pool(processes=args.n_cpus) as pool:
        list(tqdm(pool.imap_unordered(fetch_sync, tasks),
                  total=len(tasks), desc="📄 Fetching HTML"))

    logger.info("✅ All HTML files downloaded!")

# ─────────────────────────────────────────────
if __name__ == "__main__":
    main()


