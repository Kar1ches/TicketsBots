import asyncio
import json
import logging
import os
import re
import time
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

# ── Config ────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CHECK_INTERVAL  = int(os.environ.get("CHECK_INTERVAL", "60"))   # seconds
EVENTS_FILE     = os.environ.get("EVENTS_FILE", "events.json")

# ── Helpers ───────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def load_events() -> list[dict]:
    with open(EVENTS_FILE) as f:
        return json.load(f)


# ── Platform detectors ────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "etix.com" in host:
        return "etix"
    if "eventbrite.com" in host:
        return "eventbrite"
    if "ticketleap.com" in host:
        return "ticketleap"
    if "showclix.com" in host:
        return "showclix"
    return "generic"


async def check_etix(client: httpx.AsyncClient, url: str) -> dict:
    """Returns {available: bool, detail: str}"""
    r = await client.get(url, headers=HEADERS, follow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")

    # Etix shows "Add to Cart" buttons when available
    add_to_cart = soup.find_all(
        lambda t: t.name in ("button", "a", "input")
        and re.search(r"add.to.cart|buy.now|purchase|select.tickets",
                      (t.get_text() + t.get("value", "") + t.get("class", [""])[0]).lower())
    )
    sold_out_text = soup.find(
        string=re.compile(r"sold.?out|no.tickets.available|not.available", re.I)
    )

    if sold_out_text and not add_to_cart:
        return {"available": False, "detail": "Sold out"}
    if add_to_cart:
        return {"available": True, "detail": f"{len(add_to_cart)} ticket option(s) found"}
    # Fallback: check for price elements
    prices = soup.find_all(string=re.compile(r"\$\d+"))
    if prices:
        return {"available": True, "detail": f"Prices visible: {prices[0].strip()}"}
    return {"available": False, "detail": "No tickets detected"}


async def check_eventbrite(client: httpx.AsyncClient, url: str) -> dict:
    r = await client.get(url, headers=HEADERS, follow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")
    sold_out = soup.find(string=re.compile(r"sold.?out|sales.ended", re.I))
    register = soup.find(string=re.compile(r"register|get.tickets|buy.tickets", re.I))
    if register and not sold_out:
        return {"available": True, "detail": "Registration open"}
    return {"available": False, "detail": "Sold out or unavailable"}


async def check_ticketleap(client: httpx.AsyncClient, url: str) -> dict:
    r = await client.get(url, headers=HEADERS, follow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")
    sold = soup.find(string=re.compile(r"sold.?out", re.I))
    buy = soup.find(string=re.compile(r"buy.tickets|add.to.cart|get.tickets", re.I))
    if buy and not sold:
        return {"available": True, "detail": "Tickets available"}
    return {"available": False, "detail": "Sold out"}


async def check_generic(client: httpx.AsyncClient, url: str) -> dict:
    """Universal fallback checker."""
    r = await client.get(url, headers=HEADERS, follow_redirects=True)
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True).lower()

    sold_patterns = [
        "sold out", "soldout", "no tickets available",
        "not available", "sales ended", "event is over",
    ]
    avail_patterns = [
        "add to cart", "buy tickets", "get tickets",
        "purchase tickets", "select tickets", "book now",
        "register now", "buy now",
    ]

    is_sold = any(p in text for p in sold_patterns)
    is_avail = any(p in text for p in avail_patterns)

    if is_avail and not is_sold:
        matched = next(p for p in avail_patterns if p in text)
        return {"available": True, "detail": f'Found: "{matched}"'}
    if is_sold:
        return {"available": False, "detail": "Sold out text detected"}
    return {"available": False, "detail": "Could not determine status"}


CHECKERS = {
    "etix": check_etix,
    "eventbrite": check_eventbrite,
    "ticketleap": check_ticketleap,
    "showclix": check_generic,
    "generic": check_generic,
}


# ── Telegram ──────────────────────────────────────────────────────────────────

async def send_alert(bot: Bot, event: dict, result: dict, was_available: bool):
    emoji = "🎟" if result["available"] else "❌"
    status = "TICKETS AVAILABLE" if result["available"] else "SOLD OUT AGAIN"
    ts = datetime.now().strftime("%b %d, %Y %H:%M")

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
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def send_startup(bot: Bot, events: list[dict]):
    names = "\n".join(f"  • {e['name']}" for e in events)
    msg = (
        f"🤖 *Ticket Monitor started*\n"
        f"Checking every *{CHECK_INTERVAL}s*\n\n"
        f"*Watching {len(events)} event(s):*\n{names}"
    )
    await bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=msg,
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Main loop ─────────────────────────────────────────────────────────────────

async def monitor():
    bot = Bot(token=TELEGRAM_TOKEN)
    events = load_events()
    state: dict[str, bool | None] = {e["url"]: None for e in events}

    await send_startup(bot, events)
    log.info("Bot started. Monitoring %d events.", len(events))

    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            for event in events:
                url = event["url"]
                platform = detect_platform(url)
                checker = CHECKERS[platform]
                try:
                    result = await checker(client, url)
                    prev = state[url]
                    now_avail = result["available"]

                    # Alert on state change OR first check if available
                    if prev != now_avail or (prev is None and now_avail):
                        log.info(
                            "[%s] %s → %s (%s)",
                            platform, event["name"],
                            "AVAILABLE" if now_avail else "SOLD OUT",
                            result["detail"],
                        )
                        await send_alert(bot, event, result, prev or False)
                    state[url] = now_avail

                except Exception as exc:
                    log.warning("Error checking %s: %s", url, exc)

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(monitor())
