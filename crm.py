"""Lead statuses for a Google Sheet: what the campaign knows (invite, acceptance, the last message in the chat) plus
what the founders set by hand there (status, next step, notes).

service.py serves it at /api/sheet (Bearer SHEET_TOKEN or SERVICE_TOKEN); sheets/crm.gs in the sheet calls it every
10 minutes and on every edit. Hand-set fields are kept in crm.json (in the database in db mode). Setting a person who
hasn't been invited yet to Lost or Not relevant stops the campaign for them, the same as the dashboard's stop button.
Each reply is labelled Positive / Neutral / Negative by gpt-5-mini (cached in reply_sentiment.json); a label set by
hand in the sheet wins.
"""
import datetime as dt
import hashlib
import os
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import campaigns
import store

TZ = ZoneInfo("Europe/Warsaw")
CRM = store.path("crm.json")
# set by hand; the rest are worked out from the campaign
MANUAL = ["To review", "Needs follow-up", "Meeting booked", "Won", "Lost", "Not relevant"]
AUTO = ["To review", "Needs follow-up", "Waiting for reply", "Accepted", "In sequence", "Invite sent", "Queued",
        "Invite not accepted", "Skipped"]
ORDER = ["To review", "Needs follow-up", "Meeting booked", "Waiting for reply", "Accepted", "In sequence", "Invite sent",
         "Won", "Queued", "Invite not accepted", "Lost", "Not relevant", "Skipped"]
STOPS = {"Lost", "Not relevant"}
OUTCOMES = ["Meeting booked", "Won", "Lost", "Not relevant"]  # what came of a conversation, set by hand
FOLLOW_UP_DAYS = 3  # no answer to our last message for this long: follow up
ACCEPTED_DAYS = 2   # accepted an invite that carried the message, and said nothing for this long
PIPELINE = {"queued": "Queued", "viewed": "Profile viewed", "invited": "Invite sent", "connected": "Accepted",
            "sent_m1": "Message 1 sent", "sent_f1": "Message 2 sent", "sent_f2": "Message 3 sent", "done": "Accepted",
            "replied": "Replied", "skipped": "Skipped", "expired": "Invite not accepted"}
NAMES = {"nazarii": "Nazarii", "hleb": "Hleb"}
CAMPAIGN_NAMES = {"job_boards": "Job boards", "websummit": "Web Summit 2026"}
REPLY_TYPES = ["Positive", "Neutral", "Negative"]
SENTIMENT = store.path("reply_sentiment.json")
SENTIMENT_SYSTEM = """You label a reply to a cold LinkedIn message from a founder of Dealaris (a real-time AI copilot
for sales calls; some messages were about meeting at Web Summit). Read only what the prospect wrote.
Positive: open to talking, meeting, a call or a demo, asks about the product, gives a contact or a time, or invites
them over (to a booth, an event). Negative: not interested, no need, already has a tool, asks not to be contacted,
or is clearly annoyed. Neutral: anything else, such as a thank-you, an emoji, an out-of-office note, a redirect to
someone else without interest, or small talk."""
SENTIMENT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["label"],
                    "properties": {"label": {"type": "string", "enum": REPLY_TYPES}}}


def _t(iso):
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")) if iso else None


def _day(iso):
    t = _t(iso)
    return t.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if t else ""


def auto_status(s, now):
    stage, chat = s.get("stage", "queued"), s.get("chat") or {}
    if stage == "replied" or chat.get("last_in_at"):
        if chat.get("last_from") == "us":
            old = now - _t(chat["last_at"]) > dt.timedelta(days=FOLLOW_UP_DAYS)
            return "Needs follow-up" if old else "Waiting for reply"
        return "To review"
    if stage == "done" and s.get("note_sent"):  # accepted; the message went with the invite
        old = s.get("connected_at") and now - _t(s["connected_at"]) > dt.timedelta(days=ACCEPTED_DAYS)
        return "Needs follow-up" if old else "Accepted"
    if stage == "done" or stage == "connected" or stage.startswith("sent_"):
        return "In sequence"
    return {"invited": "Invite sent", "expired": "Invite not accepted", "skipped": "Skipped"}.get(stage, "Queued")


def status(s, m, now):
    """The status the sheet shows: the hand-set one, unless the person wrote again after a hand-set To review /
    Needs follow-up, which brings them back to To review."""
    if not m.get("status"):
        return auto_status(s, now)
    last_in = (s.get("chat") or {}).get("last_in_at")
    if m["status"] in ("To review", "Needs follow-up") and last_in and _t(last_in) > _t(m["status_at"]) \
            and (s.get("chat") or {}).get("last_from") == "them":
        return "To review"
    return m["status"]


REPLY_TEXT = store.path("reply_text.json")  # what people wrote, read from Unipile for replies older than chat tracking


def add_saved_texts(lead_states):
    """Fill in, in memory only, the reply text of people whose chat the sender hasn't read since texts were kept."""
    for pid, text in store.read_json(REPLY_TEXT, {}).items():
        s = lead_states.get(pid)
        if s is not None and not (s.get("chat") or {}).get("in_text"):
            s["chat"] = {**(s.get("chat") or {}), "in_text": text}
    return lead_states


def their_text(s):
    chat = s.get("chat") or {}
    return chat.get("in_text") or (chat.get("last_text") if chat.get("last_from") == "them" else "")


