import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz
import re
import time
import html
import json
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urljoin
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

# ====== Nastavenia ======
TIMEZONE = pytz.timezone("Europe/Bratislava")

# ====== Pomocné funkcie ======
def parse_date(text):
    match = re.findall(r"\d{2}\.\d{2}\.\d{4}", text or "")
    if not match:
        return None, None
    start = datetime.strptime(match[0], "%d.%m.%Y")
    end = datetime.strptime(match[-1], "%d.%m.%Y")
    return start, end

def normalize_key(title, date):
    return (title.strip().lower(), date.strftime("%Y-%m-%d"))

def get_event_blocks(url):
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    if r.status_code != 200:
        print(f"[!] Chyba pri načítaní {url} -> {r.status_code}")
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    return soup.find_all("div", class_="e-loop-item")

def normalize_source(src: str) -> str:
    s = (src or "").strip().upper()
    if s in ("ITVALLEY", "AMCHAM", "SOPK", "ICKK"):
        return s
    return "OTHER"

# ====== 1️⃣ Košice IT Valley ======
def scrape_itvalley_events():
    BASE_URL = "https://www.kosiceitvalley.sk/podujatia/"
    MAX_PAGES = 10  # poistka
    all_events, seen = [], set()
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
                "source": "ITVALLEY",
            })

        if current_titles == last_titles:
            print("🟡 Opakujúci sa obsah – končím po stránke", page)
            break
        last_titles = current_titles

    print(f"✅ IT Valley: {len(all_events)} podujatí")
    return all_events

# ====== 2️⃣ AmCham (Selenium pre LOAD MORE) ======
def scrape_amcham_events():
    url = "https://amcham.sk/events"
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 12)

    driver.get(url)
    events, seen = [], set()

    # ---------- UPCOMING ----------
    while True:
        try:
            load_more = wait.until(EC.element_to_be_clickable((By.ID, "data-load-more")))
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", load_more)
            driver.execute_script("arguments[0].click();", load_more)
            time.sleep(1.0)
        except Exception:
            break

    soup = BeautifulSoup(driver.page_source, "html.parser")
    up_cont = soup.select_one("#event-list-upcoming--24")
    events += extract_amcham_events_from_soup([up_cont] if up_cont else [], seen)
    print(f"✅ AmCham Upcoming: {len(events)}")

    # ---------- PAST (LAST YEAR) ----------
    try:
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(0.8)

        try:
            past_tab = wait.until(EC.element_to_be_clickable(
                (By.CSS_SELECTOR, "#select-past-year, [data-bs-target='#tab-event-list-past-year']")))
            driver.execute_script("arguments[0].click();", past_tab)
            time.sleep(0.8)
        except TimeoutException:
            pass

        cont_el = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "[id^='event-list-past-year-']")))
        cont_id = cont_el.get_attribute("id")

        def find_load_more_for_container():
            css = (
                f"#tab-event-list-past-year #data-load-more[data-target='{cont_id}'], "
                f"[id^='tab-event-list-past-year'] #data-load-more[data-target='{cont_id}']"
            )
            btns = driver.find_elements(By.CSS_SELECTOR, css)
            return btns[0] if btns else None

        last_count = 0
        stall_hits = 0
        MAX_STALLS = 3
        for _ in range(60):
            curr_count = len(driver.find_elements(By.CSS_SELECTOR, f"#{cont_id} .event-item"))
            stall_hits = (stall_hits + 1) if curr_count == last_count else 0
            last_count = curr_count

            btn = find_load_more_for_container()
            if not btn:
                break
            style = (btn.get_attribute("style") or "").replace(" ", "").lower()
            if "display:none" in style:
                break

            try:
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                driver.execute_script("arguments[0].click();", btn)
            except StaleElementReferenceException:
                btn = find_load_more_for_container()
                if not btn:
                    break
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                driver.execute_script("arguments[0].click();", btn)

            try:
                wait.until(lambda d: len(d.find_elements(By.CSS_SELECTOR, f"#{cont_id} .event-item")) > curr_count)
            except TimeoutException:
                if stall_hits >= MAX_STALLS:
                    break
            time.sleep(0.8)

        soup_past = BeautifulSoup(driver.page_source, "html.parser")
        past_container = soup_past.select_one(f"#{cont_id}")
        new_events = extract_amcham_events_from_soup([past_container] if past_container else [], seen)
        events += new_events
        print(f"✅ AmCham Past (Last Year): {len(new_events)}")

    except Exception:
        pass

    driver.quit()
    print(f"✅ AmCham spolu: {len(events)} podujatí")
    return events

