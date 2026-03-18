import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from telegram import Bot
from telegram.constants import ParseMode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL   = int(os.environ.get("CHECK_INTERVAL", "60"))
EVENTS_FILE      = os.environ.get("EVENTS_FILE", "events.json")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.tixr.com/",
}


def load_events():
    with open(EVENTS_FILE) as f:
        return json.load(f)


def detect_platform(url):
    host = urlparse(url).netloc.lower()
    if "tixr.com" in host:       return "tixr"
    if "etix.com" in host:       return "etix"
    if "eventbrite.com" in host: return "eventbrite"
    if "ticketleap.com" in host: return "ticketleap"
    return "generic"


def parse_seat(label):
    m = re.match(r"^(.*)-([A-Za-z]+)-(\d+)$", label.strip())
    if m:
        return m.group(1).strip(), m.group(2).upper(), int(m.group(3))
    m = re.match(r"^(?:Row\s*)?([A-Za-z]+)\s*(?:Seat\s*)?(\d+)$", label.strip(), re.I)
    if m:
        return "", m.group(1).upper(), int(m.group(2))
    return None


def find_adjacent_pairs(all_seats):
    rows = defaultdict(list)
    for seat in all_seats:
        parsed = parse_seat(seat["seatLabel"])
        if parsed:
            section, row, num = parsed
            rows[(section, row)].append((num, seat["seatLabel"], seat["price"]))
    pairs = []
    for seats in rows.values():
        seats.sort(key=lambda x: x[0])
        for i in range(len(seats) - 1):
            num1, label1, price1 = seats[i]
            num2, label2, _ = seats[i + 1]
            if num2 == num1 + 1:
                pairs.append((label1, label2, price1))
    return pairs


def extract_tixr_event_id(url):
    m = re.search(r"-(\d{5,})(?:[?#]|$)", url)
    return m.group(1) if m else None


async def check_tixr(client, url, event):
    event_id = extract_tixr_event_id(url)
    if not event_id:
        return {"available": False, "pairs": [], "total": 0, "detail": "Could not extract Tixr event ID"}
    api_url = f"https://www.tixr.com/api/events/{event_id}/listing-prices"
    try:
        r = await client.get(api_url, headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"available": False, "pairs": [], "total": 0, "detail": f"API error: {e}"}

    listings = data.get("listingsByid") or data.get("listingsById") or {}
    if not listings:
        return {"available": False, "pairs": [], "total": 0, "detail": "No listings found"}

    all_seats = []
    for listing in listings.values():
        for item in listing.get("items", []):
            all_seats.append({"seatLabel": item.get("seatLabel", ""), "price": item.get("price", 0)})

    total = len(all_seats)
    pairs = find_adjacent_pairs(all_seats)

    if pairs:
        pair_info = ", ".join(f"{s1} + {s2} (${p:.0f}/ea)" for s1, s2, p in pairs[:5])
        if len(pairs) > 5:
            pair_info += f" +{len(pairs)-5} more"
        detail = f"{len(pairs)} adjacent pair(s): {pair_info}"
    else:
        detail = f"Only singles ({total} seats, no adjacent pairs)"

    return {"available": total > 0, "pairs": pairs, "total": total, "detail": detail}


