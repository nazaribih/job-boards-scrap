"""Where the outreach data lives.

JB_STORE=db (Railway, with DATABASE_URL): Postgres, schema jb, next to the outreach-helper tables but separate from them.
  campaigns, messages, leads, excluded   who each campaign writes to and what it says; changed from the dashboard/Telegram
  lead_state, accounts, events           what the sender did; written only by campaign.Campaign
  docs                                   personal research files (people found, decision makers, seeds, name caches,
                                         agent and queue state), each kept whole under its file name
  log                                    the service's logs (sender, LinkedIn queue, agent)
Otherwise (local runs, or switched back): the same data in the JSON/CSV files under data/, as before.
Company-level research (companies, vacancies, judge/niche caches, LinkedIn company cache, pool) is files either way.

On its first start in db mode the service copies the files into the database (migrate_from_files) before anything
runs, so no write can land in a file after the copy.
"""
import datetime as dt
import io
import json
import os
import threading
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TZ = ZoneInfo("Europe/Warsaw")
DB = os.environ.get("DATABASE_URL") if os.environ.get("JB_STORE") == "db" else None

# research files that name people: in the database in db mode
PERSONAL = {"people_cache.json", "site_people.json", "seed_people.json", "decision_makers.csv", "lang_cache.json",
            "pline_cache.json", "voc_cache.json", "exclude_outreach_helper.json", "icp_hits.json", "outreach_export.json",
            "agent_queries.json", "agent_log.json", "agent_state.json", "linkedin_queue_state.json", "guard.json",
            "pipeline_status.json"}
LOGS = {"campaign.log": "campaign", "queue.log": "queue", "agent.out": "agent"}

SCHEMA = """
create schema if not exists jb;
create table if not exists jb.campaigns (id text primary key, enabled boolean not null,
  updated_at timestamptz not null default now(), updated_by text);
create table if not exists jb.messages (campaign text not null, key text not null, body text not null,
  updated_at timestamptz not null default now(), updated_by text, primary key (campaign, key));
create table if not exists jb.leads (campaign text not null, key text not null, source text not null, pos int not null,
  provider_id text, public_identifier text, name text, company text, owner text, track text, lang text, score real,
  data jsonb not null, added_at timestamptz not null default now(), primary key (campaign, key));
create table if not exists jb.excluded (key text primary key, added_at timestamptz not null default now(), added_by text);
create table if not exists jb.lead_state (provider_id text primary key, stage text, data jsonb not null,
  updated_at timestamptz not null default now());
create table if not exists jb.accounts (account text primary key, data jsonb not null,
  updated_at timestamptz not null default now());
create table if not exists jb.events (id bigserial primary key, t timestamptz not null, account text, kind text,
  lead text, name text, company text, note text);
create index if not exists events_t on jb.events (t);
create table if not exists jb.docs (name text primary key, body text not null, updated_at timestamptz not null default now());
create table if not exists jb.log (id bigserial primary key, t timestamptz not null default now(), source text, line text);
create index if not exists log_source on jb.log (source, id);
"""

_local = threading.local()
_ready = False


def _conn():
    import psycopg
    global _ready
    c = getattr(_local, "conn", None)
    if c is None or c.closed or c.broken:
        c = _local.conn = psycopg.connect(DB, autocommit=True, connect_timeout=15)
        if not _ready:
            c.execute(SCHEMA)
            _ready = True
    return c


def q(sql, args=None):
    """Run one statement; returns rows for a query. Retries once on a dropped connection."""
    import psycopg
    for attempt in (0, 1):
        try:
            cur = _conn().execute(sql, args)
            return cur.fetchall() if cur.description else None
        except psycopg.OperationalError:
            _local.conn = None
            if attempt:
                raise


def tx(statements):
    """Several statements in one transaction: [(sql, args), ...]."""
    c = _conn()
    with c.transaction():
        for sql, args in statements:
            c.execute(sql, args)


