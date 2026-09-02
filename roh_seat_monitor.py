#!/usr/bin/env python3
"""
ROH Seat Availability Monitor
------------------------------
Polls the Royal Opera House seatmap API for a given performance and
alerts you (via ntfy.sh push notification, free & no signup) when the
number of available seats increases, or when specific seats appear.

SETUP:
1. pip install requests
2. Adjust CONFIG below.
3. Run once with --inspect to print the raw JSON structure so you can
   confirm the field names used to detect "available" seats.
4. Then run normally: python monitor.py
   (or schedule it with cron / Task Scheduler to run every N minutes)

NOTIFICATIONS:
Uses ntfy.sh — install the ntfy app (iOS/Android) or just visit
https://ntfy.sh/<your-topic> in a browser, subscribe to a topic name
of your choosing, and put that same topic name in CONFIG below.
No account needed. If you'd rather use email, see the send_email()
stub near the bottom.
"""

import requests
import json
import os
import time
import sys
from pathlib import Path

# ----------------- CONFIG -----------------
# All of these can be overridden via environment variables / GitHub
# Actions inputs, so the same script works for any future performance
# without editing code.
PERFORMANCE_ID = os.environ.get("PERFORMANCE_ID", "74463")

URL = (
    f"https://www.rbo.org.uk/api/v2-proxy/TXN/Performances/{PERFORMANCE_ID}/Seats"
    f"?constituentId=0&modeOfSaleId=10&performanceId={PERFORMANCE_ID}"
)

PRICES_URL = (
    "https://www.rbo.org.uk/api/v2-proxy/TXN/Performances/Prices"
    f"?expandPerformancePriceType=&includeOnlyBasePrice=&modeOfSaleId=10"
    f"&performanceIds={PERFORMANCE_ID}&priceTypeId=&sourceId=108"
)

# Only alert on seats in zones where a ticket is currently offered
# within this price range (inclusive). Set via the Prices endpoint
# each run so it stays correct even if ROH changes which zones are on
# sale at these prices. Defaults to 39/39 to match earlier behaviour.
TARGET_PRICE_MIN = float(os.environ.get("TARGET_PRICE_MIN", "39"))
TARGET_PRICE_MAX = float(os.environ.get("TARGET_PRICE_MAX", "39"))

# This endpoint returned data without a login cookie, so auth isn't
# needed. If that ever changes (HTTP 401/403), set a COOKIE_HEADER
# environment variable (or a GitHub Actions secret of the same name)
# with the value copied from DevTools -> Network -> this request ->
# Headers -> Cookie.
COOKIE_HEADER = os.environ.get("COOKIE_HEADER", "")

# A seat is treated as "available" when SeatStatusId == this value.
# CONFIRMED from ROH's own ReferenceData/SeatStatuses endpoint:
#   0  = AVL "Available"        <- this is what we want
#   13 = TKD "Ticketed"          (already sold/booked - NOT available)
#   4  = HLD "Held"
#   5  = NIA "Not In Allocation"
#   6  = BLK "Blacked Out"
#   7  = RUP "Reserved, Unpaid"
#   8  = RPD "Reserved, Paid"
# Earlier versions of this script had the polarity backwards (treating
# HoldCodeId==0 or SeatStatusId==13 as available), which is why counts
# were wildly inflated. This is the authoritative fix.
AVAILABLE_SEAT_STATUS_ID = 0

# ntfy.sh topic name -- pick something unique/hard to guess, e.g.
# "roh-perf74463-desmond-x9k2". Set via NTFY_TOPIC env var / GitHub
# secret so you don't have to commit it into the repo.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "CHANGE-ME-roh-alert")

POLL_SECONDS = 120  # only used in --loop mode (local/manual runs)
STATE_FILE = Path(__file__).parent / f"last_state_{PERFORMANCE_ID}.json"
# -------------------------------------------


def fetch_seats():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SeatMonitor/1.0)",
        "Accept": "application/json",
    }
    if COOKIE_HEADER:
        headers["Cookie"] = COOKIE_HEADER

    resp = requests.get(URL, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_prices():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SeatMonitor/1.0)",
        "Accept": "application/json",
    }
    if COOKIE_HEADER:
        headers["Cookie"] = COOKIE_HEADER

    resp = requests.get(PRICES_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_target_price_zone_ids(prices):
    """
    Returns the set of ZoneIds where a ticket priced within
    [TARGET_PRICE_MIN, TARGET_PRICE_MAX] is currently enabled for sale,
    under any price type. A zone can carry multiple price-type rows
    (e.g. general public vs. a concession rate); we want the zone if
    ANY of its enabled rows fall in range.
    """
    return {
        p["ZoneId"]
        for p in prices
        if p.get("Enabled") is True
        and p.get("Price") is not None
        and TARGET_PRICE_MIN <= p["Price"] <= TARGET_PRICE_MAX
    }


def send_notification(message):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": "ROH Seat Alert"},
            timeout=10,
        )
    except Exception as e:
        print(f"[warn] failed to send notification: {e}")


