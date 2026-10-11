"""LinkedIn work at a human pace: decision-maker searches, profile matches for people found on websites, and
company headcount lookups, spread over working days on both accounts.

  python linkedin_queue.py            # runs until the queue is empty; safe to stop and restart (state is on disk)
  python linkedin_queue.py --status   # print today's budget and what is left

Why the pacing: on 4 Oct a few hundred back-to-back searches per account got both accounts' search suspended
("unusual search traffic"). The rules here keep each account well under that and make the rhythm irregular:
- at most DAILY_CAP actions per account per day, Mon-Fri 9:00-17:30 Europe/Warsaw, nothing over lunch;
- sessions of 6-12 actions, 50-210 s between actions, 25-70 min between sessions, accounts take turns;
- an account stops for the day after 3 empty searches in a row (that is how the suspension showed up: 200 with
  no results), and for 48 h on 401/403/429 or anything that looks like a checkpoint.
Tasks run in value order: people at tier A+B companies first, then LinkedIn matches for people read off company
websites, then headcount for companies LinkedIn never verified. Env: UNIPILE_DSN, UNIPILE_API_KEY,
UNIPILE_ACCOUNT_ID_NAZARII, UNIPILE_ACCOUNT_ID_HLEB.
"""
import argparse
import csv
import datetime as dt
import json
import os
import random
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import guard
import people
from jbscrap.score import company_key
import store
from jbscrap.unipile import CACHE as LI_CACHE, _pick, _query

DATA = Path(__file__).resolve().parent / "data"
STATE = store.path("linkedin_queue_state.json")
SITE = store.path("site_people.json")
POOL = DATA / "company_pool.json"
QUERIES = store.path("agent_queries.json")
ICP_HITS = store.path("icp_hits.json")
TZ = ZoneInfo("Europe/Warsaw")
DAILY_CAP = 25
RAMP_STEP, RAMP_MAX = 5, 30  # +5 a day per account after each clean working day, up to 30; back to the start on trouble
HOURS = ((9, 0), (12, 30)), ((13, 30), (17, 30))
ACCOUNTS = {"nazarii": "UNIPILE_ACCOUNT_ID_NAZARII", "hleb": "UNIPILE_ACCOUNT_ID_HLEB"}


def nap(seconds):
    """Sleep by the wall clock in short steps. time.sleep stops while the laptop sleeps, so one long sleep
    wakes up as many hours late as the lid was closed."""
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(min(60, max(0, end - time.time())))


def log(msg):
    print(f"[{dt.datetime.now(TZ):%a %H:%M}] {msg}", flush=True)


def load_state():
    s = json.loads(STATE.read_text()) if STATE.exists() else {}
    s.setdefault("accounts", {a: {} for a in ACCOUNTS})
    s.setdefault("done", 0)
    return s


def save_state(s):
    STATE.write_text(json.dumps(s, ensure_ascii=False, indent=1))


PL_HOLIDAYS = {"01-01", "01-06", "05-01", "05-03", "08-15", "11-01", "11-11", "12-24", "12-25", "12-26"}
# the same search term every time is a machine's habit; people type whatever comes to mind
TERM_VARIANTS = {"CEO": ["CEO", "prezes", "właściciel", "CEO"], "sales": ["sales", "sprzedaż", "handlowy", "sales director"]}


EXTEND = {}  # {"date": "YYYY-MM-DD", "until": (h, m)}: one evening of overtime, set by --until


def in_hours(now):
    if EXTEND.get("date") == now.date().isoformat() and (17, 30) <= (now.hour, now.minute) < EXTEND["until"]:
        return True
    if now.weekday() >= 5 or now.strftime("%m-%d") in PL_HOLIDAYS:
        return False
    if now.weekday() == 4 and (now.hour, now.minute) >= (15, 0):
        return False  # Friday afternoon
    t = (now.hour, now.minute)
    return any(a <= t < b for a, b in HOURS)


def next_open(now):
    t = now.replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    while not in_hours(t):
        t += dt.timedelta(minutes=5)
    return t + dt.timedelta(minutes=random.randint(0, 25))  # don't start on the dot every morning


