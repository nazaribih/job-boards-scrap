"""Export decision makers of qualified tier A+B companies for the outreach helper (dealaris-outreach-helper).

  python export_outreach.py        # -> data/outreach_export.json

The helper imports it with scripts/import-job-board-leads.ts as `hiring_sales` leads, held out of digests until
released. Only people whose headline names the company or names no employer at all are exported (people.py drops
the rest); provider ids and network distance come from data/people_cache.json.
"""
import csv
import json
from pathlib import Path

import store
from jbscrap.score import company_key

DATA = Path(__file__).resolve().parent / "data"


def main():
    cache = json.loads(store.path("people_cache.json").read_text())
    companies = {c["company"]: c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8"))}
    li = json.loads((DATA / "unipile_cache.json").read_text())
    out = []
    for r in csv.DictReader(store.path("decision_makers.csv").open(encoding="utf-8")):
        c = companies.get(r["company"])
        if not r["name"] or not c or c["status"] != "qualified" or c["tier"] not in "AB":
            continue
        k = company_key(r["company"])
        hit = next((i for t in cache.get(k, {}).get("hits", {}).values() for i in t
                    if (i.get("public_profile_url") or i.get("profile_url") or "").split("?")[0] == r["person_linkedin"]), None)
        if not hit:
            continue
        out.append({
            "provider_id": hit["id"], "name": hit["name"], "headline": hit.get("headline"), "location": hit.get("location"),
            "public_profile_url": hit.get("public_profile_url"), "profile_url": hit.get("profile_url"),
            "network_distance": hit.get("network_distance"), "role": r["role"], "company": r["company"],
            "company_linkedin_id": ((li.get(k) or {}).get("profile") or {}).get("id"), "company_linkedin_url": c["li_url"],
            "li_headcount": int(c["li_headcount"]) if c["li_headcount"].isdigit() else None,
            "company_score": float(c["company_score"]), "tier": c["tier"], "llm_verdict": c["llm_verdict"],
            "llm_channel": c["llm_channel"], "llm_segment": c["llm_segment"], "llm_reason": c["llm_reason"],
            "top_vacancy": c["top_vacancy"], "top_vacancy_url": c["top_vacancy_url"],
            "mentions_company": r["mentions_company"] == "True",
        })
    store.path("outreach_export.json").write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"{len(out)} people from {len({p['company'] for p in out})} companies -> data/outreach_export.json")


if __name__ == "__main__":
    main()
