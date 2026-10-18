#!/usr/bin/env python3
"""Konzertkalender: liest Termine der Clubs, erkennt neue und meldet Treffer per ntfy."""
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "known_events.json"
EVENTS_FILE = ROOT / "events.json"
HEADERS = {"User-Agent": "Konzertkalender-privat/1.0 (privates Hobbyprojekt)"}
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
CANCEL_WORDS = ("abgesagt", "fällt aus", "faellt aus")
MAX_PUSH_CHARS = 3500
KEEP_DAYS = 60
NEW_DAYS = 7
DETAIL_MAX_AGE_DAYS = 7
DETAIL_MAX_FETCHES = 70
DETAIL_PAUSE = 1.0

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


def norm(text):
    return " ".join((text or "").split())


def parse_chemiefabrik(html, base="https://www.chemiefabrik.info/"):
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.select("a.event"):
        href = a.get("href", "")
        m_id = re.search(r"/gigs/(\d+)", href)
        date_box = a.select_one(".date")
        m = DATE_RE.search(norm(date_box.get_text(" ")) if date_box else "")
        if not m_id or not m:
            continue
        year = int(m.group(4))
        if year < 100:
            year += 2000
        try:
            date = dt.date(year, int(m.group(3)), int(m.group(2)))
        except ValueError:
            continue
        time_box = a.select_one(".time")
        m_time = re.search(r"\d{1,2}:\d{2}", time_box.get_text() if time_box else "")

        headlines = [norm(x.get_text(" ")) for x in a.select(".headlines .item")]
        headlines = [h for h in headlines if h and not h.lower().startswith("infos folgen")]

        lineup = []
        for item in a.select(".artists .item"):
            name_el, info_el = item.select_one(".artist-name"), item.select_one(".artist-info")
            name = norm(name_el.get_text(" ")) if name_el else norm(item.get_text(" "))
            info = norm(info_el.get_text(" ")) if info_el else ""
            if name:
                lineup.append({"name": name, "info": info})

        img = a.select_one("img")
        alt = norm(img.get("alt", "")) if img else ""
        alt = re.sub(r"^Flyer\s+", "", alt)
        if alt.lower().startswith("infos folgen"):
            alt = ""
        names = [x["name"] for x in lineup]
        title = alt or (headlines[0] if headlines else "") or " + ".join(names[:3]) or "Termin"
        preview = ", ".join(n for n in names if n.lower() != title.lower())

        flat = norm(a.get_text(" "))
        event = {
            "club": "Chemiefabrik",
            "id": m_id.group(1),
            "date": date.isoformat(),
            "title": title,
            "detail": preview,
            "url": urljoin(base, href),
            "text": f"{alt} {flat}",
            "lineup": lineup,
            "time": m_time.group(0) if m_time else "",
        }
        old = found.get(event["id"])
        if old is None or len(event["text"]) > len(old["text"]):
            found[event["id"]] = event
    return list(found.values())


def parse_chemiefabrik_detail(html):
    """Liest Einlass/Beginn, Preise und Ticketlinks von der Detailseite eines Konzerts."""
    soup = BeautifulSoup(html, "html.parser")
    out = {}
    box = soup.select_one("#event .time") or soup.select_one(".time")
    if box:
        txt = norm(box.get_text(" "))
        m = re.search(r"Einlass:?\s*(\d{1,2}:\d{2})", txt)
        if m:
            out["doors"] = m.group(1)
        m = re.search(r"(?:Start|Beginn):?\s*(\d{1,2}:\d{2})", txt)
        if m:
            out["start"] = m.group(1)
    vvk = soup.select_one(".vvk")
    if vvk:
        m = re.search(r"Vorverkauf:?\s*([\d.,]+)\s*€", norm(vvk.get_text(" ")))
        if m:
            out["vvk"] = m.group(1) + " €"
        if vvk.select_one(".vvk-gebuehr"):
            out["vvk_note"] = "zzgl. Gebühren"
        tickets = []
        for link in vvk.select("a.vvk-stelle"):
            url = link.get("href", "")
            if url.startswith(("http://", "https://")):
                tickets.append({"name": norm(link.get_text(" ")) or "Tickets", "url": url})
        if tickets:
            out["tickets"] = tickets
    ak = soup.select_one(".ak")
    if ak:
        m = re.search(r"Abendkasse:?\s*([\d.,]+)\s*€", norm(ak.get_text(" ")))
        if m:
            out["ak"] = m.group(1) + " €"
    return out


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


