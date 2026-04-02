# -*- coding: utf-8 -*-
"""
Persistent Wiley scraper with optional 2Captcha support.

Usage:
  # 1) First run (manual CAPTCHA pass) - opens persistent profile
  python 01_collect_html_pages_playwright_persistent.py --profile pw_profile --headful --start 0 --end 0

  # 2) After manual pass (automated run)
  python 01_collect_html_pages_playwright_persistent.py --profile pw_profile --start 0 --end 91

Options:
  --2captcha-key YOUR_KEY    Optional: API key for 2Captcha if you want automatic CAPTCHA solving.
  --proxy http://user:pass@ip:port  Optional proxy for outbound requests (also used by CAPTCHA solver where applicable).
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

# Try stealth import (best-effort)
stealth = None
try:
    from playwright_stealth.stealth import stealth
except Exception:
    try:
        from playwright_stealth import stealth as stealth
    except Exception:
        stealth = None

# Constants
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
        # scroll a bit
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
    """Heuristics: check for common challenge texts/selectors."""
    try:
        html = page.content()
        # quick checks for common patterns
        lower = html.lower()
        triggers = [
            "verifying you are human",
            "please enable javascript and cookies to continue",
            "cf-turnstile", "g-recaptcha", "h-captcha", "captcha"
        ]
        for t in triggers:
            if t in lower:
                return True
        # also check for typical iframe selectors
        if page.query_selector("iframe[src*='recaptcha']") or page.query_selector("iframe[src*='turnstile']"):
            return True
    except Exception:
        pass
    return False

# ---------- Optional 2Captcha integration (basic) ----------
# NOTE: Using a captcha solving service is optional and may incur cost.
# This is a minimal implementation for sitekey-based captchas (reCAPTCHA / Turnstile / hCaptcha).
def solve_captcha_2captcha(api_key, sitekey, pageurl, proxy=None, service="turnstile"):
    """
    Request 2captcha to solve a captcha.
    service: "turnstile" | "recaptcha" | "hcaptcha" etc depending on 2captcha docs.
    Returns token string or raises RuntimeError.
    """
    # This implementation uses 2captcha's 'in.php' and 'res.php' endpoints.
    # Check 2captcha docs for exact param names for each service type.
    in_url = "http://2captcha.com/in.php"
    res_url = "http://2captcha.com/res.php"
    payload = {
        "key": api_key,
        "method": "userrecaptcha" if service == "recaptcha" else ("turnstile" if service == "turnstile" else "hcaptcha"),
        "googlekey": sitekey,  # for recaptcha/hcaptcha; for turnstile may be same param
        "pageurl": pageurl,
        "json": 1,
        # if proxy is required, you can pass proxy params (2captcha supports proxy type/addr)
    }
    if proxy:
        # 2captcha expects proxy param in a certain format, here skipping for brevity.
        # For production you must format 'proxy' and 'proxytype' fields.
        logger.info("Note: proxy provided but 2captcha proxy param handling not fully implemented in this example.")
    r = requests.post(in_url, data=payload, timeout=30)
    j = r.json()
    if j.get("status") != 1:
        raise RuntimeError(f"2captcha in.php failed: {j}")
    task_id = j.get("request")
    # poll
    for _ in range(0, 60):  # up to ~60*5s ~5min
        time.sleep(5)
        resp = requests.get(res_url, params={"key": api_key, "action": "get", "id": task_id, "json": 1}, timeout=30)
        jr = resp.json()
        if jr.get("status") == 1:
            token = jr.get("request")
            logger.info("2captcha solved token received.")
            return token
        elif jr.get("request") == "CAPCHA_NOT_READY":
            continue
        else:
            raise RuntimeError(f"2captcha error: {jr}")
    raise RuntimeError("2captcha timed out while waiting for solution")

# ---------- Main fetch logic ----------
def fetch_range_persistent(profile_dir, out_dir, start, end, headful=False, proxy=None, cookies_path=DEFAULT_COOKIES,
                           two_captcha_key=None):
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    user_agent = ua_gen.random if ua_gen else (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121 Safari/537.36"
    )
    viewport = {"width": random.choice([1280,1366,1440,1600]), "height": random.choice([720,800,900,1050])}
    locale = random.choice(["en-US","en-GB","fr-FR","de-DE"])

    with sync_playwright() as p:
        logger.info("Launching persistent context at %s (headful=%s)", profile_dir, headful)
        context = p.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=not headful,
            # Playwright accepts proxy per launch for chromium
            proxy={"server": proxy} if proxy else None,
            args=[
                "--no-sandbox",
                "--disable-quic",
                "--disable-http3",
                "--disable-features=NetworkService,AutomationControlled",
                "--disable-blink-features=AutomationControlled",
            ],
            # slow_mo left out for persistent; we can sleep manually
        )

        page = context.new_page()
        # apply stealth if available
        if stealth:
            try:
                stealth(page)
            except Exception as e:
                logger.debug("stealth() call failed: %s", e)

        # set headers / UA for this session
        try:
            page.set_extra_http_headers({
                "accept-language": locale,
                "referer": "https://chemistry-europe.onlinelibrary.wiley.com/"
            })
        except Exception:
            pass

        logger.info("Profile UA=%s viewport=%s locale=%s", user_agent, viewport, locale)
        try:
            # Optionally set UA/viewport via context (not all fields may apply after launch_persistent_context)
            # context.set_default_navigation_timeout(45000)
            page.evaluate("() => { Object.defineProperty(navigator, 'webdriver', {get: () => undefined}) }")
        except Exception:
            pass

        # if cookies_path exists, attempt to restore via storage_state (persistent context uses profile)
        # Now iterate pages
        for idx in tqdm(range(start, end+1), desc="Pages"):
            params = {
                "SeriesKey": "25666223",
                "sortBy": "Earliest",
                "startPage": str(idx),
                "pageSize": "20",
            }
            url = BASE_URL + "?" + "&".join(f"{k}={v}" for k,v in params.items())
            outpath = os.path.join(out_dir, f"page_{idx:03d}.html")
            logger.info("Navigating -> %s", url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:
                logger.warning("goto failed: %s", e)
            # small pause + human actions
            time.sleep(random.uniform(1.0, 2.5))
            human_like(page)
            # detect captcha presence
            if detect_captcha(page):
                logger.warning("CAPTCHA-like content detected on page %d", idx)
                # if headful, let user try to solve it manually
                if headful:
                    logger.info("Please solve the CAPTCHA in the opened browser window. Press Enter here when done.")
                    input("Press Enter after manual CAPTCHA pass...")
                    # save profile state after manual pass
                    save_profile_state(context, cookies_path)
                elif two_captcha_key:
                    # attempt automated solve via 2captcha
                    logger.info("Attempting to solve CAPTCHA via 2Captcha (key provided).")
                    # find sitekey (best-effort)
                    try:
                        # search for iframe with recaptcha/turnstile, or meta tags
                        frames = page.query_selector_all("iframe")
                        found_sitekey = None
                        for f in frames:
                            src = f.get_attribute("src") or ""
                            if "api2/anchor" in src and "k=" in src:
                                # recaptcha v2 embed
                                # parse k= param
                                import urllib.parse as up
                                parsed = up.urlparse(src)
                                qs = up.parse_qs(parsed.query)
                                if "k" in qs:
                                    found_sitekey = qs["k"][0]
                                    break
                            if "turnstile" in src and "k=" in src:
                                import urllib.parse as up
                                parsed = up.urlparse(src)
                                qs = up.parse_qs(parsed.query)
                                if "k" in qs:
                                    found_sitekey = qs["k"][0]
                                    break
                        if not found_sitekey:
                            # fallback: try to find data-sitekey attributes
                            el = page.query_selector("[data-sitekey]")
                            if el:
                                found_sitekey = el.get_attribute("data-sitekey")
                        if not found_sitekey:
                            raise RuntimeError("Couldn't locate sitekey for captcha")
                        logger.info("Found sitekey: %s", found_sitekey)
                        token = solve_captcha_2captcha(two_captcha_key, found_sitekey, url, proxy=proxy, service="turnstile")
                        logger.info("Injecting token into page and triggering callbacks (best-effort)")
                        # Many implementations accept inserting into hidden textarea named 'cf-turnstile-response' or 'g-recaptcha-response'
                        try:
                            page.evaluate(f"document.querySelector('textarea[name=\"cf-turnstile-response\"]').value = '{token}';")
                        except Exception:
                            pass
                        # Some pages expect a callback; try submitting forms or dispatching events
                        try:
                            page.evaluate("window.dispatchEvent(new Event('captcha-solved'))")
                        except Exception:
                            pass
                        # wait and retry load
                        time.sleep(3)
                        page.reload()
                        time.sleep(2)
                        if detect_captcha(page):
                            logger.warning("CAPTCHA still present after automated attempt.")
                            # fallback: save screenshot
                            page.screenshot(path=os.path.join(out_dir, f"captcha_fail_{idx}.png"))
                        else:
                            logger.info("CAPTCHA cleared.")
                            save_profile_state(context, cookies_path)
                    except Exception as e:
                        logger.error("Automated CAPTCHA attempt failed: %s", e)
                        page.screenshot(path=os.path.join(out_dir, f"captcha_error_{idx}.png"))
                        # if automated fails, break/continue based on your policy
                        # here we continue after waiting
                        time.sleep(random.uniform(10, 30))
                else:
                    logger.warning("No 2captcha key provided and not headful; cannot solve automatically. Sleeping and retrying later.")
                    time.sleep(random.uniform(10, 30))
                    # try reload and continue
                    try:
                        page.reload(timeout=30000)
                    except Exception:
                        pass

            else:
                # not captcha case: extract UL if present
                try:
                    page.wait_for_selector("ul#search-result", timeout=15000)
                    html_inner = page.locator("ul#search-result").inner_html(timeout=5000)
                    with open(outpath, "w", encoding="utf-8") as f:
                        f.write(f"<ul id='search-result'>{html_inner}</ul>")
                    logger.info("Saved %s", outpath)
                    # save cookies/state after success frequently
                    save_profile_state(context, cookies_path)
                except PLTimeout:
                    logger.warning("Search-result not found on page %d; saved screenshot for debugging.", idx)
                    try:
                        page.screenshot(path=os.path.join(out_dir, f"no_result_{idx}.png"))
                    except Exception:
                        pass
                except Exception as e:
                    logger.error("Unexpected error while extracting: %s", e)
                    try:
                        page.screenshot(path=os.path.join(out_dir, f"error_extract_{idx}.png"))
                    except Exception:
                        pass

            # human-like randomized delay before next page
            sleep_t = random.uniform(3.5, 9.0)
            logger.info("Sleeping %.1fs before next page", sleep_t)
            time.sleep(sleep_t)

        # done
        logger.info("Loop finished, saving final state.")
        save_profile_state(context, cookies_path)
        try:
            context.close()
        except Exception:
            pass

# ---------- CLI ----------
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", required=True, help="Path to persistent profile directory (user_data_dir).")
    p.add_argument("--out", default=DEFAULT_OUT, help="Output directory")
    p.add_argument("--start", type=int, default=0)
    p.add_argument("--end", type=int, default=91)
    p.add_argument("--headful", action="store_true", help="Run with visible browser for manual CAPTCHA solve")
    p.add_argument("--proxy", type=str, default=None, help="Optional proxy server (http://user:pass@ip:port)")
    p.add_argument("--cookies", type=str, default=DEFAULT_COOKIES)
    p.add_argument("--2captcha-key", type=str, default=None, help="Optional 2Captcha API key to attempt automated solves")
    return p.parse_args()

if __name__ == "__main__":
    args = parse_args()
    # Make sure profile dir exists (Playwright will create it on first run)
    Path(args.profile).mkdir(parents=True, exist_ok=True)
    fetch_range_persistent(args.profile, args.out, args.start, args.end, headful=args.headful, proxy=args.proxy,
                           cookies_path=args.cookies, two_captcha_key=args.__dict__.get("2captcha_key"))
