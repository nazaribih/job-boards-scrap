"""Rebuild every output from the caches without touching LinkedIn: scores, decision makers, outreach export, report,
and data/pipeline_status.json for the live status artifact.

  python refresh.py
"""
import collections
import csv
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import people

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
PY = sys.executable


def sh(*args):
    subprocess.run([PY, *args], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


def main():
    sh("run.py", "--no-scrape", "--max-lookups", "0")
    cos = [c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8"))]
    q = [c for c in cos if c["status"] == "qualified"]
    ab = [c for c in q if c["tier"] in "AB"]
    skipped = [c for c in ab if people.STAFFING_INDUSTRY.search(c["li_industry"])]
    cache = json.loads(people.CACHE.read_text()) if people.CACHE.exists() else {}
    people.write_csv([c for c in ab if c not in skipped], skipped, cache, 3)
    sh("export_outreach.py")
    sh("report/build.py")

    dm = [r for r in csv.DictReader(open(DATA / "decision_makers.csv", encoding="utf-8")) if r["name"]]
    site = json.loads(people.SITE.read_text()) if people.SITE.exists() else {}
    state = json.loads((DATA / "linkedin_queue_state.json").read_text()) if (DATA / "linkedin_queue_state.json").exists() else {}
    left = subprocess.run([PY, "linkedin_queue.py", "--status"], cwd=ROOT, capture_output=True, text=True).stdout
    left = json.loads(left) if left.strip().startswith("{") else {}
    today = dt.date.today().isoformat()
    status = {
        "updated": dt.datetime.now(dt.timezone.utc).isoformat(),
        "companies": {"all": len(cos), "qualified": len(q), "tiers": dict(collections.Counter(c["tier"] for c in q)),
                      "verified_headcount": sum(c["headcount_verified"] == "True" for c in q)},
        "dm": {"ab_companies": len(ab), "with_people": len({r["company"] for r in dm}), "people": len(dm),
               "from_linkedin": sum(r["source"] == "linkedin" for r in dm), "from_website": sum(r["source"] == "website" for r in dm),
               "with_linkedin_url": sum(bool(r["person_linkedin"]) for r in dm),
               "roles": dict(collections.Counter(r["role"] for r in dm))},
        "website": {"read": len(site), "with_people": sum(bool(v["people"]) for v in site.values()),
                    "with_nip": sum(bool(v.get("nip")) for v in site.values())},
        "queue": {"left": left.get("left"), "by_kind": left.get("by_kind", {}), "next_session": state.get("next_session"),
                  "accounts": {name: {"used_today": a.get("used", 0) if a.get("day") == today else 0, "cap": a.get("day_cap", state.get("cap", 25)),
                                      "paused_until": a.get("paused_until"), "stopped_today": a.get("day") == today and a.get("stopped_today"),
                                      "last_error": a.get("last_error")}
                               for name, a in state.get("accounts", {}).items()}},
        "top": sorted(({"company": r["company"], "name": r["name"], "role": r["role"], "score": float(r["lead_score"] or 0),
                        "url": r["person_linkedin"], "source": r["source"]} for r in dm), key=lambda x: -x["score"])[:12],
    }
    (DATA / "pipeline_status.json").write_text(json.dumps(status, ensure_ascii=False))
    print(json.dumps({k: status[k] for k in ("companies", "dm", "website")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
