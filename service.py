"""Railway service: runs the human-paced LinkedIn queue without a laptop, rebuilds outputs, serves them, and pushes
company-level results to GitHub every 50 new decision makers.

  python service.py

Env:
  UNIPILE_DSN, UNIPILE_API_KEY, UNIPILE_ACCOUNT_ID_NAZARII, UNIPILE_ACCOUNT_ID_HLEB  - LinkedIn via Unipile
  QUEUE_CAP (default 15)                    - starting actions per account per day; the queue ramps it up itself
  SERVICE_TOKEN                             - required to read anything over HTTP
  GITHUB_TOKEN, GITHUB_REPO, GITHUB_BRANCH  - where company-level results are pushed (contents API)
  PORT                                      - set by Railway

State lives on a volume mounted at /data: on first boot the repo's data/ is copied there and data/ becomes a
symlink to it, so the queue's caches survive redeploys. With JB_STORE=db and DATABASE_URL, everything that names
people (campaign leads and state, decision makers, people and website caches) and the logs are in Postgres instead
(store.py); the files are copied in on the first start in that mode. Personal data is only served behind the token;
GitHub gets companies.csv and pipeline_stats.json.
"""
import base64
import csv
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import store

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
VOLUME = Path(os.environ.get("VOLUME_DIR", "/data"))
PUSH_EVERY = 50
SERVED = {"/status.json": "pipeline_status.json", "/decision_makers.csv": "decision_makers.csv",
          "/companies.csv": "companies.csv", "/queue.log": "queue.log", "/campaign.log": "campaign.log",
          "/campaign_state.json": "campaign_state.json", "/agent.out": "agent.out", "/guard.json": "guard.json"}


def log(msg):
    print(f"[service {dt.datetime.now():%m-%d %H:%M}] {msg}", flush=True)


def mount_volume():
    if not VOLUME.exists():
        log(f"no volume at {VOLUME}, keeping state in the container")
        return
    # copy whatever the image has that the volume doesn't (new files after a redeploy); never overwrite live state
    if not DATA.is_symlink():
        added = []
        for f in DATA.rglob("*"):  # files and folders the volume doesn't have yet, at any depth
            dst = VOLUME / f.relative_to(DATA)
            if store.in_db(str(f.relative_to(DATA))):
                continue  # kept in the database
            if f.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
            elif not dst.exists():
                shutil.copy2(f, dst)
                added.append(str(f.relative_to(DATA)))
            elif (str(f.relative_to(DATA)).startswith("seq_export/") or (str(f.relative_to(DATA)).startswith("campaigns/")
                  and f.name in ("leads.json", "sequences.json"))) and f.read_bytes() != dst.read_bytes():
                shutil.copy2(f, dst)  # campaign leads/messages and exported texts are inputs: the deployed copy wins
                added.append(str(f.relative_to(DATA)) + " (updated)")
        src, dst = DATA / "campaign_exclude.json", VOLUME / "campaign_exclude.json"  # people taken out by hand
        if not store.DB and src.exists() and dst.exists() and "campaign_exclude.json" not in added:
            live, new = json.loads(dst.read_text()), json.loads(src.read_text())
            if set(new) - set(live):
                dst.write_text(json.dumps(live + [x for x in new if x not in live], ensure_ascii=False, indent=1))
                added.append(f"campaign_exclude.json (+{len(set(new) - set(live))})")
        for f in (DATA / "seq_export").rglob("*.json"):  # message templates are code: the image's copy wins
            dst = VOLUME / f.relative_to(DATA)
            if dst.exists() and dst.read_bytes() != f.read_bytes():
                shutil.copy2(f, dst)
                added.append(str(f.relative_to(DATA)))
        for name in ("lang_cache.json", "pline_cache.json", "niche_cache.json", "judge_cache.json"):
            src, dst = DATA / name, VOLUME / name  # LLM results computed elsewhere: add the keys the volume lacks
            if not isinstance(store.path(name), Path):
                dst = store.path(name)  # kept in the database
            if src.exists() and dst.exists() and name not in added:
                live, new = json.loads(dst.read_text()), json.loads(src.read_text())
                missing = {k: v for k, v in new.items() if k not in live}
                if missing:
                    dst.write_text(json.dumps({**live, **missing}, ensure_ascii=False, indent=1))
                    added.append(f"{name} (+{len(missing)})")
        if added:
            log(f"copied to {VOLUME}: {', '.join(sorted(added))[:600]}")
    if not DATA.is_symlink():
        shutil.rmtree(DATA)
        DATA.symlink_to(VOLUME)


def people_count():
    p = store.path("decision_makers.csv")
    return sum(1 for r in csv.DictReader(p.open(encoding="utf-8")) if r["name"]) if p.exists() else 0


