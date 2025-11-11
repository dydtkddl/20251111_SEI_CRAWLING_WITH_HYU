# -*- coding: utf-8 -*-
"""
01_collect_html_pages_playwright.py
──────────────────────────────────────────────
Wiley Batteries & Supercaps Search Scraper
- Loops startPage=0..91
- Tries to bypass JS/Cloudflare challenge using Playwright + stealth (if available)
- Saves only <ul id="search-result">…</ul> HTML per page
- Features: headful/headless, cookies storage reuse, optional proxy, screenshots on failure
──────────────────────────────────────────────
Usage examples:
  # first run (manual CAPTCHA pass) - open browser UI
  python 01_collect_html_pages_playwright.py --headful --start 0 --end 0

  # after cookies.json is created, run headless for full range
  python 01_collect_html_pages_playwright.py --start 0 --end 91

  # with proxy
  python 01_collect_html_pages_playwright.py --proxy "http://ip:port" --start 0 --end 91
"""
import os
import time
import random
import argparse
import logging
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PLTimeout, Error as PLBaseError
from tqdm import tqdm

# Try robust imports for playwright-stealth (different versions expose function differently)
stealth = None
try:
    # preferred: playwright_stealth.stealth import stealth
    from playwright_stealth.stealth import stealth  # type: ignore
except Exception:
    try:
        # fallback: top-level import
        from playwright_stealth import stealth as stealth  # type: ignore
    except Exception:
        stealth = None  # not available

BASE_URL = "https://chemistry-europe.onlinelibrary.wiley.com/action/doSearch"
OUTDIR = "wiley_html_pages"
COOKIES = "cookies.json"
DEFAULT_MAX_ATTEMPTS = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("wiley-scraper")


def human_like(page):
    """Do small human-like moves: random mouse moves, scrolls, short waits."""
    try:
        vs = page.viewport_size or {"width": 1280, "height": 800}
        w = vs.get("width", 1280)
        h = vs.get("height", 800)
        for _ in range(random.randint(1, 3)):
            x = random.randint(int(w * 0.2), int(w * 0.8))
            y = random.randint(int(h * 0.2), int(h * 0.8))
            page.mouse.move(x, y, steps=random.randint(5, 20))
            time.sleep(random.uniform(0.05, 0.25))
        page.mouse.wheel(0, random.randint(200, 800))
        time.sleep(random.uniform(0.3, 1.0))
    except Exception:
        # don't fail because of these minor actions
        pass


