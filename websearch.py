"""Company facts and decision makers from an OpenAI web search, so LinkedIn is only the fallback.

  python websearch.py [--tiers ABC] [--limit 0] [--workers 4]

For each qualified job-board company not looked up yet: its website, LinkedIn company page, headcount and the people
who run it or its sales (CEO / prezes / owner / sales director), with their LinkedIn profile links. The model
searches the web (the company's own site, LinkedIn pages, KRS / rejestr.io, press) and may report only what a page
it opened says, with that page's link. People already read off the company's website (site_people.py) are passed in,
so the search can find their LinkedIn profiles too.

Results are cached in websearch_cache.json (in the database in db mode). Read by:
  run.py           headcount for the 10-200 filter when LinkedIn never looked the company up;
  people.py        decision makers with a LinkedIn link go into decision_makers.csv (source "websearch");
  linkedin_queue   skips the LinkedIn lookups the search already answered.
A person found this way enters the campaign by LinkedIn link; the profile view before the invite checks the headline
doesn't name another employer (campaign.py).
"""
import argparse
import csv
import datetime as dt
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

import requests

import store
from jbscrap.score import company_key
from judge import load_env

DATA = Path(__file__).resolve().parent / "data"
CACHE = store.path("websearch_cache.json")
SITE = store.path("site_people.json")
MODEL = "gpt-5-mini"
VERSION = 1

SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["website", "linkedin_company_url", "employees", "employees_basis", "people", "note"],
          "properties": {
              "website": {"type": "string"},
              "linkedin_company_url": {"type": "string"},
              "employees": {"type": "integer", "description": "best estimate of employees; 0 if no page gives it"},
              "employees_basis": {"type": "string", "description": "where the number comes from, e.g. 'LinkedIn page: 51-200, 87 members'"},
              "people": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["name", "title", "linkedin_url", "source_url", "current"],
                  "properties": {"name": {"type": "string"}, "title": {"type": "string"},
                                 "linkedin_url": {"type": "string", "description": "https://www.linkedin.com/in/... or ''"},
                                 "source_url": {"type": "string"},
                                 "current": {"type": "boolean", "description": "the source shows they hold the role now"}}}},
              "note": {"type": "string"}}}

SYSTEM = """You research one Polish company for a B2B sales team. Use web search: the company's own website, its
LinkedIn company page, KRS / rejestr.io / aleo.com / other company registries, and press. Return:
- website: the company's own site ('' if none found);
- linkedin_company_url: its LinkedIn company page ('' if not found);
- employees: the best estimate of headcount from a page you opened (LinkedIn page size or member count, registry
  data, the site's own statements); 0 if nothing gives it. employees_basis says which page and what it said;
- people: up to 4 people who decide on buying sales tools: CEO / prezes zarządu, owner / founder / wspólnik, board
  members, sales director / head of sales / sales manager. For each, the LinkedIn profile URL only when a search
  result or page shows that exact profile (linkedin.com/in/...) for that person at this company; otherwise ''.
  source_url is the page that names them in that role. current=false if the source is old or says they left.
Report only what pages you opened say. Never guess a name, a profile URL or a number, and never build a URL from
a name. Wrong company with a similar name: leave it out. note: one short sentence on anything uncertain."""

lock = threading.Lock()


def ask(key, prompt):
    body = {"model": MODEL, "reasoning": {"effort": "low"}, "tools": [{"type": "web_search", "search_context_size": "low"}],
            "instructions": SYSTEM, "input": prompt,
            "text": {"format": {"type": "json_schema", "name": "company", "strict": True, "schema": SCHEMA}}}
    for attempt in range(5):
        r = requests.post("https://api.openai.com/v1/responses", json=body, timeout=300,
                          headers={"Authorization": f"Bearer {key}"})
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * 2 ** attempt)
            continue
        r.raise_for_status()
        out = r.json()
        text = next(c["text"] for o in out["output"] if o["type"] == "message" for c in o["content"] if c["type"] == "output_text")
        searches = sum(o["type"] == "web_search_call" for o in out["output"])
        return json.loads(text), searches
    r.raise_for_status()


def clean_li(url):
    url = (url or "").split("?")[0].strip().rstrip("/")
    if "linkedin.com/in/" not in url:
        return ""
    # LinkedIn's own results spell non-ASCII slugs out (rafałrasmus); pages often carry them encoded (rafa%C5%82rasmus)
    return "https://www.linkedin.com/in/" + unquote(url.split("linkedin.com/in/", 1)[1].split("/")[0])


def load():
    return store.read_json(CACHE, {})


def people_for(k, cache=None):
    """Decision makers the search found at a company, with a LinkedIn link, who hold the role now."""
    e = (cache if cache is not None else load()).get(k) or {}
    return [p for p in e.get("people", []) if p.get("linkedin_url") and p.get("current")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="ABC")
    ap.add_argument("--limit", type=int, default=0, help="at most this many companies this run (0: all)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    load_env()
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return print("[websearch] OPENAI_API_KEY not set")
    cache = load()
    site = store.read_json(SITE, {})
    cos = [c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8"))
           if c["status"] == "qualified" and c["tier"] in args.tiers]
    cos.sort(key=lambda c: -float(c["company_score"] or 0))
    todo = [c for c in cos if (cache.get(company_key(c["company"])) or {}).get("v") != VERSION]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[websearch] {len(cos)} qualified companies in tiers {args.tiers}, {len(todo)} to look up", flush=True)
    stats = {"done": 0, "searches": 0, "people": 0, "with_li": 0, "headcount": 0, "failed": 0}

    def one(c):
        k = company_key(c["company"])
        named = [f"{p['name']} ({p.get('role', '')})" for p in (site.get(k) or {}).get("people", [])]
        prompt = "\n".join(x for x in (
            f"Company: {c['company']}",
            f"City: {c['cities'].replace('|', ', ')}" if c["cities"] else "",
            f"Website on record: {c['li_website'] or c['website']}" if c["li_website"] or c["website"] else "",
            f"LinkedIn page on record: {c['li_url']}" if c["li_url"] else "",
            f"Hiring now: {c['top_vacancy']}",
            f"Named on their website (find these people's LinkedIn profiles too): {'; '.join(named)}" if named else "",
        ) if x)
        try:
            res, n = ask(key, prompt)
        except Exception as e:
            with lock:
                stats["failed"] += 1
            return print(f"[websearch] {c['company']!r}: {str(e)[:150]}", flush=True)
        for p in res["people"]:
            p["linkedin_url"] = clean_li(p["linkedin_url"])
        with lock:
            cache[k] = dict(res, company=c["company"], v=VERSION, at=dt.datetime.now(dt.timezone.utc).isoformat(), searches=n)
            stats["done"] += 1
            stats["searches"] += n
            stats["people"] += len(res["people"])
            stats["with_li"] += len(people_for(k, cache))
            stats["headcount"] += res["employees"] > 0
            if stats["done"] % 10 == 0:
                store.write_json(CACHE, cache)
                print(f"[websearch] {stats}", flush=True)

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(one, todo))
    store.write_json(CACHE, cache)
    print(f"[websearch] {stats}", flush=True)


if __name__ == "__main__":
    main()
