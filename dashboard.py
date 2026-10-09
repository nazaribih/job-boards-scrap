"""Dashboard for the job-board LinkedIn campaign, served by service.py.

  /?token=...           the page (the token is kept in a cookie after the first visit)
  /api/campaign.json    the data the page polls every 60 s

data() reads data/campaign_leads.json and data/campaign_state.json; page() is a static shell that renders it.
"""
import collections
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

DATA = Path(__file__).resolve().parent / "data"
TZ = ZoneInfo("Europe/Warsaw")
STEPS = [("queued", "Усього в кампанії"), ("viewed", "Переглянули профіль"), ("invited", "Надіслали інвайт"), ("connected", "Прийняли"),
         ("m1", "Повідомлення 1"), ("f1", "Повідомлення 2"), ("f2", "Повідомлення 3"), ("f3", "Повідомлення 4"), ("replied", "Відповіли")]
STAGE_LABEL = {"queued": "У черзі", "viewed": "Профіль переглянуто", "invited": "Чекає прийняття", "connected": "Прийняв, скоро повідомлення",
               "sent_m1": "Надіслано 1 з 4", "sent_f1": "Надіслано 2 з 4", "sent_f2": "Надіслано 3 з 4", "done": "Сіквенс завершено",
               "replied": "Відповів", "skipped": "Пропущено", "expired": "Інвайт не прийняли"}


def _load(name, default):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else default


def _reached(s):
    st, out = s.get("stage", "queued"), {"queued"}
    if s.get("viewed_at") or st != "queued":
        out.add("viewed")
    if s.get("invited_at"):
        out.add("invited")
    if s.get("connected_at"):
        out |= {"connected", "invited"}
    out |= set(s.get("sent", {}))
    if st == "replied":
        out.add("replied")
    return out


SOURCE_LABEL = {"job_boards": "Job-борди", "booking": "Сайти з Calendly / book a demo", "calling": "Скан телефонних продажів",
                "seed": "Ранній скан LinkedIn", "icp": "Пошук агента в LinkedIn", "pool": "ЛПРи з пулу компаній"}


def research(leads):
    """What the research agent has found and done: the company pool, where leads come from, its own log."""
    pool = _load("company_pool.json", [])
    li = _load("unipile_cache.json", {})
    pc = _load("people_cache.json", {})
    st = _load("agent_state.json", {})
    qs = _load("agent_queries.json", [])
    src = collections.Counter(s for c in pool for s in c.get("sources", []))
    fit = [c for c in pool if c.get("verdict") == "fit"]
    last = st.get("last_scrape")
    return {
        "pool_total": len(pool), "by_source": [[SOURCE_LABEL.get(k, k), n] for k, n in src.most_common()],
        "verdicts": [["Підходить", sum(c.get("verdict") == "fit" for c in pool)], ["Можливо", sum(c.get("verdict") == "maybe" for c in pool)],
                     ["Не підходить", sum(c.get("verdict") == "no" for c in pool)]],
        "fit_progress": [["Компаній, що підходять", len(fit)], ["Знайдено на LinkedIn", sum(c["key"] in li for c in fit)],
                         ["Шукали ЛПРів", sum(c["key"] in pc for c in fit)],
                         ["Мають ЛПРа в кампанії", len({l["company_key"] for l in leads if l.get("source") in ("pool", None, "job_boards")} & {c["key"] for c in fit})]],
        "lead_sources": [[SOURCE_LABEL.get(k, k), n] for k, n in collections.Counter(l.get("source", "job_boards") for l in leads).most_common()],
        "queue": st.get("queue", {}), "last_scrape": last,
        "next_scrape": (dt.datetime.fromisoformat(last) + dt.timedelta(days=3)).isoformat() if last else None,
        "queries": [{"q": q["keywords"], "status": q["status"], "results": q.get("results")} for q in qs[-30:][::-1]],
        "log": _load("agent_log.json", [])[-40:][::-1],
    }


