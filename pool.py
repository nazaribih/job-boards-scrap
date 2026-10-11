"""Company pool and seed people for the research agent, from every source we have.

  python pool.py build     # merge sources -> data/company_pool.json, data/seed_people.json
  python pool.py judge     # LLM verdict for pool companies not judged yet (website + what the source knew)

Sources:
  job_boards   data/companies.csv: qualified companies from the job-board pipeline (already judged)
  booking      semi-manual-dealaris-outreach/scraped leads poland/dealaris_all_leads.csv, tier A/B: Polish companies
               whose site has Calendly or a "book a demo" button, i.e. sales-led
  calling      .../icps/output/scraped_companies_calling_fit.csv, everything but not_phone / not_applicable
  seed people  .../icps/output/linkedin_leads_scored.csv, band A/B: decision makers found earlier, with their
               degree to each founder
Each company appears once (domain, else normalised name); job-board data wins over the other sources.
"""
import csv
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import store

from jbscrap.score import company_key

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
POOL = DATA / "company_pool.json"
SEEDS = store.path("seed_people.json")
SRC = Path(os.environ.get("SEMI_MANUAL_DIR", ROOT.parent.parent.parent / "semi-manual-dealaris-outreach"))
TRACK_B_SEGMENTS = {"ua_outsourcing", "ua_ecom_cc", "pl_b2b_telesales", "pl_finance_brokers", "pl_oze"}


def domain_of(url):
    host = re.sub(r"^[a-z]+://", "", (url or "").strip().lower()).split("/")[0]
    return host[4:] if host.startswith("www.") else host


def _rows(name):
    # local_sources: copies of the semi-manual files, so the service has them too
    for p in (store.path(f"local_sources/{name}"), SRC / "scraped leads poland" / name, SRC / "icps" / "output" / name):
        if p.exists():
            return list(csv.DictReader(p.open(encoding="utf-8-sig")))
    return []


def build():
    pool = {c["key"]: c for c in json.loads(POOL.read_text())} if POOL.exists() else {}
    seen = {}

    def add(key, name, domain, source, **extra):
        dk = domain or company_key(name)
        if dk in seen:
            pool[seen[dk]].setdefault("sources", [])
            if source not in pool[seen[dk]]["sources"]:
                pool[seen[dk]]["sources"].append(source)
            return
        seen[dk] = key
        c = pool.setdefault(key, {"key": key, "name": name, "domain": domain, "sources": [source], "status": "new"})
        c.update({k: v for k, v in extra.items() if v not in (None, "")})
        if source not in c["sources"]:
            c["sources"].append(source)

    for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8")):
        if c["status"] == "qualified":
            add(company_key(c["company"]), c["company"], domain_of(c["li_website"] or c["website"]), "job_boards",
                verdict=c["llm_verdict"], fit=c["llm_fit"], tier=c["tier"], score=c["company_score"], judged="job_boards")
    for r in _rows("dealaris_all_leads.csv"):
        if r["tier"] in ("A", "B"):
            add(company_key(r["company"]), r["company"], domain_of(r["domain"] or r["url"]), "booking",
                city=r["city"], industry=r["industry"], prior=r["why"], site=r["url"], nip=r["nip"], krs=r["krs"])
    for r in _rows("scraped_companies_calling_fit.csv"):
        if r["band"] not in ("not_phone", "not_applicable"):
            add(company_key(r["company"]), r["company"], domain_of(r["domain"] or r["url"]), "calling",
                city=r["city"], prior=r["verdict"], site=r["url"])
    POOL.write_text(json.dumps(list(pool.values()), ensure_ascii=False, indent=1))

    seeds = []
    for r in _rows("linkedin_leads_scored.csv"):
        if r["band"] not in ("A", "B") or not r["linkedin_url"]:
            continue
        seeds.append({"name": r["name"], "headline": r["headline"], "url": r["linkedin_url"].split("?")[0].rstrip("/"),
                      "public_identifier": r["linkedin_url"].split("?")[0].rstrip("/").rsplit("/", 1)[-1],
                      "segment": r["segment"], "band": r["band"], "score": float(r["score"] or 0), "why": r["why"],
                      "degree": {"hleb": r["degree_to_hleb"], "nazarii": r["degree_to_nazarii"]},
                      "track": "B" if r["segment"] in TRACK_B_SEGMENTS else "A",
                      "lang_hint": "uk" if r["segment"].startswith("ua_") else "pl"})
    SEEDS.write_text(json.dumps(seeds, ensure_ascii=False, indent=1))
    from collections import Counter
    print(f"[pool] {len(pool)} companies by source {Counter(s for c in pool.values() for s in c['sources'])}, {len(seeds)} seed people")


def judge():
    """Same judge as the job-board pipeline, on what we know about pool companies plus their website."""
    sys.path.insert(0, str(ROOT))
    from judge import SCHEMA, SYSTEM, ask, load_env, site_text
    load_env()
    key = os.environ["OPENAI_API_KEY"]
    pool = json.loads(POOL.read_text())
    todo = [c for c in pool if not c.get("judged")]
    print(f"[pool] judging {len(todo)} companies")

    def one(c):
        text = (f"Company: {c['name']}\nWebsite: {c.get('site') or c.get('domain')}\nIndustry/registry: {c.get('industry', '')}\n"
                f"What an earlier scan noted: {c.get('prior', '')}\nCity: {c.get('city', '')}\n"
                f"Website text: {site_text(c.get('site') or c.get('domain'))}")
        try:
            return c["key"], ask(key, text, SYSTEM, SCHEMA)
        except Exception as e:
            return c["key"], {"error": str(e)[:100]}

    by_key = {c["key"]: c for c in pool}
    with ThreadPoolExecutor(8) as ex:
        for k, v in ex.map(one, todo):
            if "error" not in v:
                by_key[k].update(verdict=v["verdict"], fit=v["fit"], channel=v["sales_channel"], segment=v["segment"],
                                 reason=v["reason"], judged="llm")
    POOL.write_text(json.dumps(pool, ensure_ascii=False, indent=1))
    from collections import Counter
    print(f"[pool] verdicts {Counter(c.get('verdict') for c in pool)}")


if __name__ == "__main__":
    {"build": build, "judge": judge}[sys.argv[1] if len(sys.argv) > 1 else "build"]()
