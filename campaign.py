"""LinkedIn outreach campaign for the job-board decision makers, run by both founders' accounts through Unipile.

  python campaign.py prepare     # build data/campaign_leads.json + data/sequences.json (needs OPENAI_API_KEY)
  python campaign.py run         # the sender loop (service.py starts it when CAMPAIGN_ENABLED=true)
  python campaign.py status      # counts by stage and account

Per lead, one owner (Nazarii or Hleb) does everything: views the profile, sends a connection request without a
note a day or more later, and after acceptance sends four messages (day 0, 3, 6, 9). Any reply, any earlier
conversation with either account, or an invite already pending stops automation for that person for good.

Owner: a 1st-degree connection of one founder goes to that founder (no invite needed); Ukrainian-language leads
go to Nazarii; the rest alternate in lead-score order so both get an even share of the best leads.

Pacing per account: Mon-Fri 9:00-17:30 Europe/Warsaw, 3-9 min between actions, up to 15 profile views,
invites starting at 12 a day (+2 per clean day, max 20, max 90 a week) and 25 messages a day; no more than one new
invite into the same company per week. 401/403/429 or a restriction pauses the account for 48-72 h.
"""
import csv
import datetime as dt
import json
import os
import random
import re
import sys
import threading
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import guard

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LEADS = DATA / "campaign_leads.json"
SEQ = DATA / "sequences.json"
VOC = DATA / "voc_cache.json"  # Ukrainian leads: first name in Cyrillic, vocative case
STATE = DATA / "campaign_state.json"
LOG = DATA / "campaign.log"
TZ = ZoneInfo("Europe/Warsaw")
ACCOUNTS = {"nazarii": "UNIPILE_ACCOUNT_ID_NAZARII", "hleb": "UNIPILE_ACCOUNT_ID_HLEB"}
STAGES = ["m1", "f1", "f2", "f3"]
GAP_DAYS = {"f1": 3, "f2": 3, "f3": 3}
CAPS = {"view": 15, "message": 25}
INVITE_START, INVITE_STEP, INVITE_MAX, INVITE_WEEK = 12, 2, 20, 90
TRACK_B = {"Call-центр / BPO / аутсорс продажів", "Windykacja / стягнення боргів", "Фінанси / кредити / страхування",
           "Телеком", "Логістика / транспорт", "Енергетика / ОЗЕ", "Нерухомість"}
NICHE_LINE = {"B2B SaaS": "saas", "IT-послуги / software house": "it", "Маркетинг / реклама / lead-gen": "agency",
              "Медіа / видавництва / івенти": "media", "E-commerce / B2B-торгівля": "ecom",
              "Call-центр / BPO / аутсорс продажів": "callcenter", "Windykacja / стягнення боргів": "collections",
              "Фінанси / кредити / страхування": "finance", "Логістика / транспорт": "logistics",
              "Освіта / онлайн-курси": "education", "Телеком": "callcenter", "Енергетика / ОЗЕ": "callcenter",
              "Нерухомість": "finance", "Юридичні / консалтинг / бізнес-послуги": "agency", "HR / рекрутинг": "agency",
              "Медицина / здоров'я": "saas", "Інше": "saas"}
PL_HOLIDAYS = {"01-01", "01-06", "05-01", "05-03", "08-15", "11-01", "11-11", "12-24", "12-25", "12-26"}
LOCK = threading.RLock()


def now():
    return dt.datetime.now(TZ)


def log(msg):
    line = f"[{now():%m-%d %H:%M}] {msg}"
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(path)


# ─── prepare: leads, owners, personal lines, sequences ──────────────────────────────────────────────────────────

LINE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["line", "company"],
               "properties": {"line": {"type": "string"}, "company": {"type": "string"}}}
LINE_SYSTEM = """You fill one sentence of a short LinkedIn message. Given a template, the company's legal name and the
job title it is hiring for, return the sentence with {company} replaced by the short name people use (no legal form
like sp. z o.o.) and {role} replaced by the job title in the grammatically correct form for the sentence's
language (e.g. Polish genitive after "szuka": "Specjalisty ds. sprzedaży"; English titles stay as they are; drop
"(k/m)" and similar). Keep the template's wording otherwise. No dashes as punctuation. Also return the short
company name on its own."""


