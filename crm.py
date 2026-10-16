"""Lead statuses for a Google Sheet: what the campaign knows (invite, acceptance, the last message in the chat) plus
what the founders set by hand there (status, next step, notes).

service.py serves it at /api/sheet (Bearer SHEET_TOKEN or SERVICE_TOKEN); sheets/crm.gs in the sheet calls it every
10 minutes and on every edit. Hand-set fields are kept in crm.json (in the database in db mode). Setting a person who
hasn't been invited yet to Lost or Not relevant stops the campaign for them, the same as the dashboard's stop button.
"""
import datetime as dt
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
FOLLOW_UP_DAYS = 3  # no answer to our last message for this long: follow up
ACCEPTED_DAYS = 2   # accepted an invite that carried the message, and said nothing for this long
PIPELINE = {"queued": "Queued", "viewed": "Profile viewed", "invited": "Invite sent", "connected": "Accepted",
            "sent_m1": "Message 1 sent", "sent_f1": "Message 2 sent", "sent_f2": "Message 3 sent", "done": "Accepted",
            "replied": "Replied", "skipped": "Skipped", "expired": "Invite not accepted"}
NAMES = {"nazarii": "Nazarii", "hleb": "Hleb"}


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


def rows(cid):
    if cid not in campaigns.CAMPAIGNS:
        raise ValueError(f"unknown campaign {cid!r}")
    st = campaigns.load_state()
    crm = store.read_json(CRM, {})
    ex = campaigns.excluded()
    now = dt.datetime.now(dt.timezone.utc)
    out = []
    for l in campaigns.leads(cid):
        s, m = st["leads"].get(l["provider_id"], {}), crm.get(l["provider_id"], {})
        chat = s.get("chat") or {}
        stopped = bool({l["provider_id"], l["url"], l["url"].rstrip("/")} & ex)
        last = max(filter(None, [s.get("viewed_at"), s.get("invited_at"), s.get("connected_at"), s.get("replied_at"),
                                 chat.get("last_at")]), key=_t, default="")
        out.append({
            "key": l["provider_id"], "status": status(s, m, now), "name": l["name"], "url": l["url"],
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
    return {"ok": True, "campaign": campaigns.CAMPAIGNS[cid]["name"], "statuses": MANUAL + [a for a in AUTO if a not in MANUAL],
            "rows": out, "at": _day(now.isoformat())}


def _neg(s):
    return "".join(chr(0x10FFFF - ord(c)) for c in s)  # descending string order inside an ascending sort


def apply(cid, changes, who="sheet"):
    """Hand edits from the sheet: [{key, status?, next_step?, notes?}]. Returns how many people changed."""
    leads = {l["provider_id"]: l for l in campaigns.leads(cid)}
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
