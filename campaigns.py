"""Outreach campaigns: who each one targets, which LinkedIn account sends it, and where its leads and messages live.

  python campaigns.py list
  python campaigns.py import websummit leads.csv      # add people to a campaign (see import_csv for the columns)

All campaigns share one LinkedIn state (keyed by person) and one budget per account, so a
person is never approached twice and an account's daily limits hold no matter how many campaigns it sends. A campaign
runs only when it is listed in CAMPAIGNS_ENABLED (env, comma list; default "job_boards"), has leads and has text for
every message it would send; a lead whose message text is missing is held back, never sent a half-empty message.

Leads, switches, texts and the sender's state are in Postgres in production (store.py, JB_STORE=db) and in the
files named here otherwise; every read and write goes through the functions below.
"""
import csv
import json
import os
import re
import sys
from pathlib import Path

import store

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

CAMPAIGNS = {
    "job_boards": {
        "name": "Job-борди",
        "about": "Компанії, що наймають продавців на дзвінки (job-борди PL), і ранній скан LinkedIn.",
        "accounts": ["hleb"],
        "leads": "campaign_leads.json",  # built by campaign.prepare() and the research agent
        "sequences": "sequences.json",   # built from data/seq_export (the sequences artifact)
        "priority": 0,
        "stages": ["m1", "f1", "f2", "f3"],
        "tracks": {"A": "Онлайн-демо (SaaS, IT, агенції)", "B": "Телефон (call-центри, фінанси, windykacja)"},
        "langs": ["pl", "en", "uk"],
    },
    "websummit": {
        "name": "Web Summit 2026",
        "about": "Люди, з якими зустрічаємось на Web Summit (Лісабон, 9–12 листопада), і ті, кого хочемо там побачити.",
        "accounts": ["nazarii"],
        "leads": "campaigns/websummit/leads.json",
        "sequences": "campaigns/websummit/sequences.json",
        "priority": 1,  # event follow-ups are time-sensitive: they go before job-board leads on a shared account
        "stages": ["m1"],  # one message, and it goes out as the connection request's note (see invite_note)
        "invite_note": True,  # m1 rides on the invite itself (Premium: up to 300 characters); nothing after acceptance
        "invite_start": 15,  # daily invites on its account start here (then +2 a clean day, up to 20): 15, 17, 19, 20
        "tracks": {"A": "Sales roles", "B": "Owners, CEOs and others"},
        "langs": ["en"],
    },
}
STAGES = ["m1", "f1", "f2", "f3"]
NOTE_MAX = 300  # LinkedIn's limit for an invitation note on a Premium account


def _load(path, default):
    p = DATA / path
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        return default


CONTROL = "campaign_control.json"  # runtime switches written by the dashboard and the Telegram bot
EXCLUDE = "campaign_exclude.json"  # people taken out by hand (LinkedIn URL or member id)


def _save(path, obj):
    p = DATA / path
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    tmp.replace(p)


def enabled_ids():
    ctl = store.enabled() if store.DB else _load(CONTROL, {}).get("enabled", {})
    default = [c.strip() for c in os.environ.get("CAMPAIGNS_ENABLED", "job_boards").split(",")]
    return [cid for cid in CAMPAIGNS if ctl.get(cid, cid in default)]


def set_enabled(cid, on, who=""):
    if store.DB:
        return store.set_enabled(cid, on, who)
    ctl = _load(CONTROL, {})
    ctl.setdefault("enabled", {})[cid] = bool(on)
    _save(CONTROL, ctl)


def stages(cid):
    return CAMPAIGNS[cid]["stages"]


def messages(cid):
    """Deployed texts, with edits made from the dashboard or Telegram on top."""
    base = _load(CAMPAIGNS[cid]["sequences"], {}).get("messages", {})
    return {**base, **(store.messages(cid) if store.DB else _load(f"campaigns/{cid}/messages_live.json", {}))}


def set_message(cid, key, text, who=""):
    if store.DB:
        return store.set_message(cid, key, text, who)
    live = _load(f"campaigns/{cid}/messages_live.json", {})
    live[key] = text
    _save(f"campaigns/{cid}/messages_live.json", live)


