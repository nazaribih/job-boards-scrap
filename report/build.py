"""Render report/dealaris-prospects.html (stats artifact) from data/*.csv."""
import csv, json, collections, html
from pathlib import Path
HERE = Path(__file__).resolve().parent
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
disq = collections.Counter(x['disqualify_reason'] for x in c if x['status'] != 'qualified')
src = collections.Counter(o['source'] for o in raw)
sales_vac = len(v)
funnel = [('Оголошень зібрано', len(raw)), ('Унікальних вакансій', UNIQUE),
          ('Sales-вакансій', sales_vac), ('Компаній із sales-ролями', len(c)), ('Пройшли фільтри', len(q)),
          ('Tier A + B', tiers['A']+tiers['B'])]
verified = sum(x['headcount_verified'] == 'True' for x in c)
NOTE = ('<b>Headcount ще не перевірено.</b> Enterprise відсіяні поки що лише за списком відомих брендів. Межі 10–200 працівників застосуються після збагачення через Unipile (LinkedIn). Тоді статуси й кількість кваліфікованих компаній зміняться.' if not verified else f'<b>Headcount перевірено через LinkedIn для {verified} з {len(c)} компаній.</b> Відсіяно всіх, хто має понад 200 або менше 10 працівників. Компанії без збігу на LinkedIn лишаються в списку з позначкою «не перевірено».')
rows = [dict(hc=x['li_headcount'] or x['li_headcount_range'], li=x['li_url'], n=x['company'], t=x['tier'], f=float(x['fit_score']), v=int(x['sales_vacancies']), p=int(x['phone_roles']),
             l=x['hiring_sales_leader']=='True', r=x['top_vacancy'], u=x['top_vacancy_url'],
             c=x['cities'].split('|')[0] if x['cities'] else '', cls=[ROLE.get(k,k) for k in x['role_classes'].split('|') if k]) for x in q if x['tier'] in 'ABC']
stats = dict(qualified=len(q), multi=sum(int(x['sales_vacancies'])>=3 for x in q), leader=sum(x['hiring_sales_leader']=='True' for x in q),
             phone=sum(int(x['phone_roles'])>0 for x in q))
tpl = open(HERE / 'template.html').read()
def bars(items, total=None, cls=''):
    m = max(n for _, n in items)
    out = []
    for label, n in items:
        pct = n/m*100
        out.append(f'<div class="bar-row {cls}" tabindex="0" title="{html.escape(label)}: {n}"><span class="bar-label">{html.escape(label)}</span>'
                   f'<span class="bar-track"><span class="bar-fill" style="width:{pct:.1f}%"></span></span><span class="bar-val">{n:,}</span></div>'.replace(',', ' '))
    return '\n'.join(out)
page = (tpl.replace('{{FUNNEL}}', bars(funnel, cls='funnel'))
        .replace('{{ROLES}}', bars([(ROLE.get(k,k), n) for k, n in roles]))
        .replace('{{TIERS}}', bars([(f'Tier {t}', tiers[t]) for t in 'ABCD']))
        .replace('{{DISQ}}', bars([(k.replace(' (known brand, pending LinkedIn check)','').replace('staffing agency / hidden client','Кадрові агенції / прихований клієнт').replace('enterprise','Enterprise за брендом (до перевірки LinkedIn)'), n) for k, n in disq.most_common()]))
        .replace('{{SRC}}', bars(src.most_common()))
        .replace('{{ROWS}}', json.dumps(rows, ensure_ascii=False))
        .replace('{{NOTE}}', NOTE)
        .replace('{{Q}}', str(stats['qualified'])).replace('{{MULTI}}', str(stats['multi'])).replace('{{LEADER}}', str(stats['leader']))
        .replace('{{PHONE}}', str(stats['phone'])).replace('{{A}}', str(tiers['A'])).replace('{{B}}', str(tiers['B'])).replace('{{C}}', str(tiers['C'])))
open(HERE / 'dealaris-prospects.html', 'w').write(page)
print(funnel, tiers, len(rows))