def account_ready(s, name, now):
    a = s["accounts"].setdefault(name, {})
    if a.get("day") != now.date().isoformat():
        week = now.strftime("%G-W%V")
        if a.get("week") != week:
            a.update(week=week, week_used=0)
        meeting = now.replace(hour=random.randint(10, 15), minute=random.randint(0, 59), second=0, microsecond=0)
        # ramp: yesterday ran clean (some work, no stop, no error) -> raise this account's base cap by one step
        if a.get("day") and a.get("used", 0) > 0 and not a.get("stopped_today") and not a.get("had_error"):
            a["base_cap"] = min(RAMP_MAX, a.get("base_cap", DAILY_CAP) + RAMP_STEP)
        base = a.setdefault("base_cap", DAILY_CAP)
        a.update(day=now.date().isoformat(), used=0, empty_streak=0, stopped_today=False, had_error=False,
                 day_cap=max(3, round(base * random.uniform(0.7, 1.3))),
                 day_off=random.random() < 0.1,  # some days a person just doesn't prospect
                 meeting_from=meeting.isoformat(),
                 meeting_to=(meeting + dt.timedelta(minutes=random.randint(60, 120))).isoformat())
    if guard.paused_until(name):  # one pause for everything, lifted by time or by hand (guard.resume)
        return False
    overtime = EXTEND.get("date") == now.date().isoformat()  # asked to work today: no day off
    if (a.get("day_off") and not overtime) or a.get("week_used", 0) >= a.get("base_cap", DAILY_CAP) * 4:
        return False
    if dt.datetime.fromisoformat(a["meeting_from"]) <= now < dt.datetime.fromisoformat(a["meeting_to"]):
        return False
    return not a["stopped_today"] and a["used"] < a.get("day_cap", DAILY_CAP)


class Api:
    def __init__(self):
        dsn = os.environ["UNIPILE_DSN"].strip().rstrip("/")
        self.base = dsn if dsn.startswith("http") else f"https://{dsn}"
        self.s = requests.Session()
        self.s.headers.update({"X-API-KEY": os.environ["UNIPILE_API_KEY"], "accept": "application/json"})

    def call(self, account, method, path, **kw):
        r = self.s.request(method, f"{self.base}{path}", params={"account_id": os.environ[ACCOUNTS[account]]},
                           timeout=60, **kw)
        return r.status_code, (r.json() if r.headers.get("content-type", "").startswith("application/json") else {})