def build_seq():
    """Templates from data/seq_export (shipped with the image; service.py refreshes them on every deploy)."""
    seq = {"messages": {}, "lines": {}}
    for f in (DATA / "seq_export" / "messages").glob("*.json"):
        d = json.loads(f.read_text())
        seq["messages"][f.stem] = d.get("text") or d.get("data", {}).get("text", "")
    for f in (DATA / "seq_export" / "lines").glob("*.json"):
        d = json.loads(f.read_text())
        seq["lines"][f.stem] = d.get("data", d)
    save(SEQ, seq)
    return seq


VOC_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ukrainian", "name"],
              "properties": {"ukrainian": {"type": "boolean"}, "name": {"type": "string"}}}
VOC_SYSTEM = """A formal Ukrainian business message opens with "Добрий день, <name>!". Given a person's full name as
written on LinkedIn (any script), decide whether the person is Ukrainian (a Ukrainian or other East Slavic first name
and surname). ukrainian=false for Polish and any other names (Marek Kowalski, Anna Nowak, John Smith): those keep
their name as written, so return name "". If ukrainian=true, return only their first name in Ukrainian Cyrillic, in
the vocative case (кличний відмінок), using the standard Ukrainian form of the same name. Examples: Oleksandr,
Aleksandr, Александр -> Олександре; Olena, Елена -> Олено; Dmytro, Дмитрий -> Дмитре; Iryna -> Ірино; Serhii,
Sergey -> Сергію; Andrii -> Андрію; Yuliia -> Юліє; Kateryna -> Катерино. Return name "" if the first word is not a
person's first name (initials, a company, an emoji)."""


def vocative(name):
    """First name for a Ukrainian message: "Oleksandr Petrenko" -> "Олександре", "" for non-Ukrainians (their name
    stays as written), None if it can't be worked out right now."""
    cache = load(VOC, {})
    if name not in cache:
        sys.path.insert(0, str(ROOT))
        from judge import ask, load_env
        load_env()
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            return None
        try:
            got = ask(key, f"Full name: {name}", VOC_SYSTEM, VOC_SCHEMA)
        except Exception as e:
            log(f"vocative for {name!r}: {str(e)[:100]}")
            return None
        with LOCK:
            cache = load(VOC, {})
            cache[name] = got
            save(VOC, cache)
    return cache[name]["name"].strip() if cache[name].get("ukrainian") else ""


