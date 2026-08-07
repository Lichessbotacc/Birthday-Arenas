#!/usr/bin/env python3
"""
Lichess Birthday-Turnier-Bot
=============================
Prüft das Forum-Thread https://lichess.org/forum/team-darkonteams/birthdays
auf neue Beiträge. Erkennt Nutzer, die ein Geburtsdatum posten, erstellt für
sie automatisch ein Lichess-Arena-Turnier ("{Username} Birthday"), das genau
an ihrem Geburtstag startet (2h, 3+0 Blitz, zufällig 12/14/16/18 Uhr deutscher
Zeit), und postet eine Antwort im Forum-Thread mit dem Link.

State (verarbeitete Post-IDs, letzte Forumsseite) wird in data/state.json
gespeichert. Der Workflow committet diese Datei nach jedem Lauf zurück.
"""

import json
import os
import random
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta

import requests
from bs4 import BeautifulSoup, NavigableString

# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

FORUM_SLUG = "team-darkonteams/birthdays"
FORUM_BASE = f"https://lichess.org/forum/{FORUM_SLUG}"
TEAM_ID = "darkonteams"
STATE_PATH = Path("data/state.json")
TOKEN = os.environ.get("LICHESS_TOKEN")

POSSIBLE_HOURS = [12, 14, 16, 18]
TZ = ZoneInfo("Europe/Berlin")

MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
MONTH_NAMES_PATTERN = "|".join(sorted(MONTHS.keys(), key=len, reverse=True))

POST_LINK_RE = re.compile(r"^/@/([\w-]+)$")
PERMALINK_RE = re.compile(r"\?page=\d+#([A-Za-z0-9]+)$")

session = requests.Session()
session.headers.update({"User-Agent": "darkonteams-birthday-bot"})
if TOKEN:
    session.headers.update({"Authorization": f"Bearer {TOKEN}"})


# ---------------------------------------------------------------------------
# Geburtsdatum aus Text extrahieren
# ---------------------------------------------------------------------------

def extract_birthday(text: str):
    """Gibt (month, day) zurück oder None, wenn kein plausibles Datum gefunden wurde."""
    text = text.strip()

    # "25 January", "3rd December"
    m = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_NAMES_PATTERN})[a-z]*\b",
        text, re.IGNORECASE,
    )
    if m:
        day, month = int(m.group(1)), MONTHS[m.group(2).lower()]
        if 1 <= day <= 31:
            return month, day

    # "January 25", "Jul 7th"
    m = re.search(
        rf"\b({MONTH_NAMES_PATTERN})[a-z]*\s+(\d{{1,2}})(?:st|nd|rd|th)?\b",
        text, re.IGNORECASE,
    )
    if m:
        month, day = MONTHS[m.group(1).lower()], int(m.group(2))
        if 1 <= day <= 31:
            return month, day

    # Numerisch: 24/01, 8/09, 24-2, 15.07 usw.
    m = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})\b", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        day_month_valid = 1 <= a <= 31 and 1 <= b <= 12   # DD/MM
        month_day_valid = 1 <= a <= 12 and 1 <= b <= 31   # MM/DD

        if day_month_valid and not month_day_valid:
            return b, a
        if month_day_valid and not day_month_valid:
            return a, b
        if day_month_valid and month_day_valid:
            # beides plausibel (z.B. 8/09) -> europäisches Format DD/MM annehmen
            return b, a
        # beides ungültig (z.B. 67/67) -> kein echtes Datum

    return None


# ---------------------------------------------------------------------------
# Forum scrapen (Lesen)
# ---------------------------------------------------------------------------

def fetch_page(page: int) -> str:
    res = session.get(f"{FORUM_BASE}?page={page}", timeout=30)
    res.raise_for_status()
    return res.text


def get_max_page(html: str) -> int:
    pages = [int(n) for n in re.findall(r"\?page=(\d+)", html)]
    return max(pages) if pages else 1


def extract_posts(html: str):
    """
    Extrahiert [{username, post_id, text}] in Reihenfolge des Auftretens.

    Robuster Ansatz (statt naivem Regex über den ganzen HTML-Text):
    - Ein echter Post-Header ist ein Link auf /@/USERNAME, DIREKT gefolgt
      (nächstes <a>-Tag) von einem Link auf .../?page=N#POSTID. Das
      Muster tritt bei Mentions im Fließtext (z.B. "@DarkOnCrack") nicht
      auf, weil danach kein Permalink-Link folgt.
    - <blockquote>-Elemente (zitierte Antworten) werden vorher entfernt,
      damit ein Datum im zitierten Text nicht fälschlich dem
      Antwortenden zugeschrieben wird.
    """
    soup = BeautifulSoup(html, "html.parser")

    for bq in soup.find_all("blockquote"):
        bq.decompose()

    all_links = soup.find_all("a", href=True)

    headers = []  # (anchor_tag, username, post_id)
    for i, a in enumerate(all_links):
        m = POST_LINK_RE.match(a["href"])
        if not m:
            continue
        username = m.group(1)
        # Permalink muss das UNMITTELBAR nächste <a>-Tag sein (strikt, um
        # False Positives bei Mentions im Text zu vermeiden)
        if i + 1 < len(all_links):
            pm = PERMALINK_RE.search(all_links[i + 1]["href"])
            if pm:
                headers.append((all_links[i + 1], username, pm.group(1)))

    if not headers:
        return []

    marker = "@@POST_BOUNDARY_MARKER@@"
    for anchor, _, _ in headers:
        anchor.insert_after(NavigableString(marker))

    full_text = soup.get_text("\n")
    chunks = full_text.split(marker)[1:]  # erstes Element = Text vor dem 1. Post

    posts = []
    for (_, username, post_id), chunk in zip(headers, chunks):
        text = re.sub(r"\s+", " ", chunk).strip()
        posts.append({"username": username, "post_id": post_id, "text": text})

    return posts


