"""Fetchers for Polish job boards. Each returns a list of normalized offer dicts."""
import html
import re
import time

import requests

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
S = requests.Session()
S.headers["User-Agent"] = UA


def _offer(**kw):
    base = dict(source="", offer_id="", title="", company="", city="", workplace="",
                category="", seniority="", salary="", description="", url="", published="",
                company_size="", company_url="")
    base.update(kw)
    return base


def _salary_jj(emp_types):
    for e in emp_types or []:
        if e.get("currency") == "PLN" and e.get("currencySource") == "original":
            lo, hi = e.get("from"), e.get("to")
            if lo or hi:
                return f"{int(lo or 0)}-{int(hi or 0)} PLN/{e.get('unit', '').lower()} {e.get('type', '')}"
    return ""


def rocketjobs(categories=("sales",), page_size=100, max_items=5000):
    """rocketjobs.pl (non-IT sister board of justjoin.it), JSON candidate API."""
    out = []
    for cat in categories:
        start = 0
        while start < max_items:
            r = S.get("https://rocketjobs.pl/api/candidate-api/offers",
                      params={"itemsCount": page_size, "categories": cat, "from": start},
                      headers={"Version": "2"}, timeout=30)
            r.raise_for_status()
            d = r.json()
            data = d.get("data", [])
            for o in data:
                out.append(_offer(
                    source="rocketjobs.pl", offer_id=o["guid"], title=o["title"],
                    company=o["companyName"], city=o.get("city", ""),
                    workplace=o.get("workplaceType", ""), category=o["category"]["key"],
                    seniority=o.get("experienceLevel", ""), salary=_salary_jj(o.get("employmentTypes")),
                    description=", ".join(s["name"] for s in o.get("requiredSkills", [])),
                    url=f"https://rocketjobs.pl/oferta-pracy/{o['slug']}",
                    published=(o.get("lastPublishedAt") or o.get("publishedAt") or "")[:10]))
            if len(data) < page_size or start + page_size >= d["meta"]["totalItems"]:
                break
            start += page_size
            time.sleep(0.3)
    return out


def _rj_detail(o):
    slug = o["url"].rsplit("/", 1)[-1]
    for attempt in range(3):
        try:
            r = S.get(f"https://rocketjobs.pl/api/candidate-api/offers/{slug}", headers={"Version": "2"}, timeout=30)
            if r.status_code == 200:
                d = r.json()
                body = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", d.get("body") or ""))).strip()
                o.update(description=f"{body} | skills: {o['description']}"[:4000],
                         company_size=d.get("companySize") or "", company_url=d.get("companyUrl") or "")
                return
            if r.status_code == 404:
                return
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))


def rocketjobs_details(offers, workers=4):
    """Full description + companySize/companyUrl for the given rocketjobs offers (mutates in place)."""
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(_rj_detail, offers))


def nofluffjobs(categories=("sales",), page_size=100, max_pages=20):
    """nofluffjobs.com search API (POST)."""
    out = []
    for cat in categories:
        for page in range(1, max_pages + 1):
            r = S.post("https://nofluffjobs.com/api/search/posting",
                       params={"pageTo": page, "pageSize": page_size, "salaryCurrency": "PLN",
                               "salaryPeriod": "month", "region": "pl", "language": "pl-PL"},
                       headers={"Content-Type": "application/infiniteSearch+json"},
                       json={"criteriaSearch": {"category": [cat]}, "page": page}, timeout=30)
            r.raise_for_status()
            d = r.json()
            for p in d.get("postings", []):
                places = p.get("location", {}).get("places", [])
                city = next((pl.get("city") for pl in places if pl.get("city")), "")
                sal = p.get("salary") or {}
                out.append(_offer(
                    source="nofluffjobs.com", offer_id=p["id"], title=p["title"],
                    company=p["name"], city=city,
                    workplace="remote" if p.get("location", {}).get("fullyRemote") else "",
                    category=p.get("category", cat), seniority=",".join(p.get("seniority", [])),
                    salary=f"{sal.get('from', '')}-{sal.get('to', '')} {sal.get('currency', '')} {sal.get('type', '')}" if sal else "",
                    description=", ".join(t.get("value", "") for t in p.get("tiles", {}).get("values", [])),
                    url=f"https://nofluffjobs.com/pl/job/{p.get('url') or p['id']}",
                    published=time.strftime("%Y-%m-%d", time.gmtime((p.get("renewed") or p.get("posted") or 0) / 1000))))
            if page >= d.get("totalPages", 1):
                break
            time.sleep(0.3)
    return out