def prepare():
    sys.path.insert(0, str(ROOT))
    from jbscrap.score import company_key
    from judge import ask, load_env
    load_env()
    key = os.environ.get("OPENAI_API_KEY")

    seq = build_seq()

    pc = load(DATA / "people_cache.json", {})
    site = load(DATA / "site_people.json", {})
    langs = load(DATA / "lang_cache.json", {})
    niches = load(DATA / "niche_cache.json", {})
    plines = load(DATA / "pline_cache.json", {})
    cos = {c["company"]: c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8"))}
    top_vac = {}
    for v in sorted(csv.DictReader(open(DATA / "vacancies.csv", encoding="utf-8")), key=lambda v: -float(v["role_score"] or 0)):
        top_vac.setdefault(v["company"], v["title"])

    def hit_for(r):
        k = company_key(r["company"])
        url = r["person_linkedin"]
        for t in (pc.get(k) or {}).get("hits", {}).values():
            for i in t:
                if (i.get("public_profile_url") or i.get("profile_url") or "").split("?")[0] == url:
                    return i
        for p in (site.get(k) or {}).get("people", []):
            li = p.get("li") or {}
            if li and (li.get("public_profile_url") or li.get("profile_url") or "").split("?")[0] == url:
                return li
        return None

    old = {l["provider_id"]: l for l in load(LEADS, [])}
    exclude = set(load(DATA / "campaign_exclude.json", []))  # provider ids or LinkedIn URLs taken out by hand
    # people the outreach-helper is already working (queued, invited, messaged...): one sender per person
    for pub, pid, _ in load(DATA / "exclude_outreach_helper.json", []):
        exclude.update(x for x in (pid, pub and f"https://www.linkedin.com/in/{pub}") if x)
    leads = []
    for r in csv.DictReader(open(DATA / "decision_makers.csv", encoding="utf-8")):
        c = cos.get(r["company"])
        if not r["name"] or not r["person_linkedin"] or not c or c["status"] != "qualified" or c["tier"] not in "AB":
            continue
        h = hit_for(r)
        if r["person_linkedin"].rstrip("/") in exclude or r["person_linkedin"] in exclude or (h and h.get("id") in exclude):
            continue
        lang = (langs.get(r["name"]) or {}).get("lang")
        if not h or not h.get("id") or not lang:
            continue
        if lang == "en" and r["mentions_company"] != "True":
            continue  # foreign HQ staff or a namesake company: nobody we can tell works at the Polish team
        niche = (niches.get(company_key(r["company"])) or {}).get("niche", "Інше")
        track = "B" if niche in TRACK_B else "A"
        line_id = NICHE_LINE.get(niche, "saas")
        template = (seq["lines"].get(line_id) or {}).get(lang) or (seq["lines"].get(line_id) or {}).get("en", "")
        role = top_vac.get(r["company"], "")
        ck = f"{line_id}|{lang}|{r['company']}|{role}|{template}"
        if ck not in plines and key and template:
            try:
                plines[ck] = ask(key, f"Template: {template}\nCompany legal name: {r['company']}\nJob title: {role}\nLanguage: {lang}",
                                 LINE_SYSTEM, LINE_SCHEMA)
            except Exception as e:
                print(f"[prepare] line for {r['company']!r}: {str(e)[:100]}")
        pl = plines.get(ck)
        if not pl:
            continue
        first = r["name"].split()[0].strip(",.")
        prev = old.get(h["id"], {})
        leads.append({"provider_id": h["id"], "name": r["name"], "first_name": first.capitalize() if first.isupper() else first,
                      "public_identifier": r["person_linkedin"].rstrip("/").rsplit("/", 1)[-1], "url": r["person_linkedin"],
                      "role": r["role"], "headline": r["headline"], "company": r["company"], "company_short": pl["company"],
                      "company_key": company_key(r["company"]), "tier": r["tier"], "score": float(r["lead_score"] or 0),
                      "niche": niche, "track": track, "lang": lang, "personal_line": pl["line"],
                      "distance": h.get("network_distance"), "owner": prev.get("owner")})
    leads += extra_leads({l["provider_id"] for l in leads} | {l["public_identifier"] for l in leads}, exclude, plines, key, old)
    save(DATA / "pline_cache.json", plines)

    # owners: keep earlier assignments; Ukrainian-language leads to Nazarii; the rest balanced in score order
    leads.sort(key=lambda l: -l["score"])
    load_n = {a: sum(l["owner"] == a for l in leads) for a in ACCOUNTS}
    for l in leads:
        if l["owner"]:
            continue
        deg = l.get("degree") or {}
        rank = {a: DEGREE_RANK.get((deg.get(a) or "").strip().lower(), 9) for a in ACCOUNTS}
        if min(rank.values()) < 9 and len(set(rank.values())) > 1:
            l["owner"] = min(rank, key=rank.get)  # whoever is closer in the network
        else:
            l["owner"] = "nazarii" if l["lang"] == "uk" else min(ACCOUNTS, key=lambda a: (load_n[a], random.random()))
        load_n[l["owner"]] += 1
    save(LEADS, leads)
    print(f"[prepare] {len(leads)} leads, owners {load_n}, tracks "
          f"{ {t: sum(l['track'] == t for l in leads) for t in 'AB'} }, langs { {x: sum(l['lang'] == x for l in leads) for x in ('pl', 'en', 'uk')} }")


DEGREE_RANK = {"1st": 1, "2nd": 2, "3rd": 3, "out of network": 4}
HEAD_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["line", "company"],
               "properties": {"line": {"type": "string"}, "company": {"type": "string"}}}
