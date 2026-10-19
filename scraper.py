#!/usr/bin/env python3
"""PassiveX Daily Update.

Scrapes the investorgain live IPO GMP table, keeps IPOs with GMP above 10%,
and posts one message to a WhatsApp Channel through Whapi.Cloud.
"""
import os
import re
import sys
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://www.investorgain.com/report/live-ipo-gmp/331/"
WHAPI_URL = "https://gate.whapi.cloud/messages/text"
IST = ZoneInfo("Asia/Kolkata")
GMP_THRESHOLD = 10.0

# Fallback 0-based column indices (your 1-based columns 1, 2, 5, 7, 9).
# The script reads the table's header row first and only uses these if that fails.
FALLBACK_COLS = {"name": 0, "gmp": 1, "price": 4, "lot": 6, "close": 8}
HEADER_PREFIXES = {
    "name": ("name", "ipo", "company"),
    "gmp": ("gmp",),
    "price": ("price",),
    "lot": ("lot",),
    "close": ("close",),
    "listing": ("listing",),   # no fallback: listing reminders need a real header
}

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.investorgain.com/",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("passivex")

NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
DATE = re.compile(r"(\d{1,2})[-\s]([A-Za-z]{3})")


def make_session() -> requests.Session:
    s = requests.Session()
    # Retry GETs only: retrying the WhatsApp POST could post twice.
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


def to_float(text: str, last: bool = False):
    """'₹96 (13.85%)' -> 96.0. With last=True takes the last number ('₹650-693' -> 693)."""
    if not text:
        return None
    nums = NUM.findall(text.replace("₹", " "))
    if not nums:
        return None
    try:
        return float((nums[-1] if last else nums[0]).replace(",", ""))
    except ValueError:
        return None


def cell_text(td) -> str:
    a = td.find("a")
    return (a or td).get_text(" ", strip=True)


def parse_date(text: str, today: datetime):
    """'23-Oct' -> tz-aware datetime in the right year (handles Dec -> Jan)."""
    m = DATE.search(text or "")
    if not m:
        return None
    try:
        d = datetime.strptime(f"{m.group(1)}-{m.group(2).title()}-{today.year}",
                              "%d-%b-%Y").replace(tzinfo=IST)
    except ValueError:
        return None
    if (today - d).days > 180:
        d = d.replace(year=d.year + 1)
    return d


def detect_columns(rows) -> dict:
    """Find column positions from the header row; fall back to fixed indices."""
    for tr in rows:
        labels = [c.get_text(" ", strip=True).lower() for c in tr.find_all(["th", "td"])]
        if not (any(l.startswith("gmp") for l in labels)
                and any(l.startswith("price") for l in labels)):
            continue
        found = {}
        for key, prefixes in HEADER_PREFIXES.items():
            for idx, label in enumerate(labels):
                if label.startswith(prefixes):
                    found[key] = idx
                    break
        cols = {k: found.get(k, FALLBACK_COLS[k]) for k in FALLBACK_COLS}
        cols["listing"] = found.get("listing")
        log.info("Columns from header: %s", cols)
        return cols
    log.warning("Header row not found; using fixed column positions, listing reminders off.")
    return {**FALLBACK_COLS, "listing": None}


def fetch_html(session: requests.Session) -> str:
    r = session.get(URL, timeout=30)
    r.raise_for_status()
    return r.text


def parse_ipos(html: str, today: datetime) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table tr")
    if not rows:
        raise RuntimeError("No table rows found; page may be JS-rendered or blocked.")
    cols = detect_columns(rows)
    need = max(cols[k] for k in FALLBACK_COLS) + 1
    ipos = []
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < need:
            continue
        try:
            name = cell_text(tds[cols["name"]])
            gmp = to_float(tds[cols["gmp"]].get_text(" ", strip=True))
            price = to_float(tds[cols["price"]].get_text(" ", strip=True), last=True)
            lot_f = to_float(tds[cols["lot"]].get_text(" ", strip=True))
            close_raw = tds[cols["close"]].get_text(" ", strip=True)
        except Exception as e:  # malformed row
            log.warning("Skipping row: %s", e)
            continue
        if not name or gmp is None or not price or lot_f is None:
            continue
        close_dt = parse_date(close_raw, today)
        if close_dt is None:
            continue
        # The site appends extra text ("19-Oct GMP: 515"), so rebuild a clean "19-Oct".
        close_txt = f"{close_dt.day}-{close_dt.strftime('%b')}"
        listing_dt = None
        li = cols["listing"]
        if li is not None and li < len(tds):
            listing_dt = parse_date(tds[li].get_text(" ", strip=True), today)
        lot = int(lot_f)
        ipos.append({
            "name": name, "gmp": gmp, "price": price, "lot": lot,
            "close_raw": close_txt, "close_dt": close_dt, "listing_dt": listing_dt,
            "gmp_pct": gmp / price * 100.0,      # signed: negative GMP never passes
            "profit": gmp * lot,
        })
    log.info("Parsed %d IPO rows", len(ipos))
    if ipos:
        log.info("First row sample: %s", {k: v for k, v in ipos[0].items()
                                          if k not in ("close_dt", "listing_dt")})
    return ipos


