"""Find the website of qualified companies that have none on record, without LinkedIn or a search engine.

  python find_sites.py      # -> data/sites_found.json, read by site_people.py

Candidates come from the job ads first (e-mail and link domains in the ad text, minus job boards and free mail),
then from the model's guesses. A candidate counts only if the page loads and its title or text carries the
company's distinctive name tokens.
"""
import csv
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from jbscrap.score import company_key
from jbscrap.unipile import _tokens
from judge import ask, load_env

DATA = Path(__file__).resolve().parent / "data"
OUT = DATA / "sites_found.json"
SKIP = re.compile(r"pracuj|rocketjobs|justjoin|praca\.pl|aplikuj|nofluff|olx|indeed|linkedin|facebook|instagram|google|"
                  r"gmail|wp\.pl|o2\.pl|onet|interia|outlook|hotmail|yahoo|icloud|gov\.pl|youtube|traffit|erecruiter|"
                  r"teamtailor|workable|recruitee|greenhouse|lever\.co|smartrecruiters|elevato|hrlink|w3\.org|schema\.org")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["domains"],
          "properties": {"domains": {"type": "array", "items": {"type": "string"}, "description": "Up to 3 bare domains"}}}
SYSTEM = """Given a Polish company's name and its job ads, list up to 3 likely domains of its own website
(e.g. "firma.pl"), most likely first. Use the brand in the name and anything in the ads. Bare domains only."""


def domains_in(text):
    found = re.findall(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)|https?://(?:www\.)?([\w-]+(?:\.[\w-]+)+)|\bwww\.([\w-]+(?:\.[\w-]+)+)", text)
    out = []
    for parts in found:
        d = next(p for p in parts if p).lower().strip(".")
        if not SKIP.search(d) and d not in out:
            out.append(d)
    return out


def verify(domain, toks):
    for url in (f"https://{domain}", f"https://www.{domain}", f"http://{domain}"):
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/141 Safari/537.36"})
        except Exception:
            continue
        if r.status_code >= 400:
            continue
        head = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text[:200000])).lower()
        host = re.sub(r"^https?://(www\.)?", "", r.url).split("/")[0].lower()
        if re.search(r"aftermarket|sedo|dan\.com|parking|godaddy|hostinger|domain", host):
            continue  # parked domain for sale
        if not any(t in host.replace("-", "") for t in toks):
            continue  # someone else's site that merely mentions the name
        if toks and sum(t in head for t in toks) >= max(1, len(toks) // 2 + (len(toks) > 2)):
            return r.url
    return None


def main():
    load_env()
    key = os.environ["OPENAI_API_KEY"]
    vacs = {}
    for v in csv.DictReader(open(DATA / "vacancies.csv", encoding="utf-8")):
        vacs.setdefault(company_key(v["company"]), []).append(v)
    cos = [c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8"))
           if c["status"] == "qualified" and c["tier"] in "AB" and not (c["li_website"] or c["website"])]
    found = json.loads(OUT.read_text()) if OUT.exists() else {}
    todo = [c for c in cos if company_key(c["company"]) not in found]
    print(f"[sites] {len(todo)} companies without a website")

    def one(c):
        k = company_key(c["company"])
        toks = sorted(_tokens(c["company"]), key=len, reverse=True)[:3]
        ads = vacs.get(k, [])[:5]
        cands = domains_in(" ".join(v["description"] + " " + v["url"] for v in ads))
        try:
            guess = ask(key, f"Company: {c['company']}\nCity: {c['cities']}\nAds:\n" +
                        "\n".join(f"- {v['title']}: {v['description'][:400]}" for v in ads), SYSTEM, SCHEMA)["domains"]
        except Exception:
            guess = []
        for d in cands + [g.lower().removeprefix("www.").strip("/ ") for g in guess]:
            url = verify(d, toks)
            if url:
                return k, {"company": c["company"], "website": url, "from": "ad" if d in cands else "llm"}
        return k, {"company": c["company"], "website": None}

    with ThreadPoolExecutor(6) as ex:
        for k, v in ex.map(one, todo):
            found[k] = v
    OUT.write_text(json.dumps(found, ensure_ascii=False, indent=1))
    hits = [v for v in found.values() if v["website"]]
    print(f"[sites] found {len(hits)} of {len(found)} (from ads {sum(v['from'] == 'ad' for v in hits)}, guessed {sum(v['from'] == 'llm' for v in hits)})")


if __name__ == "__main__":
    main()
