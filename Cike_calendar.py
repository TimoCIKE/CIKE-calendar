import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz
import re
import time
import html
import json
from urllib.parse import urljoin
from ics import Calendar, Event

# ====== Nastavenia ======
TIMEZONE = pytz.timezone("Europe/Bratislava")

# ====== Pomocné funkcie ======
def parse_date(text):
    match = re.findall(r"\d{2}\.\d{2}\.\d{4}", text)
    if not match:
        return None, None
    start = datetime.strptime(match[0], "%d.%m.%Y")
    end = datetime.strptime(match[-1], "%d.%m.%Y")
    return start, end

def normalize_key(title, date):
    return (title.strip().lower(), date.strftime("%Y-%m-%d"))

def get_event_blocks(url):
    headers = {"User-Agent": "Mozilla/5.0"}
    resp = requests.get(url, headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"[!] Chyba pri načítaní {url} -> {resp.status_code}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    return soup.find_all("div", class_="e-loop-item")


# ====== 1️⃣ Scraper – Košice IT Valley ======
def scrape_itvalley_events():
    BASE_URL = "https://www.kosiceitvalley.sk/podujatia/"
    MAX_PAGES = 10
    all_events = []
    seen = set()
    last_titles = set()

    for page in range(1, MAX_PAGES + 1):
        url = BASE_URL if page == 1 else f"{BASE_URL}?e-page-bd2a498={page}"
        blocks = get_event_blocks(url)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] IT Valley – stránka {page}: {len(blocks)} blokov")

        if not blocks:
            print("🟡 Žiadne ďalšie stránky – končím prehľadávanie.")
            break

        current_titles = set()

        for block in blocks:
            title_el = block.find("h2", class_="elementor-heading-title")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            current_titles.add(title)

            spans = [s.get_text(strip=True) for s in block.find_all("span", class_="elementor-icon-list-text")]
            date_text = next((s for s in spans if re.search(r"\d{2}\.\d{2}\.\d{4}", s)), None)
            location = next((s for s in spans if not re.search(r"\d{2}\.\d{2}\.\d{4}", s)), "Košice")

            desc_el = block.find("div", class_="elementor-widget-theme-post-excerpt")
            desc = desc_el.get_text(strip=True) if desc_el else ""
            link_el = block.find("a", href=True)
            link = link_el["href"] if link_el else BASE_URL

            start, end = parse_date(date_text or "")
            if not start:
                continue

            key = normalize_key(title, start)
            if key in seen:
                continue
            seen.add(key)

            all_events.append({
                "summary": title,
                "location": location,
                "description": f"{desc}\n\n{link}",
                "start": start,
                "end": end,
            })

        if current_titles == last_titles:
            print("🟡 Opakujúci sa obsah – končím po stránke", page)
            break
        last_titles = current_titles

    print(f"✅ IT Valley: {len(all_events)} podujatí")
    return all_events


# ====== 2️⃣ Scraper – SOPK ======
SOPK_BASE = "https://www.sopk.sk/events/zoznam/"
SOPK_MAX_PAGES = 3

def _http_get(url):
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            return r
        print(f"⚠️ HTTP {r.status_code}: {url}")
    except Exception as e:
        print(f"⚠️ SOPK GET fail {url}: {e}")
    return None

def _parse_iso_to_naive(dtstr):
    try:
        dt = datetime.fromisoformat(dtstr.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except Exception:
        return None

def scrape_sopk_events():
    pages = [SOPK_BASE] + [urljoin(SOPK_BASE, f"page/{i}/") for i in range(2, SOPK_MAX_PAGES + 1)]
    all_events = []
    seen = set()

    for idx, url in enumerate(pages, start=1):
        print(f"   • SOPK page[{idx}]: {url}")
        resp = _http_get(url)
        if not resp:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for sc in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(sc.string)
                if isinstance(data, dict) and data.get("@type") == "Event":
                    start = _parse_iso_to_naive(data.get("startDate"))
                    end = _parse_iso_to_naive(data.get("endDate")) or start
                    title = data.get("name", "")
                    desc = BeautifulSoup(data.get("description", ""), "html.parser").get_text(" ", strip=True)
                    url = data.get("url", SOPK_BASE)
                    if not start or not title:
                        continue
                    key = (title.lower().strip(), start.date())
                    if key in seen:
                        continue
                    seen.add(key)
                    all_events.append({
                        "summary": title,
                        "description": f"{desc}\n\n{url}",
                        "location": "Slovenská obchodná a priemyselná komora",
                        "start": start,
                        "end": end,
                    })
            except Exception:
                continue
    print(f"✅ SOPK: {len(all_events)} podujatí")
    return all_events


# ====== 3️⃣ Scraper – ICKK ======
ICKK_BASE = "https://ickk.sk/vzdelavanie/"

def scrape_ickk_events():
    print("🔹 ICKK – načítavam zoznam vzdelávania…")
    try:
        r = requests.get(ICKK_BASE, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        print(f"⚠️ ICKK – chyba pri načítaní listu: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    events = []
    for card in soup.select(".ewpe-inner-wrapper"):
        try:
            title = card.select_one(".ewpe-event-title").get_text(strip=True)
            date_el = card.select_one(".ewpe-ev-day")
            mon_el = card.select_one(".ewpe-ev-mo")
            if not title or not date_el or not mon_el:
                continue
            day = int(date_el.get_text(strip=True))
            month = datetime.strptime(mon_el.get_text(strip=True)[:3], "%b").month
            year = datetime.now().year
            start = datetime(year, month, day)
            link_el = card.select_one("a.event-link")
            link = link_el["href"] if link_el else ICKK_BASE
            desc = card.select_one(".ewpe-evt-excerpt")
            desc_text = desc.get_text(" ", strip=True) if desc else ""
            events.append({
                "summary": title,
                "description": f"{desc_text}\n\n{link}",
                "location": "Košice",
                "start": start,
                "end": start,
            })
        except Exception as e:
            print(f"⚠️ ICKK chyba: {e}")
    print(f"✅ ICKK: {len(events)} podujatí")
    return events


# ====== Export do ICS ======
def export_events_to_ics(events, filename="events.ics"):
    cal = Calendar()
    for ev in events:
        e = Event()
        e.name = ev["summary"]
        e.begin = ev["start"]
        e.end = ev["end"] + timedelta(days=1)
        e.location = ev["location"]
        e.description = ev["description"]
        cal.events.add(e)

    with open(filename, "w", encoding="utf-8") as f:
        f.writelines(cal.serialize_iter())

    print(f"✅ ICS súbor '{filename}' vytvorený.")


# ====== Spustenie ======
if __name__ == "__main__":
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Spúšťam scraper...\n")
    events = []
    events += scrape_itvalley_events()
    events += scrape_sopk_events()
    events += scrape_ickk_events()

    if events:
        print(f"[+] Načítaných spolu {len(events)} podujatí")
        export_events_to_ics(events)
    else:
        print("⚠️ Nenašli sa žiadne podujatia.")