def data():
    leads = _load("campaign_leads.json", [])
    state = _load("campaign_state.json", {"leads": {}, "accounts": {}, "events": []})
    S, now = state["leads"], dt.datetime.now(TZ)
    today = now.date().isoformat()
    reach = {l["provider_id"]: _reached(S.get(l["provider_id"], {})) for l in leads}

    accounts = {}
    guards = _load("guard.json", {})
    for a in ("nazarii", "hleb"):
        mine = [l for l in leads if l["owner"] == a]
        n = lambda k: sum(k in reach[l["provider_id"]] for l in mine)
        ac = state["accounts"].get(a, {})
        used = ac.get("used", {}) if ac.get("day") == today else {}
        paused = None
        g = guards.get(a) or {}
        if g.get("paused_until") and dt.datetime.fromisoformat(g["paused_until"]) > now:
            paused = max(filter(None, [paused, g["paused_until"]]))
            ac = {**ac, "last_error": f"{g.get('reason')} ({g.get('source')})"}
        nxt = min((S[l["provider_id"]]["next_at"] for l in mine if S.get(l["provider_id"], {}).get("next_at")), default=None)
        accounts[a] = {"leads": len(mine), "viewed": n("viewed"), "invited": n("invited"), "connected": n("connected"),
                       "messaged": n("m1"), "replied": n("replied"), "paused_until": paused, "last_error": ac.get("last_error"),
                       "today": {"view": [used.get("view", 0), 15], "invite": [used.get("invite", 0), ac.get("invite_cap", 12)],
                                 "message": [used.get("message", 0), 25]}, "next_at": nxt,
                       "unipile_status": ((guards.get(a) or {}).get("status") or {}).get("sources")}

    voc = _load("voc_cache.json", {})
    rows = []
    for l in leads:
        s = S.get(l["provider_id"], {})
        rows.append({"name": l["name"], "url": l["url"], "role": l["role"] or l.get("headline", "")[:80], "company": l["company"], "niche": l["niche"],
                     "source": l.get("source", "job_boards"), "pid": l["provider_id"], "first_name": (l["lang"] == "uk" and (voc.get(l["name"]) or {}).get("ukrainian") and voc[l["name"]]["name"]) or l["first_name"],
                     "personal_line": l["personal_line"], "company_short": l.get("company_short", ""), "sent": s.get("sent", {}),
                     "track": l["track"], "lang": l["lang"], "owner": l["owner"], "score": l["score"],
                     "stage": s.get("stage", "queued"), "stage_label": STAGE_LABEL.get(s.get("stage", "queued"), s.get("stage", "")),
                     "next_at": s.get("next_at") or s.get("invite_after"), "reason": s.get("reason", ""),
                     "replied_at": s.get("replied_at"), "reach": sorted(reach[l["provider_id"]]), "last": max([v for v in (s.get("viewed_at"), s.get("invited_at"), s.get("connected_at"),
                                                                              *s.get("sent", {}).values(), s.get("replied_at")) if v] or [""])})
    days = collections.defaultdict(collections.Counter)
    for e in state["events"]:
        k = "message" if e["kind"].startswith("message") else "viewed" if e["kind"].startswith("viewed") else e["kind"]
        days[e["t"][:10]][k] += 1
    by_day = [{"day": d, **{k: days[d][k] for k in ("viewed", "invited", "accepted", "message", "replied")}}
              for d in [(now - dt.timedelta(days=i)).date().isoformat() for i in range(13, -1, -1)]]
    return {"updated": now.isoformat(), "started": state["events"][0]["t"] if state["events"] else None, "research": research(leads),
            "sequences": _load("sequences.json", {}).get("messages", {}),
            "funnel": [[label, sum(k in reach[l["provider_id"]] for l in leads)] for k, label in STEPS],
            "accounts": accounts, "leads": rows, "by_day": by_day, "events": state["events"][-60:][::-1], "steps": STEPS,
            "all_events": [e for e in state["events"] if e["t"] >= (now - dt.timedelta(days=30)).isoformat()][::-1],
            "tracks": dict(collections.Counter(l["track"] for l in leads)), "langs": dict(collections.Counter(l["lang"] for l in leads)),
            "niches": collections.Counter(l["niche"] for l in leads).most_common(10)}


def page():
    return PAGE


