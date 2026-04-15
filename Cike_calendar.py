import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone
import pytz
import re
import time
import html
import json
import hashlib

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from urllib.parse import urljoin
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

from ics import Calendar, Event

# =========================
# Nastavenia
# =========================
TIMEZONE = pytz.timezone("Europe/Bratislava")
TZ = pytz.timezone("Europe/Bratislava")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; EventsBot/1.0)"
}

# =========================
# Pomocné funkcie
# =========================

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

EMOJI_MAP = {
    "ITVALLEY": "🔵",
    "AMCHAM": "🟢",
    "SOPK": "🟧",
    "ICKK": "🟣",
    "OTHER": "⚪",
}

_PREFIX_RE = re.compile(
    r"^(?:[\u2600-\u27BF\U0001F300-\U0001FAFF]\s*)?\[(ITVALLEY|AMCHAM|SOPK|ICKK|OTHER)\]\s*",
    re.IGNORECASE
)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def normalize_key(title, date):
    return (re.sub(r"\s+", " ", clean_text(title).lower()), date.strftime("%Y-%m-%d"))


def normalize_source(src: str) -> str:
    s = (src or "").strip().upper()
    if s in ("ITVALLEY", "AMCHAM", "SOPK", "ICKK"):
        return s
    return "OTHER"


def http_get(url: str, timeout: int = 25, retries: int = 3, verify: bool = True):
    last_err = None
    for _ in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, verify=verify)
            if r.status_code == 200:
                return r
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(1.0)
    print(f"⚠️ GET fail {url}: {last_err}")
    return None


