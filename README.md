# event-scrapers

Cheap, tiered event scrapers for multiple ticketing/discovery platforms. Every
site normalizes into **one shared `Event` schema** (`schema.py`), so all sources
produce identical records — trivial to merge, dedup, and store.

The core idea: **match the technique to the site's anti-bot tier, and always
take the cheapest path that works.** Five distinct extraction techniques are
demonstrated here — reuse whichever fits a new target.

## Sources & techniques

| Site | Region | Technique | Anti-bot | File |
|------|--------|-----------|----------|------|
| **SortMyScene** | IN | Direct JSON API (`/events/all-v2`) | none | `sortmyscene.py` |
| **AllEvents.in** | global | schema.org JSON-LD in page | none | `jsonld_scraper.py` |
| **HighApe** | IN | schema.org JSON-LD in page | none | `jsonld_scraper.py` |
| **BookMyShow** | IN | discovery API via `curl_cffi` TLS impersonation | Cloudflare | `bookmyshow.py` |
| **District** (Zomato) | IN | Playwright intercepts discovery API | Akamai sensor cookie | `district.py` |
| **Platinumlist** | Gulf/ME | raw HTML parse (listing + detail) | none | `platinumlist.py` |

### The five techniques (reusable playbook)
1. **Direct JSON/GraphQL API** — probe the site for a hidden backend before anything else. Cheapest possible. (SortMyScene; also RA.co has a public GraphQL API.)
2. **schema.org JSON-LD** — many sites embed `<script type="application/ld+json">` Event objects for SEO. One generic extractor works across all of them. (AllEvents, HighApe.)
3. **TLS-fingerprint impersonation** (`curl_cffi`) — defeats Cloudflare's passive TLS/JA3 check without a browser. Reverse-engineer the API contract from the page's `__INITIAL_STATE__`/JS bundle. (BookMyShow.)
4. **Browser interception** (Playwright) — when anti-bot needs real JS execution (Akamai sensor cookies), let a headless browser do the work and intercept the API responses it fires. Reconstruct nothing. (District.)
5. **Raw HTML parse** — no API, no JSON-LD, no data island: parse the server-rendered listing + detail pages directly. (Platinumlist.)

**Decision order for a new site:** API → JSON-LD → `curl_cffi`+API → Playwright interception → HTML parse.

## Install
```bash
pip install -r requirements.txt
playwright install chromium     # one-time, only needed for District
```

## Run each source
```bash
# SortMyScene — public JSON API (city + date range, ISO or DD-MM-YYYY)
python3 sortmyscene.py --city Bengaluru --start 28-07-2026 --end 15-08-2026

# AllEvents / HighApe — generic JSON-LD extractor (--enrich fetches detail pages for lineup)
python3 jsonld_scraper.py allevents --city Bengaluru --enrich
python3 jsonld_scraper.py highape --city Bengaluru

# BookMyShow — Cloudflare-fronted discovery API (swap the path for any category)
python3 bookmyshow.py --path "comedy-shows-bengaluru?daygroups=custom:20260725-20260725" --city Bengaluru

# District — Playwright interception across category hubs
python3 district.py --categories music,nightlife,comedy,sports,performances --only-city Bengaluru

# Platinumlist — Middle East; city = subdomain, currency via cookie
python3 platinumlist.py --city dubai --currency AED           # all categories, paginated
python3 platinumlist.py --city riyadh --paths /concert,/comedy
```

Each writes `<out>.json` (full records incl. `raw` source blob) and `<out>.csv`
(flat, minus `raw`). See `samples/` for example output and a viewable HTML table.

## The shared schema (`schema.py`)
Every source maps into this. Fields absent at a source are left null.
```
source, source_id, title, url, city, venue, start, end, date_label,
start_time, end_time, price, currency, categories, lineup,
description, image, raw
```
- **`lineup`** — structured artists where the source has them (BookMyShow, District concerts, RA.co); else parsed from the title (`ft./feat./b2b`).
- **`description`** — raw unstructured blurb. Not every source publishes one; where present it names artists/venue/date and is intended for downstream **AI extraction** of anything not structured.
- **`url` / booking** — the event's ticket page (the booking link) for every source.

## Per-source notes / gotchas
- **BookMyShow**: passes Cloudflare only via `curl_cffi` (Chrome impersonation). Magic headers: `x-app-code: MOBAND2`, `x-platform: WEB`, `x-bms-id: guest`, cookie `rgn=|Code=<REGION>|`. Date/time/lineup come from a per-event `bottomsheet/<ETcode>` detail call. Paginates via `scrollId`.
- **District**: data is gated by a short-lived Akamai `bm_sv` cookie that only a real browser can mint — hence Playwright. We intercept `get_discovery_results` while scrolling; lineup + description come from `getBySlug/<slug>` (needs the district headers). Real event URL is `{slug}-buy-tickets`. Hubs mix in some non-local promos — filter by `city`.
- **Platinumlist**: pure server-rendered HTML. City = subdomain (`dubai.`, `riyadh.`…). Price geo-localizes to the request IP — force currency with the `user_currency` cookie (`--currency AED`). Listing gives title+price+url; detail page adds venue+date+description.
- **SortMyScene**: has no structured lineup or description — artists live in the event title (parsed). Public API, 0-indexed `page`, 20/page, loop on `hasMore`.

## Extending
- **New city**: pass `--city`; for BookMyShow add its region code to `REGIONS`, for Platinumlist it's just the subdomain.
- **New JSON-LD site**: add one line to `SITES` in `jsonld_scraper.py`.
- **Merge sources**: all outputs share the schema — concatenate the JSON/CSV, dedup on `(source, title, venue)`.

## Not yet built (candidates)
RA.co (public GraphQL — easy), Party Hunt (WordPress REST API), Townscript/Explara (SPA, needs API probe). See the technique decision order above.
