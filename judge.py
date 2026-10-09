"""LLM-as-a-judge: does this company fit the Dealaris pitch deck, and does it sell mostly by phone / online?

  python judge.py [--tiers ABCD] [--workers 8]

Reads data/companies.csv (qualified rows), the vacancies and the LinkedIn profile from data/unipile_cache.json,
asks OpenAI for a structured verdict and caches it in data/judge_cache.json (keyed by company + prompt version).
run.py applies the cached verdicts: "no" disqualifies, the rest add llm_* columns. Env: OPENAI_API_KEY (from .env).
"""
import argparse
import csv
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from jbscrap.score import company_key

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CACHE = DATA / "judge_cache.json"
MODEL = "gpt-5-mini"
VERSION = "v3"

PITCH = """Dealaris (pitch deck, Oct 2026): an AI copilot that sits on every live sales call. It listens to the call and
shows the rep short suggestions on screen while the client is talking (objection handling built on the company's own
script), researches the client before the call and fills the CRM after it. Desktop app for macOS/Windows, works with
any softphone and with online meetings, 35+ languages incl. Polish. $75 per seat per month, no seat minimum, built for
SMB and mid-market sales teams (roughly 10-200 employees). Who buys:
- phone sales teams: SDR/BDR teams, call centres, telesales, outsourced sales / lead-gen agencies;
- high-ticket B2C sold over the phone or video: financial, credit and insurance advice, online courses, edtech and
  info-products, energy contracts and solar / heat pumps when leads are worked by phone, subscriptions and telecom;
- sales-led B2B SaaS and B2B services sold through calls and online meetings: software and IT services, marketing
  and advertising agencies, media and publishing (ad sales, subscriptions), training and events, consulting, B2B
  telecom and energy, logistics and freight brokerage, B2B e-commerce and wholesale with an inside-sales desk.
Not a fit: companies whose selling happens mostly face to face: car dealerships and car showrooms (always "no",
even when ads mention calling leads), retail shop floor, showroom walk-ins, field reps visiting clients, door to
door, trade counters, pharmacy/medical reps, customer service desks that don't sell,
manufacturers or distributors whose 'sales' is order taking, and large enterprises."""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["reason", "sales_channel", "remote_share", "segment", "fit", "verdict"],
    "properties": {
        "reason": {"type": "string", "description": "1-2 sentences IN UKRAINIAN (not Polish), concrete to this company: "
                                                    "what it sells, to whom, and how its reps sell"},
        "sales_channel": {"type": "string", "enum": ["phone", "online", "phone_and_online", "mixed", "in_person"],
                          "description": "Where this company's sales conversations mostly happen"},
        "remote_share": {"type": "integer", "minimum": 0, "maximum": 100,
                         "description": "Estimated % of sales conversations held by phone or video call"},
        "segment": {"type": "string", "enum": ["phone_sales", "high_ticket_b2c", "b2b_saas_services", "other"]},
        "fit": {"type": "integer", "minimum": 0, "maximum": 10,
                "description": "How good a Dealaris customer: 10 = ideal (remote sales team in a target segment), 0 = no fit"},
        "verdict": {"type": "string", "enum": ["fit", "maybe", "no"], "description": "fit if fit>=7, no if fit<=3, else maybe"},
    },
}

SYSTEM = f"""You qualify sales prospects for Dealaris. {PITCH}

Judge the company from its job ads and LinkedIn profile. The deciding question: do its salespeople mostly sell
remotely, by phone or online video calls? This usually follows from the niche: telesales, call centres, SDR/BDR,
lead generation, online courses, SaaS and B2B services are remote; real estate and solar installers mix site visits
with phone work (mixed, or phone_and_online when the ads stress calling leads); car dealerships, retail, field sales,
merchandising and door-to-door are in_person. Use your general knowledge of the niche and, if you know it, of the
company itself to fill gaps in the ads; say so in the reason when you do.
verdict "fit": remote_share >= 60 and a segment Dealaris targets. "no": sales mostly in person, no real sales team,
or clearly outside the ICP. Otherwise "maybe". Don't invent specific facts about the company you don't know.
Staffing, temp-work and recruitment agencies (agencja pracy, leasing pracowniczy, rekrutacja) post call-centre and
sales roles for their clients: those ads say nothing about the agency's own sales. Judge only the agency's own
selling (usually account managers visiting or calling employers): normally "maybe" at best, "no" when it is a pure
temp-work agency. Outsourced call centres and lead-gen agencies that run the calls themselves are a fit.
Write the reason in Ukrainian even though the evidence is in Polish."""

