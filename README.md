# job-boards-scrap

Prospect list for **Dealaris** (live AI copilot for sales calls) built from Polish job boards: companies that are
hiring people who sell over the phone or on online meetings are the ones whose reps need help on every call.

## Pipeline

```
python run.py              # scrape boards -> score -> dedupe -> LinkedIn headcount (if Unipile env set)
python run.py --no-scrape  # re-score / enrich from data/raw_offers.json
```

1. **Scrape** (own fetchers, public endpoints, no paid scraping APIs)
   - rocketjobs.pl — JSON candidate API, categories `sales`, `support`
   - nofluffjobs.com — search API, categories `sales`, `customerService`
   - praca.pl, aplikuj.pl — HTML listings, 44 phone/SDR/AE/high-ticket keywords
   - rocketjobs offer details — full description + employer size (`501+` → enterprise)
   - pracuj.pl, olx.pl, indeed, gowork.pl, careerjet.pl, theprotocol.it sit behind a Cloudflare challenge and are skipped.
2. **Score each vacancy** (`jbscrap/score.py`): role class from the title (telesales/call centre 95, SDR/BDR 90,
   AE/B2B SaaS 80, high-ticket B2C 80, sales leadership 70, …; retail/field sales 0) plus signals from the
   description (phone, online meetings, remote, junior/onboarding, scripts/CRM, commission).
3. **Dedupe** vacancies (company + title + city) and companies (name normalised, legal forms stripped).
4. **Company fit score** = best role × 0.75 + hiring volume (up to 15) + share of phone roles (up to 5)
   + 5 if hiring a sales leader (new buyer). Tiers: A ≥ 80, B ≥ 70, C ≥ 60, D < 60.
5. **Disqualify** staffing agencies / hidden clients, known enterprise brands, and — once Unipile is available —
   anything with LinkedIn headcount > 200 or < 10.

## Unipile (LinkedIn headcount)

Set in the environment: `UNIPILE_DSN`, `UNIPILE_API_KEY` (or `UNIPILE_TOKEN`), `UNIPILE_ACCOUNT_ID` (or `NAZARII_UNIPILE_LINKEDIN_ID`).
The Claude cloud environment only allows outbound HTTPS on port 443, and Unipile DSNs use custom ports, so run the
enrichment locally (or anywhere without that restriction) and commit `data/unipile_cache.json`.
Same values as the `dealaris-outreach-helper` service on Railway (`zippy-nourishment`). Each company costs two
calls (search + profile), 4–9 s apart; results are cached in `data/unipile_cache.json`, so a re-run only looks up
new companies. `--max-lookups` caps a run (default 600).

## Output

- `data/companies.csv` — one row per company: tier, status, reason, fit score, vacancy counts, top vacancy, LinkedIn fields
- `data/vacancies.csv` — every sales vacancy with role class, score, signals, link

## Report

`python report/build.py` renders `report/dealaris-prospects.html` (stats page, published as a claude.ai artifact)
from `data/companies.csv` / `data/vacancies.csv`.

## Local run (pracuj.pl + LinkedIn)

Two steps can't run in the Claude cloud sandbox: pracuj.pl blocks datacenter IPs behind Cloudflare, and the sandbox
only allows outbound HTTPS on port 443 while Unipile DSNs use custom ports. On a laptop with Google Chrome:

```
git clone -b claude/sales-prospect-research-vlgnq1 https://github.com/nazaribih/job-boards-scrap
cd job-boards-scrap && python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
python run.py --sources pracuj --merge --max-lookups 0   # plain HTTP from a home IP; falls back to Chrome if Cloudflare blocks it
export UNIPILE_DSN=... UNIPILE_TOKEN=... NAZARII_UNIPILE_LINKEDIN_ID=...
python run.py --no-scrape --max-lookups 800               # LinkedIn headcount, ~1.5 h, resumable via cache
python report/build.py
```