def leads(cid):
    raw = store.leads(cid) if store.DB else _load(CAMPAIGNS[cid]["leads"], []) + _load(f"campaigns/{cid}/added.json", [])
    return [{**l, "campaign": cid} for l in raw]


def job_leads():
    """The job-board campaign's generated list (campaign.prepare), without hand-added people."""
    return store.leads("job_boards", "prepare") if store.DB else _load(CAMPAIGNS["job_boards"]["leads"], [])


def set_job_leads(items):
    if store.DB:
        return store.set_leads("job_boards", items, "prepare")
    _save(CAMPAIGNS["job_boards"]["leads"], items)


def load_state(track=False):
    """The sender's state: {"leads": {provider_id: ...}, "accounts": {...}, "events": [...], ...}."""
    if store.DB:
        return store.load_state(track=track)
    st = _load("campaign_state.json", {})
    return {"leads": {}, "accounts": {}, "events": [], **st}


def save_state(state):
    if store.DB:
        return store.save_state(state)
    _save("campaign_state.json", state)


def excluded():
    return store.excluded() if store.DB else set(_load(EXCLUDE, []))


def set_excluded(keys, on, who=""):
    if store.DB:
        return store.set_excluded(keys, on, who)
    ex = excluded()
    ex = (ex | set(keys)) if on else (ex - set(keys))
    _save(EXCLUDE, sorted(ex))


def find_lead(query):
    """Leads matching a LinkedIn URL / slug / member id exactly, else by name or company (case-insensitive)."""
    q = query.strip().lower().rstrip("/")
    allx = all_leads(only_enabled=False)
    exact = [l for l in allx if q in (l["url"].lower().rstrip("/"), l["public_identifier"].lower(), l["provider_id"].lower())
             or (q.startswith("http") and q.rsplit("/", 1)[-1] == l["public_identifier"].lower())]
    return exact or [l for l in allx if q in l["name"].lower() or q in (l.get("company") or "").lower()]


def short_company(name):
    n = (name or "").split("/")[0].split("|")[0].strip()
    n = re.sub(r",?\s+(inc|ltd|llc|gmbh|s\.?a\.?|sp\.? z o\.?o\.?|b\.?v\.?|ag|plc|lda|s\.?l\.?|limited)\.?$", "", n, flags=re.I)
    return n.strip(" ,.") or name


def add_lead(cid, url, name="", company="", role="", track="", note="", lang=""):
    """One person added by hand (dashboard / Telegram). The profile view fills in the LinkedIn id."""
    url = url.split("?")[0].rstrip("/")
    if "/in/" not in url:
        raise ValueError("потрібне посилання на профіль LinkedIn (linkedin.com/in/...)")
    pub = url.rsplit("/", 1)[-1]
    if any(l["public_identifier"] == pub for l in all_leads(only_enabled=False)):
        raise ValueError("ця людина вже є в одній із кампаній")
    c = CAMPAIGNS[cid]
    name = name.strip() or pub
    track = (track or "").upper() if (track or "").upper() in c["tracks"] else (
        "A" if re.search(r"sales|sprzeda|продаж|revenue|commercial|business development", role or "", re.I) else "B")
    lead = {"provider_id": f"url:{pub}", "public_identifier": pub, "url": url, "name": name, "first_name": name.split()[0],
            "company": company, "company_short": short_company(company), "company_key": pub, "role": role, "headline": role,
            "lang": lang or c["langs"][0], "track": track, "personal_line": "", "note": note, "score": 75.0,
            "niche": c["name"], "source": "manual", "tier": "", "owner": c["accounts"][0]}
    if store.DB:
        store.add_lead(cid, lead)
        return lead
    added = _load(f"campaigns/{cid}/added.json", [])
    added.append(lead)
    _save(f"campaigns/{cid}/added.json", added)
    return lead


def invite_cap(account, ac):
    """Today's invite limit of an account (state ac), counting a campaign's ramp start not yet applied by the sender."""
    for cid, c in CAMPAIGNS.items():
        if account in c["accounts"] and c.get("invite_start") and ac.get("ramp_for") != cid:
            return c["invite_start"]
    return ac.get("invite_cap", 12)


