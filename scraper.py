#!/usr/bin/env python3
"""PassiveX IPO Listing-Day Reminder.

Posts only IPOs listing today whose GMP percentage is above the configured
threshold. IPO closing dates are deliberately not used in the report.
"""

import logging
import os
import re
import sys
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

# Fallback indices are used only for fields whose columns are known.
# The listing-date column must be detected from the table header.
FALLBACK_COLS = {
    "name": 0,
    "gmp": 1,
    "price": 4,
    "lot": 6,
}
HEADER_PREFIXES = {
    "name": ("name", "ipo", "company"),
    "gmp": ("gmp",),
    "price": ("price",),
    "lot": ("lot",),
    "listing": ("listing",),
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.investorgain.com/",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("passivex")

NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
DATE = re.compile(r"\b(\d{1,2})[-\s]+([A-Za-z]{3})(?:[-\s]+(\d{4}))?\b")


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


def cell_text(cell) -> str:
    return cell.get_text(" ", strip=True)


def to_float(text: str, last: bool = False):
    """Extract a numeric value; ignore percentage text when reading GMP amount."""
    if not text:
        return None
    cleaned = text.replace("₹", " ")
    # GMP cells sometimes look like '₹96 (13.85%) ...'; remove percentages
    # before extracting the GMP amount.
    if not last:
        cleaned = re.sub(r"\(?-?\d+(?:\.\d+)?\s*%\)?", " ", cleaned)
    numbers = NUM.findall(cleaned)
    if not numbers:
        return None
    value = numbers[-1] if last else numbers[0]
    try:
        return float(value.replace(",", ""))
    except ValueError:
        return None


def parse_date(text: str, now: datetime):
    """Parse dates such as 23-Oct, 23 Oct, or 23-Oct-2026 into an IST date."""
    match = DATE.search(text or "")
    if not match:
        return None
    day, month, year_text = match.groups()
    try:
        year = int(year_text) if year_text else now.year
        parsed = datetime.strptime(f"{day}-{month}-{year}", "%d-%b-%Y").replace(tzinfo=IST)
        if not year_text:
            difference = (parsed.date() - now.date()).days
            if difference < -180:
                parsed = parsed.replace(year=year + 1)
            elif difference > 180:
                parsed = parsed.replace(year=year - 1)
        return parsed.date()
    except ValueError:
        return None


def detect_columns(rows) -> dict:
    """Detect the listing-date column; use fallback positions for other fields."""
    for row in rows:
        labels = [
            cell_text(cell).lower()
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        if not labels:
            continue

        found = {}
        for key, prefixes in HEADER_PREFIXES.items():
            for index, label in enumerate(labels):
                if any(label.startswith(prefix) for prefix in prefixes):
                    found[key] = index
                    break

        if "gmp" not in found or "price" not in found or "listing" not in found:
            continue

        columns = {key: found.get(key, fallback) for key, fallback in FALLBACK_COLS.items()}
        columns["listing"] = found["listing"]
        log.info("Detected columns: %s", columns)
        return columns

    raise RuntimeError(
        "Could not detect a listing-date column in the IPO table. "
        "No message was sent because using the closing date would be incorrect."
    )


def fetch_html(session: requests.Session) -> str:
    response = session.get(URL, timeout=30)
    response.raise_for_status()
    return response.text


def parse_ipos(html: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table tr")
    if not rows:
        raise RuntimeError("No table rows found; the website may have changed or blocked the request.")

    columns = detect_columns(rows)
    required_count = max(columns.values()) + 1
    ipos = []

    for row in rows:
        cells = row.find_all("td", recursive=False)
        if len(cells) < required_count:
            continue

        try:
            name = cell_text(cells[columns["name"]])
            gmp_text = cell_text(cells[columns["gmp"]])
            price_text = cell_text(cells[columns["price"]])
            lot_text = cell_text(cells[columns["lot"]])
            listing_text = cell_text(cells[columns["listing"]])
        except IndexError:
            continue

        listing_date = parse_date(listing_text, now)
        gmp = to_float(gmp_text)
        price = to_float(price_text, last=True)
        lot_value = to_float(lot_text)

        if (
            not name
            or listing_date is None
            or gmp is None
            or price is None
            or price <= 0
            or lot_value is None
            or lot_value <= 0
        ):
            continue

        # Use the site's percentage if it is explicitly present in the GMP cell.
        percent_match = re.search(r"(-?\d+(?:\.\d+)?)\s*%", gmp_text)
        gmp_percent = float(percent_match.group(1)) if percent_match else (gmp / price * 100.0)

        ipos.append({
            "name": name,
            "gmp": gmp,
            "price": price,
            "lot": int(lot_value),
            "listing_date": listing_date,
            "gmp_pct": gmp_percent,
            "profit": gmp * int(lot_value),
        })

    log.info("Parsed %d IPO rows with listing dates", len(ipos))
    for ipo in ipos[:5]:
        log.info(
            "Parsed: %s | listing %s | GMP %.2f%% | estimated profit/lot ₹%.0f",
            ipo["name"], ipo["listing_date"].strftime("%d-%b-%Y"),
            ipo["gmp_pct"], ipo["profit"],
        )
    return ipos


def build_message(ipos: list[dict], now: datetime):
    """Return only today's listing reminders with GMP above the threshold."""
    today = now.date()
    listing_today = sorted(
        (
            ipo for ipo in ipos
            if ipo["listing_date"] == today and ipo["gmp_pct"] > GMP_THRESHOLD
        ),
        key=lambda ipo: ipo["name"].lower(),
    )

    if not listing_today and os.getenv("ALWAYS_POST") != "1":
        return None

    lines = [
        "📢 *PassiveX IPO Listing Reminder*",
        f"Date: {now.strftime('%a, %d %b %Y')}",
        "",
    ]

    if not listing_today:
        lines.extend([
            f"No IPOs listing today have GMP above {GMP_THRESHOLD:.0f}%.",
            "",
        ])
    else:
        lines.extend([
            f"🚨 *Today's IPO listing(s) — GMP above {GMP_THRESHOLD:.0f}%*",
            "",
        ])
        for ipo in listing_today:
            lines.extend([
                f"• *{ipo['name']}*",
                f"  📅 Listing date: {ipo['listing_date'].strftime('%d-%b-%Y')}",
                f"  📈 GMP: ₹{ipo['gmp']:,.2f} ({ipo['gmp_pct']:.2f}%)",
                f"  💰 Est. profit/1 lot: ₹{round(ipo['profit']):,}",
                "",
            ])

    lines.extend([
        "⚠️ GMP is unofficial market information. Actual listing gains may differ.",
        "",
        "— PassiveX",
    ])
    return "\n".join(lines)


def deliver(session: requests.Session, message: str) -> None:
    if os.getenv("DRY_RUN") == "1":
        log.info("DRY_RUN=1; message printed only, not sent.")
        return

    token = os.getenv("WHAPI_TOKEN", "").strip()
    group_id = (
        os.getenv("WHATSAPP_GROUP_ID", "").strip()
        or os.getenv("WHATSAPP_CHANNEL_ID", "").strip()
    )
    if not token or not group_id:
        raise RuntimeError("Set WHAPI_TOKEN and WHATSAPP_GROUP_ID before sending.")

    response = session.post(
        WHAPI_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"to": group_id, "body": message},
        timeout=30,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Whapi delivery failed: HTTP {response.status_code}: {response.text[:500]}")
    log.info("Whapi accepted the message (HTTP %s).", response.status_code)


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    # TEST ONLY: set TEST_DATE=2026-10-21 in GitHub Actions to simulate
    # the listing day. Remove TEST_DATE from the workflow after testing.
    now = datetime.now(IST)
    test_date = os.getenv("TEST_DATE", "").strip()
    if test_date:
        try:
            fake_date = datetime.strptime(test_date, "%Y-%m-%d").date()
            now = datetime.combine(fake_date, now.timetz()).replace(tzinfo=IST)
            log.warning(
                "TEST MODE ACTIVE: filtering against %s, not today's real date.",
                test_date,
            )
        except ValueError:
            log.error("TEST_DATE must be YYYY-MM-DD, for example 2026-10-21.")
            return 2

    session = make_session()
    try:
        ipos = parse_ipos(fetch_html(session), now)
        message = build_message(ipos, now)
        if message is None:
            log.info("No IPO listing today meets the GMP threshold; no message sent.")
            return 0
        print("\n" + message + "\n")
        deliver(session, message)
        return 0
    except requests.RequestException as error:
        log.error("Network error: %s", error)
    except Exception as error:
        log.exception("Bot failed: %s", error)
    return 1


if __name__ == "__main__":
    sys.exit(main())