PAGE = r"""<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Job-board Campaign</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Onest:wght@600;700&family=Golos+Text:wght@400;500;600&family=JetBrains+Mono:wght@400;600&display=swap">
<style>
/* Layout: an operations console. Sticky header with tabs; Overview = KPI strip, funnel, two account cards with today's budget meters, activity by day; Leads = searchable list; Replies; Log. Tables become cards under 680px. */
:root{--bg:#f3f5f8;--sf:#fff;--ln:#dde2ea;--ink:#111622;--i2:#4b5466;--i3:#768093;--ac:#2a78d6;--acs:#e5eefb;
--ok:#1d8a4b;--okb:#e2f4e9;--warn:#a35d00;--warnb:#fff1dc;--bad:#b42a21;--badb:#fbe7e5;
--fd:"Onest","Golos Text",system-ui,sans-serif;--fb:"Golos Text",system-ui,sans-serif;--fm:"JetBrains Mono",ui-monospace,Menlo,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#0e1117;--sf:#161a22;--ln:#2a303c;--ink:#f1f3f7;--i2:#b8bfcc;--i3:#8a92a2;--ac:#4b93ea;--acs:#1b293d;
--ok:#5cc98a;--okb:#14301e;--warn:#f2a54a;--warnb:#3a2a14;--bad:#f08a80;--badb:#3a1a18;color-scheme:dark}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--fb)}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--ln);padding:env(safe-area-inset-top,0px) 0 0}
.hin{max-width:1120px;margin:0 auto;padding:14px 18px 0;display:grid;gap:10px}
.top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
h1{font:700 22px/1.15 var(--fd);margin:0}.up{font:12px var(--fm);color:var(--i3)}
.tabs{display:flex;gap:4px;overflow-x:auto;scrollbar-width:none}.tabs::-webkit-scrollbar{display:none}
.tabs button{font:600 14px var(--fb);color:var(--i2);background:none;border:0;border-bottom:2px solid transparent;padding:8px 12px 10px;cursor:pointer;white-space:nowrap}
.tabs button[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--ac)}.tabs .n{font:12px var(--fm);color:var(--i3);margin-left:4px}
main{max-width:1120px;margin:0 auto;padding:22px 18px calc(48px + env(safe-area-inset-bottom,0px));display:grid;gap:26px}
h2{font:700 17px var(--fd);margin:0 0 10px}.m{color:var(--i3);font-size:13px}.note{background:var(--warnb);color:var(--ink);border-radius:8px;padding:10px 14px;font-size:14px}
.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.kpi{background:var(--sf);border:1px solid var(--ln);border-radius:10px;padding:12px 14px;display:grid;gap:2px}
.kpi b{font:700 26px/1.1 var(--fd);font-variant-numeric:tabular-nums}.kpi span{font-size:13px;color:var(--i2)}.kpi i{font:normal 12px var(--fm);color:var(--i3)}
.bars{display:grid;gap:7px}.row{display:grid;grid-template-columns:minmax(0,12em) minmax(0,1fr) 3em;gap:10px;align-items:center}
.lbl{font-size:13.5px;color:var(--i2);overflow-wrap:anywhere}.tr{height:12px;background:var(--acs);border-radius:4px;overflow:hidden}
.fl{display:block;height:100%;background:var(--ac);min-width:2px}.val{text-align:right;font:13px var(--fm);font-variant-numeric:tabular-nums}
.g2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 26px}
.acct{background:var(--sf);border:1px solid var(--ln);border-radius:10px;padding:14px 16px;display:grid;gap:12px;align-content:start}
.ah{display:flex;justify-content:space-between;align-items:center;gap:8px}.ah b{font:700 17px var(--fd)}
.pill{display:inline-flex;align-items:center;gap:6px;font:600 12px var(--fm);padding:3px 9px;border-radius:999px;white-space:nowrap}
.pill::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.p-ok{color:var(--ok);background:var(--okb)}.p-bad{color:var(--bad);background:var(--badb)}.p-warn{color:var(--warn);background:var(--warnb)}.p-mute{color:var(--i2);background:var(--acs)}
.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}.stats div{display:grid}.stats b{font:700 19px var(--fd);font-variant-numeric:tabular-nums}.stats span{font-size:12.5px;color:var(--i3)}
.meter{display:grid;grid-template-columns:7.5em minmax(0,1fr) 4.2em;gap:8px;align-items:center;font-size:13px;color:var(--i2)}
.meter .val{color:var(--ink)}
.card{background:var(--sf);border:1px solid var(--ln);border-radius:10px;overflow:hidden}
table{border-collapse:collapse;width:100%}th{text-align:left;font-size:12px;letter-spacing:.04em;text-transform:uppercase;color:var(--i3);padding:9px 12px;border-bottom:1px solid var(--ln);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--ln);vertical-align:top}tr:last-child td{border-bottom:0}td.n,th.n{text-align:right;font-family:var(--fm);font-variant-numeric:tabular-nums}
.who a,.who span{font-weight:600}a{color:var(--ac);text-underline-offset:2px}
.ctl{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.ctl input,.ctl select{font:14px var(--fb);color:var(--ink);background:var(--sf);border:1px solid var(--ln);border-radius:8px;padding:8px 10px;min-width:0}
.ctl input{flex:1 1 220px;max-width:360px}.ctl .m{margin-left:auto}
.gf{padding:10px 12px;background:var(--sf);border:1px solid var(--ln);border-radius:10px}.gf[hidden]{display:none}
.gf button{font:14px var(--fb);color:var(--ac);background:none;border:1px solid var(--ln);border-radius:8px;padding:8px 12px;cursor:pointer}
.gf.on{border-color:var(--ac);box-shadow:0 0 0 1px var(--ac) inset}
.gf select{flex:0 1 auto;max-width:220px}
button:focus-visible,input:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid var(--ac);outline-offset:2px}
.log{list-style:none;margin:0;padding:0}.log li{display:grid;grid-template-columns:8.5em minmax(0,1fr);gap:4px 12px;padding:9px 14px;border-bottom:1px solid var(--ln);font-size:14px}
.log li:last-child{border-bottom:0}.log time{font:12.5px var(--fm);color:var(--i3)}
.empty{padding:18px;color:var(--i3)}
.msg{display:grid;grid-template-columns:6.5em minmax(0,1fr);gap:14px}.msg .rail{display:grid;align-content:start;gap:2px;padding-top:12px}
.msg .rail b{font:600 13px var(--fm)}.msg .rail span{font:12px var(--fm);color:var(--i3)}
.bubble{background:var(--sf);border:1px solid var(--ln);border-radius:12px 12px 12px 4px;padding:12px 14px;white-space:pre-wrap;line-height:1.55}
.bubble mark{background:var(--acs);color:var(--ink);border-radius:4px;padding:0 3px}.mstate{margin-top:6px;font-size:12.5px}
@media (max-width:680px){.msg{grid-template-columns:minmax(0,1fr);gap:4px}.msg .rail{grid-auto-flow:column;justify-content:start;gap:10px;padding-top:0}}
[hidden]{display:none!important}
@media (max-width:860px){.kpis{grid-template-columns:repeat(3,minmax(0,1fr))}}
@media (max-width:680px){
 .kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.g2{grid-template-columns:minmax(0,1fr)}
 .row{grid-template-columns:minmax(0,9.5em) minmax(0,1fr) 2.6em}
 table.stack thead{display:none}table.stack tr{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:2px 10px;padding:10px 14px;border-bottom:1px solid var(--ln)}
 table.stack tr:last-child{border-bottom:0}table.stack td{border:0;padding:0}table.stack td[data-k]::before{content:attr(data-k) " ";color:var(--i3);font-size:12px}
 table.stack td.wide{grid-column:1/-1}.log li{grid-template-columns:minmax(0,1fr)}
 .ctl .m{margin-left:0;width:100%}.ctl input{flex:1 1 100%;max-width:none}.ctl select{flex:1 1 40%}
 .kpis .kpi:first-child{grid-column:1/-1}
}
</style></head><body>
<header><div class="hin">
 <div class="top"><h1>Кампанія по job-бордах</h1><span class="up" id="up">Завантажую…</span></div>
 <nav class="tabs" role="tablist">
  <button role="tab" id="t-ov" aria-selected="true" data-tab="ov">Огляд</button>
  <button role="tab" id="t-ld" aria-selected="false" data-tab="ld">Ліди<span class="n" id="n-ld"></span></button>
  <button role="tab" id="t-rp" aria-selected="false" data-tab="rp">Відповіді<span class="n" id="n-rp"></span></button>
  <button role="tab" id="t-ms" aria-selected="false" data-tab="ms">Повідомлення</button>
  <button role="tab" id="t-rs" aria-selected="false" data-tab="rs">Пошук лідів</button>
  <button role="tab" id="t-lg" aria-selected="false" data-tab="lg">Журнал</button>
 </nav>
</div></header>
<main>
 <p class="note" id="err" hidden></p>
 <div class="ctl gf" id="gf" role="group" aria-label="Фільтри">
  <select id="gfo" aria-label="Акаунт"><option value="">Обидва акаунти</option><option value="nazarii">Назарій</option><option value="hleb">Глеб</option></select>
  <select id="gft" aria-label="Трек"><option value="">Усі треки</option><option value="A">Трек A · онлайн-демо</option><option value="B">Трек B · телефон</option></select>
  <select id="gfl" aria-label="Мова"><option value="">Усі мови</option><option value="pl">Польська</option><option value="en">Англійська</option><option value="uk">Українська</option></select>
  <select id="gfn" aria-label="Ніша"><option value="">Усі ніші</option></select>
  <select id="gfs" aria-label="Джерело"><option value="">Усі джерела</option></select>
  <select id="gfp" aria-label="Період активності"><option value="14">Активність: 14 днів</option><option value="7">7 днів</option><option value="30">30 днів</option><option value="1">Сьогодні</option></select>
  <button type="button" id="gfr">Скинути</button><span class="m" id="gfc"></span>
 </div>
 <section id="ov">
  <div style="display:grid;gap:26px">
   <p class="m" id="sched" style="margin:0"></p>
   <div class="kpis" id="kpis"></div>
   <div class="g2" id="accts"></div>
   <div class="g2"><section><h2>Воронка</h2><div class="bars" id="funnel"></div></section>
    <section><h2>Хто в кампанії</h2><div class="bars" id="mix"></div></section></div>
   <section><h2>По днях</h2><div class="card"><table class="stack"><thead><tr><th>День</th><th class="n">Перегляди</th><th class="n">Інвайти</th><th class="n">Прийняли</th><th class="n">Повідомлення</th><th class="n">Відповіді</th></tr></thead><tbody id="days"></tbody></table></div></section>
  </div>
 </section>
 <section id="ld" hidden>
  <div class="ctl"><input type="search" id="q" placeholder="Ім'я, компанія, посада" aria-label="Пошук лідів">
   <select id="fs" aria-label="Етап"><option value="">Усі етапи</option></select><span class="m" id="cnt"></span></div>
  <div class="card"><table class="stack"><thead><tr><th>Людина</th><th>Компанія</th><th>Етап</th><th>Акаунт</th><th>Далі</th></tr></thead><tbody id="leads"></tbody></table></div>
 </section>
 <section id="rp" hidden><div class="card"><table class="stack"><thead><tr><th>Коли</th><th>Хто</th><th>Компанія</th><th>Акаунт</th></tr></thead><tbody id="replies"></tbody></table></div></section>
 <section id="ms" hidden>
  <div class="ctl"><select id="mt" aria-label="Трек"><option value="A">Трек A · онлайн-демо</option><option value="B">Трек B · телефон</option></select>
   <select id="ml" aria-label="Мова"><option value="pl">Польська</option><option value="en">Англійська</option><option value="uk">Українська</option></select>
   <input type="search" id="mq" list="mlist" placeholder="Показати для ліда: почніть вводити ім'я" aria-label="Лід"><datalist id="mlist"></datalist></div>
  <p class="m" id="mhint" style="margin:0 0 12px"></p>
  <div style="display:grid;gap:12px" id="msgs"></div>
 </section>
 <section id="rs" hidden>
  <div style="display:grid;gap:26px">
   <p class="m" id="rs-sched" style="margin:0"></p>
   <div class="g2"><section><h2>Пул компаній</h2><div class="bars" id="rs-src"></div><h2 style="margin-top:16px">Вердикт судді</h2><div class="bars" id="rs-ver"></div></section>
    <section><h2>Компанії, що підходять</h2><div class="bars" id="rs-fit"></div><h2 style="margin-top:16px">Звідки ліди в кампанії</h2><div class="bars" id="rs-lead"></div></section></div>
   <div class="g2"><section><h2>Черга LinkedIn</h2><div class="bars" id="rs-q"></div><h2 style="margin-top:16px">Пошукові запити агента</h2><div class="card"><ul class="log" id="rs-qs"></ul></div></section>
    <section><h2>Рішення агента</h2><div class="card"><ul class="log" id="rs-log"></ul></div></section></div>
  </div>
 </section>
 <section id="lg" hidden><div class="card"><ul class="log" id="log"></ul></div></section>
</main>
<script>
const $=id=>document.getElementById(id);
const NAME={nazarii:"Назарій",hleb:"Глеб"};
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const when=iso=>iso?new Date(iso).toLocaleString("uk-UA",{weekday:"short",day:"2-digit",month:"2-digit",hour:"2-digit",minute:"2-digit"}):"—";
const pct=(a,b)=>b?Math.round(100*a/b)+"%":"—";
let D=null;
const SRC={job_boards:"Job-борди",seed:"Ранній скан",icp:"Пошук агента",pool:"Пул компаній",booking:"Calendly-сайти",calling:"Телефонні продажі"};
const GF=["gfo","gft","gfl","gfn","gfs","gfp"];
function F(){return {o:$("gfo").value,t:$("gft").value,l:$("gfl").value,n:$("gfn").value,s:$("gfs").value,p:+$("gfp").value||14}}
function active(){const f=F();return !!(f.o||f.t||f.l||f.n||f.s)}
function FL(){const f=F();return D.leads.filter(l=>(!f.o||l.owner===f.o)&&(!f.t||l.track===f.t)&&(!f.l||l.lang===f.l)&&(!f.n||l.niche===f.n)&&(!f.s||(l.source||"job_boards")===f.s))}
function FE(){const f=F(),ids=new Set(FL().map(l=>l.pid)),since=new Date(Date.now()-f.p*864e5);if(f.p===1)since.setHours(0,0,0,0);
 return (D.all_events||D.events).filter(e=>new Date(e.t)>=since&&(e.lead?ids.has(e.lead):(!f.o||e.account===f.o)&&!active()))}
function saveF(){try{localStorage.setItem("jbf",JSON.stringify(Object.fromEntries(GF.map(k=>[k,$(k).value]))))}catch(e){}}
function loadF(){try{const v=JSON.parse(localStorage.getItem("jbf")||"{}");for(const k of GF)if(v[k]!==undefined)$(k).dataset.want=v[k]}catch(e){}}
function bars(el,rows){const m=Math.max(1,...rows.map(r=>r[1]));el.innerHTML=rows.map(([k,n])=>`<div class="row"><span class="lbl">${esc(k)}</span><span class="tr"><span class="fl" style="width:${(n/m*100).toFixed(1)}%"></span></span><span class="val">${n}</span></div>`).join("")}
function meter(label,[u,c]){return `<div class="meter"><span>${label}</span><span class="tr"><span class="fl" style="width:${Math.min(100,u/Math.max(1,c)*100).toFixed(0)}%"></span></span><span class="val">${u} з ${c}</span></div>`}
function stagePill(s){const cls={replied:"p-ok",skipped:"p-mute",expired:"p-mute",done:"p-mute",invited:"p-warn"}[s.stage]||"p-mute";return `<span class="pill ${cls}">${esc(s.stage_label)}</span>`}
function overview(){
 const fl=FL(),ev=FE(),fun=(D.steps||[]).map(([k,label])=>[label,fl.filter(l=>(l.reach||[]).includes(k)).length]);
 const f=Object.fromEntries(fun.length?fun:D.funnel);const L=fl.length;
 $("sched").textContent=D.started?`Кампанія йде з ${when(D.started)}. Працює в будні 9:00–17:30 за Варшавою.`:"Кампанія увімкнена й чекає робочих годин: будні 9:00–17:30 за Варшавою. Спершу перегляди профілів, через день інвайти.";
 $("kpis").innerHTML=[[L,"лідів у кампанії",active()?`з ${D.leads.length} за фільтром`:"нові ЛПРи додаються щодня"],[f["Надіслали інвайт"],"інвайтів",`${f["Переглянули профіль"]} переглядів профілю`],[f["Прийняли"],"прийняли",pct(f["Прийняли"],f["Надіслали інвайт"])+" від інвайтів"],[f["Повідомлення 1"],"отримали повідомлення",""],[f["Відповіли"],"відповіли",pct(f["Відповіли"],f["Повідомлення 1"])+" від написаних"]]
  .map(([b,s,i])=>`<div class="kpi"><b>${b}</b><span>${s}</span><i>${i||"&nbsp;"}</i></div>`).join("");
 $("accts").innerHTML=Object.entries(D.accounts).filter(([k])=>!F().o||k===F().o).map(([k,A])=>{const m=fl.filter(l=>l.owner===k),has=s=>m.filter(l=>(l.reach||[]).includes(s)).length;
  const a={...A,leads:m.length,invited:has("invited"),connected:has("connected"),messaged:has("m1"),replied:has("replied")};const st=a.paused_until?`<span class="pill p-bad">Пауза до ${when(a.paused_until)}</span> <a href="resume?account=${k}" style="font-size:13px">Зняти</a>`:`<span class="pill p-ok">Працює</span>`;
  return `<div class="acct"><div class="ah"><b>${NAME[k]}</b>${st}</div>
  <div class="stats"><div><b>${a.leads}</b><span>лідів</span></div><div><b>${a.invited}</b><span>інвайтів · ${pct(a.connected,a.invited)} прийняли</span></div><div><b>${a.replied}</b><span>відповідей з ${a.messaged}</span></div></div>
  <div style="display:grid;gap:6px"><span class="m">Сьогодні</span>${meter("Перегляди",a.today.view)}${meter("Інвайти",a.today.invite)}${meter("Повідомлення",a.today.message)}</div>
  <span class="m">${a.last_error?esc(a.last_error):a.next_at?"Наступна запланована дія: "+when(a.next_at):"Наступна дія: за чергою в робочі години"}${a.unipile_status?` · статус в Unipile: ${esc(a.unipile_status.join(", "))}`:""}</span></div>`}).join("");
 bars($("funnel"),fun.length?fun:D.funnel);
 const cnt=k=>v=>fl.filter(l=>l[k]===v).length;bars($("mix"),[["Трек A · онлайн-демо",cnt("track")("A")],["Трек B · телефон",cnt("track")("B")],["Польською",cnt("lang")("pl")],["Англійською",cnt("lang")("en")],["Українською",cnt("lang")("uk")]]);
 const per=F().p,days=[];for(let i=0;i<Math.max(per,1);i++){const d=new Date(Date.now()-i*864e5);days.push(d.toLocaleDateString("sv-SE",{timeZone:"Europe/Warsaw"}))}
 const by=Object.fromEntries(days.map(d=>[d,{day:d,viewed:0,invited:0,accepted:0,message:0,replied:0}]));
 for(const e of ev){const d=new Date(e.t).toLocaleDateString("sv-SE",{timeZone:"Europe/Warsaw"}),k=e.kind.startsWith("message")?"message":e.kind.startsWith("viewed")?"viewed":e.kind;if(by[d]&&k in by[d])by[d][k]++}
 $("days").innerHTML=days.map(d=>by[d]).map(d=>`<tr><td class="wide"><b>${new Date(d.day).toLocaleDateString("uk-UA",{weekday:"short",day:"2-digit",month:"2-digit"})}</b></td><td class="n" data-k="Перегляди">${d.viewed}</td><td class="n" data-k="Інвайти">${d.invited}</td><td class="n" data-k="Прийняли">${d.accepted}</td><td class="n" data-k="Повідомл.">${d.message}</td><td class="n" data-k="Відповіді">${d.replied}</td></tr>`).join("");
}
function next(l){if(["replied","done","skipped","expired"].includes(l.stage))return "—";if(l.stage==="queued")return "за чергою";if(!l.next_at)return "—";
 return new Date(l.next_at)<=new Date()?"зараз, у найближчу робочу годину":when(l.next_at)}
function leads(){
 const q=$("q").value.trim().toLowerCase(),s=$("fs").value;
 const rows=FL().filter(l=>(!s||l.stage===s)&&(!q||(l.name+" "+l.company+" "+l.role).toLowerCase().includes(q))).sort((a,b)=>b.score-a.score);
 $("cnt").textContent=`${rows.length} з ${D.leads.length}`;
 $("leads").innerHTML=rows.map(l=>`<tr><td class="wide who"><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.name)}</a><div class="m">${esc(l.role)} · ${l.lang.toUpperCase()} · трек ${l.track}${l.source&&l.source!=="job_boards"?" · "+({seed:"ранній скан",icp:"пошук агента",pool:"пул компаній"}[l.source]||l.source):""}</div></td>
 <td class="wide">${esc(l.company)}<div class="m">${esc(l.niche)}</div></td><td>${stagePill(l)}${l.reason?`<div class="m">${esc(l.reason)}</div>`:""}</td><td data-k="Акаунт">${NAME[l.owner]}</td><td data-k="Далі">${next(l)}</td></tr>`).join("")||`<tr><td class="empty">Нікого не знайдено.</td></tr>`;
}
function replies(){
 const r=FL().filter(l=>l.stage==="replied").sort((a,b)=>(b.replied_at||"").localeCompare(a.replied_at||""));
 $("n-rp").textContent=r.length;
 $("replies").innerHTML=r.map(l=>`<tr><td data-k="Коли">${when(l.replied_at)}</td><td class="wide who"><a href="${esc(l.url)}" target="_blank" rel="noopener">${esc(l.name)}</a><div class="m">${esc(l.role)}</div></td><td class="wide">${esc(l.company)}</td><td data-k="Акаунт">${NAME[l.owner]}</td></tr>`).join("")
  ||`<tr><td class="empty">Відповідей поки немає. Коли хтось відповість, він з'явиться тут, а сіквенс для нього зупиниться. Відповідати людині ви будете самі в LinkedIn.</td></tr>`;
}
function log(){
 $("log").innerHTML=FE().slice(0,200).map(e=>`<li><time>${when(e.t)}</time><span><b>${NAME[e.account]||esc(e.account)}</b> · ${esc(e.kind)}${e.name?` · ${esc(e.name)}`:""}${e.company?` <span class="m">@ ${esc(e.company)}</span>`:""}${e.note?` <span class="m">${esc(e.note)}</span>`:""}</span></li>`).join("")
  ||`<li class="empty">Подій ще немає. Перша дія буде в робочі години.</li>`;
}
function research(){const r=D.research||{};
 $("rs-sched").textContent=(r.last_scrape?`Останній збір job-бордів: ${when(r.last_scrape)}. Наступний: ${when(r.next_scrape)}. `:"Перший збір job-бордів агент зробить найближчим проходом. ")+"Агент проходить кожні 6 годин: збирає нові компанії, оцінює їх, планує пошук у LinkedIn і додає нових людей у кампанію.";
 bars($("rs-src"),r.by_source||[]);bars($("rs-ver"),r.verdicts||[]);bars($("rs-fit"),r.fit_progress||[]);bars($("rs-lead"),r.lead_sources||[]);
 const k=(r.queue||{}).by_kind||{};bars($("rs-q"),[["Пошук ЛПРів у компанії",k.people||0],["Сторінка компанії і штат",k.company||0],["Профілі людей із сайтів",k.match||0],["Запити агента",k.icp||0]]);
 $("rs-qs").innerHTML=(r.queries||[]).map(q=>`<li><time>${q.status==="done"?esc(q.results)+" людей":"чекає"}</time><span>${esc(q.q)}</span></li>`).join("")||`<li class="empty">Агент складе запити, коли компанії з пулу закінчаться.</li>`;
 $("rs-log").innerHTML=(r.log||[]).map(e=>`<li><time>${when(e.t)}</time><span><b>${esc(e.action)}</b> · ${esc(e.detail)}${e.why?`<div class="m">${esc(e.why)}</div>`:""}</span></li>`).join("")||`<li class="empty">Рішень ще немає. Перший прохід агента через кілька хвилин після старту сервісу.</li>`}
const STG=[["m1","Повідомлення 1","день 0","щойно прийняв"],["f1","Повідомлення 2","день 3",""],["f2","Повідомлення 3","день 6",""],["f3","Повідомлення 4","день 9","останнє"]];
function msgs(){
 const dl=$("mlist");if(!dl.options.length)dl.innerHTML=D.leads.map(l=>`<option value="${esc(l.name+" · "+l.company)}">`).join("");
 const pick=D.leads.find(l=>l.name+" · "+l.company===$("mq").value);
 if(pick){$("mt").value=pick.track;$("ml").value=pick.lang}
 const t=$("mt").value,lg=$("ml").value;
 $("mhint").textContent=pick?`Так ${pick.name} побачить сіквенс (трек ${pick.track}, ${pick.lang.toUpperCase()}, пише ${NAME[pick.owner]}). Етап зараз: ${pick.stage_label}.`:"Шаблон треку й мови. Виділене підставляється для кожного ліда окремо. Оберіть ліда, щоб побачити його повідомлення.";
 const fill=txt=>{let h=esc(txt);const sub=(k,v)=>{h=h.split(esc(k)).join(v)};
  if(pick){sub("{personal_line}",`<mark>${esc(pick.personal_line)}</mark>`);sub("{first_name}",`<mark>${esc(pick.first_name)}</mark>`);sub("{company}",`<mark>${esc(pick.company_short)}</mark>`)}
  else for(const k of ["{personal_line}","{first_name}","{company}"])sub(k,`<mark>${k}</mark>`);return h};
 $("msgs").innerHTML=STG.map(([k,label,day,hint])=>{const txt=(D.sequences||{})[`${t}-${lg}-${k}`];
  let state="";if(pick){const at=(pick.sent||{})[k];state=at?`<div class="mstate"><span class="pill p-ok">Надіслано ${when(at)}</span></div>`:["replied","skipped","expired"].includes(pick.stage)?`<div class="mstate m">Не надсилатиметься: ${esc(pick.stage_label.toLowerCase())}</div>`:`<div class="mstate m">Ще не надіслано</div>`}
  return `<div class="msg"><div class="rail"><b>${day}</b><span>${hint}</span></div><div><div class="m" style="margin-bottom:4px">${label}</div>${txt?`<div class="bubble">${fill(txt)}</div>`:`<div class="bubble m">Для треку ${t} мовою ${lg.toUpperCase()} тексту немає.</div>`}${state}</div></div>`}).join("");
}
function draw(){if(!D)return;$("up").textContent="Оновлено "+new Date(D.updated).toLocaleTimeString("uk-UA",{hour:"2-digit",minute:"2-digit"});$("n-ld").textContent=D.leads.length;
 const sel=$("fs");if(sel.options.length<2){const seen=[...new Set(D.leads.map(l=>l.stage))];for(const s of seen){const l=D.leads.find(x=>x.stage===s);sel.add(new Option(l.stage_label,s))}}
 const fill=(id,vals,lab)=>{const s=$(id);if(s.options.length>1)return;for(const v of vals)s.add(new Option(lab?lab(v):v,v));if(s.dataset.want!==undefined){s.value=s.dataset.want;delete s.dataset.want}};
 fill("gfn",[...new Set(D.leads.map(l=>l.niche).filter(Boolean))].sort());fill("gfs",[...new Set(D.leads.map(l=>l.source||"job_boards"))],v=>SRC[v]||v);
 for(const k of ["gfo","gft","gfl","gfp"]){const s=$(k);if(s.dataset.want!==undefined){s.value=s.dataset.want;delete s.dataset.want}}
 $("gf").classList.toggle("on",active());$("gfc").textContent=active()?`${FL().length} з ${D.leads.length} лідів`:"";
 overview();leads();replies();log();research();msgs()}
async function pull(){try{const r=await fetch("api/campaign.json",{credentials:"same-origin"});if(!r.ok)throw new Error(r.status);D=await r.json();$("err").hidden=true;draw()}
 catch(e){$("err").hidden=false;$("err").textContent=String(e.message)==="401"?"Немає доступу. Відкрийте посилання з ?token=… ще раз.":"Не вдалося оновити дані, спробую знову за хвилину."}}
const TABS={ov:"overview",ld:"leads",rp:"replies",ms:"messages",rs:"research",lg:"log"};
function show(id){for(const x of document.querySelectorAll("[data-tab]")){x.setAttribute("aria-selected",x.dataset.tab===id);$(x.dataset.tab).hidden=x.dataset.tab!==id}
 $("gf").hidden=!["ov","ld","rp","lg"].includes(id);
 history.replaceState(null,"","#"+TABS[id])}
for(const b of document.querySelectorAll("[data-tab]"))b.addEventListener("click",()=>show(b.dataset.tab));
const start=Object.keys(TABS).find(k=>"#"+TABS[k]===location.hash);if(start)show(start);
["q","fs"].forEach(id=>$(id).addEventListener("input",leads));
GF.forEach(id=>$(id).addEventListener("change",()=>{saveF();draw()}));
$("gfr").addEventListener("click",()=>{for(const k of GF)$(k).value=k==="gfp"?"14":"";saveF();draw()});
loadF();
["mt","ml","mq"].forEach(id=>$(id).addEventListener("input",msgs));
pull();setInterval(pull,60000);
</script></body></html>"""
