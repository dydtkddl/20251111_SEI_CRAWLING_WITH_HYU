# -*- coding: utf-8 -*-
"""
Persistent Wiley scraper with optional 2Captcha support.

Usage:
  python 01_collect_html_pages_playwright_persistent.py --profile pw_profile --headful --start 0 --end 0
  python 01_collect_html_pages_playwright_persistent.py --profile pw_profile --start 0 --end 91
"""
import os
import sys
import time
import json
import random
import argparse
import logging
from pathlib import Path
import requests
from tqdm import tqdm
from playwright.sync_api import sync_playwright, TimeoutError as PLTimeout

# optional UA generator
try:
    from fake_useragent import UserAgent
    ua_gen = UserAgent()
except Exception:
    ua_gen = None

# Try stealth import
stealth = None
try:
    from playwright_stealth.stealth import stealth
except Exception:
    try:
        from playwright_stealth import stealth as stealth
    except Exception:
        stealth = None

BASE_URL = "https://chemistry-europe.onlinelibrary.wiley.com/action/doSearch"
DEFAULT_OUT = "wiley_html_pages"
DEFAULT_COOKIES = "cookies.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("wiley-persist")

# ---------- Helper functions ----------
def human_like(page):
    """Small human-like movements, occasionally click."""
    try:
        vs = page.viewport_size or {"width": 1366, "height": 850}
        w, h = vs["width"], vs["height"]
        for _ in range(random.randint(1, 3)):
            x = random.randint(100, max(200, w-100))
            y = random.randint(100, max(200, h-100))
            page.mouse.move(x, y, steps=random.randint(5, 20))
            if random.random() < 0.15:
                page.mouse.click(x, y)
            time.sleep(random.uniform(0.08, 0.28))
        page.mouse.wheel(0, random.randint(200, 800))
        time.sleep(random.uniform(0.5, 1.2))
    except Exception:
        pass

def save_profile_state(context, cookies_path):
    try:
        context.storage_state(path=cookies_path)
        logger.info("Saved storage_state -> %s", cookies_path)
    except Exception as e:
        logger.warning("Failed to save storage_state: %s", e)

def detect_captcha(page):
    try:
        html = page.content().lower()
        triggers = [
            "verifying you are human",
            "please enable javascript and cookies to continue",
            "cf-turnstile", "g-recaptcha", "h-captcha", "captcha"
        ]
        if any(t in html for t in triggers):
            return True
        if page.query_selector("iframe[src*='recaptcha']") or page.query_selector("iframe[src*='turnstile']"):
            return True
    except Exception:
        pass
    return False

# ---------- Optional 2Captcha ----------
def solve_captcha_2captcha(api_key, sitekey, pageurl, proxy=None, service="turnstile"):
    in_url = "http://2captcha.com/in.php"
    res_url = "http://2captcha.com/res.php"
    payload = {
        "key": api_key,
        "method": "userrecaptcha" if service == "recaptcha" else ("turnstile" if service == "turnstile" else "hcaptcha"),
        "googlekey": sitekey,
        "pageurl": pageurl,
        "json": 1,
    }
    if proxy:
        logger.info("Note: proxy provided but not formatted for 2captcha proxy param.")
    r = requests.post(in_url, data=payload, timeout=30).json()
    if r.get("status") != 1:
        raise RuntimeError(f"2captcha in.php failed: {r}")
    task_id = r["request"]
    for _ in range(60):
        time.sleep(5)
        jr = requests.get(res_url, params={"key": api_key, "action": "get", "id": task_id, "json": 1}, timeout=30).json()
        if jr.get("status") == 1:
            logger.info("2captcha solved token received.")
            return jr["request"]
        if jr.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha error: {jr}")
    raise RuntimeError("2captcha timeout")

