"""Dealaris-fit scoring for vacancies and companies.

ICP (pitch deck, Oct 2026): teams that sell over the phone or on video calls —
telesales / call centres / outsourced sales, SDR/BDR, sales-led B2B SaaS AEs,
high-ticket B2C (cars, real estate, PV/OZE, finance, info-products).
SMB / mid-market only: 10-200 employees, Poland first.
"""
import re
import unicodedata


def fold(s):
    s = unicodedata.normalize("NFKD", (s or "").lower().replace("ł", "l"))
    return "".join(c for c in s if not unicodedata.combining(c))


def _rx(*words):
    return re.compile("|".join(words))


# role class -> (pattern on folded title, base score 0-100)
ROLE_RULES = [
    ("retail_or_field", _rx(r"sprzedaw(ca|czyni)\b", r"kasjer", r"stacji paliw", r"w sklepie", r"salon(ie)? sprzeda",
                            r"merchandis", r"przedstawiciel(ka)? handlow", r"\bterenow", r"\bfield sales",
                            r"magazyn", r"kierowca", r"promotor", r"hostess", r"\bpos\b", r"vending", r"handlowiec terenowy"), 0),
    ("sales_leadership", _rx(r"head of sales", r"sales director", r"dyrektor(ka)? (ds\.? )?sprzeda", r"kierownik(czka)? (dzialu |zespolu )?(ds\.? )?sprzeda",
                             r"sales manager", r"team lead(er)?", r"supervisor", r"koordynator(ka)? (zespolu|sprzeda)", r"vp sales",
                             r"sales enablement", r"sales operations", r"revops", r"\bcso\b", r"lider(ka)? zespolu"), 70),
    ("telesales_callcenter", _rx(r"telesprzeda", r"telemarket", r"telefoniczn", r"call ?cent", r"contact ?cent",
                                 r"teleagent", r"sprzedaz przez telefon", r"outbound", r"konsultant(ka)? ds\.? sprzeda"), 95),
    ("sdr_bdr_inside", _rx(r"\bsdr\b", r"\bbdr\b", r"sales development", r"business development rep", r"inside sales",
                           r"lead gen", r"pozyskiwani\w* klient", r"new business", r"appointment setter", r"cold call",
                           r"junior sales", r"sales representative", r"specjalist\w* ds\.? pozyskiwania"), 90),
    ("ae_b2b_saas", _rx(r"account executive", r"\bae\b", r"saas", r"business development", r"sales executive",
                        r"sales consultant", r"solution sales", r"b2b", r"partnership", r"inside account"), 80),
    ("high_ticket_b2c", _rx(r"doradc\w* (klienta|handlow|ds\.? sprzeda|kredyt|finans|ubezpiecz|inwestyc|energet|samochod|techniczn)",
                            r"nieruchomo", r"posrednik", r"agent(ka)? (nieruchomosci|ubezpiecz)", r"fotowolta", r"\boze\b",
                            r"pomp\w* ciepla", r"energ\w* odnawial", r"samochod", r"\baut\b|motoryzac", r"kredyt", r"ubezpiecze",
                            r"inwestyc", r"edukac", r"kurs", r"szkole", r"opiekun(ka)? klienta"), 80),
    ("account_mgmt", _rx(r"account manager", r"key account", r"\bkam\b", r"customer success", r"opiekun"), 55),
    ("customer_service", _rx(r"obslug\w* klienta", r"customer (service|support|care)", r"infolini", r"helpdesk"), 35),
    ("sales_specialist", _rx(r"sales specialist", r"specjalist\w* ds\.? sprzeda", r"handlowiec"), 60),
    ("generic_sales", _rx(r"sprzeda", r"sales", r"handlow"), 50),
]

# description / tag signals (folded text) -> points
SIGNALS = [
    ("phone", _rx(r"telefon", r"rozmow\w* telefon", r"call", r"sluchawk", r"dzwon"), 10),
    ("video_calls", _rx(r"spotkania online", r"online", r"zoom", r"teams", r"google meet", r"wideo", r"video", r"demo", r"prezentac"), 6),
    ("remote", _rx(r"zdaln", r"\bremote\b"), 4),
    ("junior_ramp", _rx(r"bez doswiadczenia", r"\bjunior\b", r"stazyst", r"szkoleni", r"wdrozeni", r"trainee", r"entry"), 6),
    ("commission", _rx(r"prowizj", r"premi", r"commission", r"bonus"), 4),
    ("script_crm", _rx(r"skrypt", r"\bcrm\b", r"pipedrive", r"hubspot", r"salesforce", r"livespace"), 5),
    ("b2b_inbound_outbound", _rx(r"b2b", r"cold", r"outbound", r"lead"), 4),
]

# recruiters posting for hidden clients: the real buyer is unknown
STAFFING = _rx(r"klient rocketjobs", r"klient praca", r"randstad", r"adecco", r"manpower", r"hays", r"grafton", r"gi group",
               r"work service", r"antal", r"michael page", r"hrk\b", r"personnel service", r"trenkwalder", r"otta\b",
               r"agencja pracy", r"agencja rekrutac", r"ework", r"devire", r"sthree", r"recruit", r"rekrutac", r"\bhr\b",
               r"luxoft", r"robert walters", r"hunters", r"talent", r"pracodawca prywatny", r"poufne", r"confidential",
               r"portal praca\.pl", r"klient portalu", r"urzad pracy", r"personal", r"recru", r"job ?finder", r"headhunt")

