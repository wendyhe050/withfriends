#!/usr/bin/env python3
"""Sync the event sections of index.html with the public Luma calendar.

Reads the calendar's iCal feed, then rewrites the three regions of index.html
wrapped in <!-- luma:NAME --> ... <!-- /luma:NAME --> markers:

  featured  home page, next few upcoming events
  upcoming  events page, all upcoming events
  past      events page archive. Existing cards are kept as written (so hand
            edits survive); events that have ended and have no card get one.

Standard library only. Usage: python3 scripts/sync_luma.py [--now ISO] [--dry-run]
"""
import argparse
import html
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

CALENDAR_ID = "cal-9bE07bGSBxudNUT"
CALENDAR_SLUG = "sir.tisawendys"
ICS_URL = f"https://api.lu.ma/ics/get?entity=calendar&id={CALENDAR_ID}"
CALENDAR_URL = f"https://luma.com/{CALENDAR_SLUG}"
SITE_TZ = ZoneInfo("America/Toronto")
FEATURED_COUNT = 3
INDEX = Path(__file__).resolve().parent.parent / "index.html"

BAD_ARTIST = re.compile(r"bad artist\?\s*(ep\.?\s*\d+)?", re.I)
BAD_ARTIST_BLURB = ("Self-guided art at a cosy café. Colour pencil, markers, oil pastel — "
                    "you choose. Prompts if you want them. All levels welcome.")
SMALL_WORDS = {"a", "an", "the", "at", "of", "and", "in", "on", "for", "to", "with"}


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (withfriends-sync)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def esc(s):
    return html.escape(s, quote=False)


# ── Luma data ──

def parse_ics(text):
    text = text.replace("\r\n", "\n").replace("\n ", "")
    events = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        props = {}
        for line in block.split("\n"):
            m = re.match(r"([A-Z-]+)(?:;[^:]*)?:(.*)", line)
            if m and m.group(1) not in props:
                props[m.group(1)] = re.sub(r"\\([,;\\])", r"\1", m.group(2).replace("\\n", "\n"))
        uid = props["UID"].split("@")[0]
        m = re.search(r"https://luma\.com/([\w.-]+)", props.get("DESCRIPTION", ""))
        slug = m.group(1) if m and m.group(1) != CALENDAR_SLUG else None
        location = props.get("LOCATION", "")
        events.append({
            # External events (hosted off Luma) have no event page of their own.
            "key": slug or uid,
            "slug": slug,
            "name": props["SUMMARY"].strip(),
            "start": parse_utc(props["DTSTART"]),
            "end": parse_utc(props.get("DTEND", props["DTSTART"])),
            "venue": None if location.startswith("http") or not location else location.split(",")[0],
            "url": f"https://luma.com/{slug}" if slug
                   else (location if location.startswith("http") else CALENDAR_URL),
        })
    return events


def parse_utc(s):
    return datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def add_page_details(ev):
    """Cover image, price and venue visibility only exist on the event page."""
    if not ev["slug"]:
        return
    page = fetch(ev["url"])
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
    data = json.loads(m.group(1))["props"]["pageProps"]["initialData"]["data"]
    event = data["event"]
    ev["cover"] = event.get("cover_url")
    ev["tz"] = event.get("timezone")
    ev["sold_out"] = bool(data.get("sold_out"))
    info = data.get("ticket_info") or {}
    if info.get("is_free"):
        ev["price"] = "Free"
    elif (info.get("price") or {}).get("cents") is not None:
        cents = info["price"]["cents"]
        ev["price"] = f"${cents // 100}" if cents % 100 == 0 else f"${cents / 100:.2f}"
    if event.get("geo_address_visibility") != "public":
        ev["venue"] = None
    ev["blurb"] = first_paragraph(data.get("description_mirror"))


def first_paragraph(doc):
    for node in (doc or {}).get("content", []):
        if node.get("type") == "paragraph":
            text = "".join(c.get("text", "") for c in node.get("content", [])).strip()
            if text:
                return text if len(text) <= 170 else text[:167].rsplit(" ", 1)[0] + "…"
    return ""


# ── Formatting ──

def title_case(s, force=False):
    if not force and any(c.isupper() for c in s):
        return s
    words = s.split(" ")
    return " ".join(w if i and w in SMALL_WORDS else w[:1].upper() + w[1:] for i, w in enumerate(words))


def short_name(name):
    m = BAD_ARTIST.search(name)
    if m:
        return "Bad Artist?" + (" " + title_case(m.group(1).replace(" ", "")) if m.group(1) else "")
    return title_case(name)


def thumb(cover, size):
    path = cover.split("lumacdn.com/", 1)[-1]
    return ("https://images.lumacdn.com/cdn-cgi/image/format=auto,fit=cover,dpr=2,quality=80,"
            f"width={size},height={size}/{path}")


def clock(dt):
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}"


def when(ev):
    tz = ZoneInfo(ev.get("tz") or "America/Toronto")
    start, end = ev["start"].astimezone(tz), ev["end"].astimezone(tz)
    ampm = lambda d: "am" if d.hour < 12 else "pm"
    first = clock(start) if ampm(start) == ampm(end) else f"{clock(start)} {ampm(start)}"
    return f"{start:%a, %b} {start.day} · {first}–{clock(end)} {ampm(end)}"


