"""pracuj.pl through the user's own Chrome (Playwright, headed, persistent profile).

pracuj.pl sits behind a Cloudflare challenge that blocks datacenter IPs, so this runs on a local machine with
Google Chrome installed. The profile in data/.pracuj_profile keeps the Cloudflare clearance between runs; if a
challenge shows up, solve it in the opened window and the scraper continues.

  pip install playwright   # uses the installed Google Chrome (channel="chrome"), no browser download
"""
import json
import random
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
        if page.query_selector("script#__NEXT_DATA__") or page.query_selector('[data-test="default-offer"]'):
            return True
        if not warned and ("challenge" in page.content().lower() or "just a moment" in page.title().lower()):
            print("[pracuj] Cloudflare check in the Chrome window — solve it there, scraping continues after")
            warned = True
        time.sleep(2)
    return False


def pracuj_pl(keywords, max_pages=10, delay=(2.0, 4.5)):
    from playwright.sync_api import sync_playwright

    out, seen = [], set()
    PROFILE.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(str(PROFILE), channel="chrome", headless=False, locale="pl-PL")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        for kw in keywords:
            for pn in range(1, max_pages + 1):
                url = f"https://www.pracuj.pl/praca/{quote(kw)};kw" + (f"?pn={pn}" if pn > 1 else "")
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                if not _wait_ready(page):
                    print(f"[pracuj] no listing on {url}, skipping keyword")
                    break
                groups = []
                el = page.query_selector("script#__NEXT_DATA__")
                if el:
                    try:
                        groups = list(_walk(json.loads(el.inner_text())))
                    except json.JSONDecodeError:
                        groups = []
                if not groups:
                    groups = page.evaluate(DOM_JS)
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