HEAD_SYSTEM = """Write the one personal sentence that opens a short LinkedIn message to a decision maker, in the
given language (pl: informal "ty" form; uk: formal, address the person as "Ви" with a capital letter; en: plain). Use ONLY facts stated in their headline: their
role and the company or field it names. Do not say how their team sells, how big it is, or anything else the headline
does not state. Max 18 words, observational, no flattery, no questions, no dashes as punctuation. Examples:
pl "Widzę, że prowadzisz sprzedaż w Firmie X." uk "Бачу, що Ви керуєте відділом продажів у X." en "I saw you lead
sales at X." Also return the company's short name as written in the headline (empty if none)."""
def head_key(lang):
    return "head3" if lang == "uk" else "head2"  # head3: Ukrainian lines rewritten with the formal "Ви"


SEGMENT_TRACK = {"phone_sales": "B", "high_ticket_b2c": "B", "b2b_saas_services": "A", "other": "A"}


def extra_leads(have, exclude, plines, key, old):
    """Seed people from the earlier LinkedIn scan and decision makers found at pool companies."""
    sys.path.insert(0, str(ROOT))
    import people
    from jbscrap.unipile import _tokens
    from judge import ask
    from lang import SCHEMA as LANG_SCHEMA, SYSTEM as LANG_SYSTEM
    langs = load(DATA / "lang_cache.json", {})
    out = []

    def lang_of(name, headline, company, hint):
        if hint == "uk":
            return "uk"
        if name not in langs and key:
            try:
                langs[name] = ask(key, f"Name: {name}\nHeadline: {headline}\nCompany: {company}", LANG_SYSTEM, LANG_SCHEMA)
            except Exception:
                return None
        return (langs.get(name) or {}).get("lang")

    def make(pid, url, name, headline, role, company, ckey, track, niche, score, lang, degree, src):
        pub = url.rstrip("/").rsplit("/", 1)[-1]
        if pid in have or pub in have or url in exclude or pid in exclude or f"https://www.linkedin.com/in/{pub}" in exclude:
            return
        ck = f"{head_key(lang)}|{lang}|{name}|{headline}"
        if ck not in plines and key:
            try:
                plines[ck] = ask(key, f"Language: {lang}\nHeadline: {headline}\nCompany: {company}\nSegment: {niche}", HEAD_SYSTEM, HEAD_SCHEMA)
            except Exception:
                return
        pl = plines.get(ck)
        if not pl:
            return
        first = name.split()[0].strip(",.")
        have.update({pid, pub})
        out.append({"provider_id": pid, "name": name, "first_name": first.capitalize() if first.isupper() else first,
                    "public_identifier": pub, "url": url, "role": role, "headline": headline, "company": company or pl["company"],
                    "company_short": pl["company"] or company, "company_key": ckey or pub, "tier": "", "score": score,
                    "niche": niche, "track": track, "lang": lang, "personal_line": pl["line"], "distance": None,
                    "degree": degree, "source": src, "owner": (old.get(pid) or {}).get("owner")})

    seeds = [sd for sd in load(DATA / "seed_people.json", [])
             if sd["name"] and not sd["name"].lower().startswith(("linkedin member", "участник", "członek linkedin"))]
    if key:  # language and personal line for many people at once; make() then reads them from the caches
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(8) as ex:
            langs_found = list(ex.map(lambda sd: lang_of(sd["name"], sd["headline"], "", sd["lang_hint"]), seeds))

        def line(args):
            sd, lang = args
            ck = f"{head_key(lang)}|{lang}|{sd['name']}|{sd['headline']}"
            if lang and ck not in plines:
                try:
                    plines[ck] = ask(key, f"Language: {lang}\nHeadline: {sd['headline']}\nCompany: \nSegment: {sd['segment']}", HEAD_SYSTEM, HEAD_SCHEMA)
                except Exception:
                    pass
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(line, zip(seeds, langs_found)))
    for sd in seeds:
        lang = lang_of(sd["name"], sd["headline"], "", sd["lang_hint"])
        if lang:
            make(f"url:{sd['public_identifier']}", sd["url"], sd["name"], sd["headline"], "", "", "", sd["track"],
                 sd["segment"], sd["score"], lang, sd["degree"], "seed")

    pc = load(DATA / "people_cache.json", {})
    for c in load(DATA / "company_pool.json", []):
        if "job_boards" in c["sources"] or c.get("verdict") not in ("fit", "maybe") or c["key"] not in pc:
            continue
        toks = _tokens(c["name"])
        picked = {}
        for i in (h for t in pc[c["key"]]["hits"].values() for h in t):
            role = people.role_of(i.get("headline") or "")
            head = i.get("headline") or ""
            if not role or not i.get("name") or i["name"].lower().startswith(("linkedin member", "участник")):
                continue
            if not toks & _tokens(head) and re.search(r"\bat\b|\bw\b|@|\bdla\b", head.lower()):
                continue
            picked.setdefault(i["id"], (role, i))
        for (rank, label), i in sorted(picked.values(), key=lambda x: x[0][0])[:2]:
            url = (i.get("public_profile_url") or i.get("profile_url") or "").split("?")[0]
            lang = lang_of(i["name"], i.get("headline") or "", c["name"], "")
            if url and lang:
                make(i["id"], url, i["name"], i.get("headline") or "", label, c["name"], c["key"],
                     SEGMENT_TRACK.get(c.get("segment"), "A"), c.get("segment") or "pool", round(float(c.get("fit") or 5) * 7 + 20 * people.ROLE_WEIGHT.get(label, .5), 1),
                     lang, None, "pool")
    save(DATA / "lang_cache.json", langs)
    print(f"[prepare] +{sum(l['source'] == 'seed' for l in out)} seed people, +{sum(l['source'] == 'pool' for l in out)} from pool companies")
    return out