def label_replies(texts, limit=60):
    """Positive / Neutral / Negative for each reply text, from the cache or from gpt-5-mini (at most `limit` new)."""
    cache = store.read_json(SENTIMENT, {})
    key = lambda t: hashlib.sha1(t.encode()).hexdigest()[:16]
    todo = list({key(t): t for t in texts if t and key(t) not in cache}.items())[:limit]
    if todo and os.environ.get("OPENAI_API_KEY"):
        from judge import ask

        def one(kt):
            try:
                return kt[0], ask(os.environ["OPENAI_API_KEY"], kt[1], SENTIMENT_SYSTEM, SENTIMENT_SCHEMA)["label"]
            except Exception:  # unlabelled for now; the next sync tries again
                return kt[0], None
        with ThreadPoolExecutor(6) as ex:
            for k, label in ex.map(one, todo):
                if label:
                    cache[k] = label
        store.write_json(SENTIMENT, cache)
    return {t: cache.get(key(t), "") for t in texts if t}


def rows(cid="all"):
    if cid != "all" and cid not in campaigns.CAMPAIGNS:
        raise ValueError(f"unknown campaign {cid!r}")
    st = campaigns.load_state()
    crm = store.read_json(CRM, {})
    ex = campaigns.excluded()
    now = dt.datetime.now(dt.timezone.utc)
    leads = campaigns.all_leads(only_enabled=False, state=st["leads"]) if cid == "all" else campaigns.leads(cid)
    add_saved_texts(st["leads"])
    labels = label_replies([their_text(st["leads"].get(l["provider_id"], {})) for l in leads])
    out = []
    for l in leads:
        s, m = st["leads"].get(l["provider_id"], {}), crm.get(l["provider_id"], {})
        chat = s.get("chat") or {}
        replied = s.get("stage") == "replied" or chat.get("last_in_at")
        stopped = bool({l["provider_id"], l["url"], l["url"].rstrip("/")} & ex)
        last = max(filter(None, [s.get("viewed_at"), s.get("invited_at"), s.get("connected_at"), s.get("replied_at"),
                                 chat.get("last_at")]), key=_t, default="")
        out.append({
            "key": l["provider_id"], "status": status(s, m, now), "name": l["name"], "url": l["url"],
            "campaign": CAMPAIGN_NAMES.get(l["campaign"], l["campaign"]),
            "reply_type": m.get("reply") or (labels.get(their_text(s), "") if replied else ""),
            "company": l.get("company_short") or l["company"], "role": l.get("role") or l.get("headline", ""),
            "country": l.get("country", ""), "owner": NAMES.get(l["owner"], l["owner"]),
            "pipeline": PIPELINE.get(s.get("stage", "queued"), s.get("stage", "")) + (" (stopped)" if stopped else ""),
            "last_message": (("Them: " if chat.get("last_from") == "them" else "Us: ") + chat.get("last_text", "")) if chat.get("last_at") else "",
            "last_activity": _day(last), "invited": _day(s.get("invited_at")), "accepted": _day(s.get("connected_at")),
            "replied": _day(s.get("replied_at")), "next_step": m.get("next", ""), "notes": m.get("notes", ""),
            "score": l.get("score", ""), "niche": l.get("niche", ""),
            "auto_note": s.get("reason", ""),
        })
    rank = {k: i for i, k in enumerate(ORDER)}
    out.sort(key=lambda r: (rank.get(r["status"], len(ORDER)), r["last_activity"] == "", _neg(r["last_activity"]),
                            -float(r["score"] or 0)))
    return {"ok": True, "campaigns": [CAMPAIGN_NAMES.get(c, c) for c in campaigns.CAMPAIGNS],
            "statuses": MANUAL + [a for a in AUTO if a not in MANUAL], "reply_types": REPLY_TYPES,
            "rows": out, "at": _day(now.isoformat())}


def _neg(s):
    return "".join(chr(0x10FFFF - ord(c)) for c in s)  # descending string order inside an ascending sort


def apply(cid, changes, who="sheet"):
    """Hand edits from the sheet: [{key, status?, reply_type?, next_step?, notes?}]. Returns how many people changed."""
    leads = {l["provider_id"]: l for l in (campaigns.all_leads(only_enabled=False) if cid == "all" else campaigns.leads(cid))}
    crm = store.read_json(CRM, {})
    st = campaigns.load_state()["leads"]
    now = dt.datetime.now(dt.timezone.utc)
    stop, resume, n = [], [], 0
    for c in changes:
        l = leads.get(c.get("key"))
        if not l:
            continue
        m = crm.setdefault(l["provider_id"], {})
        if "status" in c:
            new = (c["status"] or "").strip()
            auto = auto_status(st.get(l["provider_id"], {}), now)
            new = "" if new == auto and not m.get("status") else new  # left as it was shown: nothing set by hand
            if new != m.get("status", ""):
                m.update(status=new, status_at=now.isoformat())
                not_invited = st.get(l["provider_id"], {}).get("stage", "queued") in ("queued", "viewed")
                if new in STOPS and not_invited:
                    stop.append(l)
                    m["stopped_by_sheet"] = True
                elif m.pop("stopped_by_sheet", None) and new not in STOPS:
                    resume.append(l)
        if "reply_type" in c:
            m["reply"] = str(c["reply_type"] or "").strip()
        if "next_step" in c:
            m["next"] = str(c["next_step"] or "").strip()
        if "notes" in c:
            m["notes"] = str(c["notes"] or "").strip()
        m.update(by=who, at=now.isoformat())
        n += 1
    store.write_json(CRM, crm)
    for group, on in ((stop, True), (resume, False)):
        if group:
            campaigns.set_excluded([k for l in group for k in (l["url"], l["provider_id"])], on, who)
    return {"ok": True, "changed": n, "stopped": len(stop), "resumed": len(resume)}
