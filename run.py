"""Scrape PL job boards -> score vacancies for Dealaris fit -> dedupe companies -> (optional) LinkedIn headcount via Unipile.

  python run.py                 # scrape + score, enrich with Unipile if env is set
  python run.py --no-scrape     # re-score / re-enrich from data/raw_offers.json
"""
import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from jbscrap import boards
from jbscrap.score import STAFFING, company_key, company_rollup, fold, is_known_enterprise, score_vacancy
from jbscrap.unipile import Unipile, enrich

DATA = Path(__file__).resolve().parent / "data"

PRACA_KEYWORDS = ["telesprzedaz", "telemarketing", "konsultant telefoniczny", "call center", "sprzedaz telefoniczna",
                  "inside sales", "sdr", "bdr", "business development", "account executive", "sales development",
                  "pozyskiwanie klientow", "doradca klienta", "specjalista ds sprzedazy", "kierownik sprzedazy",
                  "sales manager", "doradca nieruchomosci", "fotowoltaika sprzedaz", "doradca kredytowy"]

MIN_HEAD, MAX_HEAD = 10, 200


def scrape():
    offers = []
    for name, fn in [("rocketjobs", lambda: boards.rocketjobs(("sales",))),
                     ("nofluffjobs", lambda: boards.nofluffjobs(("sales", "customerService"))),
                     ("praca.pl", lambda: boards.praca_pl(PRACA_KEYWORDS))]:
        try:
            got = fn()
        except Exception as e:  # one board failing shouldn't sink the run
            print(f"[{name}] failed: {e}")
            got = []
        print(f"[{name}] {len(got)} offers")
        offers += got
    return offers


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scrape", action="store_true")
    ap.add_argument("--max-lookups", type=int, default=600, help="Unipile company lookups per run")
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)
    raw = DATA / "raw_offers.json"
    if args.no_scrape:
        offers = json.loads(raw.read_text())
    else:
        offers = scrape()
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
        if STAFFING.search(fold(name)):
            status, reason = "disqualified", "staffing agency / hidden client"
        elif is_known_enterprise(name):
            status, reason = "disqualified", "enterprise (known brand, pending LinkedIn check)"
        companies.append(dict(company=name, company_key=key, **roll, status=status, disqualify_reason=reason,
                              sources="|".join(sorted({v["source"] for v in vs})),
                              cities="|".join(sorted({v["city"].split(",")[0] for v in vs if v["city"]}))[:120],
                              top_vacancy=max(vs, key=lambda v: v["role_score"])["title"],
                              top_vacancy_url=max(vs, key=lambda v: v["role_score"])["url"],
                              li_match="", li_name="", li_url="", li_headcount="", li_headcount_range="",
                              li_industry="", li_website=""))

    if Unipile.available():
        todo = sorted([c for c in companies if c["status"] == "qualified"], key=lambda c: -c["fit_score"])
        n = enrich(todo, max_lookups=args.max_lookups)
        print(f"[unipile] {n} new lookups")
        for c in companies:
            hc = c["li_headcount"]
            rng_hi = c["li_headcount_range"].split("-")[-1] if c["li_headcount_range"] else ""
            size = hc if hc != "" else (int(rng_hi) if rng_hi.isdigit() else None)
            if size is None or c["status"] != "qualified":
                continue
            if size > MAX_HEAD:
                c["status"], c["disqualify_reason"] = "disqualified", f"enterprise ({size} on LinkedIn)"
            elif size < MIN_HEAD:
                c["status"], c["disqualify_reason"] = "disqualified", f"too small ({size} on LinkedIn)"
    else:
        print("[unipile] UNIPILE_DSN / UNIPILE_API_KEY / UNIPILE_ACCOUNT_ID not set — headcount filter skipped")

    for c in companies:
        c["headcount_verified"] = c["li_headcount"] != "" or c["li_headcount_range"] != ""
        f = c["fit_score"]
        c["tier"] = "A" if f >= 80 else "B" if f >= 70 else "C" if f >= 60 else "D"
    companies.sort(key=lambda c: (c["status"] != "qualified", -c["fit_score"], -c["sales_vacancies"]))
    status_of = {c["company_key"]: c for c in companies}
    vacs.sort(key=lambda v: (-status_of.get(v["company_key"], {}).get("fit_score", 0), v["company_key"], -v["role_score"]))
    for v in vacs:
        c = status_of.get(v["company_key"])
        v["company_status"] = c["status"] if c else "no_sales_roles"
        v["company_fit_score"] = c["fit_score"] if c else 0

    vac_cols = ["company", "company_status", "company_fit_score", "title", "role_class", "role_score", "signals", "city",
                "workplace", "salary", "seniority", "source", "url", "published", "description"]
    co_cols = ["company", "tier", "status", "disqualify_reason", "fit_score", "sales_vacancies", "phone_roles", "hiring_sales_leader",
               "role_classes", "top_vacancy", "top_vacancy_url", "cities", "sources", "headcount_verified", "li_headcount",
               "li_headcount_range", "li_url", "li_industry", "li_website", "li_match"]
    for path, rows, cols in [(DATA / "vacancies.csv", [v for v in vacs if v["role_score"] > 0], vac_cols),
                             (DATA / "companies.csv", companies, co_cols)]:
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
    q = sum(c["status"] == "qualified" for c in companies)
    print(f"{len(offers)} offers -> {len(vacs)} unique vacancies -> {len(companies)} companies with sales roles -> {q} qualified")


if __name__ == "__main__":
    main()
