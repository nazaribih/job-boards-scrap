"""Niche of each tier A+B company: one label from a fixed list plus what it sells, read by the LLM from its
LinkedIn profile, the judge's verdict and its job ads. Cached in data/niche_cache.json.

  python niche.py
"""
import csv
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jbscrap.score import company_key
from judge import ask, load_env

DATA = Path(__file__).resolve().parent / "data"
CACHE = DATA / "niche_cache.json"
NICHES = ["Call-центр / BPO / аутсорс продажів", "Windykacja / стягнення боргів", "B2B SaaS",
          "IT-послуги / software house", "Маркетинг / реклама / lead-gen", "Медіа / видавництва / івенти",
          "Освіта / онлайн-курси", "Фінанси / кредити / страхування", "Енергетика / ОЗЕ",
          "Нерухомість", "Логістика / транспорт", "Телеком", "Медицина / здоров'я", "HR / рекрутинг",
          "E-commerce / B2B-торгівля", "Юридичні / консалтинг / бізнес-послуги", "Інше"]
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["niche", "sells"],
          "properties": {"niche": {"type": "string", "enum": NICHES},
                         "sells": {"type": "string", "description": "What the company sells and to whom, max 12 words, Ukrainian"}}}
SYSTEM = "Classify a Polish company into exactly one niche from the list by what it sells. Base it on the evidence."


def main():
    load_env()
    key = os.environ["OPENAI_API_KEY"]
    li = json.loads((DATA / "unipile_cache.json").read_text())
    vacs = {}
    for v in csv.DictReader(open(DATA / "vacancies.csv", encoding="utf-8")):
        vacs.setdefault(company_key(v["company"]), []).append(v["title"])
    cos = [c for c in csv.DictReader(open(DATA / "companies.csv", encoding="utf-8"))
           if c["status"] == "qualified" and c["tier"] in "AB"]
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    todo = [c for c in cos if company_key(c["company"]) not in cache]
    print(f"[niche] {len(todo)} to classify")

    def one(c):
        k = company_key(c["company"])
        prof = (li.get(k) or {}).get("profile") or {}
        text = (f"Company: {c['company']}\nLinkedIn industry: {c['li_industry']}\nLinkedIn: {(prof.get('description') or '')[:600]}\n"
                f"Judge: {c['llm_reason']}\nJob ads: {'; '.join(vacs.get(k, [])[:8])}")
        try:
            return k, ask(key, text, SYSTEM, SCHEMA)
        except Exception as e:
            print(f"[niche] {c['company']!r}: {str(e)[:100]}")
            return k, None

    with ThreadPoolExecutor(8) as ex:
        for k, v in ex.map(one, todo):
            if v:
                cache[k] = v
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    print(f"[niche] {len(cache)} classified")


if __name__ == "__main__":
    main()