# ─── documents: a file by name ───────────────────────────────────────────────────────────────────────────────────

class _Writer(io.StringIO):
    def __init__(self, doc):
        super().__init__()
        self.doc = doc

    def close(self):
        if not self.closed:
            self.doc.write_text(self.getvalue())
        super().close()


class Doc:
    """Stands in for a data file: read_text / write_text / exists / open, stored in jb.docs."""

    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"jb.docs/{self.name}"

    def exists(self):
        return bool(q("select 1 from jb.docs where name = %s", (self.name,)))

    def read_text(self, encoding=None):
        r = q("select body from jb.docs where name = %s", (self.name,))
        if not r:
            raise FileNotFoundError(str(self))
        return r[0][0]

    def read_bytes(self):
        return self.read_text().encode()

    def write_text(self, text, encoding=None):
        q("insert into jb.docs (name, body) values (%s, %s) on conflict (name) do update set body = excluded.body, "
          "updated_at = now()", (self.name, text))

    def open(self, mode="r", newline=None, encoding=None):
        return _Writer(self) if "w" in mode else io.StringIO(self.read_text())


def in_db(name):
    """Whether a file under data/ (by relative name) lives in the database in db mode."""
    parts = name.split("/")
    return bool(DB) and (name in PERSONAL or name in LOGS or parts[0] == "local_sources" or name in (
        "campaign_leads.json", "campaign_state.json", "campaign_control.json", "campaign_exclude.json")
        or (parts[0] == "campaigns" and parts[-1] in ("leads.json", "added.json", "messages_live.json")))


def path(name):
    """A data file by its name under data/: the file itself, or its database copy when it names people."""
    if DB and (name in PERSONAL or name.startswith("local_sources/")):
        return Doc(name)
    return DATA / name