def render(lead, stage, seq):
    text = seq["messages"].get(f"{lead['track']}-{lead['lang']}-{stage}", "")
    return (text.replace("{personal_line}", lead["personal_line"]).replace("{first_name}", lead["first_name"])
            .replace("{company}", lead["company_short"])).strip()


# ─── run: the sender ────────────────────────────────────────────────────────────────────────────────────────────

class Api:
    def __init__(self):
        dsn = os.environ["UNIPILE_DSN"].strip().rstrip("/")
        self.base = dsn if dsn.startswith("http") else f"https://{dsn}"
        self.s = requests.Session()
        self.s.headers.update({"X-API-KEY": os.environ["UNIPILE_API_KEY"], "accept": "application/json"})

    def call(self, account, method, path, params=None, **kw):
        r = self.s.request(method, f"{self.base}{path}", timeout=60,
                           params={"account_id": os.environ[ACCOUNTS[account]], **(params or {})}, **kw)
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, body


def in_hours(t):
    if t.weekday() >= 5 or t.strftime("%m-%d") in PL_HOLIDAYS:
        return False
    return (9, 0) <= (t.hour, t.minute) < (17, 30)


def next_working(t, days):
    """t + days, moved forward into working hours, at a random minute."""
    t = t + dt.timedelta(days=days)
    while t.weekday() >= 5 or t.strftime("%m-%d") in PL_HOLIDAYS:
        t += dt.timedelta(days=1)
    if (t.hour, t.minute) < (9, 0) or (t.hour, t.minute) >= (17, 0):
        t = t.replace(hour=random.randint(9, 15), minute=random.randint(0, 59))
    return t


def invite_time(viewed):
    """Invite 1-2 hours after the profile view; a view late in the day invites the next working morning."""
    t = viewed + dt.timedelta(minutes=random.randint(60, 120))
    return t if in_hours(t) else next_working(viewed, 1).replace(hour=random.randint(9, 10), minute=random.randint(0, 59))


def nap(seconds):
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(min(60, max(0, end - time.time())))