def parse_numeric_or_sk_date(text: str):
    """
    Podporuje:
    - 14.04.2026
    - 4. 2. 2026
    - 18.04.2026 – 19.04.2026
    - 18. 4. 2026 – 19. 4. 2026
    - 18.–19. apríla 2026
    - 29 januára 2026
    """
    if not text:
        return None, None

    t = html.unescape(text)
    t = " ".join(t.lower().split())
    t = t.replace("—", "–")

    # rozsah dd.mm.yyyy – dd.mm.yyyy
    m = re.search(
        r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\b\s*[–-]\s*\b(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\b",
        t
    )
    if m:
        s = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        e = datetime(int(m.group(6)), int(m.group(5)), int(m.group(4)))
        return s, e

    # rozsah 18.–19. apríla 2026
    m = re.search(
        r"\b(\d{1,2})\.\s*[–-]\s*(\d{1,2})\.\s*([a-zá-ž]+)\s+(\d{4})\b",
        t
    )
    if m:
        d1 = int(m.group(1))
        d2 = int(m.group(2))
        mon_word = m.group(3).strip(".")
        y = int(m.group(4))
        mo = _SK_MONTHS.get(mon_word)
        if mo:
            s = datetime(y, mo, d1)
            e = datetime(y, mo, d2)
            return s, e

    # jedno číslicové datum
    m = re.search(r"\b(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\b", t)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        dt = datetime(y, mo, d)
        return dt, dt

    # slovný mesiac
    m = re.search(r"\b(\d{1,2})\s+([a-zá-ž]+)\s+(\d{4})\b", t)
    if m:
        d = int(m.group(1))
        mon_word = m.group(2).strip(".")
        y = int(m.group(3))
        mo = _SK_MONTHS.get(mon_word)
        if mo:
            dt = datetime(y, mo, d)
            return dt, dt

    return None, None


def parse_time_range(text: str):
    m = re.search(r"@\s*(\d{1,2}):(\d{2})\s*[-–]\s*(\d{1,2}):(\d{2})", text or "")
    if not m:
        return None
    return (
        int(m.group(1)),
        int(m.group(2)),
        int(m.group(3)),
        int(m.group(4)),
    )


# =========================
# 1) Košice IT Valley
# =========================

ITV_BASE = "https://www.kosiceitvalley.sk/podujatia/"
ITV_PAST_PARAM = "e-page-9843d5f"
ITV_MAX_PAGES = 12


def get_itv_blocks(url):
    r = http_get(url)
    if not r:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    return soup.find_all("div", class_="e-loop-item")


def scrape_itvalley_events():
    all_events = []
    seen = set()

    urls = [ITV_BASE] + [f"{ITV_BASE}?{ITV_PAST_PARAM}={i}" for i in range(2, ITV_MAX_PAGES + 1)]

    for idx, url in enumerate(urls, start=1):
        blocks = get_itv_blocks(url)
        print(f"[ITVALLEY] stránka {idx}: {len(blocks)} blokov")

        if not blocks:
            break

        page_added = 0

        for block in blocks:
            try:
                title_el = block.find("h2", class_="elementor-heading-title")
                if not title_el:
                    continue

                title = clean_text(title_el.get_text(" ", strip=True))
                if not title:
                    continue

                desc_el = block.find("div", class_="elementor-widget-theme-post-excerpt")
                desc = clean_text(desc_el.get_text(" ", strip=True)) if desc_el else ""

                link_el = block.find("a", href=True)
                link = link_el["href"].strip() if link_el else ITV_BASE

                icon_widgets = block.find_all("div", class_="elementor-widget-icon-list")

                location = "Košice"
                start = end = None

                for widget in icon_widgets:
                    widget_text = clean_text(widget.get_text(" ", strip=True))
                    if not widget_text:
                        continue

                    st, en = parse_numeric_or_sk_date(widget_text)
                    if st:
                        if not start or (en and en > st):
                            start, end = st, en

                    if not re.search(r"\d{1,2}\.\s*\d{1,2}\.\s*\d{4}", widget_text):
                        if widget_text and len(widget_text) > 2:
                            location = widget_text

                if not start:
                    raw_text = clean_text(" ".join(block.stripped_strings))
                    st, en = parse_numeric_or_sk_date(raw_text)
                    if st:
                        start, end = st, en

                if not start:
                    continue

                key = normalize_key(title, start)
                if key in seen:
                    continue
                seen.add(key)

                all_events.append({
                    "summary": title,
                    "location": location,
                    "description": (desc + ("\n\n" + link if link else "")).strip(),
                    "start": start,
                    "end": end or start,
                    "source": "ITVALLEY",
                })
                page_added += 1

                print(f"ITV DEBUG: {title} | start={start} | end={end}")

            except Exception as e:
                print(f"   - chyba ITVALLEY blok: {e}")

        print(f"   -> pridané: {page_added}")

    print(f"✅ ITVALLEY spolu: {len(all_events)} podujatí")
    return all_events


# =========================
# 2) AmCham
# =========================

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
        max_stalls = 3

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
                if stall_hits >= max_stalls:
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
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10,
        "november": 11, "december": 12,
        "jan.": 1, "feb.": 2, "mar.": 3, "apr.": 4, "máj": 5, "jún": 6,
        "júl": 7, "aug.": 8, "sep.": 9, "okt": 10, "nov.": 11, "dec.": 12,
        "január": 1, "február": 2, "marec": 3, "apríl": 4, "máj": 5, "jún": 6,
        "júl": 7, "august": 8, "september": 9, "október": 10,
        "november": 11, "december": 12,
    }

    def to_int(s):
        try:
            return int(s)
        except Exception:
            return None

    def norm_month_token(mon_raw):
        if not mon_raw:
            return None
        key = mon_raw.strip().lower()
        if key in ("january", "february", "march", "april", "august", "september", "october", "november", "december"):
            key = key[:3]
        return key

    def build_date(day_s, mon_s, year_s):
        d = to_int(day_s)
        y = to_int(year_s) or datetime.now().year
        m = months.get(norm_month_token(mon_s)) if mon_s else None
        if not (d and m and y):
            return None
        try:
            return datetime(y, m, d)
        except Exception:
            return None

    def parse_dates_from_block(block):
        date_box = block.select_one(".event-date")
        if not date_box:
            return None, None

        raw_date_text = clean_text(" ".join(date_box.stripped_strings)).lower()

        m = re.search(
            r"\b(\d{1,2})\b\D+(\d{1,2})\D+([a-zá-ž]{3,})\D+(\d{4})",
            raw_date_text
        )
        if m:
            d1 = int(m.group(1))
            d2 = int(m.group(2))
            mon = m.group(3).strip(".").lower()
            year = int(m.group(4))
            month_num = months.get(mon if mon in months else norm_month_token(mon))
            if month_num:
                try:
                    start = datetime(year, month_num, d1)
                    end = datetime(year, month_num, d2)
                    return start, end
                except ValueError:
                    pass

        day_s_el = date_box.select_one(".day.day--start")
        day_e_el = date_box.select_one(".day.day--end")
        month_el = date_box.select_one(".month.month--start") or date_box.select_one(".month")
        year_el = date_box.select_one(".year")

        day_s = (day_s_el.get_text(strip=True) if day_s_el else "") or None
        day_e = (day_e_el.get_text(strip=True) if day_e_el else "") or day_s
        mon = (month_el.get_text(strip=True) if month_el else "") or None
        year = (year_el.get_text(strip=True) if year_el else "") or None

        start = build_date(day_s, mon, year)
        end = build_date(day_e, mon, year)

        if not start:
            joined = " ".join(block.stripped_strings)
            m = re.search(
                r"\b(\d{1,2})\b(?:\D+(\d{1,2}))?\D+([A-Za-zÁÄáäÉéÍíÓóÚúÝýŤťĽľŠšČčŽžÔô]{3,})\D+(\d{4})",
                joined
            )
            if m:
                day_s = day_s or m.group(1)
                day_e = day_e or (m.group(2) or m.group(1))
                mon = mon or m.group(3)
                year = year or m.group(4)
                start = build_date(day_s, mon, year)
                end = build_date(day_e, mon, year)

        if not start:
            return None, None

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

            title = clean_text(title_el.get_text(strip=True))
            if not title:
                continue

            key = normalize_key(title, start)
            if key in seen:
                continue
            seen.add(key)

            loc_el = block.select_one(".event-item__footer span.d-flex") or \
                     block.select_one(".event-item__footer span")
            location = clean_text(loc_el.get_text(" ", strip=True)) if loc_el else ""

            desc_el = block.select_one(".event-shortdesc")
            desc = clean_text(desc_el.get_text(" ", strip=True)) if desc_el else ""

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


