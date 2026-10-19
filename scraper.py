#!/usr/bin/env python3
"""PassiveX Sunday Update: scrape investorgain GMP table, filter >10%, post to WhatsApp Channel."""
import os
import re
import sys
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://www.investorgain.com/report/live-ipo-gmp/331/"
IST = ZoneInfo("Asia/Kolkata")
GMP_THRESHOLD = 10.0

# 0-based indices for the 1-based columns you specified (1, 2, 5, 7, 9)
COL_NAME, COL_GMP, COL_PRICE, COL_LOT, COL_CLOSE = 0, 1, 4, 6, 8
MIN_CELLS = 9

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


def make_session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "POST"))
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


def to_float(text: str, last: bool = False):
    """Extract a number from text like '₹96 (13.85%)'. Takes the first number
    (or the last, for ranges like '₹650-693' when last=True)."""
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


def parse_close_date(text: str, today: datetime):
    """'23-Oct' -> datetime in the right year (handles Dec -> Jan rollover)."""
    try:
        d = datetime.strptime(f"{text.strip()}-{today.year}", "%d-%b-%Y").replace(tzinfo=IST)
    except ValueError:
        return None
    if (today - d).days > 180:
        d = d.replace(year=d.year + 1)
    return d


def fetch_html(session: requests.Session) -> str:
    r = session.get(URL, timeout=30)
    r.raise_for_status()
    return r.text


def parse_ipos(html: str, today: datetime) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table tr")
    if not rows:
        raise RuntimeError("No table rows found; page may be JS-rendered or blocked.")
    ipos = []
    for tr in rows:
        tds = tr.find_all("td")
        if len(tds) < MIN_CELLS:
            continue
        try:
            name = cell_text(tds[COL_NAME])
            gmp = to_float(tds[COL_GMP].get_text(" ", strip=True))
            price = to_float(tds[COL_PRICE].get_text(" ", strip=True), last=True)
            lot_f = to_float(tds[COL_LOT].get_text(" ", strip=True))
            close_raw = tds[COL_CLOSE].get_text(" ", strip=True)
        except Exception as e:  # malformed row, skip
            log.warning("Skipping row: %s", e)
            continue
        if not name or gmp is None or not price or lot_f is None:
            continue
        close_dt = parse_close_date(close_raw, today)
        if close_dt is None:
            continue
        lot = int(lot_f)
        ipos.append({
            "name": name, "gmp": gmp, "price": price, "lot": lot,
            "close_raw": close_raw, "close_dt": close_dt,
            "gmp_pct": gmp / price * 100.0,
            "profit": gmp * lot,
        })
    log.info("Parsed %d IPO rows", len(ipos))
    return ipos


def next_week_range(today: datetime):
    days_ahead = (7 - today.weekday()) % 7 or 7   # Sunday -> +1 day
    mon = (today + timedelta(days=days_ahead)).date()
    fri = mon + timedelta(days=4)
    return mon, fri


def build_message(ipos: list[dict], today: datetime) -> str:
    mon, fri = next_week_range(today)
    fmt = lambda d: d.strftime("%d %b")
    picks = [i for i in ipos
             if i["gmp_pct"] > GMP_THRESHOLD and i["close_dt"].date() >= today.date()]
    picks.sort(key=lambda i: i["close_dt"])

    lines = ["📢 *PassiveX Sunday Update*", "",
             f"Next week's date range: {fmt(mon)} - {fmt(fri)}", ""]
    if picks:
        lines.append("Following IPOs are there with GMP above 10%:")
        lines.append("")
        for i in picks:
            lines.append(f"• {i['name']} — closing {i['close_raw']} {i['close_dt'].year}")
            lines.append(f"  💰 Est. Profit/1 Lot: ₹{i['profit']:.0f} 🔥")
            lines.append("")
    else:
        lines += ["No IPOs currently have GMP above 10%.", ""]
    lines += ["", "Manage your funds accordingly.",
              "⚠️ GMP is unofficial market info and doesn't guarantee listing gains.",
              "", "— PassiveX"]
    return "\n".join(lines)


def deliver(session: requests.Session, text: str) -> None:
    if os.getenv("DRY_RUN") == "1":
        log.info("DRY_RUN, not sending.")
        return
    whapi_token, channel_id = os.getenv("WHAPI_TOKEN"), os.getenv("WHATSAPP_CHANNEL_ID")
    webhook = os.getenv("WEBHOOK_URL")
    if whapi_token and channel_id:
        r = session.post("https://gate.whapi.cloud/messages/text",
                         headers={"Authorization": f"Bearer {whapi_token}"},
                         json={"to": channel_id, "body": text}, timeout=30)
    elif webhook:
        r = session.post(webhook, json={"message": text}, timeout=30)
    else:
        raise RuntimeError("Set WEBHOOK_URL, or WHAPI_TOKEN + WHATSAPP_CHANNEL_ID.")
    r.raise_for_status()
    log.info("Delivered (HTTP %s)", r.status_code)


def main() -> int:
    today = datetime.now(IST)
    session = make_session()
    try:
        ipos = parse_ipos(fetch_html(session), today)
        message = build_message(ipos, today)
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