class Campaign:
    def __init__(self):
        self.api = Api()
        self.seq = build_seq()
        self.state = load(STATE, {"leads": {}, "accounts": {}, "events": []})
        for s in self.state["leads"].values():  # views made under the old 1-2 day gap move to the 1-2 hour one
            if s.get("stage") == "viewed" and s.get("viewed_at") and s.get("invite_after"):
                v = dt.datetime.fromisoformat(s["viewed_at"])
                if dt.datetime.fromisoformat(s["invite_after"]) - v > dt.timedelta(hours=3):
                    s["invite_after"] = invite_time(v).isoformat()

    def persist(self):
        with LOCK:
            self.state["events"] = self.state["events"][-3000:]
            save(STATE, self.state)

    def event(self, account, kind, lead=None, note=""):
        with LOCK:
            self.state["events"].append({"t": now().isoformat(), "account": account, "kind": kind,
                                         "lead": lead and lead["provider_id"], "name": lead and lead["name"],
                                         "company": lead and lead["company"], "note": note})
        log(f"{account}: {kind} {lead['name'] + ' @ ' + lead['company'] if lead else ''} {note}".strip())

    def acct(self, account):
        a = self.state["accounts"].setdefault(account, {})
        today, week = now().date().isoformat(), now().strftime("%G-W%V")
        if a.get("week") != week:
            a.update(week=week, invites_week=0)
        if a.get("day") != today:
            clean = a.get("day") and not a.get("had_error") and a.get("used", {}).get("invite", 0) > 0
            a["invite_cap"] = min(INVITE_MAX, a.get("invite_cap", INVITE_START) + (INVITE_STEP if clean else 0))
            a.update(day=today, used={"view": 0, "invite": 0, "message": 0}, had_error=False)
        return a

    def can(self, account, kind):
        if guard.paused_until(account):  # paused by any component (search queue, status check, this campaign)
            return False
        a = self.acct(account)
        if kind == "invite":
            return a["used"]["invite"] < a["invite_cap"] and a.get("invites_week", 0) < INVITE_WEEK
        return a["used"][kind] < CAPS[kind]

    def spend(self, account, kind):
        a = self.acct(account)
        a["used"][kind] += 1
        if kind == "invite":
            a["invites_week"] = a.get("invites_week", 0) + 1

    def trouble(self, account, status, body):
        a = self.acct(account)
        a["paused_until"] = (now() + dt.timedelta(hours=random.randint(48, 72))).isoformat()
        a["had_error"], a["invite_cap"] = True, INVITE_START
        a["last_error"] = f"HTTP {status} at {now():%a %H:%M}: {str(body)[:120]}"
        guard.pause(account, f"HTTP {status}: {str(body)[:120]}", source="кампанія")
        self.event(account, "paused", note=a["last_error"])
        self.persist()

    def reply_alert(self, account, lead):
        guard.alert(f"💬 <b>{lead['name']}</b> ({lead['company'] or lead.get('headline', '')[:60]}) відповів у LinkedIn.\n"
                    f"Сіквенс для нього зупинено, далі пишете ви: {lead['url']}", account=account)

    def st(self, lead):
        return self.state["leads"].setdefault(lead["provider_id"], {"stage": "queued"})

    # ── checks ──
    def pid(self, lead):
        """LinkedIn member id. Seed and pool leads arrive with a URL only; the profile view fills this in."""
        return self.st(lead).get("pid") or lead["provider_id"]

    def had_conversation(self, account, lead):
        """Any chat with this person on either account, or anything they wrote: hands off."""
        for acc in ACCOUNTS:
            code, body = self.api.call(acc, "GET", f"/api/v1/chat_attendees/{self.pid(lead)}/chats", {"limit": 5})
            if code in (401, 403, 429):
                self.trouble(acc, code, body)
                return True
            if code == 200 and body.get("items"):
                return True
        return False

    def replied(self, account, lead):
        s = self.st(lead)
        if not s.get("chat_id"):
            return False
        code, body = self.api.call(account, "GET", f"/api/v1/chats/{s['chat_id']}/messages", {"limit": 20})
        if code in (401, 403, 429):
            self.trouble(account, code, body)
            return None
        return any(not m.get("is_sender") for m in body.get("items") or [])

    # ── actions ──
    def view(self, account, lead):
        code, body = self.api.call(account, "GET", f"/api/v1/users/{lead['public_identifier'] or lead['provider_id']}")
        self.spend(account, "view")
        if code in (401, 403, 429):
            return self.trouble(account, code, body)
        s = self.st(lead)
        if code != 200:
            s.update(stage="skipped", reason=f"profile {code}")
            return self.event(account, "skipped", lead, f"profile {code}")
        dist = body.get("network_distance")
        s.update(viewed_at=now().isoformat(), distance=dist)
        if body.get("provider_id"):
            s["pid"] = body["provider_id"]
        elif lead["provider_id"].startswith("url:"):
            s.update(stage="skipped", reason="no LinkedIn id on profile")
            return self.event(account, "skipped", lead, "no LinkedIn id")
        if dist in ("FIRST_DEGREE", "DISTANCE_1"):
            s.update(stage="connected", connected_at=now().isoformat(),
                     next_at=next_working(now(), 0).replace(hour=random.randint(10, 16)).isoformat(), next_stage="m1")
            self.event(account, "viewed (already connected)", lead)
        else:
            s.update(stage="viewed", invite_after=invite_time(now()).isoformat())
            self.event(account, "viewed", lead)

    def invite(self, account, lead):
        s = self.st(lead)
        if self.had_conversation(account, lead):
            s.update(stage="skipped", reason="earlier conversation")
            return self.event(account, "skipped", lead, "earlier conversation")
        code, body = self.api.call(account, "POST", "/api/v1/users/invite", json={
            "provider_id": self.pid(lead), "account_id": os.environ[ACCOUNTS[account]]})
        self.spend(account, "invite")
        if code in (401, 403, 429):
            return self.trouble(account, code, body)
        if code >= 400:
            note = str(body.get("detail") or body.get("title") or code)[:120]
            if code == 422 and re.search(r"limit|ceiling|weekly", note, re.I):
                return self.trouble(account, code, body)
            s.update(stage="skipped", reason=f"invite {code}: {note}")
            return self.event(account, "skipped", lead, f"invite {code}: {note}")
        s.update(stage="invited", invited_at=now().isoformat())
        with LOCK:
            self.state.setdefault("company_invites", {})[lead["company_key"]] = now().isoformat()
        self.event(account, "invited", lead)

    def message(self, account, lead):
        s = self.st(lead)
        stage = s["next_stage"]
        r = self.replied(account, lead)
        if r is None:
            return
        if r:
            s.update(stage="replied", replied_at=now().isoformat(), next_at=None)
            self.reply_alert(account, lead)
            return self.event(account, "replied", lead)
        if stage == "m1" and self.had_conversation(account, lead):
            s.update(stage="skipped", reason="earlier conversation")
            return self.event(account, "skipped", lead, "earlier conversation")
        if lead["lang"] == "uk":
            voc = vocative(lead["name"])
            if voc is None:
                return log(f"{account}: no Ukrainian first name for {lead['name']!r} yet, retrying later")
            lead = {**lead, "first_name": voc or lead["first_name"]}
        text = render(lead, stage, self.seq)
        if not text or "{" in text:
            s.update(stage="skipped", reason=f"no text for {lead['track']}-{lead['lang']}-{stage}")
            return self.event(account, "skipped", lead, "no message text")
        if s.get("chat_id"):
            code, body = self.api.call(account, "POST", f"/api/v1/chats/{s['chat_id']}/messages", files={"text": (None, text)})
        else:
            code, body = self.api.call(account, "POST", "/api/v1/chats", files={  # Unipile takes multipart here
                "account_id": (None, os.environ[ACCOUNTS[account]]), "attendees_ids": (None, self.pid(lead)),
                "text": (None, text)})
        self.spend(account, "message")
        if code in (401, 403, 429):
            return self.trouble(account, code, body)
        if code >= 400:
            s.update(stage="skipped", reason=f"message {code}")
            return self.event(account, "skipped", lead, f"message {code}: {str(body)[:100]}")
        s.setdefault("sent", {})[stage] = now().isoformat()
        if body.get("chat_id"):
            s["chat_id"] = body["chat_id"]
        nxt = STAGES[STAGES.index(stage) + 1] if stage != STAGES[-1] else None
        s.update(stage=f"sent_{stage}", next_stage=nxt,
                 next_at=next_working(now(), GAP_DAYS[nxt]).isoformat() if nxt else None)
        if not nxt:
            s["stage"] = "done"
        self.event(account, f"message {STAGES.index(stage) + 1}", lead)

    # ── sync: acceptances and replies ──
    def sync(self, account, leads):
        code, body = self.api.call(account, "GET", "/api/v1/users/relations", {"limit": 100})
        if code in (401, 403, 429):
            return self.trouble(account, code, body)
        mine = {l["provider_id"]: l for l in leads if l["owner"] == account}
        ids = {i.get("member_id") for i in body.get("items") or []} | {i.get("public_identifier") for i in body.get("items") or []}
        for pid, l in mine.items():
            s = self.st(l)
            if s["stage"] == "invited" and (self.pid(l) in ids or l["public_identifier"] in ids):
                s.update(stage="connected", connected_at=now().isoformat(), next_stage="m1",
                         next_at=(now() + dt.timedelta(minutes=random.randint(60, 300))).isoformat())
                self.event(account, "accepted", l)
            elif s["stage"] == "invited" and dt.datetime.fromisoformat(s["invited_at"]) < now() - dt.timedelta(days=21):
                s.update(stage="expired")
                self.event(account, "invite expired", l)
        for l in mine.values():  # replies between scheduled messages
            s = self.st(l)
            if s.get("chat_id") and s["stage"].startswith("sent_"):
                r = self.replied(account, l)
                if r:
                    s.update(stage="replied", replied_at=now().isoformat(), next_at=None)
                    self.reply_alert(account, l)
                    self.event(account, "replied", l)
                time.sleep(random.uniform(2, 6))
        self.persist()

    def pick(self, account, leads):
        """Next action for this account: due messages first, then invites, then profile views."""
        t = now()
        mine = [l for l in leads if l["owner"] == account]
        due = [l for l in mine if self.st(l)["stage"] in ("connected",) or self.st(l)["stage"].startswith("sent_")]
        due = [l for l in due if self.st(l).get("next_at") and dt.datetime.fromisoformat(self.st(l)["next_at"]) <= t]
        if due and self.can(account, "message"):
            return "message", due[0]
        ci = self.state.get("company_invites", {})
        week_ago = t - dt.timedelta(days=7)
        inv = [l for l in mine if self.st(l)["stage"] == "viewed" and dt.datetime.fromisoformat(self.st(l)["invite_after"]) <= t
               and not (ci.get(l["company_key"]) and dt.datetime.fromisoformat(ci[l["company_key"]]) > week_ago)]
        if inv and self.can(account, "invite"):
            return "invite", inv[0]
        # view only as many as the invite pipeline can absorb in the next couple of days
        waiting = sum(self.st(l)["stage"] == "viewed" for l in mine)
        new = [l for l in mine if self.st(l)["stage"] == "queued"]
        if new and waiting < self.acct(account)["invite_cap"] * 2 and self.can(account, "view"):
            return "view", new[0]
        return None, None

    def work(self, account):
        nap(random.uniform(0, 15 * 60))
        last_sync = 0
        while True:
            t = now()
            if not in_hours(t):
                nap(15 * 60)
                continue
            ex = set(load(DATA / "campaign_exclude.json", []))
            leads = sorted((l for l in load(LEADS, []) if not {l["provider_id"], l["url"], l["url"].rstrip("/")} & ex),
                           key=lambda l: -l["score"])
            if time.time() - last_sync > random.uniform(90, 150) * 60:
                self.sync(account, leads)
                last_sync = time.time()
            with LOCK:
                kind, lead = self.pick(account, leads)
            if not kind:
                nap(random.uniform(15, 30) * 60)
                continue
            time.sleep(random.uniform(3, 10))
            getattr(self, kind)(account, lead)
            self.persist()
            nap(random.lognormvariate(5.6, 0.35))  # ~3-9 min, mostly around 4.5

    def run(self):
        log(f"campaign started: {len(load(LEADS, []))} leads")
        ths = [threading.Thread(target=self.work, args=(a,), name=a, daemon=True) for a in ACCOUNTS]
        for t in ths:
            t.start()
        for t in ths:
            t.join()


def status():
    leads = load(LEADS, [])
    state = load(STATE, {"leads": {}})
    from collections import Counter
    c = Counter((l["owner"], state["leads"].get(l["provider_id"], {}).get("stage", "queued")) for l in leads)
    print(json.dumps({f"{a}:{s}": n for (a, s), n in sorted(c.items())}, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "prepare":
        prepare()
    elif cmd == "run":
        Campaign().run()
    else:
        status()