def build_tasks():
    """Queue in value order. Each task is (kind, key, payload); done keys live in the caches, so this is rebuilt
    from scratch on every start and never repeats finished work."""
    li = json.loads(LI_CACHE.read_text()) if LI_CACHE.exists() else {}
    pc = json.loads(people.CACHE.read_text()) if people.CACHE.exists() else {}
    site = json.loads(SITE.read_text()) if SITE.exists() else {}
    cos = [c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8")) if c["status"] == "qualified"]
    cos.sort(key=lambda c: -float(c["company_score"] or 0))
    tasks = []
    for c in cos:  # 1. decision makers at A+B companies with a LinkedIn page
        k = company_key(c["company"])
        cid = ((li.get(k) or {}).get("profile") or {}).get("id")
        if c["tier"] in "AB" and cid and not people.STAFFING_INDUSTRY.search(c["li_industry"]):
            for term in people.TERMS[:2]:
                if term not in (pc.get(k) or {}).get("hits", {}):
                    tasks.append(("people", k, {"company": c["company"], "cid": cid, "term": term}))
    for k, e in site.items():  # 2. LinkedIn profiles for people named on company websites
        for p in e.get("people", []):
            if "li" not in p:
                tasks.append(("match", k, {"company": e["company"], "name": p["name"]}))
    # 3. pool companies (local lists + new job-board finds) the judge called a fit, best first: find the LinkedIn
    #    page and headcount, then the decision makers, skipping anyone LinkedIn shows outside 10-200 people
    pool = json.loads(POOL.read_text()) if POOL.exists() else []
    pool = sorted((c for c in pool if "job_boards" not in c["sources"] and c.get("verdict") == "fit"),
                  key=lambda c: -float(c.get("fit") or 0))
    for c in pool:
        k = c["key"]
        prof = (li.get(k) or {}).get("profile") or {}
        if k not in li:
            tasks.append(("company", k, {"company": c["name"]}))
        elif prof.get("id"):
            n = prof.get("employee_count")
            if n is not None and not (10 <= n <= 200):
                continue
            for term in people.TERMS[:2]:
                if term not in (pc.get(k) or {}).get("hits", {}):
                    tasks.append(("people", k, {"company": c["name"], "cid": prof["id"], "term": term}))
    for c in cos:  # 4. headcount for qualified job-board companies LinkedIn never looked up
        k = company_key(c["company"])
        if c["tier"] in "ABC" and k not in li:
            tasks.append(("company", k, {"company": c["company"]}))
    # 5. searches the research agent wrote when everything above runs dry (see agent.py)
    for q in json.loads(QUERIES.read_text()) if QUERIES.exists() else []:
        if q.get("status") == "pending":
            tasks.append(("icp", q["id"], {"keywords": q["keywords"], "company": ""}))
    return tasks


# a search that has always returned people (CEO at Espeo Software): empty here means LinkedIn is hiding results
CANARY = {"api": "classic", "category": "people", "company": ["377200"], "keywords": "CEO"}

LOCK = threading.RLock()  # caches and state are shared JSON files; both accounts write them
CLAIMED = set()  # tasks one account is working on, so the other doesn't pick the same one
LOCAL = threading.local()


def task_id(task):
    return f"{task[0]}:{task[1]}:{task[2].get('term') or task[2].get('name') or ''}"


def peek(api, account, items):
    """Now and then open the first result, like someone checking who they found. Counts against the budget."""
    if items and random.random() < 0.3:
        time.sleep(random.uniform(15, 50))
        api.call(account, "GET", f"/api/v1/users/{items[0].get('public_identifier') or items[0]['id']}")
        LOCAL.peeks = getattr(LOCAL, "peeks", 0) + 1


def run_task(api, account, task):
    """Returns (ok, empty, http_status). Writes results into the same caches the pipeline reads."""
    kind, k, p = task
    time.sleep(random.uniform(3, 12))  # typing the query
    if kind == "people":
        st, d = api.call(account, "POST", "/api/v1/linkedin/search",
                         json={"api": "classic", "category": "people", "company": [p["cid"]],
                               "keywords": random.choice(TERM_VARIANTS.get(p["term"], [p["term"]]))})
        if st != 200:
            return False, False, st
        items = d.get("items") or []
        with LOCK:
            pc = json.loads(people.CACHE.read_text()) if people.CACHE.exists() else {}
            e = pc.setdefault(k, {"company_id": p["cid"], "hits": {}})
            e["hits"][p["term"]] = [{f: i.get(f) for f in ("id", "name", "headline", "location", "public_profile_url",
                                                         "profile_url", "network_distance")} for i in items]
            people.CACHE.write_text(json.dumps(pc, ensure_ascii=False, indent=1))
        peek(api, account, items)
        return True, not items, st
    if kind == "match":
        st, d = api.call(account, "POST", "/api/v1/linkedin/search",
                         json={"api": "classic", "category": "people", "keywords": f"{p['name']} {_query(p['company'])}"})
        if st != 200:
            return False, False, st
        items = d.get("items") or []
        first = p["name"].split()[0].lower()
        with LOCK:
            site = json.loads(SITE.read_text())
            for person in site[k]["people"]:
                if person["name"] == p["name"]:
                    hit = next((i for i in items if (i.get("name") or "").lower().startswith(first)), None)
                    person["li"] = {f: hit.get(f) for f in ("id", "name", "headline", "public_profile_url", "profile_url",
                                                            "network_distance")} if hit else None
            SITE.write_text(json.dumps(site, ensure_ascii=False, indent=1))
        peek(api, account, items)
        return True, not items, st
    if kind == "company":
        st, d = api.call(account, "POST", "/api/v1/linkedin/search",
                         json={"api": "classic", "category": "companies", "keywords": _query(p["company"])})
        if st != 200:
            return False, False, st
        items = (d.get("items") or [])[:5]
        hit, match = _pick(p["company"], items)
        prof = {}
        if hit:
            time.sleep(random.uniform(20, 60))  # opening the page after reading the results, as a person would
            st, prof = api.call(account, "GET", f"/api/v1/linkedin/company/{hit.get('public_identifier') or hit['id']}")
            if st != 200:
                return False, False, st
        with LOCK:
            li = json.loads(LI_CACHE.read_text()) if LI_CACHE.exists() else {}
            li[k] = {"match": match, "query": _query(p["company"]), "search_hit": hit, "search_items": items, "profile": prof}
            LI_CACHE.write_text(json.dumps(li, ensure_ascii=False, indent=1))
        return True, not items, st
    if kind == "icp":
        st, d = api.call(account, "POST", "/api/v1/linkedin/search",
                         json={"api": "classic", "category": "people", "keywords": p["keywords"]})
        if st != 200:
            return False, False, st
        items = d.get("items") or []
        with LOCK:
            hits = json.loads(ICP_HITS.read_text()) if ICP_HITS.exists() else []
            for i in items:
                hits.append({**{f: i.get(f) for f in ("id", "name", "headline", "location", "public_profile_url",
                                                      "profile_url", "network_distance")},
                             "query": k, "account": account, "screened": False})
            ICP_HITS.write_text(json.dumps(hits, ensure_ascii=False, indent=1))
            qs = json.loads(QUERIES.read_text())
            for q in qs:
                if q["id"] == k:
                    q.update(status="done", results=len(items))
            QUERIES.write_text(json.dumps(qs, ensure_ascii=False, indent=1))
        return True, not items, st
    raise ValueError(kind)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--cap", type=int, default=DAILY_CAP, help="actions per account per day (start low after a block)")
    ap.add_argument("--until", help="work late today until HH:MM (still within today's per-account caps)")
    ap.add_argument("--accounts", default=",".join(ACCOUNTS), help="comma list, e.g. nazarii")
    args = ap.parse_args()
    if args.until:
        h, m = map(int, args.until.split(":"))
        EXTEND.update(date=dt.datetime.now(TZ).date().isoformat(), until=(h, m))
    for name in [n for n in ACCOUNTS if n not in args.accounts.split(",")]:
        del ACCOUNTS[name]
    globals()["DAILY_CAP"] = args.cap
    from judge import load_env
    load_env()  # Unipile keys live in the git-ignored .env
    state = load_state()
    tasks = build_tasks()
    if args.status:
        now = dt.datetime.now(TZ)
        for name in ACCOUNTS:
            account_ready(state, name, now)
        print(json.dumps({"left": len(tasks), "by_kind": {k: sum(t[0] == k for t in tasks) for k in ("people", "match", "company", "icp")},
                          "accounts": state["accounts"]}, indent=1))
        return
    state["cap"] = DAILY_CAP
    save_state(state)
    api = Api()
    log(f"{len(tasks)} tasks queued, cap {DAILY_CAP} actions per account per day, accounts work in parallel")
    threads = [threading.Thread(target=work, args=(api, state, name), name=name) for name in ACCOUNTS]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    log("queue empty")


def work(api, state, account):
    """One account's day: its own sessions and breaks, independent of the other account, like two people."""
    time.sleep(random.uniform(0, 3 * 60) if EXTEND else random.uniform(0, 20 * 60))  # the two don't start at the same minute
    while True:
        now = dt.datetime.now(TZ)
        with LOCK:
            ready = account_ready(state, account, now)
        if not in_hours(now) or not ready:
            wake = next_open(now if not in_hours(now) else (now + dt.timedelta(days=1)).replace(hour=0, minute=0))
            if in_hours(now) and not ready:
                # paused, in a meeting or done for today: look again in a while rather than tomorrow
                wake = min(wake, now + dt.timedelta(minutes=random.randint(20, 45)))
            with LOCK:
                state["accounts"][account]["next_session"] = wake.isoformat()
                state["next_session"] = min(a.get("next_session", wake.isoformat()) for a in state["accounts"].values())
                save_state(state)
            log(f"{account}: sleeping until {wake:%a %H:%M}")
            nap(max(60, (wake - now).total_seconds()))
            continue
        with LOCK:
            tasks = [t for t in build_tasks() if task_id(t) not in CLAIMED]
        if not tasks:
            return
        a = state["accounts"][account]
        size = random.randint(6, 12)
        log(f"session on {account}: up to {size} actions ({a['used']}/{a.get('day_cap', DAILY_CAP)} used today)")
        for _ in range(size):
            with LOCK:
                if not tasks or not in_hours(dt.datetime.now(TZ)) or not account_ready(state, account, dt.datetime.now(TZ)):
                    break
                tasks = [t for t in tasks if task_id(t) not in CLAIMED]
                if not tasks:
                    break
                task = tasks.pop(random.randrange(min(5, len(tasks))))  # not strictly in list order
                CLAIMED.add(task_id(task))
            LOCAL.peeks = 0
            try:
                ok, empty, st = run_task(api, account, task)
            finally:
                with LOCK:
                    CLAIMED.discard(task_id(task))
            with LOCK:
                spent = 1 + (task[0] == "company") + LOCAL.peeks
                a["used"] += spent
                a["week_used"] = a.get("week_used", 0) + spent
                if not ok and st >= 500:
                    break  # Unipile hiccup: end this session, try again after the break
                if not ok and st in (401, 403, 429):
                    a["paused_until"] = (dt.datetime.now(TZ) + dt.timedelta(hours=random.randint(48, 72))).isoformat()
                    a["last_error"] = f"{st} at {dt.datetime.now(TZ):%a %H:%M}"
                    guard.pause(account, f"HTTP {st} на пошуку", source="черга пошуку")
                    a["had_error"], a["base_cap"] = True, DAILY_CAP
                    save_state(state)
                    log(f"{account}: HTTP {st}, pausing this account for 48-72 h")
                    break
                if ok:
                    state["done"] += 1
                    a["empty_streak"] = a["empty_streak"] + 1 if empty else 0
                    log(f"{account}: {task[0]} {task[2].get('company', '')[:40]!r} "
                        f"{task[2].get('term') or task[2].get('name') or ''} -> {'empty' if empty else 'ok'}")
                    if a["empty_streak"] >= 3:
                        # Small companies often have nobody on LinkedIn, so three empties alone prove nothing.
                        # One control search that always had results tells a real block from an unlucky run.
                        time.sleep(random.uniform(40, 90))
                        st_c, d_c = api.call(account, "POST", "/api/v1/linkedin/search", json=CANARY)
                        a["used"] += 1
                        if st_c == 200 and d_c.get("items"):
                            a["empty_streak"] = 0
                            log(f"{account}: 3 empty results, control search fine, carrying on")
                            save_state(state)
                        else:
                            a["stopped_today"] = True
                            a["last_error"] = f"control search empty at {dt.datetime.now(TZ):%a %H:%M}"
                            a["base_cap"] = DAILY_CAP
                            save_state(state)
                            log(f"{account}: 3 empty results and the control search is empty too, likely a search block")
                            guard.pause(account, "пошук повертає порожньо навіть на контрольному запиті (ознака блоку пошуку)",
                                        hours=24, source="черга пошуку")
                            break
                save_state(state)
            time.sleep(random.lognormvariate(4.6, 0.35))  # ~50-210 s, mostly around 100 s
        pause = random.uniform(25, 70) * 60
        with LOCK:
            a["next_session"] = (dt.datetime.now(TZ) + dt.timedelta(seconds=pause)).isoformat()
            state["next_session"] = min(x.get("next_session", a["next_session"]) for x in state["accounts"].values())
            save_state(state)
        log(f"{account}: break for {pause / 60:.0f} min")
        nap(pause)

if __name__ == "__main__":
    sys.exit(main())
