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

PROXY_HOST = os.environ.get("PROXY_HOST", "")
PROXY_PORT = os.environ.get("PROXY_PORT", "")
PROXY_USER = os.environ.get("PROXY_USER", "")
PROXY_PASS = os.environ.get("PROXY_PASS", "")

def get_proxy_url():
    if PROXY_HOST and PROXY_PORT and PROXY_USER and PROXY_PASS:
        return f"socks5://{PROXY_USER}:{PROXY_PASS}@{PROXY_HOST}:{PROXY_PORT}"
    return None

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"macOS"',
}

API_HEADERS = {
    **HEADERS,
    "Accept": "application/json, text/plain, */*",
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


def make_client(proxy_url):
    """Create httpx client with or without proxy."""
    if proxy_url:
        return httpx.AsyncClient(
            timeout=30,
            proxies={"http://": proxy_url, "https://": proxy_url},
            verify=False,
        )
    return httpx.AsyncClient(timeout=30)


async def check_tixr(client, url, event):
    event_id = extract_tixr_event_id(url)
    if not event_id:
        return {"available": False, "pairs": [], "total": 0,
                "detail": "Could not extract Tixr event ID"}

    api_url = f"https://www.tixr.com/api/events/{event_id}/listing-prices"
    try:
        r = await client.get(api_url, headers={**API_HEADERS, "Referer": url},
                             follow_redirects=True)
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
            all_seats.append({
                "seatLabel": item.get("seatLabel", ""),
                "price": item.get("price", 0),
            })

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
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True)
        html = r.text
    except Exception as e:
        return {"available": False, "pairs": [], "total": 0, "detail": f"Fetch error: {e}"}

    # Method 1: parse window.gokuProps JS object
    goku_match = re.search(r"window\.gokuProps\s*=\s*(\{.*?\});", html, re.DOTALL)
    if goku_match:
        try:
            props = json.loads(goku_match.group(1))
            status = str(props.get("status", "")).lower()
            sold_out = props.get("soldOut") or props.get("sold_out") or props.get("isSoldOut")
            available = props.get("available") or props.get("ticketsAvailable")
            ticket_count = props.get("ticketCount") or props.get("remainingTickets", 0)
            if sold_out or status in ("sold_out", "soldout", "unavailable"):
                return {"available": False, "pairs": [], "total": 0,
                        "detail": f"Sold out (gokuProps)"}
            if available or ticket_count:
                return {"available": True, "pairs": [], "total": int(ticket_count or 1),
                        "detail": f"Tickets available (gokuProps: count={ticket_count})"}
        except (json.JSONDecodeError, ValueError):
            pass

    # Method 2: scan raw HTML for key strings
    html_lower = html.lower()
    sold_patterns = ['"soldout":true', '"sold_out":true', '"issoldout":true',
                     "sold out", "soldout", "no tickets available", "sales have ended"]
    avail_patterns = ['"available":true', '"ticketsavailable":true',
                      "add to cart", "buy tickets", "select tickets", "buy now"]

    is_sold  = any(p in html_lower for p in sold_patterns)
    is_avail = any(p in html_lower for p in avail_patterns)
    has_price = bool(re.search(r"\$\d+\.\d{2}", html))

    if is_sold and not is_avail:
        return {"available": False, "pairs": [], "total": 0, "detail": "Sold out (HTML scan)"}
    if is_avail or has_price:
        return {"available": True, "pairs": [], "total": 1, "detail": "Tickets available (HTML scan)"}

    return {"available": False, "pairs": [], "total": 0, "detail": "Could not determine status"}



async def check_generic(client, url, event):
    try:
        r = await client.get(url, headers=HEADERS, follow_redirects=True)
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True).lower()
    except Exception as e:
        return {"available": False, "pairs": [], "total": 0, "detail": f"Fetch error: {e}"}

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


async def send_startup(bot, events, proxy_url):
    names = "\n".join(f"  • {e['name']}" for e in events)
    proxy_status = "✅ Proxy connected" if proxy_url else "⚠️ No proxy (direct)"
    msg = (
        f"🤖 *Ticket Monitor started*\n"
        f"Checking every *{CHECK_INTERVAL}s*\n"
        f"{proxy_status}\n\n"
        f"*Watching {len(events)} event(s):*\n{names}"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode=ParseMode.MARKDOWN)


async def monitor():
    bot       = Bot(token=TELEGRAM_TOKEN)
    events    = load_events()
    proxy_url = get_proxy_url()
    state     = {e["url"]: {"available": None, "had_pairs": None} for e in events}

    await send_startup(bot, events, proxy_url)
    log.info("Bot started. Proxy: %s. Monitoring %d events.",
             "YES" if proxy_url else "NO", len(events))

    async with make_client(proxy_url) as client:
        while True:
            for event in events:
                url      = event["url"]
                platform = detect_platform(url)
                prev     = state[url]
                try:
                    result    = await CHECKERS[platform](client, url, event)
                    now_avail = result["available"]
                    now_pairs = bool(result.get("pairs"))

                    log.info("[%s] %s → avail=%s pairs=%s | %s",
                             platform, event["name"], now_avail, now_pairs, result["detail"])

                    if prev["available"] is None:
                        if now_avail and now_pairs:
                            await send_alert(bot, event, result, "pairs")
                    else:
                        if prev["available"] != now_avail:
                            await send_alert(bot, event, result,
                                             "available" if now_avail else "sold_out")
                        elif now_avail and now_pairs and not prev["had_pairs"]:
                            await send_alert(bot, event, result, "pairs")
                        elif now_avail and not now_pairs and prev["had_pairs"]:
                            await send_alert(bot, event, result, "no_pairs")

                    state[url] = {"available": now_avail, "had_pairs": now_pairs}

                except Exception as exc:
                    log.warning("Error checking %s: %s", url, exc)

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor())