SIZE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["reason", "headcount_band", "confidence"],
    "properties": {
        "reason": {"type": "string", "description": "1 sentence in Ukrainian: what points to this size"},
        "headcount_band": {"type": "string", "enum": ["1-9", "10-50", "51-200", "201-500", "500+", "unknown"]},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
}
SIZE_SYSTEM = """Estimate how many employees a Polish company has, for a sales-prospecting filter (target 10-200).
Use the website text, the job ads (number of openings, cities, 'zespół 50+ osób', offices), the legal form and your
general knowledge of the company if you know it. Sole proprietorships (imię i nazwisko in the name) are usually 1-9
unless the ads show a team. Say "unknown" with low confidence rather than guessing when there is nothing to go on."""

lock = threading.Lock()


def site_text(url):
    if not url:
        return ""
    url = url if url.startswith("http") else "https://" + url
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", t))[:3000]
    except Exception:
        return ""


def evidence(c, vacs, li):
    prof = (li.get(c["company_key"]) or {}).get("profile") or {}
    ads = sorted(vacs.get(c["company_key"], []), key=lambda v: -float(v["role_score"] or 0))[:6]
    lines = [f"Company: {c['company']}", f"Cities: {c['cities']}", f"Website: {c['li_website'] or c['website']}",
             f"LinkedIn industry: {c['li_industry']}", f"LinkedIn headcount: {c['li_headcount'] or c['li_headcount_range']}",
             f"LinkedIn tagline: {prof.get('tagline') or ''}",
             f"LinkedIn description: {(prof.get('description') or '')[:1200]}", f"Open sales-type roles ({c['sales_vacancies']}):"]
    for v in ads:
        lines.append(f"- {v['title']} | {v['workplace']} | {v['city']} | {(v['description'] or '')[:500]}")
    return "\n".join(lines)


def ask(key, text, system=SYSTEM, schema=SCHEMA):
    body = {"model": MODEL, "reasoning_effort": "low",
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "verdict", "strict": True, "schema": schema}}}
    for attempt in range(5):
        r = requests.post("https://api.openai.com/v1/chat/completions", json=body, timeout=120,
                          headers={"Authorization": f"Bearer {key}"})
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * 2 ** attempt)
            continue
        r.raise_for_status()
        return json.loads(r.json()["choices"][0]["message"]["content"])
    r.raise_for_status()


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() and v and not os.environ.get(k.strip()):
                os.environ[k.strip()] = v.strip().strip('"')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tiers", default="ABCD")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all", action="store_true", help="judge every company, not only qualified ones")
    ap.add_argument("--size", action="store_true", help="estimate headcount for qualified companies LinkedIn didn't verify")
    args = ap.parse_args()
    load_env()
    key = os.environ["OPENAI_API_KEY"]

    cos = [c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8"))
           if args.all or (c["status"] == "qualified" and c["tier"] in args.tiers)]
    for c in cos:
        c["company_key"] = company_key(c["company"])
    vacs = {}
    for v in csv.DictReader(open(DATA / "vacancies.csv", encoding="utf-8")):
        vacs.setdefault(company_key(v["company"]), []).append(v)
    li = json.loads((DATA / "unipile_cache.json").read_text()) if (DATA / "unipile_cache.json").exists() else {}
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    if args.size:
        todo = [c for c in cos if c["headcount_verified"] != "True" and "size" not in cache.get(c["company_key"], {})]
    else:
        todo = [c for c in cos if cache.get(c["company_key"], {}).get("version") != VERSION]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[judge] {len(cos)} qualified, {len(todo)} to judge with {MODEL}")
    done = [0]

    def one(c):
        try:
            if args.size:
                text = evidence(c, vacs, li) + "\nCompany name / legal form: " + c["company"] + \
                    "\nWebsite text: " + site_text(c["li_website"] or c["website"])
                v = ask(key, text, SIZE_SYSTEM, SIZE_SCHEMA)
            else:
                v = ask(key, evidence(c, vacs, li))
        except Exception as e:
            print(f"[judge] {c['company']!r}: {str(e)[:150]}")
            return
        with lock:
            if args.size:
                cache.setdefault(c["company_key"], {})["size"] = v
            else:
                cache[c["company_key"]] = dict(v, version=VERSION, model=MODEL, **({"size": cache[c["company_key"]]["size"]}
                                                                                  if "size" in cache.get(c["company_key"], {}) else {}))
            done[0] += 1
            if done[0] % 25 == 0 or done[0] == len(todo):
                CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
                print(f"[judge] {done[0]}/{len(todo)}")

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(one, todo))
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