# ---------- Main logic ----------
def fetch_range_persistent(profile_dir, out_dir, start, end, headful=False, proxy=None, cookies_path=DEFAULT_COOKIES,
                           two_captcha_key=None):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    user_agent = ua_gen.random if ua_gen else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/121 Safari/537.36"
    viewport = {"width": random.choice([1280,1366,1440,1600]), "height": random.choice([720,800,900,1050])}
    locale = random.choice(["en-US","en-GB","fr-FR","de-DE"])

    with sync_playwright() as p:
        logger.info("Launching persistent context: %s (headful=%s)", profile_dir, headful)
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=not headful,
            proxy={"server": proxy} if proxy else None,
            args=[
                "--no-sandbox", "--disable-quic", "--disable-http3",
                "--disable-features=NetworkService,AutomationControlled",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        page = context.new_page()
        if stealth:
            try:
                stealth(page)
            except Exception as e:
                logger.debug("stealth() failed: %s", e)

        page.set_extra_http_headers({
            "accept-language": locale,
            "referer": "https://chemistry-europe.onlinelibrary.wiley.com/"
        })
        logger.info("UA=%s viewport=%s locale=%s", user_agent, viewport, locale)

        for idx in tqdm(range(start, end+1), desc="Pages"):
            params = {"SeriesKey": "25666223", "sortBy": "Earliest", "startPage": str(idx), "pageSize": "20"}
            url = BASE_URL + "?" + "&".join(f"{k}={v}" for k,v in params.items())
            outpath = os.path.join(out_dir, f"page_{idx:03d}.html")
            logger.info("Navigating -> %s", url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                logger.warning("goto failed: %s", e)
                continue

            time.sleep(random.uniform(1.0, 2.5))
            human_like(page)

            if detect_captcha(page):
                logger.warning("⚠️ CAPTCHA detected on page %d", idx)
                if headful:
                    logger.info("Solve CAPTCHA manually, then press Enter.")
                    # input("Press Enter after solving...")
                    save_profile_state(context, cookies_path)
                elif two_captcha_key:
                    logger.info("Attempting 2Captcha auto-solve.")
                    try:
                        el = page.query_selector("[data-sitekey]")
                        sitekey = el.get_attribute("data-sitekey") if el else None
                        token = solve_captcha_2captcha(two_captcha_key, sitekey, url, proxy=proxy)
                        page.evaluate(f"document.querySelector('textarea[name=\"cf-turnstile-response\"]').value='{token}';")
                        page.reload()
                        time.sleep(2)
                    except Exception as e:
                        logger.error("2Captcha solve failed: %s", e)
                else:
                    logger.info("No CAPTCHA key provided; skipping.")
                    continue

            # ✅ UL extraction with fallback save
            try:
                page.wait_for_selector("ul#search-result", timeout=15000)
                html_inner = page.locator("ul#search-result").inner_html(timeout=5000)
                full_html = f"<ul id='search-result'>{html_inner}</ul>"
                with open(outpath, "w", encoding="utf-8") as f:
                    f.write(full_html)
                logger.info("✅ Saved %s (%d chars)", outpath, len(full_html))
                save_profile_state(context, cookies_path)

            except PLTimeout:
                fallback_path = os.path.join(out_dir, f"fallback_page_{idx:03d}.html")
                with open(fallback_path, "w", encoding="utf-8") as f:
                    f.write(page.content())
                logger.warning("⚠️ UL not found on %d. Saved full page -> %s", idx, fallback_path)
                page.screenshot(path=os.path.join(out_dir, f"no_result_{idx}.png"))

            except Exception as e:
                err_path = os.path.join(out_dir, f"error_page_{idx:03d}.html")
                with open(err_path, "w", encoding="utf-8") as f:
                    f.write(page.content())
                logger.error("❌ Error on %d: %s (saved %s)", idx, e, err_path)
                page.screenshot(path=os.path.join(out_dir, f"error_{idx}.png"))

            time.sleep(random.uniform(0.5, 1.0))

        logger.info("✅ Loop done, saving context.")
        save_profile_state(context, cookies_path)
        context.close()

# ---------- CLI ----------
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True, help="Path to persistent profile directory (user_data_dir).")
    p.add_argument("--out", default=DEFAULT_OUT, help="Output directory")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--end", type=int, default=91)
    p.add_argument("--headful", action="store_true", help="Visible browser mode")
    p.add_argument("--proxy", type=str, default=None, help="Optional proxy (http://user:pass@ip:port)")
    p.add_argument("--cookies", type=str, default=DEFAULT_COOKIES)
    p.add_argument("--2captcha-key", type=str, default=None, help="Optional 2Captcha API key")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    Path(args.profile).mkdir(parents=True, exist_ok=True)
    fetch_range_persistent(args.profile, args.out, args.start, args.end, headful=args.headful, proxy=args.proxy,
                           cookies_path=args.cookies, two_captcha_key=args.__dict__.get("2captcha_key"))