def read_json(p, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        return default


def write_json(p, obj):
    text = json.dumps(obj, ensure_ascii=False, indent=1)
    if isinstance(p, Doc):
        return p.write_text(text)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(p)


# ─── logs ────────────────────────────────────────────────────────────────────────────────────────────────────────

def log_line(source, line):
    if not DB:
        with open(DATA / {v: k for k, v in LOGS.items()}[source], "a") as f:
            f.write(line.rstrip("\n") + "\n")
        return
    try:
        q("insert into jb.log (source, line) values (%s, %s)", (source, line.rstrip("\n")))
        if hash(line) % 500 == 0:
            q("delete from jb.log where t < now() - interval '45 days'")
    except Exception as e:  # a log line is never worth crashing the sender
        print(f"[store] log write failed: {e}", flush=True)


def log_tail(source, n=400):
    rows = q("select line from (select id, line from jb.log where source = %s order by id desc limit %s) x order by id",
             (source, n))
    return "\n".join(r[0] for r in rows) + "\n"


# ─── campaigns: switches, texts, people ──────────────────────────────────────────────────────────────────────────

def enabled():
    return {cid: on for cid, on in q("select id, enabled from jb.campaigns")}


def set_enabled(cid, on, who=""):
    q("insert into jb.campaigns (id, enabled, updated_by) values (%s, %s, %s) on conflict (id) do update set "
      "enabled = excluded.enabled, updated_by = excluded.updated_by, updated_at = now()", (cid, bool(on), who))


def messages(cid):
    return {k: v for k, v in q("select key, body from jb.messages where campaign = %s", (cid,))}


def set_message(cid, key, text, who=""):
    q("insert into jb.messages (campaign, key, body, updated_by) values (%s, %s, %s, %s) on conflict (campaign, key) "
      "do update set body = excluded.body, updated_by = excluded.updated_by, updated_at = now()", (cid, key, text, who))


def _lead_key(l):
    return l.get("public_identifier") or l["provider_id"]


def _lead_row(cid, l, source, pos):
    return (cid, _lead_key(l), source, pos, l.get("provider_id"), l.get("public_identifier"), l.get("name"),
            l.get("company"), l.get("owner"), l.get("track"), l.get("lang"), l.get("score"), json.dumps(l, ensure_ascii=False))


INSERT_LEAD = ("insert into jb.leads (campaign, key, source, pos, provider_id, public_identifier, name, company, owner, track, "
               "lang, score, data) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) on conflict (campaign, key) "
               "do update set source = excluded.source, pos = excluded.pos, provider_id = excluded.provider_id, "
               "public_identifier = excluded.public_identifier, name = excluded.name, company = excluded.company, "
               "owner = excluded.owner, track = excluded.track, lang = excluded.lang, score = excluded.score, data = excluded.data")


def leads(cid, source=None):
    """A campaign's people in their saved order (generated or imported first, then added by hand)."""
    rows = q("select data from jb.leads where campaign = %s" + (" and source = %s" if source else "")
             + " order by source = 'manual', pos", (cid, source) if source else (cid,))
    return [r[0] for r in rows]


def set_leads(cid, items, source):
    """Replace every lead of one source in a campaign (a rebuilt or re-imported list); hand-added people stay."""
    seen, stmts = set(), [("delete from jb.leads where campaign = %s and source = %s", (cid, source))]
    for i, l in enumerate(items):
        if _lead_key(l) in seen:
            continue
        seen.add(_lead_key(l))
        stmts.append((INSERT_LEAD, _lead_row(cid, l, source, i)))
    tx(stmts)


def add_lead(cid, lead):
    n = q("select coalesce(max(pos), -1) + 1 from jb.leads where campaign = %s and source = 'manual'", (cid,))[0][0]
    q(INSERT_LEAD, _lead_row(cid, lead, "manual", n))


def excluded():
    return {r[0] for r in q("select key from jb.excluded")}


def set_excluded(keys, on, who=""):
    tx([("insert into jb.excluded (key, added_by) values (%s, %s) on conflict do nothing", (k, who)) if on
        else ("delete from jb.excluded where key = %s", (k,)) for k in keys])


# ─── sender state ────────────────────────────────────────────────────────────────────────────────────────────────

_snap = {"leads": {}, "accounts": {}, "extra": None, "last_t": None}


def load_state(events=3000, track=False):
    """The sender's state in the shape campaign_state.json had: {"leads", "accounts", "events", ...}. track=True (the
    sender only) remembers what was loaded so save_state writes only what changed."""
    st = {"leads": {pid: d for pid, d in q("select provider_id, data from jb.lead_state")},
          "accounts": {a: d for a, d in q("select account, data from jb.accounts")}, "events": []}
    if events:
        rows = q("select t, account, kind, lead, name, company, note from (select * from jb.events order by id desc "
                 "limit %s) x order by id", (events,))
        st["events"] = [{"t": t.astimezone(TZ).isoformat(),
                         "account": a, "kind": k, "lead": ld, "name": n, "company": c, "note": nt}
                        for t, a, k, ld, n, c, nt in rows]
    extra = read_json(Doc("campaign_state_extra.json"), {})
    st.update(extra)
    if track:
        _snap.update(leads={k: json.dumps(v, sort_keys=True) for k, v in st["leads"].items()},
                     accounts={k: json.dumps(v, sort_keys=True) for k, v in st["accounts"].items()},
                     extra=json.dumps(extra, sort_keys=True),
                     last_t=q("select max(t) from jb.events")[0][0])
    return st


def save_state(state):
    """Write what changed since the last load/save: lead states, account counters, new events, other keys."""
    stmts, new_leads, new_accounts = [], {}, {}
    for pid, s in state["leads"].items():
        j = json.dumps(s, sort_keys=True)
        if _snap["leads"].get(pid) != j:
            stmts.append(("insert into jb.lead_state (provider_id, stage, data) values (%s, %s, %s) on conflict "
                          "(provider_id) do update set stage = excluded.stage, data = excluded.data, updated_at = now()",
                          (pid, s.get("stage"), j)))
            new_leads[pid] = j
    for a, s in state["accounts"].items():
        j = json.dumps(s, sort_keys=True)
        if _snap["accounts"].get(a) != j:
            stmts.append(("insert into jb.accounts (account, data) values (%s, %s) on conflict (account) do update set "
                          "data = excluded.data, updated_at = now()", (a, j)))
            new_accounts[a] = j
    last = _snap["last_t"]
    fresh = [e for e in state.get("events", []) if last is None or dt.datetime.fromisoformat(e["t"]) > last]
    for e in fresh:
        stmts.append(("insert into jb.events (t, account, kind, lead, name, company, note) values (%s, %s, %s, %s, %s, %s, %s)",
                      (e["t"], e.get("account"), e.get("kind"), e.get("lead"), e.get("name"), e.get("company"), e.get("note"))))
    extra = {k: v for k, v in state.items() if k not in ("leads", "accounts", "events")}
    ej = json.dumps(extra, sort_keys=True)
    if ej != _snap["extra"]:
        stmts.append(("insert into jb.docs (name, body) values ('campaign_state_extra.json', %s) on conflict (name) do update "
                      "set body = excluded.body, updated_at = now()", (json.dumps(extra, ensure_ascii=False),)))
    if not stmts:
        return
    tx(stmts)
    _snap["leads"].update(new_leads)
    _snap["accounts"].update(new_accounts)
    _snap["extra"] = ej
    if fresh:
        _snap["last_t"] = max(dt.datetime.fromisoformat(e["t"]) for e in fresh)


# ─── one-time copy of the files ──────────────────────────────────────────────────────────────────────────────────

def migrate_from_files(campaign_files):
    """Copy everything personal from data/ into the database, once. campaign_files: {cid: (leads file, live texts file,
    added file)} from campaigns.py. Returns a summary, or None if it already ran."""
    if q("select 1 from jb.docs where name = '_migrated'"):
        return None
    done = {}

    def load(name, default):
        p = DATA / name
        return json.loads(p.read_text()) if p.exists() else default

    for name in sorted(PERSONAL):
        p = DATA / name
        if p.exists():
            Doc(name).write_text(p.read_text())
            done[name] = p.stat().st_size
    for p in sorted((DATA / "local_sources").glob("*")) if (DATA / "local_sources").exists() else []:
        Doc(f"local_sources/{p.name}").write_text(p.read_text(encoding="utf-8-sig"))
        done[f"local_sources/{p.name}"] = p.stat().st_size
    for p in sorted((ROOT / "ws-leads").glob("*.csv")):  # event lists as received, people without LinkedIn included
        Doc(f"ws-leads/{p.name}").write_text(p.read_text(encoding="utf-8-sig"))
        done[f"ws-leads/{p.name}"] = p.stat().st_size
    for cid, (leads_file, live_file, added_file) in campaign_files.items():
        set_leads(cid, load(leads_file, []), "import" if cid != "job_boards" else "prepare")
        for l in load(added_file, []):
            add_lead(cid, l)
        for k, v in load(live_file, {}).items():
            set_message(cid, k, v, "files")
        done[f"leads:{cid}"] = len(leads(cid))
    for cid, on in load("campaign_control.json", {}).get("enabled", {}).items():
        set_enabled(cid, on, "files")
    set_excluded(load("campaign_exclude.json", []), True, "files")
    done["excluded"] = len(excluded())
    st = load("campaign_state.json", None)
    q("delete from jb.events")  # an interrupted earlier attempt may have left some; they come from the file again
    q("delete from jb.log")
    if st:
        save_state(st)  # snapshot is empty, so everything is written
        done["lead_state"], done["events"] = len(st["leads"]), len(st.get("events", []))
    for name, source in LOGS.items():
        p = DATA / name
        if p.exists():
            lines = p.read_text(errors="replace").splitlines()[-3000:]
            tx([("insert into jb.log (source, line) values (%s, %s)", (source, l)) for l in lines])
    Doc("_migrated").write_text(json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(), **done}))
    return done
