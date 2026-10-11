"""Account safety shared by everything that touches LinkedIn (campaign, search queue, research agent), plus
Telegram alerts.

One pause per account in data/guard.json: any component that sees 401/403/429, a limit message or a bad
account status pauses that account for all of them. watch() checks each account's status in Unipile every hour
and pauses on anything but OK (checkpoint, restriction, disconnected).

  python guard.py status        # who is paused and why
  python guard.py resume nazarii

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID_NAZARII, TELEGRAM_CHAT_ID_HLEB (optional: alerts are skipped without them).
"""
import datetime as dt
import json
import os
import random
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import store

import requests

DATA = Path(__file__).resolve().parent / "data"
FILE = store.path("guard.json")
TZ = ZoneInfo("Europe/Warsaw")
ACCOUNTS = {"nazarii": "UNIPILE_ACCOUNT_ID_NAZARII", "hleb": "UNIPILE_ACCOUNT_ID_HLEB"}
NAMES = {"nazarii": "Назарій", "hleb": "Глеб"}


def _load():
    return store.read_json(FILE, {})


def _save(g):
    store.write_json(FILE, g)


def paused_until(account):
    p = (_load().get(account) or {}).get("paused_until")
    return p if p and dt.datetime.fromisoformat(p) > dt.datetime.now(TZ) else None


def pause(account, reason, hours=None, source=""):
    """Pause one account everywhere. Keeps the later end time if it is already paused."""
    g = _load()
    a = g.setdefault(account, {})
    until = dt.datetime.now(TZ) + dt.timedelta(hours=hours or random.randint(48, 72))
    if a.get("paused_until") and dt.datetime.fromisoformat(a["paused_until"]) >= until:
        return
    already = paused_until(account)
    a.update(paused_until=until.isoformat(), reason=reason, source=source, at=dt.datetime.now(TZ).isoformat())
    _save(g)
    if not already:
        link = resume_link(account)
        alert(f"⏸ <b>{NAMES[account]}: LinkedIn-акаунт на паузі</b> до {until:%d.%m %H:%M}.\n"
              f"Причина: {reason}\nДжерело: {source or '—'}\nКампанія, пошук і агент для цього акаунта стоять, "
              f"пауза зніметься сама." + (f'\n<a href="{link}">Зняти паузу зараз</a>' if link else ""), account=None)


def resume_link(account):
    host, token = os.environ.get("RAILWAY_PUBLIC_DOMAIN"), os.environ.get("SERVICE_TOKEN")
    return f"https://{host}/resume?account={account}&token={token}" if host and token else None


def resume(account):
    g = _load()
    g.setdefault(account, {})["paused_until"] = None
    _save(g)


def alert(text, account=None):
    """Telegram message to one founder, or to both when account is None. Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    for a in ([account] if account else list(ACCOUNTS)):
        chat = os.environ.get(f"TELEGRAM_CHAT_ID_{a.upper()}")
        if not chat:
            continue
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=20,
                          json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True})
        except Exception:
            pass


def check_accounts():
    """Unipile's view of each LinkedIn account: anything but OK (checkpoint, credentials, stopped) pauses it."""
    dsn = os.environ.get("UNIPILE_DSN", "").strip().rstrip("/")
    if not dsn:
        return
    base = dsn if dsn.startswith("http") else f"https://{dsn}"
    for account, var in ACCOUNTS.items():
        try:
            r = requests.get(f"{base}/api/v1/accounts/{os.environ[var]}", timeout=60,
                             headers={"X-API-KEY": os.environ["UNIPILE_API_KEY"], "accept": "application/json"})
            statuses = [s.get("status") for s in r.json().get("sources", [])] if r.ok else [f"HTTP {r.status_code}"]
        except Exception as e:
            statuses = [f"error {str(e)[:60]}"]
        g = _load()
        g.setdefault(account, {})["status"] = {"checked": dt.datetime.now(TZ).isoformat(), "sources": statuses}
        _save(g)
        if statuses and all(s == "OK" for s in statuses):
            continue
        if any(str(s).startswith(("HTTP 5", "error")) for s in statuses):
            continue  # Unipile itself is having a moment; try again next hour
        pause(account, f"статус акаунта в Unipile: {', '.join(map(str, statuses))}", hours=24, source="перевірка статусу")


def watch():
    while True:
        check_accounts()
        time.sleep(random.uniform(50, 70) * 60)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "resume":
        resume(sys.argv[2])
    print(json.dumps(_load(), ensure_ascii=False, indent=1))
