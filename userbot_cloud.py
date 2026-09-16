# -*- coding: utf-8 -*-
"""
AGET Cloud Collector — круглосуточный сбор из Telegram-каналов на сервере.

Работает в облаке (Render), комп держать включённым НЕ надо.
- слушает каналы из LEARN_SOURCES,
- разбирает новые посты через Claude,
- раз в день в 9:00 (Нью-Йорк) шлёт дайджест тебе в «Избранное».

Все секреты — из переменных окружения (ставятся на сервере):
  API_ID, API_HASH, ANTHROPIC_API_KEY, AGET_SESSION  (строка-сессия из make_session.py)
Опционально: DIGEST_HOUR (по умолч. 9), DIGEST_TZ (America/New_York), PORT (даёт Render).
"""

import os
import json
import asyncio
import threading
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SESSION = os.environ.get("AGET_SESSION", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))
DIGEST_TZ = os.environ.get("DIGEST_TZ", "America/New_York")
PORT = int(os.environ.get("PORT", "10000"))

LEARN_SOURCES = [
    "Lab_blog_bot",
    "emocional_intelligence",
    -1002321147732,      # НЕЙРОВОРОНКА 2.0
    "yanaprodast",
    "mirneyrosetey",
    -1002559051428,      # НЕЙРО-РЕАЛИТИ
    "ilioshots_life",
    "mtimochko",
    "ne_institute",
    "dashi_agent",
]

today_notes = []

LEARN_SYSTEM = (
    "Ты — методист по SMM и ИИ. Тебе дают новый пост из Telegram-канала про соцсети/ИИ. "
    "Вытащи ТОЛЬКО практические знания: приёмы, структуры рилсов, хуки, форматы, стратегии, "
    "ИИ-инструменты и как применять, цифры, правила, ошибки, идеи. По-русски, коротко. "
    "Если полезного нет — верни 'НЕТ ПОЛЕЗНОГО'."
)
DIGEST_SYS = (
    "Ты — редактор. Тебе дают заметки за сутки из Telegram-каналов про соцсети и ИИ. "
    "Сделай короткий дайджест: 5–10 самых ценных практичных пунктов, без воды и повторов. "
    "В конце — строка «Что применить сегодня»."
)


def call_claude(system, user, max_tokens=800):
    payload = {"model": MODEL, "max_tokens": max_tokens, "system": system,
               "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
        return "".join(b.get("text", "") for b in data.get("content", [])).strip()
    except Exception as e:
        return f"[Ошибка ИИ: {e}]"


# ---- keep-alive HTTP server (чтобы бесплатный сервер не засыпал) ----
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("AGET collector alive".encode("utf-8"))

    def log_message(self, *a):
        pass


def run_http():
    HTTPServer(("0.0.0.0", PORT), Ping).serve_forever()


client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)


async def digest_loop():
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(DIGEST_TZ)
    except Exception:
        from datetime import timezone
        tz = timezone(timedelta(hours=-4))
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=DIGEST_HOUR, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        if today_notes:
            joined = "\n\n".join(today_notes)[:20000]
            summary = await asyncio.get_event_loop().run_in_executor(
                None, call_claude, DIGEST_SYS, joined, 1200)
            msg = f"☀️ Дайджест за сутки — что нового в каналах:\n\n{summary}"
            today_notes.clear()
        else:
            msg = "☀️ Дайджест за сутки: новых материалов из каналов не поступало."
        try:
            await client.send_message("me", msg[:4000])
        except Exception as e:
            print("digest send error:", e)


async def main():
    await client.start()
    # разрешаем источники, чтобы live-подписка точно ловила
    resolved = []
    for s in LEARN_SOURCES:
        try:
            ent = await client.get_entity(s)
            resolved.append(ent)
        except Exception as e:
            print(f"skip source {s}: {e}")

    @client.on(events.NewMessage(chats=resolved))
    async def on_channel(event):
        text = event.raw_text or ""
        if not text.strip():
            return
        chat = await event.get_chat()
        chan = getattr(chat, "title", None) or getattr(chat, "username", None) or "канал"
        note = await asyncio.get_event_loop().run_in_executor(
            None, call_claude, LEARN_SYSTEM, text, 800)
        if "НЕТ ПОЛЕЗНОГО" in note or note.startswith("[Ошибка"):
            return
        today_notes.append(f"[{chan}]\n{note}")
        print(f"+ collected from {chan}")

    print(f"AGET cloud collector running. Sources: {len(resolved)}. Digest at {DIGEST_HOUR}:00 {DIGEST_TZ}.")
    asyncio.get_event_loop().create_task(digest_loop())
    await client.run_until_disconnected()


if __name__ == "__main__":
    threading.Thread(target=run_http, daemon=True).start()
    asyncio.get_event_loop().run_until_complete(main())