def build_message(ipos: list[dict], today: datetime):
    strong = [i for i in ipos if i["gmp_pct"] > GMP_THRESHOLD]
    open_picks = sorted((i for i in strong if i["close_dt"].date() >= today.date()),
                        key=lambda i: i["close_dt"])
    listing_today = [i for i in strong
                     if i["listing_dt"] and i["listing_dt"].date() == today.date()]

    if not open_picks and not listing_today and os.getenv("ALWAYS_POST") != "1":
        return None

    lines = ["📢 *PassiveX Daily Update*", "",
             f"Date: {today.strftime('%a, %d %b %Y')}", ""]
    if open_picks:
        lines += ["Following IPOs are there with GMP above 10%:", ""]
        for i in open_picks:
            lines.append(f"• {i['name']} — closing {i['close_raw']} {i['close_dt'].year}")
            lines.append(f"  💰 Est. Profit/1 Lot: ₹{round(i['profit'])} 🔥")
            lines.append("")
    elif not listing_today:
        lines += ["No IPOs currently have GMP above 10%.", ""]
    if listing_today:
        lines += ["⏰ *Listing today:*", ""]
        for i in listing_today:
            lines.append(f"• {i['name']} — GMP {i['gmp_pct']:.0f}%")
            lines.append(f"  💰 Est. Profit/1 Lot: ₹{round(i['profit'])} 🔥")
            lines.append("")
    lines += ["Manage your funds accordingly.",
              "⚠️ GMP is unofficial market info and doesn't guarantee listing gains.",
              "", "— PassiveX"]
    return "\n".join(lines)


def deliver(session: requests.Session, text: str) -> None:
    if os.getenv("DRY_RUN") == "1":
        log.info("DRY_RUN set, not sending.")
        return
    token = os.getenv("WHAPI_TOKEN")
    # WHATSAPP_CHANNEL_ID may hold several comma-separated IDs: the Channel
    # (ends @newsletter) and/or groups (end @g.us).
    recipients = [x.strip() for x in os.getenv("WHATSAPP_CHANNEL_ID", "").split(",") if x.strip()]
    if not token or not recipients:
        raise RuntimeError("WHAPI_TOKEN and WHATSAPP_CHANNEL_ID must be set.")
    failed = []
    for n, to in enumerate(recipients):
        if n:
            time.sleep(5)   # small gap between sends
        try:
            r = session.post(WHAPI_URL, headers={"Authorization": f"Bearer {token}"},
                             json={"to": to, "body": text}, timeout=30)
            if r.status_code >= 400:
                log.error("Whapi error %s for %s: %s", r.status_code, to, r.text[:300])
                failed.append(to)
                continue
            log.info("Delivered to %s (HTTP %s)", to, r.status_code)
        except requests.RequestException as e:
            log.error("Network error sending to %s: %s", to, e)
            failed.append(to)
    if failed:
        raise RuntimeError(f"Failed to deliver to: {', '.join(failed)}")


def main() -> int:
    # Windows consoles often can't print emoji; avoid crashing before the send.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    today = datetime.now(IST)
    session = make_session()
    try:
        ipos = parse_ipos(fetch_html(session), today)
        message = build_message(ipos, today)
        if message is None:
            log.info("Nothing to report today (no IPO above %.0f%% GMP); not posting.",
                     GMP_THRESHOLD)
            return 0
        print(message)
        deliver(session, message)
        return 0
    except requests.RequestException as e:
        log.error("Network error: %s", e)
    except Exception as e:
        log.exception("Failed: %s", e)
    return 1


if __name__ == "__main__":
    sys.exit(main())