def push_to_github(n):
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO")
    if not token or not repo:
        log("GITHUB_TOKEN / GITHUB_REPO not set, skipping push")
        return
    branch = os.environ.get("GITHUB_BRANCH", "claude/sales-prospect-research-vlgnq1")
    status = json.loads(store.path("pipeline_status.json").read_text())
    status.pop("top", None)  # names people
    (DATA / "pipeline_stats.json").write_text(json.dumps(status, ensure_ascii=False, indent=1))
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    for name in ("companies.csv", "pipeline_stats.json"):
        url = f"https://api.github.com/repos/{repo}/contents/data/{name}"
        sha = s.get(url, params={"ref": branch}, timeout=60).json().get("sha")
        r = s.put(url, timeout=120, json={
            "message": f"Queue update: {n} decision makers ({name})\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
            "content": base64.b64encode((DATA / name).read_bytes()).decode(), "branch": branch,
            **({"sha": sha} if sha else {})})
        log(f"github {name}: {r.status_code}")


def refresh_loop():
    """Rebuild outputs every 20 minutes; push company-level results each time 50 more people have been found."""
    pushed_at = people_count()
    while True:
        time.sleep(20 * 60)
        try:
            subprocess.run([sys.executable, "refresh.py"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL, timeout=900)
        except Exception as e:
            log(f"refresh failed: {e}")
            continue
        n = people_count()
        if n - pushed_at >= PUSH_EVERY:
            try:
                push_to_github(n)
                pushed_at = n
            except Exception as e:
                log(f"github push failed: {e}")


def run_logged(args, source, timeout=None):
    """Run a script, its output going to the log store (jb.log, or data/queue.log / agent.out)."""
    p = subprocess.Popen([sys.executable, "-u", *args], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, errors="replace")
    killer = threading.Timer(timeout, p.kill) if timeout else None
    if killer:
        killer.start()
    for line in p.stdout:
        store.log_line(source, line)
    code = p.wait()
    if killer:
        killer.cancel()
    return code


def queue_loop():
    cap = os.environ.get("QUEUE_CAP", "15")
    while True:
        code = run_logged(["linkedin_queue.py", "--cap", cap], "queue")
        log(f"queue exited with {code}")
        time.sleep(30 * 60 if code else 6 * 3600)  # crashed: retry soon; finished: look for new work later


def _actions():
    import control
    return {
        "/api/campaign": lambda b: control.toggle(b.get("campaign", ""), bool(b.get("enabled"))),
        "/api/lead/stop": lambda b: control.stop_lead(b.get("q", ""), True),
        "/api/lead/resume": lambda b: control.stop_lead(b.get("q", ""), False),
        "/api/lead/add": lambda b: control.add_lead(b.get("campaign", ""), b.get("url", ""), b.get("name", ""), b.get("company", ""),
                                                    b.get("role", ""), b.get("track", ""), b.get("note", "")),
        "/api/message": lambda b: control.set_message(b.get("campaign", ""), b.get("key", ""), b.get("text", "")),
    }


ACTIONS = {"/api/campaign": None, "/api/lead/stop": None, "/api/lead/resume": None, "/api/lead/add": None, "/api/message": None}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        token = os.environ.get("SERVICE_TOKEN")
        if self.path == "/health":
            return self._send(200, b"ok", "text/plain")
        path, _, query = self.path.partition("?")
        given = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("token")
        cookie = next((c.split("=", 1)[1] for c in (self.headers.get("Cookie") or "").split("; ") if c.startswith("t=")), None)
        authed = token and (self.headers.get("Authorization") == f"Bearer {token}" or token in (given, cookie))
        if path == "/resume":
            if not authed:
                return self._send(401, b"unauthorized", "text/plain")
            acc = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("account", "")
            import guard
            if acc not in guard.ACCOUNTS:
                return self._send(404, b"unknown account", "text/plain")
            until = guard.paused_until(acc)
            body = (f'<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>Зняти паузу</title><body style="font:16px system-ui;max-width:420px;margin:40px auto;padding:0 18px">'
                    + (f'<h2>{guard.NAMES[acc]}: пауза до {until[:16].replace("T", " ")}</h2><p>Кампанія, пошук і агент для цього акаунта стоять. '
                       f'Знімайте паузу, лише якщо проблему в LinkedIn вирішено.</p><form method="post" action="/api/resume?account={acc}">'
                       f'<button style="font:600 16px system-ui;padding:12px 18px;border-radius:8px;border:0;background:#2a78d6;color:#fff">Зняти паузу</button></form>'
                       if until else f'<h2>{guard.NAMES[acc]} не на паузі</h2><p>Акаунт працює.</p>')
                    + '<p><a href="/">Відкрити дашборд</a></p>').encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if given == token:
                self.send_header("Set-Cookie", f"t={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=31536000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if path in ("/api/summary", "/api/lead", "/api/messages"):
            if not authed:
                return self._send(401, b"unauthorized", "text/plain")
            import control
            from urllib.parse import parse_qs
            q = {k: v[0] for k, v in parse_qs(query).items()}
            try:
                res = (control.summary() if path == "/api/summary" else control.find(q.get("q", "")) if path == "/api/lead"
                       else control.messages(q.get("campaign", "")))
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
            return self._json(200, res)
        if path == "/api/campaign.json":
            if not authed:
                return self._send(401, b"unauthorized", "text/plain")
            import dashboard
            return self._send(200, json.dumps(dashboard.data(), ensure_ascii=False).encode(), "application/json")
        if path in ("/", "/dashboard"):
            if not authed:
                return self._send(401, "Відкрийте посилання з ?token=…".encode(), "text/plain")
            import dashboard
            body = dashboard.page().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if given == token:
                self.send_header("Set-Cookie", f"t={token}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=31536000")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        if not authed:
            return self._send(401, b"unauthorized", "text/plain")
        name = SERVED.get(path)
        if store.DB and name in store.LOGS:
            body = store.log_tail(store.LOGS[name]).encode()
        elif name == "campaign_state.json":
            import campaigns
            body = json.dumps(campaigns.load_state(), ensure_ascii=False).encode()
        elif name and store.path(name).exists():
            body = store.path(name).read_bytes()
        else:
            return self._send(404, b"not found", "text/plain")
        if name == "queue.log":
            body = body[-20000:]
        self._send(200, body, "text/csv" if name.endswith(".csv") else "application/json" if name.endswith(".json")
                   else "text/plain")

    def do_POST(self):
        token = os.environ.get("SERVICE_TOKEN")
        path, _, query = self.path.partition("?")
        cookie = next((c.split("=", 1)[1] for c in (self.headers.get("Cookie") or "").split("; ") if c.startswith("t=")), None)
        if not token or not (cookie == token or self.headers.get("Authorization") == f"Bearer {token}"):
            return self._send(401, b"unauthorized", "text/plain")
        if path == "/api/resume":
            import guard
            acc = dict(p.split("=", 1) for p in query.split("&") if "=" in p).get("account", "")
            if acc not in guard.ACCOUNTS:
                return self._send(404, b"unknown account", "text/plain")
            guard.resume(acc)
            log(f"pause lifted by hand for {acc}")
            guard.alert(f"▶️ <b>{guard.NAMES[acc]}</b>: паузу знято вручну, акаунт знову працює.", account=None)
            self.send_response(303)
            self.send_header("Location", f"/resume?account={acc}")
            self.end_headers()
            return
        if path.startswith("/api/") and path in ACTIONS:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"ok": False, "error": "тіло запиту має бути JSON"})
            who = body.pop("_who", "дашборд")
            try:
                res = _actions()[path](body)
            except ValueError as e:
                return self._json(400, {"ok": False, "error": str(e)})
            log(f"{who}: {path} {json.dumps(body, ensure_ascii=False)[:200]} -> {res.get('text', 'ok')}")
            return self._json(200, res)
        return self._send(404, b"not found", "text/plain")

    def _json(self, code, obj):
        return self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def campaign_loop():
    """LinkedIn outreach for the leads. Off unless CAMPAIGN_ENABLED=true."""
    if os.environ.get("CAMPAIGN_ENABLED", "").lower() != "true":
        log("campaign disabled (set CAMPAIGN_ENABLED=true to start it)")
        return
    import campaign
    campaign.Campaign().run()


def agent_loop():
    """Research agent: new companies and leads, every 6 hours (agent.py)."""
    time.sleep(5 * 60)
    while True:
        try:
            run_logged(["agent.py", "tick"], "agent", timeout=6 * 3600)
        except Exception as e:
            log(f"agent tick failed: {e}")
        time.sleep(6 * 3600)


def main():
    mount_volume()
    if store.DB:
        import campaigns
        done = store.migrate_from_files(campaigns.migration_files())
        log("data in Postgres (schema jb)" + (f"; copied from files: {json.dumps(done)[:500]}" if done else ""))
    threading.Thread(target=queue_loop, daemon=True).start()
    threading.Thread(target=refresh_loop, daemon=True).start()
    threading.Thread(target=campaign_loop, daemon=True).start()
    threading.Thread(target=agent_loop, daemon=True).start()
    import guard
    threading.Thread(target=guard.watch, daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    log(f"serving on :{port}")
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
