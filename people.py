"""Decision makers (CEO / prezes / founder / head of sales ...) for tier A+B companies via Unipile classic people search.

  python people.py [--tiers AB] [--max-searches 1200]

Env: UNIPILE_DSN, UNIPILE_API_KEY, and the LinkedIn account in UNIPILE_PEOPLE_ACCOUNT (falls back to UNIPILE_ACCOUNT_ID).
Classic search ignores OR, so each company gets one single-keyword search per term, and the role is picked from the
headline locally. Results are cached in data/people_cache.json; 401/403/429 stops the run.
"""
import argparse
import csv
import json
import os
import random
import re
import time
from pathlib import Path

import requests

from jbscrap.score import company_key, fold
import store
from jbscrap.unipile import _tokens

DATA = Path(__file__).resolve().parent / "data"
CACHE = store.path("people_cache.json")
SITE = store.path("site_people.json")
TERMS = ["CEO", "sales", "sprzedaży"]  # the last one only when the first two found fewer than 2 people

# (rank, label, pattern on the folded headline) - lower rank = more senior buyer
ROLES = [
    (1, "CEO / prezes", r"\bceo\b|\bprezes|\bpresident\b|managing director|\bdyrektor generaln|general manager"),
    (2, "Founder / owner / partner", r"founder|zalozyciel|\bowner\b|wlasciciel|\bpartner\b"),
    (3, "Board member", r"czlonek zarzadu|board member|wiceprezes|vice president|\bvp\b|\bcoo\b|\bcco\b"),
    (4, "Sales leader", r"\bcro\b|chief revenue|chief sales|head of sales|sales director|director of sales|dyrektor (ds\.? )?sprzedazy"
                        r"|dyrektor handlow|head of business development|business development director|head of growth"),
    (5, "Sales manager", r"sales manager|kierownik (dzialu |zespolu )?sprzedazy|menedzer sprzedazy|manager sprzedazy"
                         r"|team lead(er)? sprzedazy|sales team lead|koordynator zespolu sprzedazy"),
]
STAFFING_INDUSTRY = re.compile(r"staffing|recruiting|rekrut", re.I)


NOT_BUYER = re.compile(r"\bhr\b|recruit|rekrut|talent|people &|ex-|former|byly|była")


def role_of(headline):
    h = fold(headline)
    for rank, label, rx in ROLES:
        if re.search(rx, h):
            # "HR Business Partner", "ex-CEO", recruiters who list C-level hiring - not the buyer
            return None if NOT_BUYER.search(h) and rank > 1 else (rank, label)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="AB")
    ap.add_argument("--max-searches", type=int, default=1200)
    ap.add_argument("--per-company", type=int, default=3)
    args = ap.parse_args()

    dsn = os.environ["UNIPILE_DSN"].strip().rstrip("/")
    base = dsn if dsn.startswith("http") else f"https://{dsn}"
    account = os.environ.get("UNIPILE_PEOPLE_ACCOUNT") or os.environ["UNIPILE_ACCOUNT_ID"]
    s = requests.Session()
    s.headers.update({"X-API-KEY": os.environ["UNIPILE_API_KEY"], "accept": "application/json"})

    li = json.loads((DATA / "unipile_cache.json").read_text())
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    companies = [c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8"))
                 if c["status"] == "qualified" and c["tier"] in args.tiers and c["headcount_verified"] == "True"]
    skipped = [c for c in companies if STAFFING_INDUSTRY.search(c["li_industry"])]
    companies = [c for c in companies if not STAFFING_INDUSTRY.search(c["li_industry"])]
    # companies the LLM judge rejected are already disqualified in companies.csv; "maybe" go after "fit"
    companies.sort(key=lambda c: c.get("llm_verdict") != "fit")
    print(f"{len(companies)} companies, {len(skipped)} staffing agencies skipped")

    searches, stop = 0, False
    for c in companies:
        k = company_key(c["company"])
        cid = ((li.get(k) or {}).get("profile") or {}).get("id")
        if not cid:
            continue
        entry = cache.setdefault(k, {"company_id": cid, "hits": {}})
        for term in TERMS:
            if term in entry["hits"]:
                continue
            found = {i["id"] for t in entry["hits"].values() for i in t if role_of(i.get("headline") or "")}
            if term == TERMS[-1] and len(found) >= 2:
                break
            if searches >= args.max_searches:
                stop = True
                break
            r = s.post(f"{base}/api/v1/linkedin/search", params={"account_id": account}, timeout=60,
                       json={"api": "classic", "category": "people", "company": [cid], "keywords": term})
            searches += 1
            if r.status_code in (401, 403, 429):
                print(f"[people] {r.status_code} on {c['company']!r}, stopping: {r.text[:150]}")
                stop = True
                break
            if r.status_code >= 400:
                print(f"[people] {r.status_code} on {c['company']!r}: {r.text[:150]}")
                entry["hits"][term] = []
            else:
                entry["hits"][term] = [{f: i.get(f) for f in ("id", "name", "headline", "location", "public_profile_url",
                                                              "profile_url", "network_distance")}
                                       for i in r.json().get("items") or []]
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
            time.sleep(random.uniform(4, 9))
        if stop:
            break
    print(f"[people] {searches} searches this run, {len(cache)} companies cached")
    write_csv(companies, skipped, cache, args.per_company)


