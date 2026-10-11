"""Render report/dealaris-prospects.html (stats artifact) from data/*.csv."""
import csv, json, collections, html
from pathlib import Path
import sys
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import store  # decision makers may live in Postgres
R = str(HERE.parent / "data") + "/"
v = list(csv.DictReader(open(R+'vacancies.csv')))
c = list(csv.DictReader(open(R+'companies.csv')))
raw = json.load(open(R+'raw_offers.json'))
q = [x for x in c if x['status'] == 'qualified']
UNIQUE = json.load(open(R + 'run_stats.json'))['unique_vacancies']
qv = [x for x in v if x['company_status'] == 'qualified']
ROLE = {'high_ticket_b2c':'High-ticket B2C (OZE, авто, нерухомість, фінанси)','generic_sales':'Загальні продажі','ae_b2b_saas':'AE / B2B / SaaS',
 'account_mgmt':'Account management','sales_specialist':'Specjalista ds. sprzedaży','sales_leadership':'Керівники продажів (покупці)',
 'sdr_bdr_inside':'SDR / BDR / inside sales','telesales_callcenter':'Telesales / call center','customer_service':'Обслуговування клієнтів'}
roles = collections.Counter(x['role_class'] for x in qv).most_common()
tiers = collections.Counter(x['tier'] for x in q)
def _reason(r):
    if r.startswith('LLM size'):
        return 'LLM-оцінка штату поза 10–200'
    if r.startswith('LLM judge'):
        return 'LLM-суддя: продажі наживо або поза ICP'
    if 'on LinkedIn' in r:
        return 'Понад 200 працівників (LinkedIn)' if r.startswith('enterprise') else 'Менше 10 працівників (LinkedIn)'
    return (r.replace(' (known brand, pending LinkedIn check)', '').replace('staffing agency / hidden client', 'Кадрові агенції / прихований клієнт')
            .replace('enterprise (501+ on rocketjobs profile)', 'Понад 500 працівників (rocketjobs)').replace('enterprise', 'Enterprise за брендом'))
disq = collections.Counter(_reason(x['disqualify_reason']) for x in c if x['status'] != 'qualified')
src = collections.Counter(o['source'] for o in raw)
sales_vac = len(v)
funnel = [('Оголошень зібрано', len(raw)), ('Унікальних вакансій', UNIQUE),
          ('Sales-вакансій', sales_vac), ('Компаній із sales-ролями', len(c)), ('Пройшли фільтри', len(q)),
          ('Tier A + B', tiers['A']+tiers['B'])]
verified = sum(x['headcount_verified'] == 'True' for x in c)
pending = sum(x['li_match'] == '' for x in q)
NOTE = ('<b>Headcount ще не перевірено.</b> Enterprise відсіяні поки що лише за списком відомих брендів. Межі 10–200 працівників застосуються після збагачення через Unipile (LinkedIn). Тоді статуси й кількість кваліфікованих компаній зміняться.' if not verified else f'<b>Headcount перевірено через LinkedIn для {verified} з {len(c)} компаній.</b> Відсіяно всіх, хто має понад 200 або менше 10 працівників. Компанії без збігу на LinkedIn лишаються в списку з позначкою «не перевірено».' + (f' Ще {pending} кваліфікованих компаній (переважно tier C/D) чекають перевірки: LinkedIn обмежив частоту запитів, тож їх доперевіримо наступним прогоном.' if pending else ''))
rows = [dict(hc=x['li_headcount'] or x['li_headcount_range'], li=x['li_url'], n=x['company'], t=x['tier'], f=float(x['company_score']), v=int(x['sales_vacancies']), p=int(x['phone_roles']),
             l=x['hiring_sales_leader']=='True', r=x['top_vacancy'], u=x['top_vacancy_url'],
             c=x['cities'].split('|')[0] if x['cities'] else '', cls=[ROLE.get(k,k) for k in x['role_classes'].split('|') if k]) for x in q if x['tier'] in 'ABC']
stats = dict(qualified=len(q), multi=sum(int(x['sales_vacancies'])>=3 for x in q), leader=sum(x['hiring_sales_leader']=='True' for x in q),
             phone=sum(int(x['phone_roles'])>0 for x in q))
pr_cos = [x for x in c if x['sources'] == 'pracuj.pl']
pracuj = [('Оголошень', src['pracuj.pl']), ('Компаній із sales-ролями', sum('pracuj.pl' in x['sources'] for x in c)),
          ('Нових, яких немає на інших бордах', len(pr_cos)), ('З них кваліфіковано', sum(x['status'] == 'qualified' for x in pr_cos))]
looked = [x for x in c if x['li_match']]
li = [('Перевірено', len(looked)), ('Точний збіг назви', sum(x['li_match'] == 'exact' for x in looked)),
      ('Fuzzy (усі слова + Польща)', sum(x['li_match'] in ('fuzzy', 'domain') for x in looked)),
      ('Не знайдено на LinkedIn', sum(x['li_match'] not in ('exact', 'fuzzy', 'domain') for x in looked)),
      ('Відсіяно: понад 200 працівників', disq['Понад 200 працівників (LinkedIn)']),
      ('Відсіяно: менше 10 працівників', disq['Менше 10 працівників (LinkedIn)']),
      ('Ще чекають перевірки', pending)]