def extract_amcham_events_from_soup(containers, seen):
    import re
    MONTHS = {
        "jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
        "january":1,"february":2,"march":3,"april":4,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12,
        "jan.":1,"feb.":2,"mar.":3,"apr.":4,"máj":5,"jún":6,"júl":7,"aug.":8,"sep.":9,"okt":10,"nov.":11,"dec.":12,
        "január":1,"február":2,"marec":3,"apríl":4,"máj":5,"jún":6,"júl":7,"august":8,"september":9,"október":10,"november":11,"december":12,
    }
    def to_int(s):
        try: return int(s)
        except: return None
    def norm_month_token(mon_raw):
        if not mon_raw: return None
        key = mon_raw.strip().lower()
        if key in ("january","february","march","april","august","september","october","november","december"):
            key = key[:3]
        return key
    def build_date(day_s, mon_s, year_s):
        d = to_int(day_s)
        y = to_int(year_s) or datetime.now().year
        m = MONTHS.get(norm_month_token(mon_s)) if mon_s else None
        if not (d and m and y): return None
        try: return datetime(y, m, d)
        except: return None
    def parse_dates_from_block(block):
        date_box = block.select_one(".event-date")
        day_s_el = date_box.select_one(".day.day--start") if date_box else None
        day_e_el = date_box.select_one(".day.day--end") if date_box else None
        month_el = (date_box.select_one(".month.month--start") if date_box else None) or (date_box.select_one(".month") if date_box else None)
        year_el  = date_box.select_one(".year") if date_box else None
        day_s = (day_s_el.get_text(strip=True) if day_s_el else "") or None
        day_e = (day_e_el.get_text(strip=True) if day_e_el else "") or day_s
        mon   = (month_el.get_text(strip=True) if month_el else "") or None
        year  = (year_el.get_text(strip=True)  if year_el  else "") or None
        start = build_date(day_s, mon, year)
        end   = build_date(day_e, mon, year)
        if not start:
            joined = " ".join(block.stripped_strings)
            m = re.search(r"\b(\d{1,2})\b(?:\D+(\d{1,2}))?\D+([A-Za-zÁÄáäÉéÍíÓóÚúÝýŤťĽľŠšČčŽžÔô]{3,})\D+(\d{4})", joined)
            if m:
                day_s = day_s or m.group(1)
                day_e = day_e or (m.group(2) or m.group(1))
                mon   = mon   or m.group(3)
                year  = year  or m.group(4)
                start = build_date(day_s, mon, year)
                end   = build_date(day_e, mon, year)
        if not start: return None, None
        return start, (end or start)

    events = []
    for container in containers:
        if not container:
            continue
        blocks = container.select(".event-item")
        for block in blocks:
            start, end = parse_dates_from_block(block)
            if not start:
                continue
            title_el = block.select_one(".event-item__desc .event-title") or \
                       block.select_one(".event-title") or \
                       block.select_one("a[title]")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue
            key = normalize_key(title, start)
            if key in seen:
                continue
            seen.add(key)
            loc_el = block.select_one(".event-item__footer span.d-flex") or block.select_one(".event-item__footer span")
            location = loc_el.get_text(" ", strip=True) if loc_el else ""
            desc_el = block.select_one(".event-shortdesc")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""
            link_el = block.find("a", href=True)
            link = link_el["href"] if link_el else "https://amcham.sk/events"
            events.append({
                "summary": title,
                "location": location,
                "description": f"{desc}\n\n{link}".strip(),
                "start": start,
                "end": end,
                "source": "AMCHAM",
            })
    return events

# ====== 3️⃣ SOPK ======
SOPK_BASE = "https://www.sopk.sk/events/zoznam/"
SOPK_ALLOW_INSECURE_SSL = True
SOPK_MAX_PAGES_FUTURE = 3
SOPK_PAST_MAX_PAGE = 7
SOPK_PAST_DAYS = 365

def _http_get(url, timeout=20, retries=3):
    last = None
    for _ in range(retries):
        try:
            r = requests.get(
                url, timeout=timeout, verify=not SOPK_ALLOW_INSECURE_SSL,
                headers={"User-Agent": "Mozilla/5.0 (compatible; EventsBot/1.0)"}
            )
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(1.0)
    print(f"⚠️ SOPK GET fail {url}: {last}")
    return None