ROLE_WEIGHT = {"CEO / prezes": 1.0, "Founder / owner / partner": 0.95, "Sales leader": 0.95,
               "Board member": 0.8, "Sales manager": 0.75}


def lead_score(company_score, role, mentions, distance, source):
    """0-100 score of the PERSON as an outreach target: the company's score carries most of it (70), then how close
    the role is to the buyer (20), then evidence the person works there now (5) and how reachable they are (5)."""
    s = float(company_score or 0) * 0.7 + 20 * ROLE_WEIGHT.get(role, 0.5)
    s += 5 if (mentions or source == "website") else 0
    s += {"DISTANCE_1": 5, "DISTANCE_2": 4, "DISTANCE_3": 1}.get(distance or "", 0)
    return round(s, 1)


def site_people(k):
    site = json.loads(SITE.read_text()) if SITE.exists() else {}
    return (site.get(k) or {}).get("people", [])


def write_csv(companies, skipped, cache, per_company):
    rows = []
    for c in companies:
        e = cache.get(company_key(c["company"]))
        people = {}
        toks = _tokens(c["company"]) | _tokens(c["li_url"].rstrip("/").rsplit("/", 1)[-1].replace("-", " "))
        for i in (p for t in (e or {"hits": {}})["hits"].values() for p in t):
            head = i.get("headline") or ""
            role = role_of(head)
            name = i.get("name") or ""
            if not role or not name or name.lower().startswith(("linkedin member", "участник", "członek linkedin")):
                continue
            mention = bool(toks & _tokens(head))
            # the company filter also returns past employees: a headline naming another employer is not ours
            if not mention and re.search(r"\bat\b|\bw\b|@|\bdla\b", fold(head)):
                continue
            people.setdefault(i["id"], (role, mention, dict(i, source="linkedin")))
        for sp in site_people(company_key(c["company"])):  # named on the company's own website
            li = sp.get("li") or {}
            if li.get("id") in people:
                continue
            label = sp["role"]
            rank = next((r for r, l, _ in ROLES if l == label), 5)
            people.setdefault(li.get("id") or "site:" + sp["name"], ((rank, label), True, {
                "name": sp["name"], "headline": sp["title"], "public_profile_url": li.get("public_profile_url"),
                "profile_url": li.get("profile_url"), "network_distance": li.get("network_distance"),
                "id": li.get("id"), "source": "website", "source_url": sp["source_url"], "email": sp.get("email", ""),
                "phone": sp.get("phone", "")}))
        best = sorted(people.values(), key=lambda x: (not x[1], x[0][0]))[:per_company]
        base = {"company": c["company"], "tier": c["tier"], "company_score": c["company_score"], "li_headcount": c["li_headcount"],
                "company_linkedin": c["li_url"], "top_vacancy": c["top_vacancy"], "top_vacancy_url": c["top_vacancy_url"],
                "website": c["li_website"] or c["website"], "llm_verdict": c.get("llm_verdict", ""),
                "llm_fit": c.get("llm_fit", ""), "llm_channel": c.get("llm_channel", ""), "llm_reason": c.get("llm_reason", "")}
        base["dm_searched"] = bool(e)
        if not best:
            rows.append(dict(base, rank="", name="", role="", headline="", person_linkedin="", mentions_company="",
                             lead_score="", source="", source_url="", email="", phone=""))
        for n, ((rank, label), mention, i) in enumerate(best, 1):
            url = (i.get("public_profile_url") or i.get("profile_url") or "").split("?")[0]
            rows.append(dict(base, rank=n, name=i["name"], role=label, headline=i["headline"], person_linkedin=url,
                             mentions_company=mention, source=i.get("source", "linkedin"), source_url=i.get("source_url", ""),
                             email=i.get("email", ""), phone=i.get("phone", ""),
                             lead_score=lead_score(c.get("company_score"), label, mention, i.get("network_distance"),
                                                   i.get("source"))))
    cols = ["company", "tier", "company_score", "li_headcount", "company_linkedin", "website", "rank", "lead_score",
            "name", "role", "headline", "person_linkedin", "source", "source_url", "email", "phone", "mentions_company", "dm_searched", "llm_verdict", "llm_fit", "llm_channel", "llm_reason",
            "top_vacancy", "top_vacancy_url"]
    with store.path("decision_makers.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    stats = {"companies": len(companies), "staffing_skipped": len(skipped), "searched": sum(1 for c in companies if company_key(c["company"]) in cache),
             "with_people": len({r["company"] for r in rows if r["name"]}), "people": sum(1 for r in rows if r["name"])}
    (DATA / "people_stats.json").write_text(json.dumps(stats))
    print(stats)


if __name__ == "__main__":
    main()
