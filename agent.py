"""Research agent: keeps the campaign supplied with new, qualified leads.

  python agent.py tick      # one pass (service.py runs it every 6 hours)

Each pass it decides, and writes down why, in data/agent_log.json:
- every 3 days: scrape the job boards again (rocketjobs, nofluffjobs, praca.pl, aplikuj.pl; pracuj.pl only works
  from a home IP), judge the new companies against the pitch deck, estimate headcount, classify the niche, and add
  them to the company pool;
- when the LinkedIn queue is close to running out of companies to search, ask the model for new LinkedIn people
  searches (title + sector, in Polish), steered by which niches have been accepting and replying so far;
- screen the people those searches return (decision maker, company in Poland/Ukraine that sells by phone or online)
  and add the good ones as seed leads;
- refresh the campaign's lead list so new people get an owner, a language and a personal line.
"""
import datetime as dt
import json
import os
import subprocess
import sys
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import store

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG = store.path("agent_log.json")
STATE = store.path("agent_state.json")
QUERIES = store.path("agent_queries.json")
HITS = store.path("icp_hits.json")
SEEDS = store.path("seed_people.json")
PY = sys.executable
SCRAPE_EVERY_DAYS = 3
LOW_WATER = 40  # queue tasks left before the agent writes new LinkedIn searches

QUERY_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["why", "queries"],
                "properties": {"why": {"type": "string", "description": "2 sentences in Ukrainian: what you are aiming for and why"},
                               "queries": {"type": "array", "items": {"type": "string"}}}}
QUERY_SYSTEM = """You plan LinkedIn people searches for Dealaris, an AI copilot for live sales calls sold to SMB sales
teams (10-200 people) that sell mostly by phone or online video: call centres and outsourced sales, debt collection,
financial and insurance brokers, B2B SaaS and IT services with SDR/AE teams, agencies, online education, energy and
telecom resellers. Market: Poland first, Ukraine second.
Write 10 LinkedIn keyword searches that find DECISION MAKERS (owners, CEO/prezes, sales directors, call centre
managers) in those niches. Each search is 2-4 words, one role word plus one niche word, mostly in Polish (e.g.
"prezes call center", "dyrektor sprzedaży windykacja", "właściciel biuro kredytowe", "head of sales SaaS Warszawa").
LinkedIn does not support OR. Lean towards niches that perform best in the stats given, avoid repeating searches
already done, and include 2 searches that explore a new niche."""
SCREEN_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["fit", "role", "company", "segment", "track", "why"],
                 "properties": {"fit": {"type": "boolean"}, "role": {"type": "string"}, "company": {"type": "string"},
                                "segment": {"type": "string"}, "track": {"type": "string", "enum": ["A", "B"]},
                                "why": {"type": "string"}}}
SCREEN_SYSTEM = """Decide whether this LinkedIn person is a good outreach target for Dealaris (AI copilot for live
sales calls; buyers are owners, CEOs and sales leaders of 10-200 person companies in Poland or Ukraine whose teams sell
by phone or online calls). fit=false for employees who are not decision makers, recruiters, consultants selling to
such companies, people at big corporations or outside Poland/Ukraine, and anything unclear. Also fit=false unless the
headline shows they own or lead a commercial company that has its own sales team: heads and founders of
associations, unions, foundations, NGOs, clubs, communities and public bodies are not targets, and neither are
people who sell to consumers on their own (coaches, trainers, mentors, experts, speakers, bloggers, tutors,
psychologists, beauty, fitness and other personal services, shops). track B = phone-heavy
(call centres, collections, finance, telecom, energy, logistics), A = online demos (SaaS, IT, agencies, education).
Use only the headline and location; invent nothing. why: one short sentence."""


def now():
    return dt.datetime.now(dt.timezone.utc)


def load(p, d):
    return store.read_json(p, d)


def save(p, o):
    store.write_json(p, o)


def note(action, detail, why=""):
    log = load(LOG, [])
    log.append({"t": now().isoformat(), "action": action, "detail": detail, "why": why})
    save(LOG, log[-500:])
    print(f"[agent] {action}: {detail}", flush=True)


