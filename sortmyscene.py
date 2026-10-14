"""SortMyScene scraper — Tier 1 (direct JSON API, no auth, no browser).

Discovered endpoint (reverse-engineered from the CRA bundle + verified live):

    GET https://sortmyscene.com/events/all-v2
      params: city, startDate (ISO), endDate (ISO), page (0-indexed, 20/page),
              genres (optional, repeated), search (optional)
      -> { "events": [...], "pastEvents": [...], "hasMore": bool }

This is the cheapest possible path: pure requests, no rendering, no proxy.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone

# curl_cffi with a Chrome TLS fingerprint: plain `requests` gets 403 from the
# events API (bot protection) since at least 2026-10-09; the browser-like
# fingerprint gets 200. Same Session/get/post/json API.
from curl_cffi import requests

from schema import Event

IST = timezone(timedelta(hours=5, minutes=30))

BASE = "https://sortmyscene.com/events/all-v2"
EVENT_PAGE = "https://sortmyscene.com/event/"
PAGE_SIZE = 20
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


# SortMyScene has no structured lineup field (always []), and descriptions are
# blank — but ~1/3 of titles name the acts after a "ft./feat./b2b" marker, e.g.
# "COCKTAIL NIGHT FT. DJ KAMRA & DJ HARSH". Parse those out.
_LINEUP_MARKER = re.compile(r"\b(?:ft\.?|feat\.?|featuring|presents|pres\.)\s+", re.I)
_VENUE_TAIL = re.compile(r"\s+(?:\bat\b|@)\s+.*$|\s*(?://|\|).*$", re.I)
_ARTIST_SPLIT = re.compile(r"\s*(?:&|\+|,|/|\bb2b\b|\bx\b|\bvs\.?\b)\s*", re.I)
# trailing noise words that aren't part of an artist name
_NOISE_TAIL = re.compile(r"\s+(?:live|music|band|dj set|set|tour|show|night|edition|india)\s*$", re.I)
_NOISE_WHOLE = {"more", "others", "guests", "surprise act", "special guest", "many more", "friends"}


def parse_lineup_from_title(title: str) -> list[str]:
    m = _LINEUP_MARKER.search(title or "")
    if not m:
        return []
    tail = title[m.end():]
    tail = _VENUE_TAIL.sub("", tail)            # drop "... at Venue", "// ...", "| ..."
    parts = _ARTIST_SPLIT.split(tail)
    seen, out = set(), []
    for p in parts:
        p = p.strip(" -–—")
        p = _NOISE_TAIL.sub("", p).strip()      # "Vaishnav Girish Live" -> "Vaishnav Girish"
        if len(p) > 1 and p.lower() not in seen and p.lower() not in _NOISE_WHOLE:
            seen.add(p.lower())
            out.append(p)
    return out


def _iso(d: str) -> str:
    """Accepts DD-MM-YYYY (as in the site URL) or YYYY-MM-DD -> ISO Z string."""
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt).replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    raise ValueError(f"Unrecognised date: {d!r} (use DD-MM-YYYY or YYYY-MM-DD)")


def fetch_page(session, city, start_iso, end_iso, page):
    params = {
        "city": city,
        "startDate": start_iso,
        "endDate": end_iso,
        "page": page,
    }
    # The API rate-limits (429) bursts; back off and retry rather than lose
    # the whole city's scrape.
    for attempt in range(5):
        r = session.get(BASE, params=params, timeout=30)
        if r.status_code != 429:
            break
        wait = 20 * (attempt + 1)
        print(f"  429 on page {page}, waiting {wait}s", file=sys.stderr)
        time.sleep(wait)
    r.raise_for_status()
    return r.json()


def normalise(rec: dict) -> Event:
    raw_start = rec.get("rawStartTime")
    raw_end = rec.get("rawEndTime")
    # India time, not UTC: the importer keeps the date part, and a 1 AM show
    # stamped in UTC would land on the previous day.
    start_iso = (datetime.fromtimestamp(raw_start / 1000, tz=IST).isoformat()
                 if isinstance(raw_start, (int, float)) else None)
    end_iso = (datetime.fromtimestamp(raw_end / 1000, tz=IST).isoformat()
               if isinstance(raw_end, (int, float)) else None)
    slug = rec.get("event_url", "")
    price = rec.get("priceLabel") or (str(rec["l_price"]) if rec.get("l_price") else None)
    name = rec.get("event_name", "").strip()
    # prefer a structured artists list if the site ever populates it, else title-parse
    structured = [a.get("name") if isinstance(a, dict) else a for a in (rec.get("artists") or [])]
    lineup = [a for a in structured if a] or parse_lineup_from_title(name)
    return Event(
        source="sortmyscene",
        source_id=str(rec.get("_id", "")),
        title=name,
        url=EVENT_PAGE + slug if slug else "",
        city=rec.get("location"),
        venue=rec.get("club"),
        start=start_iso,
        end=end_iso,
        date_label=rec.get("dateLabel") or rec.get("date"),
        start_time=rec.get("start_time"),
        end_time=rec.get("end_time"),
        price=price,
        currency=rec.get("currency"),
        categories=rec.get("event_category") or [],
        lineup=lineup,                   # from structured artists if present, else parsed from title
        image=rec.get("event_img"),
        raw=rec,
    )


DETAIL_EP = "https://sortmyscene.com/events/eventId-v2"


def enrich_description(session, ev, delay=0.25):
    """SortMyScene listing carries no description; the detail sheet does.
    Grab it as raw text for AI to mine (artist names etc.)."""
    slug = ev.url.split("/event/")[-1] if ev.url else ""
    if not slug:
        return
    try:
        r = session.post(DETAIL_EP, json={"name": slug, "draft": False}, timeout=25)
        if r.status_code == 200:
            d = (r.json().get("event") or {})
            desc = d.get("description")
            if isinstance(desc, str) and desc.strip():
                ev.description = re.sub(r"<[^>]+>", " ", desc).strip()[:2000]
            structured = [a.get("name") if isinstance(a, dict) else a for a in (d.get("artists") or [])]
            structured = [a for a in structured if a]
            if structured:
                ev.lineup = structured
    except Exception:
        pass
    time.sleep(delay)


def scrape(city, start, end, max_pages=50, delay=0.5, enrich=True):
    start_iso, end_iso = _iso(start), _iso(end)
    session = requests.Session(impersonate="chrome124")
    session.headers.update({"User-Agent": UA, "Accept": "application/json"})

    events, seen = [], set()
    for page in range(max_pages):
        data = fetch_page(session, city, start_iso, end_iso, page)
        batch = data.get("events", []) or []
        for rec in batch:
            ev = normalise(rec)
            if ev.source_id in seen:
                continue
            seen.add(ev.source_id)
            events.append(ev)
        print(f"  page {page}: +{len(batch)} (total {len(events)}) hasMore={data.get('hasMore')}",
              file=sys.stderr)
        if not data.get("hasMore") or not batch:
            break
        time.sleep(delay)

    if enrich:
        print(f"  enriching {len(events)} with description...", file=sys.stderr)
        for ev in events:
            enrich_description(session, ev)
    return events


def main():
    ap = argparse.ArgumentParser(description="Scrape SortMyScene events via its public JSON API.")
    ap.add_argument("--city", default="Bengaluru")
    ap.add_argument("--start", default="25-07-2026", help="DD-MM-YYYY or YYYY-MM-DD")
    ap.add_argument("--end", default="25-07-2026", help="DD-MM-YYYY or YYYY-MM-DD")
    ap.add_argument("--out", default="sortmyscene_events")
    args = ap.parse_args()

    print(f"[sortmyscene] {args.city} {args.start}..{args.end}", file=sys.stderr)
    events = scrape(args.city, args.start, args.end)
    rows = [e.to_dict() for e in events]

    with open(f"{args.out}.json", "w") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)

    if rows:
        cols = [c for c in rows[0] if c != "raw"]
        with open(f"{args.out}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({k: ("|".join(v) if isinstance(v, list) else v) for k, v in r.items() if k != "raw"})

    print(f"[sortmyscene] {len(rows)} events -> {args.out}.json / .csv", file=sys.stderr)


if __name__ == "__main__":
    main()
