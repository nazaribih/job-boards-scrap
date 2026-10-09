"""Scrape PL job boards -> score vacancies for Dealaris fit -> dedupe companies -> (optional) LinkedIn headcount via Unipile.

  python run.py                 # scrape + score, enrich with Unipile if env is set
  python run.py --no-scrape     # re-score / re-enrich from data/raw_offers.json
  python run.py --sources pracuj --merge   # add pracuj.pl (local Chrome) to the existing offers
"""
import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from jbscrap import boards
from jbscrap.score import STAFFING, company_key, company_rollup, fold, is_known_enterprise, score_vacancy
from jbscrap.unipile import CACHE, Unipile, enrich

DATA = Path(__file__).resolve().parent / "data"
JUDGE = DATA / "judge_cache.json"

KEYWORDS = ["telesprzedaz", "telemarketing", "telemarketer", "konsultant telefoniczny", "call center", "contact center",
            "sprzedaz telefoniczna", "specjalista ds sprzedazy telefonicznej", "infolinia", "inside sales", "sdr", "bdr",
            "business development", "account executive", "sales development", "lead generation", "cold calling",
            "appointment setter", "closer", "new business", "sales representative", "pozyskiwanie klientow",
            "doradca klienta", "doradca handlowy", "specjalista ds sprzedazy", "sprzedaz b2b", "handlowiec",
            "account manager", "key account manager", "customer success", "kierownik sprzedazy", "sales manager",
            "team leader sprzedazy", "doradca nieruchomosci", "agent nieruchomosci", "fotowoltaika sprzedaz",
            "pompy ciepla sprzedaz", "doradca kredytowy", "doradca finansowy", "doradca ubezpieczeniowy",
            "doradca samochodowy", "sprzedaz zdalna", "praca zdalna sprzedaz", "sprzedaz kursow"]

MIN_HEAD, MAX_HEAD = 10, 200


SOURCES = {
    "rocketjobs": lambda: boards.rocketjobs(("sales", "support")),
    "nofluffjobs": lambda: boards.nofluffjobs(("sales", "customerService")),
    "praca": lambda: boards.praca_pl(KEYWORDS, max_pages=10),
    "aplikuj": lambda: boards.aplikuj_pl(KEYWORDS, max_pages=10),
    # needs a local machine with Google Chrome (Cloudflare blocks datacenter IPs)
    "pracuj": lambda: __import__("jbscrap.pracuj", fromlist=["pracuj_pl"]).pracuj_pl(KEYWORDS, max_pages=10),
}
DEFAULT_SOURCES = ["rocketjobs", "nofluffjobs", "praca", "aplikuj"]


def company_score(c):
    """0-100 score of the COMPANY as a Dealaris customer: pitch-deck fit and remote selling from the LLM judge
    (80 points), headcount inside the ICP (10) and hiring for sales right now (10, from the vacancy-based fit_score).
    Companies the judge hasn't seen get half the judge points, so they sink below judged fits."""
    if c["llm_fit"] != "":
        judge = int(c["llm_fit"]) * 6 + int(c["llm_remote_share"]) * 0.2
    else:
        judge = 40
    size = 10 if c["headcount_verified"] else 7 if c.get("llm_size") in ("10-50", "51-200") else 5
    return round(judge + size + c["fit_score"] / 10, 1)


