"""Render report/dm-niches.html: niches of the companies we have decision makers for, who they are hiring, and the
full decision-maker list. Reads data/decision_makers.csv, companies.csv, vacancies.csv and data/niche_cache.json.

  python report/niches.py
"""
import collections
import csv
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from jbscrap.score import company_key  # noqa: E402

DATA = HERE.parent / "data"
ROLE = {"high_ticket_b2c": "High-ticket B2C", "generic_sales": "Загальні продажі", "ae_b2b_saas": "AE / B2B / SaaS",
        "account_mgmt": "Account management", "sales_specialist": "Specjalista ds. sprzedaży",
        "sales_leadership": "Керівники продажів", "sdr_bdr_inside": "SDR / BDR / inside sales",
        "telesales_callcenter": "Telesales / call center", "customer_service": "Обслуговування клієнтів"}


def mode(w):
    w = (w or "").lower()
    return "Віддалено" if re.search(r"remote|zdaln", w) else "Гібрид" if re.search(r"hybr", w) else \
        "Офіс" if re.search(r"office|stacjonar", w) else "Не вказано"


def title_key(t):
    t = re.sub(r"\(.*?\)|\[.*?\]|[-–|•].*$", "", t.lower())
    return re.sub(r"\s+", " ", re.sub(r"\b(k/m|m/k|k/m/n|m/k/x)\b", "", t)).strip(" /,.")


def bars(items):
    m = max([n for _, n in items] or [1]) or 1
    return "\n".join(f'<div class="bar-row"><span class="bar-label">{html.escape(k)}</span><span class="bar-track">'
                     f'<span class="bar-fill" style="width:{n / m * 100:.1f}%"></span></span><span class="bar-val">{n}</span></div>'
                     for k, n in items)


def main():
    niche = json.loads((DATA / "niche_cache.json").read_text())
    cos = {c["company"]: c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8"))}
    dm = [r for r in csv.DictReader(open(DATA / "decision_makers.csv", encoding="utf-8")) if r["name"]]
    names = {r["company"] for r in dm}
    vacs = [v for v in csv.DictReader(open(DATA / "vacancies.csv", encoding="utf-8")) if v["company"] in names]
    nich = lambda c: (niche.get(company_key(c)) or {}).get("niche", "Інше")

    by_niche = collections.defaultdict(lambda: {"cos": set(), "people": 0, "scores": [], "roles": collections.Counter(),
                                                 "modes": collections.Counter(), "vac": 0})
    for r in dm:
        b = by_niche[nich(r["company"])]
        b["cos"].add(r["company"])
        b["people"] += 1
    for c in names:
        by_niche[nich(c)]["scores"].append(float(cos[c]["company_score"] or 0))
    for v in vacs:
        b = by_niche[nich(v["company"])]
        b["roles"][v["role_class"]] += 1
        b["modes"][mode(v["workplace"])] += 1
        b["vac"] += 1
    niche_rows = sorted(by_niche.items(), key=lambda kv: -len(kv[1]["cos"]))
    table = "\n".join(
        f'<tr><td class="co">{html.escape(k)}</td><td class="num">{len(b["cos"])}</td><td class="num">{b["people"]}</td>'
        f'<td class="num">{sum(b["scores"]) / max(1, len(b["scores"])):.0f}</td><td class="num">{b["vac"]}</td>'
        f'<td>{html.escape(", ".join(ROLE.get(x, x) for x, _ in b["roles"].most_common(2)))}</td>'
        f'<td class="num">{round(100 * (b["modes"]["Віддалено"] + b["modes"]["Гібрид"]) / max(1, b["vac"]))}%</td></tr>'
        for k, b in niche_rows)

    titles = collections.Counter(title_key(v["title"]) for v in vacs if title_key(v["title"]))
    top_titles = titles.most_common(15)
    vac_by_co = collections.defaultdict(list)
    for v in sorted(vacs, key=lambda v: -float(v["role_score"] or 0)):
        vac_by_co[v["company"]].append(v)

    rows = []
    for r in sorted(dm, key=lambda r: -float(r["lead_score"] or 0)):
        top = vac_by_co.get(r["company"], [{}])[0]
        rows.append({"s": float(r["lead_score"] or 0), "t": r["tier"], "c": r["company"], "n": nich(r["company"]),
                     "sells": (niche.get(company_key(r["company"])) or {}).get("sells", ""),
                     "p": r["name"], "u": r["person_linkedin"], "r": r["role"], "h": r["headline"],
                     "src": "Сайт" if r["source"] == "website" else "LinkedIn", "su": r["source_url"],
                     "e": r["email"], "ph": r["phone"], "hc": r["li_headcount"],
                     "vt": top.get("title", ""), "vu": top.get("url", ""), "vn": len(vac_by_co.get(r["company"], []))})

    stats = dict(people=len(dm), companies=len(names), niches=len(by_niche), vac=len(vacs),
                 li=sum(bool(r["person_linkedin"]) for r in dm), contact=sum(bool(r["email"] or r["phone"]) for r in dm))
    page = (HERE / "niches_template.html").read_text()
    for k, v in {"{{DATE}}": dt.date.today().strftime("%d.%m.%Y"), "{{NICHE_BARS}}": bars([(k, len(b["cos"])) for k, b in niche_rows]),
                 "{{NICHE_TABLE}}": table, "{{ROLE_BARS}}": bars([(ROLE.get(k, k), n) for k, n in collections.Counter(v["role_class"] for v in vacs).most_common()]),
                 "{{MODE_BARS}}": bars(collections.Counter(mode(v["workplace"]) for v in vacs).most_common()),
                 "{{TITLE_BARS}}": bars(top_titles), "{{DM_ROLE_BARS}}": bars(collections.Counter(r["role"] for r in dm).most_common()),
                 "{{NICHE_OPTIONS}}": "".join(f'<option value="{html.escape(k)}">{html.escape(k)}</option>' for k, _ in niche_rows),
                 "{{ROWS}}": json.dumps(rows, ensure_ascii=False).replace("</", "<\\/"),
                 **{f"{{{{{k.upper()}}}}}": str(v) for k, v in stats.items()}}.items():
        page = page.replace(k, v)
    (HERE / "dm-niches.html").write_text(page)
    print(stats)


if __name__ == "__main__":
    main()