def sh(*args, timeout=7200):
    r = subprocess.run([PY, *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + r.stderr)[-400:]


def scrape():
    before = len({c["key"] for c in load(DATA / "company_pool.json", [])})
    code, out = sh("run.py", "--sources", "rocketjobs,nofluffjobs,praca,aplikuj", "--merge", "--max-lookups", "0")
    if code:
        return note("Збір job-бордів не вдався", out[-200:])
    for step in (["judge.py"], ["judge.py", "--size"], ["run.py", "--no-scrape", "--max-lookups", "0"], ["niche.py"],
                 ["refresh.py"], ["pool.py", "build"], ["pool.py", "judge"]):
        sh(*step)
    after = len({c["key"] for c in load(DATA / "company_pool.json", [])})
    note("Зібрав job-борди", f"+{after - before} нових компаній у пулі (усього {after})",
         "Минуло 3 дні з попереднього збору, на бордах з'являються нові компанії, що наймають продавців.")


def queue_left():
    code, out = sh("linkedin_queue.py", "--status", timeout=300)
    try:
        return json.loads(out[out.index("{"):])
    except ValueError:
        return {"left": None, "by_kind": {}}


def plan_queries(key, ask):
    import campaigns
    leads = campaigns.job_leads()
    st = campaigns.load_state()["leads"]
    perf = Counter()
    for l in leads:
        s = st.get(l["provider_id"], {})
        perf[(l["niche"], "leads")] += 1
        perf[(l["niche"], "accepted")] += bool(s.get("connected_at"))
        perf[(l["niche"], "replied")] += s.get("stage") == "replied"
    stats = "\n".join(f"{n}: {perf[(n, 'leads')]} leads, {perf[(n, 'accepted')]} accepted, {perf[(n, 'replied')]} replied"
                      for n in sorted({k[0] for k in perf}))
    done = [q["keywords"] for q in load(QUERIES, [])]
    plan = ask(key, f"Campaign stats by niche:\n{stats or 'no data yet'}\n\nSearches already done:\n" + "\n".join(done[-80:]),
               QUERY_SYSTEM, QUERY_SCHEMA)
    qs = load(QUERIES, [])
    new = [{"id": uuid.uuid4().hex[:10], "keywords": q.strip(), "status": "pending", "created": now().isoformat()}
           for q in plan["queries"] if q.strip() and q.strip() not in done][:10]
    save(QUERIES, qs + new)
    note("Склав пошукові запити в LinkedIn", ", ".join(q["keywords"] for q in new), plan["why"])


def screen(key, ask):
    hits = load(HITS, [])
    todo = [h for h in hits if not h.get("screened")]
    if not todo:
        return
    seeds = load(SEEDS, [])
    known = {s["public_identifier"] for s in seeds}

    def one(h):
        try:
            return h, ask(key, f"Name: {h['name']}\nHeadline: {h['headline']}\nLocation: {h.get('location')}", SCREEN_SYSTEM, SCREEN_SCHEMA)
        except Exception:
            return h, None

    added = 0
    with ThreadPoolExecutor(6) as ex:
        for h, v in ex.map(one, todo):
            if v is None:
                continue
            h.update(screened=True, fit=v["fit"], why=v["why"])
            url = (h.get("public_profile_url") or h.get("profile_url") or "").split("?")[0].rstrip("/")
            pub = url.rsplit("/", 1)[-1] if url else ""
            if v["fit"] and pub and pub not in known and h.get("name") and not h["name"].lower().startswith("linkedin member"):
                deg = {"DISTANCE_1": "1st", "DISTANCE_2": "2nd", "DISTANCE_3": "3rd"}.get(h.get("network_distance"), "out of network")
                seeds.append({"name": h["name"], "headline": h["headline"], "url": url, "public_identifier": pub,
                              "segment": v["segment"], "band": "A", "score": 80.0, "why": v["why"], "track": v["track"],
                              "degree": {h["account"]: deg}, "lang_hint": "", "source": "icp"})
                known.add(pub)
                added += 1
    save(HITS, hits)
    save(SEEDS, seeds)
    note("Перевірив знайдених людей", f"перевірено {len(todo)}, додано в ліди {added}",
         "Беру лише власників і керівників продажів у компаніях, що продають телефоном чи онлайн.")


def refresh_campaign(key):
    if not key:
        return
    import campaign
    with campaign.LOCK:
        n_before = len(campaign.campaigns.job_leads())
        campaign.prepare()
        n_after = len(campaign.campaigns.job_leads())
    if n_after != n_before:
        note("Додав лідів у кампанію", f"+{n_after - n_before} (усього {n_after})")


def tick():
    sys.path.insert(0, str(ROOT))
    from judge import ask, load_env
    load_env()
    key = os.environ.get("OPENAI_API_KEY")
    state = load(STATE, {})
    refresh_campaign(key)  # new people into the campaign first: the long scrape below shouldn't hold them back
    last = state.get("last_scrape")
    if not last or now() - dt.datetime.fromisoformat(last) > dt.timedelta(days=SCRAPE_EVERY_DAYS):
        state["last_scrape"] = now().isoformat()
        save(STATE, state)
        scrape()
    q = queue_left()
    state["queue"] = q
    save(STATE, state)
    if key:
        pending = sum(1 for x in load(QUERIES, []) if x["status"] == "pending")
        searchable = (q.get("by_kind") or {}).get("people", 0) + (q.get("by_kind") or {}).get("company", 0)
        if q.get("left") is not None and searchable < LOW_WATER and pending < 5:
            plan_queries(key, ask)
        screen(key, ask)
        refresh_campaign(key)
    state["last_tick"] = now().isoformat()
    save(STATE, state)


if __name__ == "__main__":
    tick()
