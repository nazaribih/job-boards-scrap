"""LinkedIn company enrichment via Unipile (headcount, LinkedIn URL, industry).

Env: UNIPILE_DSN (e.g. api8.unipile.com:13851), UNIPILE_API_KEY or UNIPILE_TOKEN, and
UNIPILE_ACCOUNT_ID (falls back to UNIPILE_ACCOUNT_ID_NAZARII / NAZARII_UNIPILE_LINKEDIN_ID). Results are cached in data/unipile_cache.json so
re-runs don't spend LinkedIn lookups again.
"""
import json
import os
import random
import time
from pathlib import Path

import requests

from .score import company_key

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
            if r.status_code in (429, 500, 502, 503, 504):
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


def _pick(name, items):
    """Best search hit for a board company name: exact normalized match first, else first hit."""
    key = company_key(name)
    for it in items:
        if company_key(it.get("name", "")) == key:
            return it, "exact"
    return (items[0], "fuzzy") if items else (None, "none")


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
        if k not in cache:
            if api is None or done >= max_lookups:
                continue
            try:
                hit, match = _pick(c["company"], api.search_companies(c["company"]))
                prof = api.company(hit.get("public_identifier") or hit["id"]) if hit else {}
                cache[k] = {"match": match, "search_hit": hit, "profile": prof}
            except requests.HTTPError as e:
                cache[k] = {"match": "error", "error": str(e)[:200]}
            done += 1
            CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
            time.sleep(random.uniform(*delay))
        e = cache.get(k)
        if not e:
            continue
        p = e.get("profile") or {}
        n, lo, hi = _headcount(p)
        c.update(li_match=e.get("match", ""), li_name=p.get("name", ""),
                 li_url=p.get("profile_url") or (e.get("search_hit") or {}).get("profile_url", ""),
                 li_headcount=n if n is not None else "", li_headcount_range=f"{lo}-{hi}" if lo is not None else "",
                 li_industry=", ".join(p.get("industry") or []) if isinstance(p.get("industry"), list) else (p.get("industry") or ""),
                 li_website=p.get("website", ""))
    return done