top = [x for x in q if x['tier'] == 'A' and x['headcount_verified'] == 'True'][:20]
e = html.escape
TOP = '\n'.join(f'<tr><td class="num">{i}</td><td class="co">{e(x["company"])}</td><td class="num">{float(x["company_score"]):.1f}</td>'
                f'<td class="num">{e(x["li_headcount"] or x["li_headcount_range"])}</td>'
                f'<td><a href="{e(x["top_vacancy_url"])}" target="_blank" rel="noopener">{e(x["top_vacancy"])}</a></td>'
                f'<td><a href="{e(x["li_url"])}" target="_blank" rel="noopener">{e(x["li_url"].rstrip("/").rsplit("/", 1)[-1])}</a></td></tr>'
                for i, x in enumerate(top, 1))
dm_path = store.path('decision_makers.csv')
dm = list(csv.DictReader(dm_path.open())) if dm_path.exists() else []
dm_people = [x for x in dm if x['name']]
dm_cos = {x['company'] for x in dm}
dm_stats = [('Компаній tier A + B', len(dm_cos)), ('З хоча б одним ЛПРом', len({x['company'] for x in dm_people})),
            ('Людей зібрано', len(dm_people)), ('З профілем LinkedIn', sum(bool(x['person_linkedin']) for x in dm_people)),
            ('Лише з сайту (LinkedIn ще шукається)', sum(not x['person_linkedin'] for x in dm_people)),
            ('З імейлом або телефоном', sum(bool(x.get('email') or x.get('phone')) for x in dm_people))] if dm else [('Пошук ще не запускався', 0)]
dm_roles = collections.Counter(x['role'] for x in dm_people).most_common() or [('—', 0)]
dm_people.sort(key=lambda x: -float(x.get('lead_score') or 0))
def _person(x):
    name = e(x['name'])
    return f'<a href="{e(x["person_linkedin"])}" target="_blank" rel="noopener">{name}</a>' if x['person_linkedin'] else name
def _contact(x):
    bits = [e(v) for v in (x.get('email'), x.get('phone')) if v]
    if x.get('source') == 'website' and x.get('source_url'):
        bits.append(f'<a href="{e(x["source_url"])}" target="_blank" rel="noopener">сайт</a>')
    return ' · '.join(bits)
DM_ROWS = '\n'.join(f'<tr data-q="{e((x["company"] + " " + x["name"] + " " + x["role"] + " " + x["headline"]).lower())}">'
                    f'<td class="num">{float(x.get("lead_score") or 0):.1f}</td>'
                    f'<td><span class="tier tier-{e(x["tier"])}">{e(x["tier"])}</span></td><td class="co">{e(x["company"])}</td>'
                    f'<td>{_person(x)}</td><td>{e(x["role"])}<div class="cls">{e(x["headline"])}</div></td>'
                    f'<td>{"LinkedIn" if x.get("source") != "website" else "Сайт"}</td><td class="cls">{_contact(x)}</td></tr>'
                    for x in dm_people)
tpl = open(HERE / 'template.html').read()
def bars(items, total=None, cls=''):
    m = max(n for _, n in items) or 1
    out = []
    for label, n in items:
        pct = n/m*100
        out.append(f'<div class="bar-row {cls}" tabindex="0" title="{html.escape(label)}: {n}"><span class="bar-label">{html.escape(label)}</span>'
                   f'<span class="bar-track"><span class="bar-fill" style="width:{pct:.1f}%"></span></span><span class="bar-val">{n:,}</span></div>'.replace(',', ' '))
    return '\n'.join(out)
page = (tpl.replace('{{FUNNEL}}', bars(funnel, cls='funnel'))
        .replace('{{ROLES}}', bars([(ROLE.get(k,k), n) for k, n in roles]))
        .replace('{{TIERS}}', bars([(f'Tier {t}', tiers[t]) for t in 'ABCD']))
        .replace('{{DISQ}}', bars(disq.most_common()))
        .replace('{{SRC}}', bars(src.most_common()))
        .replace('{{PRACUJ}}', bars(pracuj)).replace('{{LINKEDIN}}', bars(li)).replace('{{TOP}}', TOP)
        .replace('{{DM_STATS}}', bars(dm_stats)).replace('{{DM_ROLES}}', bars(dm_roles)).replace('{{DM_ROWS}}', DM_ROWS)
        .replace('{{ROWS}}', json.dumps(rows, ensure_ascii=False))
        .replace('{{NOTE}}', NOTE)
        .replace('{{SOURCES_LINE}}', ', '.join(k for k, _ in src.most_common()))
        .replace('{{DATE}}', __import__('datetime').date.today().strftime('%d.%m.%Y'))
        .replace('{{BLOCKED}}', ('OLX, Indeed, gowork і careerjet закриті Cloudflare-челенджем і не зібрані.' if 'pracuj.pl' in src
                                 else 'pracuj.pl, OLX, Indeed, gowork і careerjet закриті Cloudflare-челенджем і не зібрані.'))
        .replace('{{Q}}', str(stats['qualified'])).replace('{{MULTI}}', str(stats['multi'])).replace('{{LEADER}}', str(stats['leader']))
        .replace('{{PHONE}}', str(stats['phone'])).replace('{{A}}', str(tiers['A'])).replace('{{B}}', str(tiers['B'])).replace('{{C}}', str(tiers['C'])))
open(HERE / 'dealaris-prospects.html', 'w').write(page)
print(funnel, tiers, len(rows))
