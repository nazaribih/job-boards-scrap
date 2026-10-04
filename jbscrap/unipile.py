"""LinkedIn company enrichment via Unipile (headcount, LinkedIn URL, industry).

Env: UNIPILE_DSN (e.g. api8.unipile.com:13851), UNIPILE_API_KEY or UNIPILE_TOKEN, and
UNIPILE_ACCOUNT_ID (falls back to UNIPILE_ACCOUNT_ID_NAZARII / NAZARII_UNIPILE_LINKEDIN_ID). Results are cached in data/unipile_cache.json so
re-runs don't spend LinkedIn lookups again.
"""
import json
import os
import random
import re
import time
from pathlib import Path

import requests

from .score import LEGAL, company_key, fold

CACHE = Path(__file__).resolve().parent.parent / "data" / "unipile_cache.json"


def _env(*names):
    return next((os.environ[n] for n in names if os.environ.get(n)), "")


KEY_VARS = ("UNIPILE_API_KEY", "UNIPILE_TOKEN")
ACCOUNT_VARS = ("UNIPILE_ACCOUNT_ID", "UNIPILE_ACCOUNT_ID_NAZARII", "NAZARII_UNIPILE_LINKEDIN_ID")


class Unipile:
    def __init__(self):
        dsn = os.environ["UNIPILE_DSN"].strip().rstrip("/")
        self.base = dsn if dsn.startswith("http") else f"https://{dsn}"
        self.account = _env(*ACCOUNT_VARS)
        self.s = requests.Session()
        self.s.headers.update({"X-API-KEY": _env(*KEY_VARS), "accept": "application/json"})

    @staticmethod
    def available():
        return bool(os.environ.get("UNIPILE_DSN") and _env(*KEY_VARS) and _env(*ACCOUNT_VARS))

    def _req(self, method, path, **kw):
        for attempt in range(4):
            r = self.s.request(method, f"{self.base}{path}", params={"account_id": self.account}, timeout=60, **kw)
            if r.status_code in (500, 502, 503, 504):
                time.sleep(5 * 2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    def search_companies(self, keywords, limit=5):
        d = self._req("POST", "/api/v1/linkedin/search", json={"api": "classic", "category": "companies", "keywords": keywords})
        return (d.get("items") or [])[:limit]

    def company(self, identifier):
        return self._req("GET", f"/api/v1/linkedin/company/{identifier}")


GENERIC = {"polska", "poland", "group", "grupa", "the", "and", "company", "services", "solutions", "consulting",
           "sklep", "biuro", "centrum", "firma", "studio", "agencja", "europe", "international"}
BOARD_HOSTS = ("rocketjobs.pl", "justjoin.it", "pracuj.pl", "praca.pl", "aplikuj.pl", "nofluffjobs.com")


def _tokens(name):
    return {t for t in re.split(r"[^a-z0-9]+", LEGAL.sub(" ", fold(name))) if len(t) >= 3 and t not in GENERIC}


def _domain(url):
    host = re.sub(r"^[a-z]+://", "", (url or "").strip().lower()).split("/")[0].split(":")[0]
    host = host[4:] if host.startswith("www.") else host
    return "" if not host or host.endswith(BOARD_HOSTS) else host


def _query(name):
    """Search keywords: the name without legal form / tagline ("Teltonika Poland sp. z o.o." -> "teltonika poland")."""
    q = LEGAL.sub(" ", fold(name.split("|")[0]))
    q = re.sub(r"\bsp(olka|\.)?\s*(z\s*o\.?\s*o\.?|komandytowa|k\.|j\.|akcyjna)", " ", q)
    q = " ".join(re.split(r"[^a-z0-9&.]+", q)).strip(" .")
    return re.sub(r"\s+", " ", q) or name


def _pick(name, items):
    """Best search hit for a board company name: exact normalized match, else a hit whose name contains every
    significant token of the board name ("fuzzy"); anything looser picked the wrong company too often."""
    key = company_key(name)
    for it in items:
        if company_key(it.get("name", "")) == key:
            return it, "exact"
    toks = _tokens(name)
    for it in items:
        if toks and toks <= _tokens(it.get("name", "")):
            return it, "fuzzy"
    return None, "none"


def _in_poland(prof):
    locs = prof.get("locations") or []
    if locs:
        return any((l.get("country") or "").upper() == "PL" for l in locs)
    return _domain(prof.get("website")).endswith(".pl")


def _check(entry, name, website):
    """Re-judge a cached lookup with the current rules (no API calls): re-pick from the stored hits and confirm
    or reject a fuzzy match by comparing the company's website with the LinkedIn one."""
    e = dict(entry)
    if e.get("match") in ("exact", "fuzzy"):
        hits = e.get("search_items") or [e.get("search_hit")]
        hit, match = _pick(name, [h for h in hits if h])
        prof = e.get("profile") or {}
        if hit is None or (hit.get("id") != (e.get("search_hit") or {}).get("id")):
            # the stored profile belongs to a hit the current rules no longer pick
            return dict(e, match="none" if hit is None else "repick", profile={})
        e["match"] = match
        mine, theirs = _domain(website), _domain(prof.get("website"))
        if mine and theirs:
            if mine == theirs:
                e["match"] = "domain" if match == "fuzzy" else match
            elif match == "fuzzy":
                return dict(e, match="none", profile={})
        if e["match"] == "fuzzy" and not _in_poland(prof):
            return dict(e, match="none", profile={})
    return e


def _headcount(p):
    n = p.get("employee_count")
    rng = p.get("employee_count_range") or {}
    return n, rng.get("from"), rng.get("to")


def enrich(companies, max_lookups=150, delay=(4, 9)):
    """companies: list of dicts with 'company' (display name) and 'company_key'. Mutates in place."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    api = Unipile() if Unipile.available() and max_lookups > 0 else None
    done = 0
    for c in companies:
        k = c["company_key"]
        old = cache.get(k)
        # lookups made with the raw name (legal form included) found nothing; retry those once with the clean query
        stale = old is not None and old.get("match") == "none" and old.get("query") != _query(c["company"])
        if old is None or stale:
            if api is None or done >= max_lookups:
                if old is None:
                    continue
            else:
                q = _query(c["company"])
                try:
                    items = api.search_companies(q)
                    hit, match = _pick(c["company"], items)
                    prof = api.company(hit.get("public_identifier") or hit["id"]) if hit else {}
                    cache[k] = {"match": match, "query": q, "search_hit": hit, "search_items": items, "profile": prof}
                except requests.HTTPError as e:
                    if e.response is not None and e.response.status_code in (401, 403, 429):
                        # auth problem or LinkedIn throttling the account: stop, don't keep hammering it
                        print(f"[unipile] {e.response.status_code}, stopping lookups: {str(e)[:120]}")
                        api = None
                        continue
                    cache[k] = {"match": "error", "query": q, "error": str(e)[:200]}
                except requests.ConnectionError as e:
                    # unreachable (e.g. a sandbox that only allows port 443): stop looking up, keep what's cached
                    print(f"[unipile] unreachable, skipping remaining lookups: {str(e)[:120]}")
                    api = None
                    continue
                done += 1
                CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
                time.sleep(random.uniform(*delay))
        e = cache.get(k)
        if not e:
            continue
        e = _check(e, c["company"], c.get("website"))
        p = e.get("profile") or {}
        n, lo, hi = _headcount(p)
        c.update(li_match=e.get("match", ""), li_name=p.get("name", ""),
                 li_url=p.get("profile_url") or (e.get("search_hit") or {}).get("profile_url", ""),
                 li_headcount=n if n is not None else "", li_headcount_range=f"{lo}-{hi}" if lo is not None else "",
                 li_industry=", ".join(p.get("industry") or []) if isinstance(p.get("industry"), list) else (p.get("industry") or ""),
                 li_website=p.get("website", ""))
    return done