async def check_etix(client, url, event):
    r = await client.get(url, headers=HEADERS, follow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")
    add_to_cart = soup.find_all(
        lambda t: t.name in ("button", "a", "input")
        and re.search(r"add.to.cart|buy.now|purchase|select.tickets",
                      (t.get_text() + t.get("value", "") + (t.get("class") or [""])[0]).lower())
    )
    sold_out = soup.find(string=re.compile(r"sold.?out|no.tickets.available|not.available", re.I))
    if sold_out and not add_to_cart:
        return {"available": False, "pairs": [], "total": 0, "detail": "Sold out"}
    if add_to_cart:
        return {"available": True, "pairs": [], "total": len(add_to_cart), "detail": f"{len(add_to_cart)} option(s) found"}
    return {"available": False, "pairs": [], "total": 0, "detail": "No tickets detected"}


async def check_generic(client, url, event):
    r = await client.get(url, headers=HEADERS, follow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()
    sold_p  = ["sold out", "soldout", "no tickets available", "not available", "sales ended"]
    avail_p = ["add to cart", "buy tickets", "get tickets", "book now", "buy now"]
    is_sold  = any(p in text for p in sold_p)
    is_avail = any(p in text for p in avail_p)
    if is_avail and not is_sold:
        return {"available": True, "pairs": [], "total": 1, "detail": "Tickets available"}
    if is_sold:
        return {"available": False, "pairs": [], "total": 0, "detail": "Sold out"}
    return {"available": False, "pairs": [], "total": 0, "detail": "Could not determine status"}


CHECKERS = {
    "tixr": check_tixr, "etix": check_etix,
    "eventbrite": check_generic, "ticketleap": check_generic, "generic": check_generic,
}


async def send_alert(bot, event, result, alert_type):
    ts = datetime.now().strftime("%b %d, %Y %H:%M")
    icons = {
        "pairs":     ("🎟🎟", "ADJACENT PAIRS FOUND"),
        "available": ("🎟",   "TICKETS AVAILABLE"),
        "no_pairs":  ("⚠️",  "ONLY SINGLES LEFT"),
        "sold_out":  ("❌",   "SOLD OUT"),
    }
    emoji, status = icons.get(alert_type, ("ℹ️", "UPDATE"))
    msg = (
        f"{emoji} *{status}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎭 *Event:* {event['name']}\n"
        f"📍 *Venue:* {event.get('venue', '—')}\n"
        f"📅 *Date:* {event.get('date', '—')}\n"
        f"🔍 *Detail:* {result['detail']}\n"
        f"⏰ *Checked:* {ts}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔗 [Open page]({event['url']})"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg,
                           parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)


async def send_startup(bot, events):
    names = "\n".join(f"  • {e['name']}" for e in events)
    msg = (
        f"🤖 *Ticket Monitor started*\n"
        f"Checking every *{CHECK_INTERVAL}s*\n"
        f"Mode: Alert when *adjacent pairs* appear\n\n"
        f"*Watching {len(events)} event(s):*\n{names}"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)


async def monitor():
    bot    = Bot(token=TELEGRAM_TOKEN)
    events = load_events()
    state  = {e["url"]: {"available": None, "had_pairs": None} for e in events}

    await send_startup(bot, events)
    log.info("Bot started. Monitoring %d events.", len(events))

    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            for event in events:
                url      = event["url"]
                platform = detect_platform(url)
                prev     = state[url]
                try:
                    result    = await CHECKERS[platform](client, url, event)
                    now_avail = result["available"]
                    now_pairs = bool(result.get("pairs"))

                    if prev["available"] is None:
                        if now_avail and now_pairs:
                            await send_alert(bot, event, result, "pairs")
                            log.info("[%s] %s → PAIRS on first check", platform, event["name"])
                        else:
                            log.info("[%s] %s → first check done (%d seats, pairs=%s)",
                                     platform, event["name"], result["total"], now_pairs)
                    else:
                        if prev["available"] != now_avail:
                            await send_alert(bot, event, result, "available" if now_avail else "sold_out")
                        elif now_avail and now_pairs and not prev["had_pairs"]:
                            await send_alert(bot, event, result, "pairs")
                            log.info("[%s] %s → PAIRS APPEARED", platform, event["name"])
                        elif now_avail and not now_pairs and prev["had_pairs"]:
                            await send_alert(bot, event, result, "no_pairs")
                            log.info("[%s] %s → pairs gone, singles only", platform, event["name"])

                    state[url] = {"available": now_avail, "had_pairs": now_pairs}

                except Exception as exc:
                    log.warning("Error checking %s: %s", url, exc)

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor())