def scrape(names):
    offers = []
    for name in names:
        try:
            got = SOURCES[name]()
        except Exception as e:  # one board failing shouldn't sink the run
            print(f"[{name}] failed: {e}")
            got = []
        print(f"[{name}] {len(got)} offers")
        offers += got
    # full text + company size for rocketjobs offers that look like sales (skip retail / non-sales titles)
    rj = [o for o in offers if o["source"] == "rocketjobs.pl" and score_vacancy(o)[1] > 0]
    if rj:
        boards.rocketjobs_details(rj)
        print(f"[rocketjobs] details for {len(rj)} offers, {sum(bool(o['company_size']) for o in rj)} with company size")
    return offers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scrape", action="store_true")
    ap.add_argument("--sources", default=",".join(DEFAULT_SOURCES),
                    help=f"comma list or 'all' ({', '.join(SOURCES)}); default skips pracuj")
    ap.add_argument("--merge", action="store_true",
                    help="keep offers from data/raw_offers.json for boards not scraped in this run")
    ap.add_argument("--max-lookups", type=int, default=600, help="Unipile company lookups per run")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    raw = DATA / "raw_offers.json"
    if args.no_scrape:
        offers = json.loads(raw.read_text())
    else:
        names = list(SOURCES) if args.sources == "all" else [n.strip() for n in args.sources.split(",") if n.strip()]
        offers = scrape(names)
        if args.merge and raw.exists():
            fresh = {o["source"] for o in offers}
            offers += [o for o in json.loads(raw.read_text()) if o["source"] not in fresh]
        raw.write_text(json.dumps(offers, ensure_ascii=False))

    # dedupe vacancies (same company + title + city across boards / keyword queries)
    seen, vacs = set(), []
    for o in offers:
        k = (company_key(o["company"]), fold(o["title"]).strip(), fold(o["city"]).split(",")[0].strip())
        if k in seen or not o["company"]:
            continue
        seen.add(k)
        cls, sc, hits = score_vacancy(o)
        vacs.append(dict(o, company_key=k[0], role_class=cls, role_score=sc, signals="|".join(hits)))

    by_co = defaultdict(list)
    for v in vacs:
        by_co[v["company_key"]].append(v)

    companies = []
    for key, vs in by_co.items():
        name = Counter(v["company"] for v in vs).most_common(1)[0][0]
        roll = company_rollup(vs)
        if roll["sales_vacancies"] == 0:
            continue
        status, reason = "qualified", ""
        board_size = Counter(v.get("company_size", "") for v in vs if v.get("company_size")).most_common(1)
        board_size = board_size[0][0] if board_size else ""
        if STAFFING.search(fold(name)):
            status, reason = "disqualified", "staffing agency / hidden client"
        elif is_known_enterprise(name):
            status, reason = "disqualified", "enterprise (known brand, pending LinkedIn check)"
        elif board_size == "501+":
            status, reason = "disqualified", "enterprise (501+ on rocketjobs profile)"
        companies.append(dict(company=name, company_key=key, **roll, status=status, disqualify_reason=reason,
                              board_company_size=board_size,
                              website=next((v["company_url"] for v in vs if v.get("company_url")), ""),
                              sources="|".join(sorted({v["source"] for v in vs})),
                              cities="|".join(sorted({v["city"].split(",")[0] for v in vs if v["city"]}))[:120],
                              top_vacancy=max(vs, key=lambda v: v["role_score"])["title"],
                              top_vacancy_url=max(vs, key=lambda v: v["role_score"])["url"],
                              li_match="", li_name="", li_url="", li_headcount="", li_headcount_range="",
                              li_industry="", li_website=""))

    if Unipile.available() or CACHE.exists():
        todo = sorted([c for c in companies if c["status"] == "qualified"], key=lambda c: -c["fit_score"])
        n = enrich(todo, max_lookups=args.max_lookups if Unipile.available() else 0)
        print(f"[unipile] {n} new lookups")
        for c in companies:
            hc = c["li_headcount"]
            # open-ended ranges ("10001-None") fall back to their lower bound
            bounds = [b for b in c["li_headcount_range"].split("-") if b.isdigit()]
            size = int(hc) if str(hc).isdigit() else (int(bounds[-1]) if bounds else None)
            if size is None or c["status"] != "qualified":
                continue
            if size > MAX_HEAD:
                c["status"], c["disqualify_reason"] = "disqualified", f"enterprise ({size} on LinkedIn)"
            elif size < MIN_HEAD:
                c["status"], c["disqualify_reason"] = "disqualified", f"too small ({size} on LinkedIn)"
    else:
        print("[unipile] Unipile env not set — headcount filter uses cache / board data only")

    # LLM-as-a-judge verdicts from judge.py (pitch-deck fit, sells by phone / online)
    judged = json.loads(JUDGE.read_text()) if JUDGE.exists() else {}
    for c in companies:
        j = judged.get(c["company_key"]) or {}
        c.update(llm_verdict=j.get("verdict", ""), llm_fit=j.get("fit", ""), llm_channel=j.get("sales_channel", ""),
                 llm_remote_share=j.get("remote_share", ""), llm_segment=j.get("segment", ""), llm_reason=j.get("reason", ""))
        if j.get("verdict") == "no" and c["status"] == "qualified":
            c["status"], c["disqualify_reason"] = "disqualified", "LLM judge: sales not remote / outside ICP"
        # headcount LinkedIn couldn't verify, estimated by the judge from the website and the ads
        sz = j.get("size") or {}
        c["llm_size"] = sz.get("headcount_band", "") if not (c["li_headcount"] or c["li_headcount_range"]) else ""
        if c["llm_size"] in ("1-9", "201-500", "500+") and sz.get("confidence") != "low" and c["status"] == "qualified":
            c["status"], c["disqualify_reason"] = "disqualified", f"LLM size estimate: {c['llm_size']} employees"

    for c in companies:
        c["headcount_verified"] = c["li_headcount"] != "" or c["li_headcount_range"] != ""
        c["company_score"] = company_score(c)
        f = c["company_score"]
        c["tier"] = "A" if f >= 80 else "B" if f >= 65 else "C" if f >= 50 else "D"
    companies.sort(key=lambda c: (c["status"] != "qualified", -c["company_score"], -c["fit_score"]))
    status_of = {c["company_key"]: c for c in companies}
    vacs.sort(key=lambda v: (-status_of.get(v["company_key"], {}).get("company_score", 0), v["company_key"], -v["role_score"]))
    for v in vacs:
        c = status_of.get(v["company_key"])
        v["company_status"] = c["status"] if c else "no_sales_roles"
        v["company_fit_score"] = c["company_score"] if c else 0

    vac_cols = ["company", "company_status", "company_fit_score", "title", "role_class", "role_score", "signals", "city",
                "workplace", "salary", "seniority", "source", "url", "published", "description"]
    co_cols = ["company", "tier", "company_score", "status", "disqualify_reason", "fit_score", "sales_vacancies", "phone_roles", "hiring_sales_leader",
               "role_classes", "top_vacancy", "top_vacancy_url", "cities", "sources", "website", "board_company_size",
               "headcount_verified", "li_headcount",
               "li_headcount_range", "li_url", "li_industry", "li_website", "li_match",
               "llm_verdict", "llm_fit", "llm_channel", "llm_remote_share", "llm_segment", "llm_reason", "llm_size"]
    for path, rows, cols in [(DATA / "vacancies.csv", [v for v in vacs if v["role_score"] > 0], vac_cols),
                             (DATA / "companies.csv", companies, co_cols)]:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    q = sum(c["status"] == "qualified" for c in companies)
    (DATA / "run_stats.json").write_text(json.dumps({"raw_offers": len(offers), "unique_vacancies": len(vacs),
                                                     "companies": len(companies), "qualified": q}))
    print(f"{len(offers)} offers -> {len(vacs)} unique vacancies -> {len(companies)} companies with sales roles -> {q} qualified")


if __name__ == "__main__":
    main()
