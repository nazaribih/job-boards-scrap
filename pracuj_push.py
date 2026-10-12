"""pracuj.pl from Nazarii's Mac: Cloudflare blocks the Railway server, so this machine scrapes the board and sends the
offers to jb-leads-queue (/api/offers); the research agent there processes them like the other boards.

  python pracuj_push.py           # scrape and send if the last successful send is 2+ days old
  python pracuj_push.py --force   # now, whatever the date

launchd starts it every hour while the Mac is on (~/Library/LaunchAgents/com.dealaris.pracuj-push.plist), so a run
that was due while the lid was closed happens soon after it opens. Plain HTTP only, which works from a home IP:
if pracuj.pl answers with a Cloudflare check instead, nothing opens and nothing is sent; it tries again 6 hours
later, and only a third blocked run in a row shows a notification.
Env (.env): SERVICE_TOKEN, optional JB_SERVICE_URL.
"""
import argparse
import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import requests

from judge import load_env

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "data" / "pracuj_push.json"
EVERY = dt.timedelta(days=2)
RETRY = dt.timedelta(hours=6)  # after a failed run
URL = "https://jb-leads-queue-production.up.railway.app"


def notify(text):
    subprocess.run(["osascript", "-e", f'display notification {json.dumps(text)} with title "pracuj.pl → Dealaris"'],
                   check=False, capture_output=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    load_env()
    now = dt.datetime.now(dt.timezone.utc)
    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    if not args.force:
        if not 8 <= dt.datetime.now().hour < 21:
            return
        if st.get("last_ok") and now - dt.datetime.fromisoformat(st["last_ok"]) < EVERY:
            return
        if st.get("last_try") and now - dt.datetime.fromisoformat(st["last_try"]) < RETRY:
            return
    st["last_try"] = now.isoformat()
    STATE.write_text(json.dumps(st))
    print(f"[{now:%Y-%m-%d %H:%M}] scraping pracuj.pl", flush=True)

    from jbscrap.pracuj import Blocked, pracuj_pl
    from run import KEYWORDS
    try:
        offers = pracuj_pl(KEYWORDS, max_pages=10, chrome=False)
    except Blocked as e:
        st["blocked"] = st.get("blocked", 0) + 1
        STATE.write_text(json.dumps(st))
        print(f"blocked: {e}", flush=True)
        if st["blocked"] >= 3:
            notify(f"pracuj.pl блокує запити вже {st['blocked']} рази поспіль; вакансії з інших бордів збираються як завжди")
        return
    st["blocked"] = 0
    print(f"{len(offers)} offers", flush=True)
    r = requests.post(f"{os.environ.get('JB_SERVICE_URL', URL).rstrip('/')}/api/offers", timeout=300,
                      headers={"Authorization": f"Bearer {os.environ['SERVICE_TOKEN']}"},
                      json={"source": "pracuj.pl", "offers": offers, "_who": "pracuj_push (Mac)"})
    res = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"error": r.text[:200]}
    print(r.status_code, res.get("text") or res.get("error"), flush=True)
    if r.ok and res.get("ok"):
        st["last_ok"] = now.isoformat()
        STATE.write_text(json.dumps(st))
    else:
        notify(f"Не вдалося надіслати вакансії: {res.get('error') or r.status_code}")


if __name__ == "__main__":
    main()
