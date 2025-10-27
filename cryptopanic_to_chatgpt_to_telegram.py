#!/usr/bin/env python3
"""
cryptopanic_to_chatgpt_to_telegram.py
Fixed version for Render deployment.
"""

import os
import time
import json
import logging
import threading
from io import BytesIO
from pathlib import Path
from typing import Optional, Dict, Any, List
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
import openai
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

# Load .env file if present
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = os.getenv("TARGET_CHAT_ID")
CRYPTOPANIC_API_KEY = os.getenv("CRYPTOPANIC_API_KEY")
SOCIAL_IG_HANDLE = os.getenv("SOCIAL_IG_HANDLE", "")
SOCIAL_X_HANDLE = os.getenv("SOCIAL_X_HANDLE", "")
SOCIAL_YT_HANDLE = os.getenv("SOCIAL_YT_HANDLE", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))

LAST_SEEN_FILE = Path("last_seen_ids.json")
LOGFILE = "cp_to_telegram.log"

if not OPENAI_API_KEY or not TELEGRAM_BOT_TOKEN or not TARGET_CHAT_ID:
    raise SystemExit("Set required environment variables in .env or Render dashboard")

openai.api_key = OPENAI_API_KEY
bot = Bot(token=TELEGRAM_BOT_TOKEN)

logging.basicConfig(level=logging.INFO, filename=LOGFILE, filemode="a",
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CRYPTOPANIC_API_URL = "https://cryptopanic.com/api/v1/posts/"


def read_last_seen() -> Dict[str, Any]:
    if LAST_SEEN_FILE.exists():
        try:
            return json.loads(LAST_SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"posted_ids": []}
    return {"posted_ids": []}


def write_last_seen(data: Dict[str, Any]):
    LAST_SEEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def download_image(url: str) -> Optional[BytesIO]:
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        bio = BytesIO(r.content)
        bio.name = "image.jpg"
        bio.seek(0)
        return bio
    except Exception:
        return None


def generate_fallback_image(headline: str, width=1080, height=1080) -> BytesIO:
    img = Image.new("RGB", (width, height), (18, 22, 28))
    draw = ImageDraw.Draw(img)
    try:
        font_large = ImageFont.truetype("DejaVuSans-Bold.ttf", 56)
        font_small = ImageFont.truetype("DejaVuSans.ttf", 28)
    except Exception:
        font_large = ImageFont.load_default()
        font_small = ImageFont.load_default()
    words = headline.split()
    lines, line = [], ""
    for w in words:
        if len(line + " " + w) > 28:
            lines.append(line.strip())
            line = w
        else:
            line += " " + w
    if line:
        lines.append(line.strip())
    y = 140
    for ln in lines[:6]:
        draw.text((60, y), ln, font=font_large, fill=(235, 245, 255))
        y += 80
    draw.text((60, height - 80), "Crypto With Sarvesh", font=font_small, fill=(200, 210, 220))
    out = BytesIO()
    out.name = "fallback.jpg"
    img.save(out, format="JPEG", quality=85)
    out.seek(0)
    return out


def fetch_via_api() -> List[Dict[str, Any]]:
    if not CRYPTOPANIC_API_KEY:
        return []
    params = {"auth_token": CRYPTOPANIC_API_KEY, "public": "true", "page": 1}
    try:
        resp = requests.get(CRYPTOPANIC_API_URL, params=params, timeout=20)
        resp.raise_for_status()
        j = resp.json()
        results = j.get("results") or j.get("posts") or j or []
        logger.info("Fetched %d items from CryptoPanic API", len(results))
        return results
    except Exception as e:
        logger.exception("API fetch failed: %s", e)
        return []


def scrape_public_feed() -> List[Dict[str, Any]]:
    url = "https://cryptopanic.com/news/"
    try:
        resp = requests.get(url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        items = []
        for a in soup.select("a")[:100]:
            href = a.get("href", "")
            txt = a.get_text(strip=True)
            if href and txt and "/news/" in href:
                link = href if href.startswith("http") else ("https://cryptopanic.com" + href)
                items.append({"title": txt, "url": link, "id": link})
        logger.info("Scraped %d items from public feed", len(items))
        return items
    except Exception as e:
        logger.exception("Scrape failed: %s", e)
        return []


def build_openai_prompt(item: Dict[str, Any]) -> str:
    title = item.get("title") or item.get("title_plain") or ""
    domain = ""
    if isinstance(item.get("source"), dict):
        domain = item["source"].get("title", "")
    else:
        domain = item.get("domain") or ""
    hint = item.get("excerpt") or item.get("clean_url") or ""
    prompt = f"""You are a Telegram channel editor for "Crypto With Sarvesh".
Given the news item below, produce a short HTML-formatted Telegram caption that:

- Is MAX 220 characters (count characters, do not exceed 220).
- Uses HTML formatting; bold the single most important sentence using <b>...</b>.
- Does NOT include any external links or URLs.
- Keeps tone educational and neutral (no promises/returns/price predictions).
- Output only the HTML caption, then a newline, then the plain caption prefixed by 'PLAIN_CAPTION:'.

News item:
Title: {title}
Source: {domain}
Hint: {hint}
"""
    return prompt


def call_openai_generate(prompt: str, model: str = "gpt-4o-mini") -> Optional[Dict[str, str]]:
    try:
        resp = openai.ChatCompletion.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a concise Telegram post writer."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=320,
            temperature=0.25,
        )
        text = resp.choices[0].message.content.strip()
        if "PLAIN_CAPTION:" in text:
            html_part, plain_part = text.split("PLAIN_CAPTION:", 1)
            return {"html": html_part.strip(), "plain": plain_part.strip()}
        else:
            plain = BeautifulSoup(text, "html.parser").get_text()
            return {"html": text, "plain": plain}
    except Exception as e:
        logger.exception("OpenAI call failed: %s", e)
        return None


def make_social_keyboard_popup():
    buttons = []
    if SOCIAL_IG_HANDLE:
        buttons.append(InlineKeyboardButton("IG", callback_data="social_ig"))
    if SOCIAL_X_HANDLE:
        buttons.append(InlineKeyboardButton("X", callback_data="social_x"))
    if SOCIAL_YT_HANDLE:
        buttons.append(InlineKeyboardButton("YT", callback_data="social_yt"))
    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons])