def _parse_iso_to_naive(dtstr: str):
    try:
        dt = datetime.fromisoformat(dtstr.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try: return datetime.strptime(dtstr[:len(fmt)], fmt)
            except Exception: pass
    return None

def _clean_text(s: str):
    try:
        return BeautifulSoup(html.unescape(s or ""), "html.parser").get_text(" ", strip=True)
    except Exception:
        return (s or "").strip()

def _extract_events_from_jsonld(soup, cutoff=None, past=False, seen=None):
    events = []
    seen = seen or set()
    for sc in soup.find_all("script", {"type": "application/ld+json"}):
        raw = (sc.string or sc.text or "").strip()
        if not raw: continue
        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(html.unescape(raw))
            except Exception:
                continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict) or it.get("@type") != "Event":
                continue
            title = (it.get("name") or "").strip()
            if not title: continue
            start_iso = it.get("startDate")
            if not start_iso: continue
            start_dt = _parse_iso_to_naive(start_iso)
            end_dt = _parse_iso_to_naive(it.get("endDate") or start_iso) or start_dt
            if not start_dt: continue
            if past and cutoff and start_dt < cutoff:
                continue
            norm_title = re.sub(r"\s+", " ", title.lower()).strip()
            key = (norm_title, start_dt.date())
            if key in seen:
                continue
            seen.add(key)
            location = ""
            loc = it.get("location")
            if isinstance(loc, dict):
                nm = loc.get("name") or ""
                addr = loc.get("address")
                adr = ""
                if isinstance(addr, dict):
                    parts = [addr.get("streetAddress") or "", addr.get("addressLocality") or "", addr.get("postalCode") or ""]
                    adr = ", ".join([p for p in parts if p])
                location = ", ".join([p for p in [nm, adr] if p])
            desc = _clean_text(it.get("description") or "")
            url = (it.get("url") or SOPK_BASE).strip()
            events.append({
                "summary": title,
                "location": location,
                "description": (desc + ("\n\n" + url if url else "")).strip(),
                "start": start_dt,
                "end": end_dt if end_dt >= start_dt else start_dt,
                "source": "SOPK",
            })
    return events

def _crawl_sopk_future():
    pages = [SOPK_BASE] + [urljoin(SOPK_BASE, f"page/{i}/") for i in range(2, SOPK_MAX_PAGES_FUTURE + 1)]
    all_events, seen = [], set()
    for idx, url in enumerate(pages, start=1):
        print(f"   • SOPK future[{idx}]: {url}")
        resp = _http_get(url)
        if not resp: break
        soup = BeautifulSoup(resp.text, "html.parser")
        found = _extract_events_from_jsonld(soup, past=False, seen=seen)
        if found:
            all_events.extend(found)
            print(f"     -> {len(found)} eventov")
        else:
            print("     -> 0 eventov (žiadny JSON-LD)")
    return all_events

def _crawl_sopk_past():
    past_pages = [SOPK_BASE + "?eventDisplay=past"] + \
                 [SOPK_BASE + f"page/{i}/?eventDisplay=past" for i in range(2, SOPK_PAST_MAX_PAGE + 1)]
    all_events, seen = [], set()
    cutoff = datetime.now() - timedelta(days=SOPK_PAST_DAYS)
    for idx, url in enumerate(past_pages, start=1):
        print(f"   • SOPK past[{idx}]: {url}")
        resp = _http_get(url)
        if not resp: break
        soup = BeautifulSoup(resp.text, "html.parser")
        found = _extract_events_from_jsonld(soup, cutoff=cutoff, past=True, seen=seen)
        if found:
            all_events.extend(found)
            print(f"     -> {len(found)} eventov")
        else:
            print("     -> 0 eventov (žiadny JSON-LD)")
    return all_events

def scrape_sopk_events():
    print("🔹 SOPK – budúce podujatia (max page/3)…")
    future_events = _crawl_sopk_future()
    print("🔹 SOPK – minulé podujatia (len po page/7)…")
    past_events = _crawl_sopk_past()
    events = future_events + past_events
    print(f"✅ SOPK spolu: {len(events)} podujatí")
    return events

# ====== 4️⃣ ICKK ======
ICKK_BASE = "https://ickk.sk/vzdelavanie/"
ICKK_PAST_DAYS = 365

_SK_MONTHS = {
    "január": 1, "januára": 1, "jan": 1,
    "február": 2, "februára": 2, "feb": 2,
    "marec": 3, "marca": 3, "mar": 3,
    "apríl": 4, "apríla": 4, "apr": 4,
    "máj": 5, "mája": 5, "maj": 5,
    "jún": 6, "júna": 6, "jun": 6,
    "júl": 7, "júla": 7, "jul": 7,
    "august": 8, "augusta": 8, "aug": 8,
    "september": 9, "septembra": 9, "sep": 9,
    "október": 10, "októbra": 10, "okt": 10,
    "november": 11, "novembra": 11, "nov": 11,
    "december": 12, "decembra": 12, "dec": 12,
}

