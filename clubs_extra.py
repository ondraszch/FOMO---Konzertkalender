"""Zusätzliche Clubs: Beatpol und Club Puschkin (nur Auswertung der Programmseiten)."""
import datetime as dt
import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

MONTHS_SHORT = {"jan": 1, "feb": 2, "mär": 3, "mae": 3, "mar": 3, "apr": 4, "mai": 5, "jun": 6,
                "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dez": 12}
MONTHS_LONG = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6,
               "juli": 7, "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}


def norm(text):
    return " ".join((text or "").split())


def month_from(name):
    low = norm(name).lower().rstrip(".")
    if low in MONTHS_LONG:
        return MONTHS_LONG[low]
    return MONTHS_SHORT.get(low[:3])


def fix_ticket_url(url):
    """Repariert Links, bei denen dem Club-Domainnamen eine fremde Adresse angehängt wurde."""
    url = (url or "").strip()
    m = re.match(r"^https?://(?:www\.)?beatpol\.de/((?:www\.)?[a-z0-9-]+(?:\.[a-z0-9-]+)+/.*)$", url, re.I)
    if m:
        return "https://" + m.group(1)
    return url


def ticket_name(url):
    host = (urlparse(url).hostname or "").lower()
    host = re.sub(r"^www\.", "", host)
    parts = host.split(".")
    base = parts[-2] if len(parts) >= 2 else host
    return base.capitalize() if base else "Tickets"


def parse_beatpol(html, today=None, base="https://beatpol.de/"):
    today = today or dt.date.today()
    soup = BeautifulSoup(html, "html.parser")
    events, last_date = [], None
    year_hint = today.year
    for node in soup.select("div.event"):
        link = node.select_one(".datum a")
        day_el = node.select_one(".is-day")
        if not link or not day_el:
            continue
        href = link.get("href", "")
        slug = [p for p in urlparse(href).path.split("/") if p]
        if not slug:
            continue
        m_day = re.search(r"\d{1,2}", day_el.get_text())
        rest = norm(link.get_text(" ")).replace(norm(day_el.get_text(" ")), " ")
        month = None
        for word in re.findall(r"[A-Za-zÄÖÜäöüß]+\.?", rest):
            month = month_from(word)
            if month:
                break
        if not m_day or not month:
            continue
        day = int(m_day.group(0))

        # Jahr: bei Monatswechsel nach hinten (Dez -> Jan) hochzählen
        year = year_hint
        if last_date is not None and month < last_date.month:
            year = last_date.year + 1
        elif last_date is not None:
            year = last_date.year
        try:
            date = dt.date(year, month, day)
        except ValueError:
            continue
        if last_date is None and (today - date).days > 200:
            date = dt.date(year + 1, month, day)
        last_date = date

        h2 = node.select_one("h2.entry-title") or node.select_one("h2")
        title = norm(h2.get_text(" ")) if h2 else ""
        if not title:
            continue
        einlass = norm(node.select_one(".einlass").get_text(" ")) if node.select_one(".einlass") else ""
        beginn = norm(node.select_one(".beginn").get_text(" ")) if node.select_one(".beginn") else ""
        m_e = re.search(r"\d{1,2}:\d{2}", einlass)
        m_b = re.search(r"\d{1,2}:\d{2}", beginn)

        specials = norm(node.select_one(".specials").get_text(" ")) if node.select_one(".specials") else ""
        low = specials.lower()
        moved_away = re.search(r"verlegt auf( den)?\s*([\d.]+)", low)
        text = f"{title} {specials}"
        if moved_away:
            title = f"{title} (verlegt auf {moved_away.group(2).rstrip('.')})"
            text += " abgesagt"

        tickets, seen = [], set()
        for a in node.select(".ticket-links a"):
            url = fix_ticket_url(a.get("href", ""))
            if url.startswith(("http://", "https://")) and url not in seen:
                seen.add(url)
                tickets.append({"name": ticket_name(url), "url": url})

        extra = {}
        if m_e:
            extra["doors"] = m_e.group(0)
        if m_b:
            extra["start"] = m_b.group(0)
        if tickets:
            extra["tickets"] = tickets

        events.append({
            "club": "Beatpol",
            "id": slug[-1],
            "date": date.isoformat(),
            "title": title,
            "detail": "Ostpol (kleiner Saal)" if "ostpol" in low else "",
            "url": href if href.startswith("http") else base.rstrip("/") + "/" + href.lstrip("/"),
            "text": text,
            "lineup": [],
            "time": m_e.group(0) if m_e else "",
            "extra": extra,
        })
    return events


def _lineup_from(content):
    """Liest die Namen hinter 'Line Up:' aus der Kurzbeschreibung (nur Namen, keine Texte)."""
    paragraphs = content.find_all("p")
    names = []
    for i, p in enumerate(paragraphs):
        if re.match(r"^\s*line\s*-?\s*up", norm(p.get_text(" ")), re.I):
            for q in paragraphs[i + 1:i + 3]:
                for line in q.get_text("\n").split("\n"):
                    line = re.sub(r"^[^\wÄÖÜäöü(]+", "", norm(line))
                    if 1 < len(line) <= 60 and "[+]" not in line:
                        names.append(line)
                if names:
                    break
            break
    return names[:12]


def parse_puschkin(html, today=None, base="https://www.clubpuschkin.de/"):
    today = today or dt.date.today()
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for group in soup.select(".em-events-list-grouped"):
        year = today.year
        for child in group.find_all(["h2", "li"], recursive=False):
            if child.name == "h2":
                m = re.search(r"(\d{4})", child.get_text())
                if m:
                    year = int(m.group(1))
                continue
            date_el = child.select_one(".eventlistedate")
            links = [a for a in child.find_all("a") if a.get("href", "").startswith("http") and norm(a.get_text())]
            if not date_el or not links:
                continue
            m = re.search(r"(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)", date_el.get_text(" "))
            if not m:
                continue
            month = month_from(m.group(2))
            if not month:
                continue
            try:
                date = dt.date(year, month, int(m.group(1)))
            except ValueError:
                continue
            title_link = links[0]
            title = norm(title_link.get_text(" "))
            href = title_link.get("href", "")
            slug = [p for p in urlparse(href).path.split("/") if p]
            if not slug:
                continue
            m_t = re.search(r"\d{1,2}:\d{2}", date_el.get_text(" "))
            cat_el = child.select_one(".eventcat")
            category = norm(cat_el.get_text(" ")).replace("Kategorie/n:", "").replace("\u2192", "").strip() if cat_el else ""
            content = child.select_one(".eventcontent")
            body = norm(content.get_text(" ")) if content else ""
            lineup = [{"name": n, "info": ""} for n in _lineup_from(content)] if content else []
            events.append({
                "club": "Club Puschkin",
                "id": slug[-1],
                "date": date.isoformat(),
                "title": title,
                "detail": category,
                "url": href,
                "text": f"{title} {category} {body}",
                "lineup": lineup,
                "time": m_t.group(0) if m_t else "",
                "extra": {},
            })
    return events
