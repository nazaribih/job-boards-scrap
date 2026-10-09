"""Decision makers from company websites, no LinkedIn: team / about / contact pages read by an LLM.

  python site_people.py [--tiers AB] [--workers 4]

For qualified companies without decision makers from LinkedIn, fetches the homepage and up to four pages that look
like about / team / management / contact, finds the NIP and KRS number (validated, kept for reference) and asks the
model for people who run the company or its sales, with the page each one was read from. Results are cached in
data/site_people.json. Only what the pages say is kept: the model is told not to add anyone from memory.
"""
import argparse
import csv
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from judge import MODEL, ask, load_env
from jbscrap.score import company_key

DATA = Path(__file__).resolve().parent / "data"
CACHE = DATA / "site_people.json"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
LINK_RX = re.compile(r"o-nas|onas|about|zespol|zespół|team|kadra|zarzad|zarząd|management|leadership|kontakt|contact|"
                     r"ludzie|people|firma|company|who-we-are|kim-jestesmy", re.I)

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["people"],
    "properties": {"people": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["name", "title", "role", "source_url", "email", "phone"],
        "properties": {
            "name": {"type": "string", "description": "Full name exactly as on the page"},
            "title": {"type": "string", "description": "Job title as on the page"},
            "role": {"type": "string", "enum": ["CEO / prezes", "Founder / owner / partner", "Board member",
                                                "Sales leader", "Sales manager"]},
            "source_url": {"type": "string"},
            "email": {"type": "string", "description": "Personal business email shown next to the person, else empty"},
            "phone": {"type": "string", "description": "Direct phone shown next to the person, else empty"},
        }}}},
}
SYSTEM = """You extract the people who run a Polish company or its sales team from its own web pages.
Keep only: CEO / prezes zarządu / managing director, founders, owners, partners, board members (członek zarządu,
wiceprezes), and sales leadership (dyrektor sprzedaży / handlowy, head of sales, CRO, kierownik sprzedaży,
sales manager). Skip everyone else (consultants, support, marketing, HR, office staff, testimonial authors, clients).
Use only names that appear in the given page texts, with the URL of the page they appear on. Never add people
from memory. Return an empty list when the pages name nobody in these roles."""

lock = threading.Lock()


def nip_ok(n):
    w = [6, 5, 7, 2, 3, 4, 5, 6, 7]
    return len(n) == 10 and sum(int(a) * b for a, b in zip(n, w)) % 11 == int(n[9])


def text_of(html):
    html = re.sub(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def crawl(site):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"})
    base = site if site.startswith("http") else "https://" + site
    host = urlparse(base).netloc
    pages, nip, krs = [], None, None
    try:
        r = s.get(base, timeout=20)
    except Exception:
        return pages, nip, krs
    links = []
    for href in re.findall(r'href="([^"#]+)"', r.text):
        u = urljoin(r.url, href)
        if urlparse(u).netloc.endswith(host.removeprefix("www.")) and LINK_RX.search(u) and u not in links:
            links.append(u)
    for url, html in [(r.url, r.text)] + [(u, None) for u in links[:4]]:
        if html is None:
            time.sleep(random.uniform(1.5, 4))  # read the site like a person clicking through, not a crawler
            try:
                html = s.get(url, timeout=20).text
            except Exception:
                continue
        t = text_of(html)
        for m in re.finditer(r"NIP[^0-9]{0,15}((?:\d[\s-]?){10})", t):
            n = re.sub(r"\D", "", m.group(1))
            if nip_ok(n):
                nip = nip or n
        m = re.search(r"KRS[^0-9]{0,15}(\d{10})", t)
        krs = krs or (m.group(1) if m else None)
        pages.append((url, t[:6000]))
    return pages, nip, krs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="AB")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    load_env()
    import os
    key = os.environ["OPENAI_API_KEY"]

    have = {r["company"] for r in csv.DictReader(open(DATA / "decision_makers.csv", encoding="utf-8")) if r["name"]}
    found = json.loads((DATA / "sites_found.json").read_text()) if (DATA / "sites_found.json").exists() else {}
    cos = []
    for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8")):
        if not c["li_website"] and not c["website"]:
            c["website"] = (found.get(company_key(c["company"])) or {}).get("website") or ""
        if c["status"] == "qualified" and c["tier"] in args.tiers and c["company"] not in have and (c["li_website"] or c["website"]):
            cos.append(c)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    todo = [c for c in cos if company_key(c["company"]) not in cache]
    print(f"[site] {len(cos)} companies without decision makers, {len(todo)} to read")

    def one(c):
        k = company_key(c["company"])
        pages, nip, krs = crawl((c["li_website"] or c["website"]).strip())
        people = []
        if pages:
            text = f"Company: {c['company']}\n\n" + "\n\n".join(f"PAGE {u}\n{t}" for u, t in pages)
            try:
                people = ask(key, text, SYSTEM, SCHEMA)["people"]
            except Exception as e:
                print(f"[site] {c['company']!r}: {str(e)[:120]}")
                return
        with lock:
            cache[k] = {"company": c["company"], "nip": nip, "krs": krs, "pages": [u for u, _ in pages], "people": people}
            if len(cache) % 10 == 0:
                CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
                print(f"[site] {len(cache)} read, {sum(bool(v['people']) for v in cache.values())} with people")

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(one, todo))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    print(f"[site] done: {len(cache)} read, {sum(bool(v['people']) for v in cache.values())} with people, "
          f"{sum(len(v['people']) for v in cache.values())} people, {sum(bool(v['nip']) for v in cache.values())} with NIP")


if __name__ == "__main__":
    main()