# =========================
# 3) SOPK
# =========================

SOPK_BASE = "https://www.sopk.sk/events/zoznam/"
SOPK_ALLOW_INSECURE_SSL = True
SOPK_MAX_PAGES_FUTURE = 3
SOPK_PAST_MAX_PAGE = 7
SOPK_PAST_DAYS = 365


def _parse_iso_to_naive(dtstr: str):
    try:
        dt = datetime.fromisoformat(dtstr.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(dtstr[:len(fmt)], fmt)
            except Exception:
                pass
    return None


def _clean_text(s: str):
    try:
        return BeautifulSoup(html.unescape(s or ""), "html.parser").get_text(" ", strip=True)
    except Exception:
        return (s or "").strip()


def _extract_events_from_jsonld(soup, source="OTHER", cutoff=None, past=False, seen=None):
    events = []
    seen = seen or set()

    for sc in soup.find_all("script", {"type": "application/ld+json"}):
        raw = (sc.string or sc.text or "").strip()
        if not raw:
            continue

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

            title = clean_text(it.get("name") or "")
            if not title:
                continue

            start_iso = it.get("startDate")
            if not start_iso:
                continue

            start_dt = _parse_iso_to_naive(start_iso)
            end_dt = _parse_iso_to_naive(it.get("endDate") or start_iso) or start_dt
            if not start_dt:
                continue

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
                    parts = [
                        addr.get("streetAddress") or "",
                        addr.get("addressLocality") or "",
                        addr.get("postalCode") or ""
                    ]
                    adr = ", ".join([p for p in parts if p])
                location = ", ".join([p for p in [nm, adr] if p])

            desc = _clean_text(it.get("description") or "")
            url = (it.get("url") or "").strip()

            events.append({
                "summary": title,
                "location": location,
                "description": (desc + ("\n\n" + url if url else "")).strip(),
                "start": start_dt,
                "end": end_dt if end_dt >= start_dt else start_dt,
                "source": normalize_source(source),
            })

    return events


def _crawl_sopk_future():
    pages = [SOPK_BASE] + [urljoin(SOPK_BASE, f"page/{i}/") for i in range(2, SOPK_MAX_PAGES_FUTURE + 1)]
    all_events, seen = [], set()

    for idx, url in enumerate(pages, start=1):
        print(f"   • SOPK future[{idx}]: {url}")
        resp = http_get(url, verify=not SOPK_ALLOW_INSECURE_SSL)
        if not resp:
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        found = _extract_events_from_jsonld(soup, source="SOPK", past=False, seen=seen)

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
        resp = http_get(url, verify=not SOPK_ALLOW_INSECURE_SSL)
        if not resp:
            break

        soup = BeautifulSoup(resp.text, "html.parser")
        found = _extract_events_from_jsonld(soup, source="SOPK", cutoff=cutoff, past=True, seen=seen)

        if found:
            all_events.extend(found)
            print(f"     -> {len(found)} eventov")
        else:
            print("     -> 0 eventov (žiadny JSON-LD)")

    return all_events


def scrape_sopk_events():
    print("🔹 SOPK – budúce podujatia…")
    future_events = _crawl_sopk_future()
    print("🔹 SOPK – minulé podujatia…")
    past_events = _crawl_sopk_past()

    events = future_events + past_events
    print(f"✅ SOPK spolu: {len(events)} podujatí")
    return events


# =========================
# 4) ICKK
# =========================

ICKK_LIST_BASE = "https://ickk.sk/events/zoznam/"
ICKK_PAST_DAYS = 365
ICKK_PAST_MAX_PAGE = 8


def scrape_ickk_events():
    all_events = []
    seen = set()
    cutoff = datetime.now() - timedelta(days=ICKK_PAST_DAYS)

    urls = [ICKK_LIST_BASE]
    urls.append(f"{ICKK_LIST_BASE}?eventDisplay=past")
    for i in range(2, ICKK_PAST_MAX_PAGE + 1):
        urls.append(f"{ICKK_LIST_BASE}page/{i}/?eventDisplay=past")

    for idx, url in enumerate(urls, start=1):
        r = http_get(url)
        if not r:
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        found_jsonld = _extract_events_from_jsonld(
            soup,
            source="ICKK",
            cutoff=cutoff if "eventDisplay=past" in url else None,
            past="eventDisplay=past" in url,
            seen=seen,
        )

        page_added = 0
        if found_jsonld:
            all_events.extend(found_jsonld)
            page_added += len(found_jsonld)

        text_lines = [clean_text(line) for line in soup.get_text("\n").splitlines()]
        text_lines = [x for x in text_lines if x]

        i = 0
        while i < len(text_lines):
            line = text_lines[i]
            has_time = "@" in line and re.search(r"\d{1,2}:\d{2}", line)

            if not has_time:
                i += 1
                continue

            event_datetime_line = line
            title = text_lines[i + 1] if i + 1 < len(text_lines) else ""
            if not title or len(title) < 3:
                i += 1
                continue

            start_date, end_date = parse_numeric_or_sk_date(event_datetime_line)

            if not start_date:
                for back in range(1, 4):
                    if i - back >= 0:
                        st, en = parse_numeric_or_sk_date(text_lines[i - back])
                        if st:
                            start_date, end_date = st, en
                            break

            if not start_date:
                i += 1
                continue

            tr = parse_time_range(event_datetime_line)
            if tr:
                sh, sm, eh, em = tr
                start_dt = start_date.replace(hour=sh, minute=sm)
                end_dt = start_date.replace(hour=eh, minute=em)
                if end_dt < start_dt:
                    end_dt = start_dt
            else:
                start_dt = start_date
                end_dt = end_date or start_date

            location = ""
            desc = ""

            if i + 2 < len(text_lines):
                possible_location = text_lines[i + 2]
                if len(possible_location) < 180:
                    location = possible_location

            if i + 3 < len(text_lines):
                possible_desc = text_lines[i + 3]
                if possible_desc != location:
                    desc = possible_desc

            if "eventDisplay=past" in url and start_dt < cutoff:
                i += 1
                continue

            key = normalize_key(title, start_dt)
            if key in seen:
                i += 1
                continue
            seen.add(key)

            all_events.append({
                "summary": title,
                "location": location,
                "description": (desc + ("\n\n" + url)).strip(),
                "start": start_dt,
                "end": end_dt,
                "source": "ICKK",
            })
            page_added += 1
            i += 1

        print(f"[ICKK] stránka {idx}: {url}")
        print(f"   -> pridané: {page_added}")

    print(f"✅ ICKK spolu: {len(all_events)} podujatí")
    return all_events


# =========================
# Export do ICS
# =========================

def _clean_event_title(title: str) -> str:
    t = html.unescape(title or "")
    t = _PREFIX_RE.sub("", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def _with_emoji_prefix(title: str, source: str) -> str:
    cleaned = _PREFIX_RE.sub("", html.unescape(title or "").strip())
    src = normalize_source(source)
    return f"{EMOJI_MAP.get(src, EMOJI_MAP['OTHER'])} [{src}] {cleaned}"


def _is_all_day_00(ev) -> bool:
    s, e = ev["start"], ev["end"]
    return (s.hour == 0 and s.minute == 0 and e.hour == 0 and e.minute == 0)


def _looks_fake_all_day(ev) -> bool:
    s, e = ev["start"], ev["end"]
    same_time = (s.hour == e.hour and s.minute == e.minute)
    likely_fake_hour = s.minute == 0 and s.hour in (0, 1)
    whole_days = (e.date() - s.date()).days >= 1
    return same_time and likely_fake_hour and whole_days


def _dedupe_key(ev):
    base_title = _clean_event_title(ev["summary"])
    if _is_all_day_00(ev) or _looks_fake_all_day(ev):
        return (base_title, ev["start"].strftime("%Y-%m-%d"))
    return (base_title, ev["start"].strftime("%Y-%m-%d %H:%M"))


def _stable_uid(ev):
    title = _clean_event_title(ev["summary"])
    start_part = ev["start"].strftime("%Y-%m-%d %H:%M")
    end_part = ev["end"].strftime("%Y-%m-%d %H:%M")
    kind = "ALLDAY" if (_is_all_day_00(ev) or _looks_fake_all_day(ev)) else "TIMED"
    base = f"{title}|{start_part}|{end_part}|{kind}"
    return hashlib.sha1(base.encode("utf-8")).hexdigest() + "@cike-events"


def export_events_to_ics(events, filename="events.ics"):
    seen, unique = set(), []
    for ev in events:
        k = _dedupe_key(ev)
        if k not in seen:
            seen.add(k)
            unique.append(ev)

    cal = Calendar()
    now_utc = datetime.now(timezone.utc)

    for ev in unique:
        src = normalize_source(ev.get("source", "OTHER"))
        e = Event()
        e.name = _with_emoji_prefix(ev["summary"], src)
        e.location = ev.get("location", "")
        e.description = ev.get("description", "")
        e.categories = {src}
        e.uid = _stable_uid(ev)

        # metadata pre lepšie správanie klientov
        try:
            e.created = now_utc
            e.last_modified = now_utc
        except Exception:
            pass

        s, t = ev["start"], ev["end"]

        if _is_all_day_00(ev) or _looks_fake_all_day(ev):
            start_date = s.date()
            end_date = t.date()

            e.begin = start_date

            if end_date > start_date:
                e.end = end_date

            e.make_all_day()
        else:
            if s.tzinfo is None:
                s = TZ.localize(s)
            if t.tzinfo is None:
                t = TZ.localize(t)
            e.begin = s
            e.end = t

        cal.events.add(e)

    with open(filename, "w", encoding="utf-8") as f:
        f.writelines(cal.serialize_iter())

    print(f"✅ ICS '{filename}' vytvorený – {len(unique)} udalostí (po dedupe).")
    return filename


# =========================
# Main
# =========================

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
