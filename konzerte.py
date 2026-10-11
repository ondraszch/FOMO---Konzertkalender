#!/usr/bin/env python3
"""Konzertkalender: liest Termine der Clubs, erkennt neue und meldet Treffer per ntfy."""
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "known_events.json"
HEADERS = {"User-Agent": "Konzertkalender-privat/1.0 (privates Hobbyprojekt)"}
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
CANCEL_WORDS = ("abgesagt", "fällt aus", "faellt aus")
MAX_PUSH_CHARS = 3500
KEEP_DAYS = 60

DATE_RE = re.compile(r"\b(Mo|Di|Mi|Do|Fr|Sa|So)\.?,?\s+(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
TIME_RE = re.compile(r"^\s*\d{1,2}:\d{2}\s*Uhr\s*")


def read_list(name):
    path = ROOT / name
    if not path.exists():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            items.append(line)
    return items


def get(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")


def parse_chemiefabrik(html, base="https://www.chemiefabrik.info/"):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=re.compile(r"/gigs/\d+")):
        href = a["href"]
        gig_id = re.search(r"/gigs/(\d+)", href).group(1)
        text = " ".join(a.get_text(" ", strip=True).split())
        m = DATE_RE.search(text)
        if not m:
            continue
        day, month, year = int(m.group(2)), int(m.group(3)), int(m.group(4))
        if year < 100:
            year += 2000
        try:
            date = dt.date(year, month, day)
        except ValueError:
            continue
        title = re.sub(r"^Flyer\s+", "", text[: m.start()]).strip()
        rest = TIME_RE.sub("", text[m.end():]).strip()
        if not title or title.lower().startswith("infos folgen"):
            title = rest[:60] or "Termin"
        event = {
            "club": "Chemiefabrik",
            "id": gig_id,
            "date": date.isoformat(),
            "title": title,
            "detail": rest,
            "url": urljoin(base, href),
            "text": text,
        }
        old = found.get(gig_id)
        if old is None or len(event["text"]) > len(old["text"]):
            found[gig_id] = event
    return list(found.values())


def unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def unescape(value):
    return (
        value.replace("\\n", " ").replace("\\N", " ")
        .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")
    )


def parse_ics(text, club):
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", unfold(text), flags=re.S):
        props = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            props.setdefault(key.split(";")[0].upper(), value.strip())
        m = re.match(r"(\d{4})(\d{2})(\d{2})", props.get("DTSTART", ""))
        if not m:
            continue
        try:
            date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        summary = unescape(props.get("SUMMARY", "")).strip()
        description = unescape(props.get("DESCRIPTION", "")).strip()
        uid = props.get("UID") or f"{summary}-{date.isoformat()}"
        events.append({
            "club": club,
            "id": uid,
            "date": date.isoformat(),
            "title": summary or "Termin",
            "detail": "",
            "url": props.get("URL", ""),
            "text": f"{summary} {description}",
        })
    return events


SOURCES = [
    ("Chemiefabrik", lambda: parse_chemiefabrik(get("https://www.chemiefabrik.info/"))),
    ("GrooveStation", lambda: parse_ics(get("https://www.groovestation.de/calendar.ics"), "GrooveStation")),
    ("Scheune", lambda: parse_ics(get("https://scheune.org/shows.ics"), "Scheune")),
]


def find_hits(text, genres, bands):
    low = text.lower()
    hits = [g for g in genres if g.lower() in low]
    for band in bands:
        pattern = r"(?<!\w)" + re.escape(band.lower()) + r"(?!\w)"
        if re.search(pattern, low):
            hits.append(band)
    return hits


def format_message(events):
    events = sorted(events, key=lambda e: (e["date"], e["club"]))
    lines = []
    for i, e in enumerate(events):
        d = dt.date.fromisoformat(e["date"])
        title, detail = e["title"], e.get("detail", "")
        if detail and detail.lower().startswith(title.lower()):
            label = detail[:90]
        elif detail:
            label = f"{title} - {detail[:70]}"
        else:
            label = title
        line = f"{WEEKDAYS[d.weekday()]} {d:%d.%m.} {label} ({e['club']})"
        if sum(len(x) + 1 for x in lines) + len(line) > MAX_PUSH_CHARS:
            lines.append(f"... und {len(events) - i} weitere")
            break
        lines.append(line)
    return "\n".join(lines)


def send_ntfy(title, body):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("NTFY_TOPIC ist nicht gesetzt - keine Nachricht gesendet")
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{topic}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Tags": "guitar"},
            timeout=30,
        )
        print(f"ntfy: Status {r.status_code}")
        return r.ok
    except requests.RequestException:
        print("ntfy: Verbindungsfehler")
        return False


def main(today=None):
    today = today or dt.date.today()
    genres, bands = read_list("genres.txt"), read_list("bands.txt")
    print(f"{len(genres)} Genre-Woerter, {len(bands)} Bands geladen")

    known = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    seen_clubs = {k.split(":", 1)[0] for k in known}

    events, errors = [], []
    for name, fetch in SOURCES:
        try:
            got = fetch()
        except Exception as exc:  # noqa: BLE001
            errors.append(name)
            print(f"FEHLER bei {name}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        got = [e for e in got if e["date"] >= today.isoformat()]
        print(f"{name}: {len(got)} kommende Termine gelesen")
        if not got:
            print(f"WARNUNG: {name} lieferte keine Termine - evtl. hat sich die Seite geaendert")
        events.extend(got)

    new_hits, initial = [], {}
    for e in events:
        key = f"{e['club']}:{e['id']}"
        if key in known:
            continue
        known[key] = {"title": e["title"], "date": e["date"]}
        hits = find_hits(e["text"], genres, bands)
        low = e["text"].lower()
        cancelled = any(w in low for w in CANCEL_WORDS)
        if e["club"] not in seen_clubs:
            stats = initial.setdefault(e["club"], [0, 0])
            stats[0] += 1
            stats[1] += 1 if hits and not cancelled else 0
            continue
        if hits and not cancelled:
            e["hits"] = hits
            new_hits.append(e)

    failed = False
    if new_hits:
        ok = send_ntfy(f"Neue Konzerte: {len(new_hits)}", format_message(new_hits))
        if not ok:
            failed = True
            for e in new_hits:
                known.pop(f"{e['club']}:{e['id']}", None)
    if initial:
        lines = [f"{club}: {n} Termine erfasst, {m} passen" for club, (n, m) in sorted(initial.items())]
        lines.append("Ab jetzt kommen Nachrichten nur noch bei neuen Terminen.")
        send_ntfy("Konzertkalender eingerichtet", "\n".join(lines))

    cutoff = (today - dt.timedelta(days=KEEP_DAYS)).isoformat()
    known = {k: v for k, v in known.items() if v.get("date", "9999") >= cutoff}
    STATE_FILE.write_text(json.dumps(known, ensure_ascii=False, indent=1, sort_keys=True), "utf-8")
    print(f"{len(known)} Termine gespeichert, {len(new_hits)} neue Treffer")

    if errors or failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