def extract_available_seats(data, target_zone_ids=None):
    """
    Returns a dict of {seat_id: "RowLetter SeatNumber"} for every seat
    currently available. Per ROH's own ReferenceData/SeatStatuses,
    SeatStatusId 0 = "AVL" (Available) is the correct signal.
    If target_zone_ids is given, only seats in those zones are included
    (used to restrict to seats currently priced within the target range).
    """
    seats = data if isinstance(data, list) else data.get("Seats", data)
    available = {}
    for s in seats:
        if not s.get("IsSeat"):
            continue
        if s.get("SeatStatusId") != AVAILABLE_SEAT_STATUS_ID:
            continue
        if target_zone_ids is not None and s.get("ZoneId") not in target_zone_ids:
            continue
        label = f"Row {s.get('SeatRow')}, Seat {s.get('SeatNumber')}"
        available[s["Id"]] = label
    return available


def check_once():
    """Single check-and-alert pass. This is what GitHub Actions runs each
    time the scheduled workflow fires — check, alert if needed, exit."""
    price_label = (
        f"\u00a3{TARGET_PRICE_MIN:g}"
        if TARGET_PRICE_MIN == TARGET_PRICE_MAX
        else f"\u00a3{TARGET_PRICE_MIN:g}-\u00a3{TARGET_PRICE_MAX:g}"
    )

    prev_ids = set()
    first_run = not STATE_FILE.exists()
    if STATE_FILE.exists():
        try:
            prev_ids = set(json.loads(STATE_FILE.read_text()).get("seat_ids", []))
        except Exception:
            prev_ids = set()

    prices = fetch_prices()
    target_zone_ids = get_target_price_zone_ids(prices)
    print(f"  (zones currently offering {price_label}: {len(target_zone_ids)})")

    data = fetch_seats()
    available = extract_available_seats(data, target_zone_ids)
    current_ids = set(available.keys())
    print(f"[{time.strftime('%H:%M:%S')}] available {price_label} seats detected: {len(current_ids)}")

    new_ids = current_ids - prev_ids
    if new_ids and not first_run:
        labels = ", ".join(available[i] for i in sorted(new_ids))
        send_notification(
            f"{len(new_ids)} new {price_label} seat(s) available for performance "
            f"{PERFORMANCE_ID}: {labels}"
        )
        print(f"  -> ALERT sent: {labels}")
    elif first_run:
        send_notification(
            f"Monitor is live for performance {PERFORMANCE_ID} ({price_label} seats). "
            f"Baseline: {len(current_ids)} seat(s) currently available. "
            f"You'll be alerted when new ones open up."
        )
        print("  -> first run: baseline established, confirmation notification sent")

    STATE_FILE.write_text(json.dumps({"seat_ids": list(current_ids)}))


def summarize():
    """Fetches live data and prints counts grouped by the fields that
    likely determine visual availability on the seatmap, so we can
    figure out why our count doesn't match what the map shows."""
    from collections import Counter

    data = fetch_seats()
    seats = data if isinstance(data, list) else data.get("Seats", data)

    total = len(seats)
    is_seat_true = sum(1 for s in seats if s.get("IsSeat"))
    is_seat_false = total - is_seat_true

    hold_counts = Counter(s.get("HoldCodeId") for s in seats if s.get("IsSeat"))
    status_counts = Counter(s.get("SeatStatusId") for s in seats if s.get("IsSeat"))
    screen_counts = Counter(s.get("ScreenId") for s in seats if s.get("IsSeat"))
    section_counts = Counter(s.get("SectionId") for s in seats if s.get("IsSeat"))

    # Cross-tab: for each HoldCodeId, which SeatStatusIds appear with it
    combo_counts = Counter(
        (s.get("HoldCodeId"), s.get("SeatStatusId"))
        for s in seats if s.get("IsSeat")
    )

    print(f"Total entries: {total}")
    print(f"IsSeat=True: {is_seat_true}   IsSeat=False: {is_seat_false}")
    print()
    print("Counts by HoldCodeId (among IsSeat=True):")
    for k, v in sorted(hold_counts.items(), key=lambda x: -x[1]):
        print(f"  HoldCodeId={k}: {v}")
    print()
    print("Counts by SeatStatusId (among IsSeat=True):")
    for k, v in sorted(status_counts.items(), key=lambda x: -x[1]):
        print(f"  SeatStatusId={k}: {v}")
    print()
    print("Counts by ScreenId:")
    for k, v in sorted(screen_counts.items(), key=lambda x: -x[1]):
        print(f"  ScreenId={k}: {v}")
    print()
    print("Counts by SectionId (top 15):")
    for k, v in sorted(section_counts.items(), key=lambda x: -x[1])[:15]:
        print(f"  SectionId={k}: {v}")
    print()
    print("HoldCodeId x SeatStatusId combinations:")
    for (h, st), v in sorted(combo_counts.items(), key=lambda x: -x[1]):
        print(f"  HoldCodeId={h}, SeatStatusId={st}: {v}")


def main():
    if "--inspect" in sys.argv:
        data = fetch_seats()
        print(json.dumps(data, indent=2)[:3000])
        print("\n... (truncated if longer). Use this to confirm field names.")
        return

    if "--summarize" in sys.argv:
        summarize()
        return

    if "--loop" in sys.argv:
        print("Starting continuous monitor. Press Ctrl+C to stop.")
        while True:
            try:
                check_once()
            except requests.HTTPError as e:
                print(f"[error] HTTP error: {e} — you may need to be logged in (set COOKIE_HEADER)")
            except Exception as e:
                print(f"[error] {e}")
            time.sleep(POLL_SECONDS)
        return

    # Default: single check-and-exit (used by GitHub Actions)
    check_once()


if __name__ == "__main__":
    main()