def accounts_in_use():
    return sorted({a for cid in enabled_ids() for a in CAMPAIGNS[cid]["accounts"]})


def all_leads(only_enabled=True, state=None):
    """Every campaign's leads, each person once. When the same person (LinkedIn slug) is in two campaigns, the one
    already in progress keeps them; otherwise the higher-priority campaign does."""
    state = state or {}
    ids = enabled_ids() if only_enabled else list(CAMPAIGNS)
    best = {}
    for cid in ids:
        for l in leads(cid):
            key = l.get("public_identifier") or l["provider_id"]
            prev = best.get(key)
            if prev is None:
                best[key] = l
                continue
            started = lambda x: state.get(x["provider_id"], {}).get("stage", "queued") not in ("queued",)
            if started(prev):
                continue
            if started(l) or CAMPAIGNS[cid]["priority"] > CAMPAIGNS[prev["campaign"]]["priority"]:
                best[key] = l
    return list(best.values())


def summary():
    """What the dashboard shows about each campaign before any stats."""
    out = []
    on = enabled_ids()
    for cid, c in CAMPAIGNS.items():
        msgs = messages(cid)
        langs = sorted({k.split("-")[1] for k, v in msgs.items() if v.strip() and k.count("-") == 2})
        ready = {lg: min(sum(bool(msgs.get(f"{t}-{lg}-{s}", "").strip()) for s in stages(cid)) for t in c["tracks"]) for lg in langs}
        out.append({"id": cid, "name": c["name"], "about": c["about"], "accounts": c["accounts"], "enabled": cid in on,
                    "leads": len(leads(cid)), "messages": sum(bool(v.strip()) for v in msgs.values()), "langs": langs,
                    "ready": ready, "stages": stages(cid), "tracks": c["tracks"], "all_langs": c["langs"],
                    "invite_note": bool(c.get("invite_note"))})
    return out


def migration_files():
    """For store.migrate_from_files: each campaign's lead, live-text and hand-added files."""
    return {cid: (c["leads"], f"campaigns/{cid}/messages_live.json", f"campaigns/{cid}/added.json") for cid, c in CAMPAIGNS.items()}


def import_csv(cid, path):
    """Add people to a campaign from a CSV with columns:
         linkedin_url (required), name, company, role, lang (pl/en/uk; guessed when empty), track (A/B, default A),
         personal_line (the sentence put into {personal_line}; empty means none), note (where you met, what you discussed;
         available to messages as {note}), score (0-100, default 70).
    Existing people in the campaign are updated, not duplicated."""
    c = CAMPAIGNS[cid]
    have = {l["public_identifier"]: l for l in (store.leads(cid, "import") if store.DB else _load(c["leads"], []))}
    added = updated = 0
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        url = (r.get("linkedin_url") or "").split("?")[0].rstrip("/")
        if "/in/" not in url:
            continue
        pub = url.rsplit("/", 1)[-1]
        name = (r.get("name") or pub).strip()
        lead = {"provider_id": have.get(pub, {}).get("provider_id", f"url:{pub}"), "public_identifier": pub, "url": url,
                "name": name, "first_name": name.split()[0], "company": (r.get("company") or "").strip(),
                "company_short": (r.get("company") or "").strip(), "company_key": pub, "role": (r.get("role") or "").strip(),
                "headline": (r.get("role") or "").strip(), "lang": (r.get("lang") or "en").strip().lower() or "en",
                "track": (r.get("track") or "A").strip().upper() or "A", "personal_line": (r.get("personal_line") or "").strip(),
                "note": (r.get("note") or "").strip(), "score": float(r.get("score") or 70), "niche": "Web Summit",
                "source": cid, "tier": "", "owner": have.get(pub, {}).get("owner") or c["accounts"][0]}
        updated += pub in have
        added += pub not in have
        have[pub] = lead
    if store.DB:
        store.set_leads(cid, list(have.values()), "import")
    else:
        _save(c["leads"], list(have.values()))
    print(f"[{cid}] {added} added, {updated} updated, {len(have)} in the campaign")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "import":
        import_csv(sys.argv[2], sys.argv[3])
    else:
        print(json.dumps(summary(), ensure_ascii=False, indent=1))
