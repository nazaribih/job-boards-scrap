"""Which language to write each decision maker in: pl, uk or en, from name, headline language and location.
Cached in data/lang_cache.json; people.py / outreach export read it.

  python lang.py
"""
import csv
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import store
from judge import ask, load_env

DATA = Path(__file__).resolve().parent / "data"
CACHE = store.path("lang_cache.json")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["lang", "origin", "confidence"],
          "properties": {"lang": {"type": "string", "enum": ["pl", "uk", "en"]},
                         "origin": {"type": "string", "description": "Likely origin of the name, e.g. Polish, Ukrainian, Lithuanian, French"},
                         "confidence": {"type": "string", "enum": ["high", "medium", "low"]}}}
SYSTEM = """Pick the language for a cold LinkedIn message to a person at a company in Poland.
pl: Polish name, or the headline is written in Polish (a Polish headline means they work in Polish, whatever the name).
uk: the name is clearly Ukrainian (e.g. -enko, -chuk, -iv, Oleksandr, Mykola, Yuliia) and the headline is not in Polish.
en: everyone else (other nationalities, English-only headlines with non-Polish names, unclear cases).
Never pick uk for Russian or Belarusian names; use en for those unless the headline is Polish."""


def main():
    load_env()
    key = os.environ["OPENAI_API_KEY"]
    rows = [r for r in csv.DictReader(store.path("decision_makers.csv").open(encoding="utf-8")) if r["name"]]
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    todo = [r for r in rows if r["name"] not in cache]

    def one(r):
        try:
            return r["name"], ask(key, f"Name: {r['name']}\nHeadline: {r['headline']}\nCompany: {r['company']}", SYSTEM, SCHEMA)
        except Exception:
            return r["name"], None

    with ThreadPoolExecutor(8) as ex:
        for name, v in ex.map(one, todo):
            if v:
                cache[name] = v
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    from collections import Counter
    print(Counter(cache[r["name"]]["lang"] for r in rows if r["name"] in cache))


if __name__ == "__main__":
    main()
