"""pracuj.pl through the user's own Chrome (Playwright, headed, persistent profile).

pracuj.pl sits behind a Cloudflare challenge that blocks datacenter IPs, so this runs on a local machine with
Google Chrome installed. The profile in data/.pracuj_profile keeps the Cloudflare clearance between runs; if a
challenge shows up, solve it in the opened window and the scraper continues.

  pip install playwright   # uses the installed Google Chrome (channel="chrome"), no browser download
"""
import json
import random
import re
import time
from pathlib import Path
from urllib.parse import quote

from .boards import _offer

PROFILE = Path(__file__).resolve().parent.parent / "data" / ".pracuj_profile"

# DOM fallback if __NEXT_DATA__ changes shape
DOM_JS = """() => [...document.querySelectorAll('[data-test="default-offer"], [data-test="positioned-offer"]')].map(el => {
  const q = s => el.querySelector(s);
  const a = q('a[data-test="link-offer"]') || q('h2 a') || q('a[href*="/praca/"]');
  return {
    jobTitle: (q('[data-test="offer-title"]') || a || {}).textContent || '',
    companyName: (q('[data-test="text-company-name"]') || {}).textContent || '',
    displayWorkplace: (q('[data-test="text-region"]') || {}).textContent || '',
    offerAbsoluteUri: a ? a.href : '',
    salaryDisplayText: (q('[data-test="offer-salary"]') || {}).textContent || '',
  };
})"""


def _walk(o):
    """Yield every dict in a nested JSON structure that looks like a pracuj offer group."""
    if isinstance(o, dict):
        if "jobTitle" in o and "companyName" in o:
            yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _norm(g, kw):
    offers = g.get("offers") or [{}]
    first = offers[0] if offers else {}
    url = first.get("offerAbsoluteUri") or g.get("offerAbsoluteUri") or ""
    return _offer(
        source="pracuj.pl", offer_id=str(g.get("groupId") or first.get("partitionId") or url),
        title=(g.get("jobTitle") or "").strip(), company=(g.get("companyName") or "").strip(),
        city=(first.get("displayWorkplace") or g.get("displayWorkplace") or "").strip(),
        workplace=", ".join(g.get("workModes") or []), seniority=", ".join(g.get("positionLevels") or []),
        salary=(g.get("salaryDisplayText") or "").strip(), description=(g.get("jobDescription") or "").strip(),
        category=f"kw:{kw}", url=url, published=(g.get("lastPublicated") or "")[:10])


def _wait_ready(page, timeout=180):
    """Wait until the listing is there; a Cloudflare challenge is left for the person to solve in the window."""
    t0 = time.time()
    warned = False
    while time.time() - t0 < timeout:
        try:
            if page.query_selector('[data-test="default-offer"]') or (
                    page.query_selector("script#__NEXT_DATA__") and "just a moment" not in page.title().lower()):
                return True
            if not warned and ("challenge" in page.content().lower() or "just a moment" in page.title().lower()):
                print("[pracuj] Cloudflare check in the Chrome window — solve it there, scraping continues after")
                warned = True
        except Exception:  # the challenge page navigates away once solved
            pass
        time.sleep(2)
    return False


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/141.0.0.0 Safari/537.36")
NEXT_RX = re.compile(r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S)


def _parse(html):
    m = NEXT_RX.search(html)
    return list(_walk(json.loads(m.group(1)))) if m else None


def pracuj_pl(keywords, max_pages=10, delay=(2.0, 4.5)):
    """Plain HTTP first (from a residential IP pracuj serves the page without a challenge); the Chrome
    path below is the fallback when Cloudflare blocks it."""
    import requests

    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml", "Accept-Language": "pl-PL,pl;q=0.9"})
    out, seen = [], set()
    for kw in keywords:
        for pn in range(1, max_pages + 1):
            url = f"https://www.pracuj.pl/praca/{quote(kw)};kw" + (f"?pn={pn}" if pn > 1 else "")
            try:
                groups = _parse(s.get(url, timeout=60).text)
            except Exception as e:
                print(f"[pracuj] {url}: {str(e)[:120]}")
                groups = []
            if groups is None:
                print("[pracuj] blocked over plain HTTP, switching to Chrome")
                return out + [o for o in pracuj_chrome(keywords, max_pages, delay) if o["url"] not in seen]
            new = 0
            for g in groups:
                o = _norm(g, kw)
                key = o["url"] or (o["company"], o["title"], o["city"])
                if not o["title"] or key in seen:
                    continue
                seen.add(key)
                out.append(o)
                new += 1
            print(f"[pracuj] {kw!r} page {pn}: {new} new")
            time.sleep(random.uniform(*delay))
            if new == 0:
                break
    return out


def pracuj_chrome(keywords, max_pages=10, delay=(2.0, 4.5)):
    from playwright.sync_api import sync_playwright

    out, seen = [], set()
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        # without the automation flags Cloudflare sees a regular Chrome (otherwise the challenge loops forever)
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE), channel="chrome", headless=False, locale="pl-PL", no_viewport=True,
            ignore_default_args=["--enable-automation"], args=["--disable-blink-features=AutomationControlled"])
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for kw in keywords:
            for pn in range(1, max_pages + 1):
                url = f"https://www.pracuj.pl/praca/{quote(kw)};kw" + (f"?pn={pn}" if pn > 1 else "")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=90000)
                except Exception as e:
                    print(f"[pracuj] {url}: {str(e)[:120]}")
                if not _wait_ready(page, timeout=600):
                    print(f"[pracuj] no listing on {url}, skipping keyword")
                    break
                groups = []
                try:
                    el = page.query_selector("script#__NEXT_DATA__")
                    if el:
                        groups = list(_walk(json.loads(el.inner_text())))
                    if not groups:
                        groups = page.evaluate(DOM_JS)
                except Exception as e:  # JSON change or a navigation mid-read: skip the page, keep the rest
                    print(f"[pracuj] {url}: parse failed: {str(e)[:120]}")
                new = 0
                for g in groups:
                    o = _norm(g, kw)
                    key = o["url"] or (o["company"], o["title"], o["city"])
                    if not o["title"] or key in seen:
                        continue
                    seen.add(key)
                    out.append(o)
                    new += 1
                print(f"[pracuj] {kw!r} page {pn}: {new} new")
                if new == 0:
                    break
                time.sleep(random.uniform(*delay))
        ctx.close()
    return out