def fetch_page(p, startpage: int, outdir: str, headful: bool = True, max_attempts: int = DEFAULT_MAX_ATTEMPTS,
               cookies_path: str = COOKIES, proxy: str | None = None, delay_min: float = 2.0, delay_max: float = 5.0):
    """
    Fetch one search page and save <ul id="search-result"> wrapper html to file.
    Returns True on success, False on failure.
    """
    params = {
        "SeriesKey": "25666223",
        "sortBy": "Earliest",
        "startPage": str(startpage),
        "pageSize": "20",
    }
    url = BASE_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
    outpath = os.path.join(outdir, f"page_{startpage:03d}.html")

    attempt = 0
    success = False

    launch_kwargs = {"headless": not headful, "slow_mo": 50}
    if proxy:
        # Playwright accepts proxy dict on launch
        launch_kwargs["proxy"] = {"server": proxy}

    browser = None
    context = None
    page = None

    try:
        browser = p.chromium.launch(
    headless=not headful,
    slow_mo=50,
    proxy={"server": proxy} if proxy else None,
    args=[
        "--no-sandbox",
        "--disable-quic",
        "--disable-http3",
        "--disable-features=NetworkService"
    ],
)
        storage_state = cookies_path if os.path.exists(cookies_path) else None
        context = browser.new_context(
            storage_state=storage_state,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36"),
            viewport={"width": random.choice([1200, 1366, 1440]), "height": random.choice([720, 800, 900])},
            locale="en-US",
            timezone_id="Europe/London",
        )
        page = context.new_page()

        # Apply stealth if available
        if stealth is not None:
            try:
                stealth(page)
            except Exception as e:
                logger.debug(f"stealth() call failed: {e}")
        else:
            logger.warning("playwright-stealth not available; continuing without stealth. Install playwright-stealth for better evasion.")

        while attempt < max_attempts and not success:
            attempt += 1
            logger.info(f"[page {startpage}] Attempt {attempt}/{max_attempts} -> {url}")
            try:
                page.set_default_navigation_timeout(45000)
                page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # small human-like pause + interactions
                time.sleep(random.uniform(0.8, 1.8))
                human_like(page)

                # wait for the UL search results - this is the main indicator
                page.wait_for_selector("ul#search-result", timeout=20000)
                html_inner = page.locator("ul#search-result").inner_html(timeout=5000)

                # save wrapper only
                with open(outpath, "w", encoding="utf-8") as f:
                    f.write(f"<ul id='search-result'>{html_inner}</ul>")

                # store cookies/state for reuse
                try:
                    context.storage_state(path=cookies_path)
                except Exception as e:
                    logger.debug(f"Could not save storage_state: {e}")

                logger.info(f"[page {startpage}] ✅ Saved {outpath}")
                success = True

            except PLTimeout:
                logger.warning(f"[page {startpage}] Timeout/wait failed (attempt {attempt}/{max_attempts}). Trying reload and screenshot.")
                # take screenshot for debugging
                try:
                    ss = os.path.join(outdir, f"timeout_page{startpage}_attempt{attempt}.png")
                    page.screenshot(path=ss, full_page=False)
                    logger.info(f"[page {startpage}] Screenshot saved: {ss}")
                except Exception:
                    pass
                try:
                    page.reload(timeout=20000)
                except Exception:
                    pass

            except PLBaseError as e:
                logger.error(f"[page {startpage}] Playwright error: {e}")
                try:
                    ss = os.path.join(outdir, f"error_page{startpage}_attempt{attempt}.png")
                    page.screenshot(path=ss, full_page=False)
                    logger.info(f"[page {startpage}] Error screenshot saved: {ss}")
                except Exception:
                    pass
                try:
                    page.reload(timeout=20000)
                except Exception:
                    pass

            except Exception as e:
                logger.error(f"[page {startpage}] Unexpected error: {e}")
                try:
                    ss = os.path.join(outdir, f"unexpected_page{startpage}_attempt{attempt}.png")
                    page.screenshot(path=ss, full_page=False)
                    logger.info(f"[page {startpage}] Screenshot saved: {ss}")
                except Exception:
                    pass
                try:
                    page.reload(timeout=20000)
                except Exception:
                    pass

            # randomized backoff
            backoff = random.uniform(delay_min, delay_max) + attempt * 0.5
            logger.debug(f"[page {startpage}] sleeping backoff {backoff:.2f}s")
            time.sleep(backoff)

    finally:
        try:
            if page:
                page.close()
        except Exception:
            pass
        try:
            if context:
                context.close()
        except Exception:
            pass
        try:
            if browser:
                browser.close()
        except Exception:
            pass

    return success


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUTDIR, help="Output directory for HTML pages and screenshots")
    ap.add_argument("--start", type=int, default=0, help="Start page index (inclusive)")
    ap.add_argument("--end", type=int, default=91, help="End page index (inclusive)")
    ap.add_argument("--headful", action="store_true", help="Run with visible browser (safer for JS challenges)")
    ap.add_argument("--proxy", type=str, default=None, help="Optional proxy server (e.g. http://ip:port)")
    ap.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    ap.add_argument("--cookies", type=str, default=COOKIES, help="Path to save/load cookies (storage_state JSON)")
    ap.add_argument("--delay-min", type=float, default=2.0, help="Minimum delay between attempts (s)")
    ap.add_argument("--delay-max", type=float, default=5.0, help="Maximum delay between attempts (s)")
    return ap.parse_args()


def main():
    args = parse_args()
    outdir = args.out
    Path(outdir).mkdir(parents=True, exist_ok=True)

    logger.info("Starting Wiley pages collection")
    logger.info(f"Output dir: {outdir} | pages: {args.start}..{args.end} | headful: {args.headful} | proxy: {args.proxy}")

    with sync_playwright() as p:
        for i in tqdm(range(args.start, args.end + 1), desc="Collecting Wiley pages"):
            ok = fetch_page(p, i, outdir, headful=args.headful, max_attempts=args.max_attempts,
                            cookies_path=args.cookies, proxy=args.proxy,
                            delay_min=args.delay_min, delay_max=args.delay_max)
            if not ok:
                logger.warning(f"⚠️ page {i} failed to fetch after {args.max_attempts} attempts")
            # lightweight random delay between pages
            time.sleep(random.uniform(1.0, 2.5))

    logger.info("Done.")


if __name__ == "__main__":
    main()