# ---------------------------------------------------------------------------
# Turnier erstellen
# ---------------------------------------------------------------------------

def next_occurrence(month: int, day: int) -> datetime:
    now = datetime.now(TZ)
    hour = random.choice(POSSIBLE_HOURS)

    try:
        candidate = datetime(now.year, month, day, hour, 0, 0, tzinfo=TZ)
    except ValueError:
        candidate = datetime(now.year, month, day - 1, hour, 0, 0, tzinfo=TZ)

    if candidate <= now + timedelta(minutes=30):
        candidate = candidate.replace(year=candidate.year + 1)

    return candidate


def create_tournament(username: str, month: int, day: int) -> dict:
    start = next_occurrence(month, day)
    start_millis = int(start.astimezone(ZoneInfo("UTC")).timestamp() * 1000)

    name = f"{username} Birthday"
    if len(name) > 30:
        name = name[:30]

    data = {
        "name": name,
        "clockTime": "3",
        "clockIncrement": "0",
        "minutes": "120",
        "startDate": str(start_millis),
        "variant": "standard",
        "rated": "true",
        "conditions.teamMember.teamId": TEAM_ID,
        "description": f"Happy Birthday {username}! 🎂 Auto-created tournament (3+0, 2h).",
    }

    print(f'-> Creating tournament "{name}" for {start.strftime("%d.%m.%Y %H:%M")} (Berlin)')

    res = session.post("https://lichess.org/api/tournament", data=data, timeout=30)
    if not res.ok:
        raise RuntimeError(f"Lichess API error ({res.status_code}): {res.text}")

    tournament = res.json()
    print(f"   ✓ https://lichess.org/tournament/{tournament['id']}")
    return tournament


# ---------------------------------------------------------------------------
# Forum-Antwort posten (EXPERIMENTELL - keine offizielle API)
# ---------------------------------------------------------------------------

def post_forum_reply(message: str) -> bool:
    try:
        form_res = session.get(FORUM_BASE, timeout=30)
        form_res.raise_for_status()

        csrf_match = re.search(r'name="csrfToken"[^>]*value="([^"]+)"', form_res.text)
        if not csrf_match:
            print("   ! Konnte kein CSRF-Token finden, überspringe Forum-Reply.")
            return False
        csrf_token = csrf_match.group(1)

        reply_res = session.post(
            f"{FORUM_BASE}/reply",
            data={"text": message, "csrfToken": csrf_token},
            headers={"Referer": FORUM_BASE},
            timeout=30,
        )

        if reply_res.ok:
            print("   ✓ Forum-Reply gepostet.")
            return True

        print(f"   ! Forum-Reply fehlgeschlagen ({reply_res.status_code}), "
              f"Turnier wurde trotzdem erstellt.")
        return False

    except Exception as e:
        print(f"   ! Forum-Reply fehlgeschlagen ({e}), Turnier wurde trotzdem erstellt.")
        return False


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"lastPage": 1, "processedIds": []}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state["processedIds"] = state["processedIds"][-500:]
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not TOKEN:
        print("Error: LICHESS_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    state = load_state()

    html = fetch_page(state["lastPage"])
    max_page = get_max_page(html)

    pages_to_check = [state["lastPage"]]
    if max_page > state["lastPage"]:
        pages_to_check.append(max_page)
        state["lastPage"] = max_page

    all_posts = []
    for i, page in enumerate(pages_to_check):
        page_html = html if i == 0 else fetch_page(page)
        all_posts.extend(extract_posts(page_html))

    processed = set(state["processedIds"])
    new_posts = [p for p in all_posts if p["post_id"] not in processed]

    if not new_posts:
        print("No new posts found.")

    for post in new_posts:
        username = post["username"]
        birthday = extract_birthday(post["text"])

        if birthday is None:
            print(f'- Skipping post from {username} (no date detected): '
                  f'"{post["text"][:60]}"')
        else:
            month, day = birthday
            print(f'+ Birthday detected: {username} -> {day}.{month}.')
            try:
                tournament = create_tournament(username, month, day)
                url = f"https://lichess.org/tournament/{tournament['id']}"
                message = f"🎂 Birthday tournament for @{username} created: {url}"
                post_forum_reply(message)
            except Exception as e:
                print(f'Error processing post from {username}: {e}', file=sys.stderr)

        state["processedIds"].append(post["post_id"])

    save_state(state)


if __name__ == "__main__":
    main()