def callback_poller_loop(sleep_seconds: int = 2):
    logger.info("Starting callback poller thread")
    offset = 0
    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout=10)
            for upd in updates:
                offset = max(offset, upd.update_id + 1)
                if upd.callback_query:
                    cq = upd.callback_query
                    data = cq.data
                    text = "Handle not set."
                    if data == "social_ig" and SOCIAL_IG_HANDLE:
                        text = f"IG: {SOCIAL_IG_HANDLE}"
                    elif data == "social_x" and SOCIAL_X_HANDLE:
                        text = f"X: {SOCIAL_X_HANDLE}"
                    elif data == "social_yt" and SOCIAL_YT_HANDLE:
                        text = f"YT: {SOCIAL_YT_HANDLE}"
                    bot.answer_callback_query(callback_query_id=cq.id, text=text, show_alert=True)
            time.sleep(sleep_seconds)
        except Exception as e:
            logger.exception("Callback poller error: %s", e)
            time.sleep(5)


def process_once():
    last_seen = read_last_seen()
    posted_ids = set(last_seen.get("posted_ids", []))
    items = fetch_via_api() if CRYPTOPANIC_API_KEY else scrape_public_feed()
    if not items:
        logger.info("No items fetched")
        return
    normalized = []
    for it in items:
        nid = str(it.get("id") or it.get("url") or it.get("title") or "")
        title = it.get("title") or it.get("title_plain") or ""
        normalized.append({"id": nid, "title": title, "raw": it})
    normalized = list(reversed(normalized))
    for it in normalized:
        item_id = it["id"]
        if not item_id or item_id in posted_ids:
            continue
        raw = it["raw"]
        media_url = None
        if isinstance(raw.get("media"), list) and raw.get("media"):
            media_url = raw["media"][0].get("url")
        media_url = media_url or raw.get("thumbnail") or raw.get("image") or ""
        image_stream = download_image(media_url) if media_url.startswith("http") else None
        if not image_stream:
            image_stream = generate_fallback_image(it["title"])
        prompt = build_openai_prompt(raw)
        gen = call_openai_generate(prompt)
        if not gen:
            continue
        keyboard = make_social_keyboard_popup()
        try:
            bot.send_photo(chat_id=TARGET_CHAT_ID, photo=image_stream, caption=gen["html"], parse_mode="HTML", reply_markup=keyboard)
            logger.info("Posted item %s", item_id)
            posted_ids.add(item_id)
            last_seen["posted_ids"] = list(posted_ids)
            write_last_seen(last_seen)
            time.sleep(1.2)
        except Exception as e:
            logger.exception("Telegram post failed: %s", e)


def main(loop=False):
    threading.Thread(target=callback_poller_loop, daemon=True).start()
    if loop:
        logger.info("Running in loop mode")
        while True:
            process_once()
            time.sleep(POLL_INTERVAL_SECONDS)
    else:
        process_once()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    args = parser.parse_args()
    main(loop=args.loop)