def enrich_chemiefabrik(events, known, today):
    """Holt fehlende oder veraltete Detailseiten (mit Pause) und merkt sich das Ergebnis."""
    details, fetched, failed = {}, 0, 0
    for e in events:
        if e["club"] != "Chemiefabrik":
            continue
        key = f"{e['club']}:{e['id']}"
        cached = (known.get(key) or {}).get("d")
        if cached:
            age = (today - dt.date.fromisoformat(cached.get("f", "2000-01-01"))).days
            if age < DETAIL_MAX_AGE_DAYS:
                details[key] = cached
                continue
        if fetched >= DETAIL_MAX_FETCHES:
            if cached:
                details[key] = cached
            continue
        if fetched:
            time.sleep(DETAIL_PAUSE)
        fetched += 1
        try:
            data = parse_chemiefabrik_detail(get(e["url"]))
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"Detailseite {e['id']}: {type(exc).__name__}")
            if cached:
                details[key] = cached
            continue
        data["f"] = today.isoformat()
        details[key] = data
    print(f"Chemiefabrik-Detailseiten: {fetched} abgerufen, {failed} fehlgeschlagen")
    return details


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
        label = f"{title} - {detail[:70]}" if detail else title
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

    details = enrich_chemiefabrik(events, known, today)

    new_hits, initial, out = [], {}, []
    for e in events:
        key = f"{e['club']}:{e['id']}"
        hits = find_hits(e["text"], genres, bands)
        low = e["text"].lower()
        cancelled = any(w in low for w in CANCEL_WORDS)
        is_hit = bool(hits) and not cancelled
        entry = known.get(key)
        extra = details.get(key) or (entry or {}).get("d") or {}
        if entry is None:
            entry = {"title": e["title"], "date": e["date"]}
            known[key] = entry
            if e["club"] in seen_clubs:
                entry["seen"] = today.isoformat()
                if is_hit:
                    e["hits"] = hits
                    new_hits.append(e)
            else:
                stats = initial.setdefault(e["club"], [0, 0])
                stats[0] += 1
                stats[1] += 1 if is_hit else 0
        if extra:
            entry["d"] = extra
        seen = entry.get("seen", "")
        is_new = bool(seen) and (today - dt.date.fromisoformat(seen)).days <= NEW_DAYS
        out.append({
            "club": e["club"],
            "id": e["id"],
            "date": e["date"],
            "title": e["title"],
            "detail": e.get("detail", "")[:160],
            "url": e.get("url", ""),
            "lineup": e.get("lineup", []),
            "time": e.get("time", ""),
            "doors": extra.get("doors", ""),
            "start": extra.get("start", ""),
            "vvk": extra.get("vvk", ""),
            "vvk_note": extra.get("vvk_note", ""),
            "ak": extra.get("ak", ""),
            "tickets": extra.get("tickets", []),
            "soldout": "ausverkauft" in low,
            "hit": is_hit,
            "hits": hits if is_hit else [],
            "cancelled": cancelled,
            "new": is_new,
        })

    # Ausgefallene Quellen: letzten bekannten Stand der Seite behalten
    if errors and EVENTS_FILE.exists():
        try:
            prev = json.loads(EVENTS_FILE.read_text("utf-8")).get("events", [])
        except ValueError:
            prev = []
        out += [x for x in prev if x.get("club") in errors and x.get("date", "") >= today.isoformat()]
    out.sort(key=lambda x: (x["date"], x["club"], x["title"]))
    EVENTS_FILE.write_text(
        json.dumps({"updated": today.isoformat(), "events": out}, ensure_ascii=False, indent=1),
        "utf-8",
    )

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