def upcoming_card(ev, featured):
    flagship = bool(BAD_ARTIST.search(ev["name"]))
    tags = ['<span class="event-tag hl">Flagship</span>'] if flagship else []
    tags.append(f'<span class="event-tag">{esc(title_case(ev["venue"], True) if ev["venue"] else "Location on RSVP")}</span>')
    if ev.get("price"):
        tags.append(f'<span class="event-tag">{ev["price"]}</span>')
    if ev.get("sold_out"):
        tags.append('<span class="event-tag">Sold out</span>')
    img = (f'<img class="event-thumb" src="{thumb(ev["cover"], 500)}" alt="" loading="lazy">'
           if ev.get("cover") else "")
    if featured:
        button = ("btn-outline", "Register on Luma →")
    else:
        button = ("btn-dark", "Sign Up →" if ev.get("price") in (None, "Free") else "Sign Up & Pay →")
    return (f'<div class="event-card" data-end="{ev["end"]:%Y-%m-%dT%H:%M:%SZ}">{img}'
            f'<div class="event-date">{when(ev)}</div>'
            f'<div class="event-name">{esc(title_case(ev["name"]))}</div>'
            f'<div class="event-desc">{esc(BAD_ARTIST_BLURB if flagship else ev.get("blurb", ""))}</div>'
            f'<div class="event-tags">{"".join(tags)}</div>'
            f'<a class="btn {button[0]} btn-sm" href="{ev["url"]}" target="_blank" rel="noopener" '
            f'style="margin-top:auto">{button[1]}</a></div>')


def upcoming_region(events, featured):
    cards = [upcoming_card(e, featured) for e in events]
    # Shown by the page script once every card above it has ended.
    cards.append(f'<p class="section-body events-empty" style="grid-column:1/-1"{"" if not events else " hidden"}>'
                 f'No upcoming events right now — new dates are announced on '
                 f'<a href="{CALENDAR_URL}" target="_blank" rel="noopener" style="color:inherit">Luma</a>.</p>')
    return cards


def past_card(ev):
    date = ev["start"].astimezone(SITE_TZ)
    if BAD_ARTIST.search(ev["name"]):
        desc = f'Self-guided art at {esc(title_case(ev["venue"], True))}.' if ev["venue"] else "Self-guided art at a cosy Toronto café."
    else:
        desc = esc(ev.get("blurb", ""))
    img = (f'<img src="{thumb(ev["cover"], 300)}" alt="" loading="lazy">' if ev.get("cover") else "✳")
    return (f'<a class="past-card" href="{ev["url"]}" target="_blank" rel="noopener">'
            f'<div class="pe-thumb">{img}</div>'
            f'<div class="pe-date">{date:%b} {date.day}, {date.year}</div>'
            f'<div class="pe-name">{esc(short_name(ev["name"]))}</div>'
            f'<div class="pe-desc">{desc}</div></a>')


def past_region(existing, past_events):
    """Keep existing cards untouched, add missing ones, newest first."""
    by_key = {e["key"]: e for e in past_events}
    cards = []
    for card in re.findall(r'<a class="past-card".*?</a>', existing, re.S):
        m = re.search(r'data-luma="([^"]+)"', card) or re.search(r'href="https://luma\.com/([\w-]+)"', card)
        ev = by_key.pop(m.group(1), None) if m else None
        if ev:
            sort = ev["start"]
        else:
            date = re.search(r'class="pe-date">([^<]+)<', card).group(1)
            sort = datetime.strptime(date, "%b %d, %Y").replace(hour=12, tzinfo=SITE_TZ)
        cards.append((sort, card))
    for ev in by_key.values():
        add_page_details(ev)
        cards.append((ev["start"], past_card(ev)))
        print(f"  + past: {ev['name']}")
    cards.sort(key=lambda c: c[0], reverse=True)
    return [c for _, c in cards]


# ── index.html ──

def replace_region(page, name, build):
    pattern = re.compile(rf"(?P<indent>[ \t]*)<!-- luma:{name} -->\n(?P<body>.*?)[ \t]*<!-- /luma:{name} -->", re.S)
    m = pattern.search(page)
    if not m:
        sys.exit(f"index.html is missing the <!-- luma:{name} --> markers")
    indent = m.group("indent")
    lines = "".join(f"{indent}{line}\n" for line in build(m.group("body")))
    return page[:m.start()] + f"{indent}<!-- luma:{name} -->\n{lines}{indent}<!-- /luma:{name} -->" + page[m.end():]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--now", help="pretend it is this UTC time (ISO 8601), for testing")
    ap.add_argument("--dry-run", action="store_true", help="report changes without writing index.html")
    args = ap.parse_args()
    now = datetime.fromisoformat(args.now).replace(tzinfo=timezone.utc) if args.now else datetime.now(timezone.utc)

    events = parse_ics(fetch(ICS_URL))
    if not events:
        sys.exit("Luma feed returned no events; leaving index.html alone")
    upcoming = sorted((e for e in events if e["end"] > now), key=lambda e: e["start"])
    past = [e for e in events if e["end"] <= now]
    print(f"Luma: {len(upcoming)} upcoming, {len(past)} past")
    for ev in upcoming:
        add_page_details(ev)
        print(f"  upcoming: {when(ev)}  {ev['name']}")

    before = INDEX.read_text(encoding="utf-8")
    page = replace_region(before, "featured", lambda _: upcoming_region(upcoming[:FEATURED_COUNT], True))
    page = replace_region(page, "upcoming", lambda _: upcoming_region(upcoming, False))
    page = replace_region(page, "past", lambda body: past_region(body, past))

    if page == before:
        print("index.html already up to date")
    elif args.dry_run:
        print("index.html would change (dry run)")
    else:
        INDEX.write_text(page, encoding="utf-8")
        print("index.html updated")


if __name__ == "__main__":
    main()
