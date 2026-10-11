"""Management actions shared by the dashboard and the Telegram bot (through service.py's /api/* routes).

Every action returns a dict that is safe to show to the user; errors are raised as ValueError with a message in
Ukrainian. Nothing here talks to LinkedIn: actions change what the campaign does on its next pass.
"""
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import campaigns
import guard

DATA = Path(__file__).resolve().parent / "data"
TZ = ZoneInfo("Europe/Warsaw")
NAMES = {"nazarii": "Назарій", "hleb": "Глеб"}
STAGE_UA = {"queued": "у черзі", "viewed": "профіль переглянуто", "invited": "чекає прийняття", "connected": "прийняв",
            "sent_m1": "надіслано 1-ше", "sent_f1": "надіслано 2-ге", "sent_f2": "надіслано 3-тє", "done": "сіквенс завершено",
            "replied": "відповів", "skipped": "пропущено", "expired": "не прийняв інвайт"}


def _state():
    try:
        return json.loads((DATA / "campaign_state.json").read_text())
    except (FileNotFoundError, ValueError):
        return {"leads": {}, "accounts": {}, "events": []}


def _cid(x):
    x = (x or "").strip().lower().replace(" ", "")
    for cid, c in campaigns.CAMPAIGNS.items():
        if x in (cid, c["name"].lower().replace(" ", ""), cid.replace("_", "")) or (x and c["name"].lower().replace(" ", "").startswith(x)):
            return cid
    raise ValueError(f"немає кампанії «{x}». Є: " + ", ".join(campaigns.CAMPAIGNS))


def summary():
    st = _state()
    S = st["leads"]
    today = dt.datetime.now(TZ).date().isoformat()
    out = {"campaigns": [], "accounts": {}}
    ex = campaigns.excluded()
    for c in campaigns.summary():
        ls = campaigns.leads(c["id"])
        stg = lambda l: S.get(l["provider_id"], {})
        n = lambda f: sum(1 for l in ls if f(stg(l)))
        out["campaigns"].append({**c, "stopped_leads": sum(1 for l in ls if l["url"] in ex or l["provider_id"] in ex),
                                 "viewed": n(lambda s: s.get("viewed_at")), "invited": n(lambda s: s.get("invited_at")),
                                 "accepted": n(lambda s: s.get("connected_at")), "messaged": n(lambda s: s.get("sent")),
                                 "replied": n(lambda s: s.get("stage") == "replied"),
                                 "queued": n(lambda s: s.get("stage", "queued") == "queued")})
    for a in campaigns.CAMPAIGNS["job_boards"]["accounts"] + campaigns.CAMPAIGNS["websummit"]["accounts"]:
        ac = st["accounts"].get(a, {})
        used = ac.get("used", {}) if ac.get("day") == today else {}
        out["accounts"][a] = {"in_use": a in campaigns.accounts_in_use(), "paused_until": guard.paused_until(a),
                              "invites": [used.get("invite", 0), campaigns.invite_cap(a, ac)], "views": used.get("view", 0),
                              "messages": used.get("message", 0)}
    return out


def toggle(campaign, enabled):
    cid = _cid(campaign)
    campaigns.set_enabled(cid, enabled)
    c = campaigns.CAMPAIGNS[cid]
    return {"ok": True, "text": f"«{c['name']}» {'запущено' if enabled else 'зупинено'}." +
            (" Почне в найближчі робочі години (будні 9:00–17:30)." if enabled else " Нові дії не відбуватимуться; уже надіслане не відкликається.")}


def find(query):
    S = _state()["leads"]
    ex = campaigns.excluded()
    res = []
    for l in campaigns.find_lead(query)[:10]:
        s = S.get(l["provider_id"], {})
        res.append({"name": l["name"], "company": l["company"], "url": l["url"], "campaign": campaigns.CAMPAIGNS[l["campaign"]]["name"],
                    "owner": l["owner"], "stage": STAGE_UA.get(s.get("stage", "queued"), s.get("stage", "")),
                    "stopped": l["url"] in ex or l["provider_id"] in ex, "provider_id": l["provider_id"]})
    return {"ok": True, "leads": res}


def stop_lead(query, stop=True):
    found = campaigns.find_lead(query)
    if not found:
        raise ValueError("нікого не знайдено")
    if len(found) > 1 and not query.strip().lower().startswith("http"):
        names = ", ".join(f"{l['name']} ({l['company']})" for l in found[:6])
        raise ValueError(f"знайдено {len(found)}: {names}. Уточніть або дайте посилання на LinkedIn.")
    l = found[0]
    campaigns.set_excluded([l["url"], l["provider_id"]], stop)
    return {"ok": True, "text": f"{l['name']} ({l['company']}): " + ("зупинено, кампанія більше нічого не надішле." if stop else "повернуто в кампанію.")}


def add_lead(campaign, url, name="", company="", role="", track="", note=""):
    cid = _cid(campaign)
    l = campaigns.add_lead(cid, url, name=name, company=company, role=role, track=track, note=note)
    c = campaigns.CAMPAIGNS[cid]
    return {"ok": True, "text": f"{l['name']} додано в «{c['name']}» (варіант {l['track']}: {c['tracks'][l['track']]}). "
                                f"Пише {', '.join(NAMES.get(a, a) for a in c['accounts'])}: перегляд профілю, інвайт без нотатки, після прийняття повідомлення."}


def set_message(campaign, key, text):
    cid = _cid(campaign)
    c = campaigns.CAMPAIGNS[cid]
    track, lang, stage = (key.split("-") + ["", "", ""])[:3]
    if track not in c["tracks"] or lang not in c["langs"] or stage not in c["stages"]:
        raise ValueError(f"ключ має бути трек-мова-етап, напр. A-{c['langs'][0]}-m1. Треки: {', '.join(c['tracks'])}; "
                         f"мови: {', '.join(c['langs'])}; етапи: {', '.join(c['stages'])}")
    if c.get("invite_note") and stage == "m1":  # the text is the invitation note: LinkedIn refuses it past the limit
        fill = lambda l: (text.strip().replace("{first_name}", l["first_name"]).replace("{company}", l.get("company_short") or "")
                          .replace("{personal_line}", l.get("personal_line", "")).replace("{note}", l.get("note", "")))
        ls = [l for l in campaigns.leads(cid) if l["track"] == track and l["lang"] == lang]
        over = [l for l in ls if len(fill(l)) > campaigns.NOTE_MAX]
        if over:
            raise ValueError(f"текст іде в інвайті, а LinkedIn приймає до {campaigns.NOTE_MAX} символів. З іменами й компаніями "
                             f"{len(over)} з {len(ls)} лідів виходять за ліміт (найдовший {max(len(fill(l)) for l in over)}). Скоротіть текст.")
    campaigns.set_message(cid, key, text.strip())
    return {"ok": True, "text": f"Текст {key} у «{c['name']}» оновлено. Діє з наступного надісланого повідомлення."}


def messages(campaign):
    cid = _cid(campaign)
    return {"ok": True, "campaign": campaigns.CAMPAIGNS[cid]["name"], "tracks": campaigns.CAMPAIGNS[cid]["tracks"],
            "messages": {k: v for k, v in campaigns.messages(cid).items() if v.strip()}}