# well-known large employers; provisional enterprise flag until LinkedIn headcount is in
KNOWN_ENTERPRISE = _rx(r"t-mobile", r"orange", r"play polska", r"p4 sp", r"polkomtel", r"cyfrowy polsat", r"vectra",
                       r"ttec", r"lyreco", r"general logistics", r"\bgls\b", r"zepter", r"wittchen",
                       r"\bupc\b", r"netia", r"pko", r"mbank", r"santander", r"\bing\b", r"pekao", r"millennium", r"alior",
                       r"credit agricole", r"bnp paribas", r"citi", r"allianz", r"\bpzu\b", r"generali", r"warta", r"ergo hestia",
                       r"aviva", r"axa", r"uniqa", r"compensa", r"nationale", r"prudential", r"circle k", r"orlen", r"lidl",
                       r"biedronka", r"jeronimo", r"pepco", r"rossmann", r"kaufland", r"carrefour", r"auchan", r"ikea", r"leroy",
                       r"castorama", r"obi\b", r"media expert", r"rtv euro", r"\bx-kom", r"allegro", r"teleperformance", r"majorel",
                       r"webhelp", r"concentrix", r"transcom", r"sitel", r"foundever", r"tauron", r"\bpge\b", r"enea", r"energa",
                       r"e\.on", r"innogy", r"inpost", r"\bdhl\b", r"\bdpd\b", r"poczta polska", r"\bups\b", r"fedex", r"samsung",
                       r"\blg\b", r"philips", r"siemens", r"\babb\b", r"microsoft", r"google", r"amazon", r"oracle", r"\bsap\b",
                       r"salesforce", r"\bibm\b", r"accenture", r"capgemini", r"deloitte", r"\bpwc\b", r"\bkpmg\b", r"\bey\b",
                       r"provident", r"vivus", r"wonga", r"unilever", r"procter", r"nestle", r"coca",
                       r"pepsi", r"mars\b", r"mondelez", r"l.oreal", r"avon", r"oriflame", r"benefit systems", r"medicover",
                       r"luxmed", r"lux med", r"enel-med", r"pracuj", r"grupa pracuj", r"olx", r"otodom", r"booksy", r"docplanner",
                       r"znanylekarz", r"livechat", r"text s\.a", r"comarch", r"asseco", r"wirtualna polska", r"\bwp\b",
                       r"onet", r"ringier", r"agora", r"kruk", r"best s\.a", r"intrum", r"eurobank", r"cofidis", r"santander consumer",
                       r"\bbik\b", r"smartney", r"bricomarche", r"intermarche", r"decathlon", r"ccc\b", r"lpp\b",
                       r"empik", r"zabka", r"żabka", r"dino\b", r"stokrotka", r"netto", r"aldi", r"\bnovo nordisk", r"bayer",
                       r"enter air", r"\blot\b", r"ryanair", r"wizz", r"veolia", r"\bmetro\b", r"selgros", r"makro")

LEGAL = re.compile(r"\b(p\.?\s*s\.?\s*a\.?|sp\.?\s*z\s*o\.?\s*o\.?|spolka z ograniczona odpowiedzialnoscia|s\.?\s*a\.?|sp\.?\s*k\.?|sp\.?\s*j\.?|"
                   r"spolka (akcyjna|komandytowa|jawna)|sp\.?\s*z\.?o\.?o\.?|sp\. k\.|gmbh|ltd|llc|inc|sa|s\.r\.o|oddzial w polsce|"
                   r"poland|polska|polsce|group|grupa)\b\.?")


def company_key(name):
    s = fold(name)
    s = LEGAL.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or re.sub(r"[^a-z0-9]+", "", fold(name))


def is_known_enterprise(name):
    # dealers / partners carry the big brand's name but are SMBs ("Orange partner - Impro", "Skoda Dobroń")
    n = fold(name)
    return bool(KNOWN_ENTERPRISE.search(n)) and not re.search(r"partner|autoryzowan|dealer|salon", n)


HIGH_TICKET_GOODS = _rx(r"samochod", r"nieruchomo", r"fotowolta", r"pomp\w* ciepla", r"\boze\b", r"jacht", r"maszyn")


def classify(title):
    t = fold(title)
    for cls, rx, base in ROLE_RULES:
        if rx.search(t):
            # showroom sellers of cars / real estate / PV still run long phone + meeting cycles
            if cls == "retail_or_field" and HIGH_TICKET_GOODS.search(t):
                return "high_ticket_b2c", 70
            return cls, base
    return "non_sales", 0


def score_vacancy(o):
    cls, base = classify(o["title"])
    text = fold(" ".join([o.get("title", ""), o.get("description", ""), o.get("workplace", ""), o.get("seniority", "")]))
    hits = [name for name, rx, _ in SIGNALS if rx.search(text)]
    pts = sum(p for name, _, p in SIGNALS if name in hits)
    score = 0 if base == 0 else min(100, base + pts)
    return cls, score, hits


def company_rollup(vacs):
    """vacs: scored vacancies for one company -> dict of company-level metrics + fit score."""
    scored = [v for v in vacs if v["role_score"] > 0]
    classes = sorted({v["role_class"] for v in scored})
    best = max((v["role_score"] for v in scored), default=0)
    n = len(scored)
    phone_heavy = sum(v["role_class"] in ("telesales_callcenter", "sdr_bdr_inside") for v in scored)
    leader_hire = any(v["role_class"] == "sales_leadership" for v in scored)
    # best role fit, then volume (hiring several reps = ramp pain), phone share, and a new sales leader (fresh buyer)
    # role fit dominates: one AE / SDR opening at a SaaS company is already a real prospect
    fit = best * 0.75 + min(n, 6) / 6 * 15 + (phone_heavy / n * 5 if n else 0) + (5 if leader_hire else 0)
    return dict(fit_score=round(min(fit, 100), 1), sales_vacancies=n, role_classes="|".join(classes),
                phone_roles=phone_heavy, hiring_sales_leader=leader_hire)