def _items(page_html):
    page_html = page_html.split('class="al__bottom')[0]
    for chunk in page_html.split('<li class="listing-v2__item')[1:]:
        m = re.search(r'data-ad-id="(\d+)"', chunk[:300])
        if m:
            yield m.group(1), chunk


def _txt(rx, s):
    m = re.search(rx, s, re.S)
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1)))).strip() if m else ""


def praca_pl(keywords, max_pages=15):
    """praca.pl keyword search, HTML listing (30 per page, ',N.html' pagination)."""
    out, seen = [], set()
    for kw in keywords:
        slug = kw.replace(" ", "-")
        for page in range(1, max_pages + 1):
            url = f"https://www.praca.pl/s-{slug}.html" if page == 1 else f"https://www.praca.pl/s-{slug},{page}.html"
            r = S.get(url, timeout=30)
            if r.status_code != 200:
                break
            items = list(_items(r.text))
            if not items:
                break
            new = 0
            for ad_id, body in items:
                if ad_id in seen:
                    continue
                seen.add(ad_id)
                new += 1
                tags = [html.unescape(t) for t in re.findall(r'listing-v2__tag-text">([^<]*)<', body)]
                out.append(_offer(
                    source="praca.pl", offer_id=ad_id,
                    title=_txt(r'class="listing-v2__title"[^>]*>(.*?)</a>', body),
                    company=_txt(r'class="listing-v2__employer"[^>]*>(.*?)</a>', body)
                    or _txt(r'class="listing-v2__employer"[^>]*>(.*?)</span>', body),
                    city=_txt(r'class="listing-v2__location"[^>]*>(?:\s*<i[^>]*></i>)?(.*?)</span>', body),
                    workplace=next((t for t in tags if t in ("Stacjonarna", "Hybrydowa", "Zdalna", "Mobilna")), ""),
                    salary=next((t for t in tags if "zł" in t), ""),
                    category=f"kw:{kw}",
                    seniority=next((t for t in tags if "(" in t and "junior" in t or "senior" in t or "kierownik" in t.lower()), ""),
                    description=_txt(r'class="listing-v2__teaser-text">(.*?)</p>', body),
                    url=_txt(r'class="listing-v2__title" href="([^"#]*)', body),
                    published=_txt(r'class="listing-v2__published">(.*?)</span>', body)))
            if new == 0 or len(items) < 30:
                break
            time.sleep(0.5)
    return out


def _ap_cards(page_html):
    page_html = re.sub(r"<svg.*?</svg>|<template.*?</template>", "", page_html, flags=re.S)
    # main result list only; promo sliders use "offer-card small"
    for chunk in re.split(r'x-ref="offer-\d+"', page_html)[1:]:
        yield chunk


def aplikuj_pl(keywords, max_pages=10):
    """aplikuj.pl keyword search, HTML listing (/praca/<kw>/strona-N)."""
    out, seen = [], set()
    for kw in keywords:
        slug = kw.replace(" ", "-")
        for page in range(1, max_pages + 1):
            url = f"https://www.aplikuj.pl/praca/{slug}" + (f"/strona-{page}" if page > 1 else "")
            r = S.get(url, timeout=30)
            if r.status_code != 200:
                break
            cards = list(_ap_cards(r.text))
            new = 0
            for c in cards:
                href = _txt(r'<a href="([^"]+)"\s+class="offer-title', c)
                m = re.search(r"/oferta/(\d+)/", href)
                if not m or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                new += 1
                out.append(_offer(
                    source="aplikuj.pl", offer_id=m.group(1), title=_txt(r'class="offer-title"[^>]*>(.*?)</a>', c),
                    company=_txt(r'href="https://www\.aplikuj\.pl/pracodawca/[^"]*"[^>]*>(.*?)</a>', c),
                    city=_txt(r'offer-card-labels-list-item--workPlace">(.*?)</li>', c),
                    salary=_txt(r'labels-list-item--salary">(.*?)</li>', c),
                    workplace=", ".join(w for w, k in (("zdalna", "remoteWork"), ("hybrydowa", "hybridWork"), ("mobilna", "mobileWork"))
                                        if f"--{k}" in c),
                    seniority="bez doświadczenia" if "--inexperience" in c else "",
                    category=f"kw:{kw}", url=href, published=_txt(r'class="offer-card-date[^"]*">(.*?)</time>', c)))
            if new == 0 or len(cards) < 20:
                break
            time.sleep(0.5)
    return out