def _parse_sk_date_human(text: str):
    if not text:
        return None
    t = " ".join(text.lower().split())
    m = re.search(r"(\d{1,2})\.\s*([a-zá-ž]+)\s+(\d{4})", t)
    if m:
        d = int(m.group(1)); mon_word = m.group(2).strip("."); y = int(m.group(3))
        mon = _SK_MONTHS.get(mon_word)
        if mon:
            try: return datetime(y, mon, d)
            except ValueError: return None
    m2 = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", t)
    if m2:
        try: return datetime.strptime(m2.group(0), "%d.%m.%Y")
        except ValueError: pass
    return None

def _infer_year(month: int, day: int):
    today = datetime.now()
    year = today.year
    try:
        cand = datetime(year, month, day)
        if cand.date() < today.date():
            cand = datetime(year + 1, month, day)
        return cand
    except ValueError:
        return None

def scrape_ickk_events():
    print("🔹 ICKK – načítavam zoznam vzdelávania…")
    try:
        r = requests.get(ICKK_BASE, timeout=25, headers={"User-Agent": "Mozilla/5.0 (compatible; EventsBot/1.0)"})
        r.raise_for_status()
    except Exception as e:
        print(f"⚠️ ICKK – chyba pri načítaní listu: {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    events, seen = [], set()
    cutoff_past = datetime.now() - timedelta(days=ICKK_PAST_DAYS)

    cards = soup.select(".ewpe-inner-wrapper")
    print(f"   • ICKK (upcoming) karty: {len(cards)}")
    for idx, card in enumerate(cards, start=1):
        try:
            mo_txt = (card.select_one(".ewpe-ev-mo") or {}).get_text(strip=True) if card.select_one(".ewpe-ev-mo") else ""
            day_txt = (card.select_one(".ewpe-ev-day") or {}).get_text(strip=True) if card.select_one(".ewpe-ev-day") else ""
            mon = _SK_MONTHS.get(mo_txt.lower()); day = int(day_txt) if day_txt.isdigit() else None
            a = card.select_one("a.event-link") or card.select_one(".ewpe-event-title ~ a")
            title_el = card.select_one(".ewpe-event-title")
            title = title_el.get_text(strip=True) if title_el else (a.get_text(strip=True) if a else "").strip()
            link = a["href"] if a and a.has_attr("href") else ICKK_BASE
            loc_el = card.select_one(".ewpe-event-venue-details .ewpe-add-city")
            location = loc_el.get_text(strip=True) if loc_el else "Košice"
            desc_el = card.select_one(".ewpe-evt-excerpt")
            desc = BeautifulSoup((desc_el.get_text(" ", strip=True) if desc_el else ""), "html.parser").get_text(" ", strip=True)
            start_dt = _infer_year(mon, day) if (mon and day) else None
            if not (title and start_dt): continue
            key = (re.sub(r"\s+", " ", title.lower()).strip(), start_dt.date())
            if key in seen: continue
            seen.add(key)
            events.append({
                "summary": title,
                "location": location,
                "description": (desc + ("\n\n" + link if link else "")).strip(),
                "start": start_dt,
                "end": start_dt,
                "source": "ICKK",
            })
        except Exception as e:
            print(f"     - upcoming[{idx:02d}] chyba: {e}")

    past_items = soup.select(".rt-tpg-container .tpg-post-holder")
    print(f"   • ICKK (past) položky: {len(past_items)}")
    for idx, it in enumerate(past_items, start=1):
        try:
            a = it.select_one(".entry-title a")
            title = a.get_text(strip=True) if a else ""
            link = a["href"] if a and a.has_attr("href") else ICKK_BASE
            date_a = it.select_one(".post-meta-tags .date a")
            date_text = date_a.get_text(" ", strip=True) if date_a else ""
            start_dt = _parse_sk_date_human(date_text)
            if not (title and start_dt): continue
            if start_dt < cutoff_past: continue
            key = (re.sub(r"\s+", " ", title.lower()).strip(), start_dt.date())
            if key in seen: continue
            seen.add(key)
            excerpt_el = it.select_one(".tpg-excerpt-inner")
            desc = BeautifulSoup((excerpt_el.get_text(" ", strip=True) if excerpt_el else ""), "html.parser").get_text(" ", strip=True)
            events.append({
                "summary": title,
                "location": "Košice",
                "description": (desc + ("\n\n" + link if link else "")).strip(),
                "start": start_dt,
                "end": start_dt,
                "source": "ICKK",
            })
        except Exception as e:
            print(f"     - past[{idx:02d}] chyba: {e}")

    print(f"✅ ICKK spolu: {len(events)} podujatí")
    return events

# ====== Export do ICS ======
from ics import Calendar, Event
import hashlib
import re
from datetime import timedelta
import pytz

EMOJI_MAP = {
    "ITVALLEY": "🔵",
    "AMCHAM":   "🟢",
    "SOPK":     "🟧",
    "ICKK":     "🟣",
    "OTHER":    "⚪",
}

# rovnaký regex ako máš – odstraňuje staré prefixy, aby sa nereťazili
_PREFIX_RE = re.compile(
    r"^(?:[\u2600-\u27BF\U0001F300-\U0001FAFF]\s*)?\[(ITVALLEY|AMCHAM|SOPK|ICKK|OTHER)\]\s*",
    re.IGNORECASE
)

# zjednotený zdroj (máš ju už vyššie v kóde)
# def normalize_source(...): ...

TZ = pytz.timezone("Europe/Bratislava")

def _with_emoji_prefix(title: str, source: str) -> str:
    cleaned = _PREFIX_RE.sub("", (title or "").strip())
    src = normalize_source(source)
    return f"{EMOJI_MAP.get(src, EMOJI_MAP['OTHER'])} [{src}] {cleaned}"

def _is_all_day(ev) -> bool:
    s, e = ev["start"], ev["end"]
    return (s.hour == 0 and s.minute == 0 and e.hour == 0 and e.minute == 0)

def _dedupe_key(ev):
    """Na dedupe používame (čistený titul, dátum) alebo (čistený titul, dátum+čas), ak je čas známy."""
    base_title = _PREFIX_RE.sub("", ev["summary"].strip().lower())
    if _is_all_day(ev):
        return (base_title, ev["start"].strftime("%Y-%m-%d"))
    else:
        return (base_title, ev["start"].strftime("%Y-%m-%d %H:%M"))

def _stable_uid(ev):
    """UID je stabilné; pre časové eventy berie do úvahy aj čas, pre all-day len dátum."""
    src = normalize_source(ev.get("source", "OTHER"))
    title = _PREFIX_RE.sub("", ev["summary"].strip().lower())
    part = ev["start"].strftime("%Y-%m-%d %H:%M") if not _is_all_day(ev) else ev["start"].strftime("%Y-%m-%d")
    base = f"{title}|{part}|{src}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest() + "@cike-events"

def export_events_to_ics(events, filename="events.ics"):
    # 1) dedupe
    seen, unique = set(), []
    for ev in events:
        k = _dedupe_key(ev)
        if k not in seen:
            seen.add(k)
            unique.append(ev)

    # 2) zápis do ICS
    cal = Calendar()
    for ev in unique:
        src = normalize_source(ev.get("source", "OTHER"))

        e = Event()
        e.name = _with_emoji_prefix(ev["summary"], src)
        e.location = ev.get("location", "")
        e.description = ev.get("description", "")
        e.categories = {src}  # Outlook desktop využije; web ho ignoruje
        e.uid = _stable_uid(ev)

        if _is_all_day(ev):
            # all-day: ICS používa exkluzívny DTEND → +1 deň, no bez časovej zložky
            e.begin = ev["start"].date()
            e.end   = (ev["end"].date() + timedelta(days=1))
            e.make_all_day()
        else:
            # časové: zachovaj presný čas (bez +1 dňa)
            # ak máš naive datetimes, pridáme timezone, aby sa v Outlooke nevznikal posun
            s = ev["start"]
            e_dt = ev["end"]
            if s.tzinfo is None:
                s = TZ.localize(s)
            if e_dt.tzinfo is None:
                e_dt = TZ.localize(e_dt)
            e.begin = s
            e.end   = e_dt

        cal.events.add(e)

    with open(filename, "w", encoding="utf-8") as f:
        f.writelines(cal.serialize_iter())

    print(f"✅ ICS '{filename}' vytvorený – {len(unique)} udalostí (po dedupe).")
    return filename

# ====== Spustenie ======
if __name__ == "__main__":
    print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Spúšťam scraper...\n")
    events = []
    events += scrape_itvalley_events()
    events += scrape_amcham_events()
    events += scrape_sopk_events()
    events += scrape_ickk_events()
    if events:
        print(f"[+] Načítaných spolu {len(events)} podujatí zo všetkých zdrojov")
        export_events_to_ics(events, filename="events.ics")
    else:
        print("⚠️ Nenašli sa žiadne podujatia – skontroluj štruktúru stránok.")
