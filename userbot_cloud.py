# -*- coding: utf-8 -*-
"""
AGET Cloud Collector — круглосуточный сбор из Telegram-каналов на сервере.

Работает в облаке (Render), комп держать включённым НЕ надо.
- слушает каналы из LEARN_SOURCES,
- разбирает новые посты через Claude,
- раз в день в 9:00 (Нью-Йорк) шлёт дайджест тебе в «Избранное».

Секреты — из переменных окружения на сервере:
  API_ID, API_HASH, ANTHROPIC_API_KEY, AGET_SESSION
Опц.: DIGEST_HOUR (9), DIGEST_TZ (America/New_York), PORT (даёт Render).
"""

import os
import ssl
import json
import smtplib
import asyncio
import threading
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SESSION = os.environ.get("AGET_SESSION", "")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")  # дёшево
DIGEST_HOUR = int(os.environ.get("DIGEST_HOUR", "9"))
DIGEST_TZ = os.environ.get("DIGEST_TZ", "America/New_York")
PORT = int(os.environ.get("PORT", "10000"))

# Почта (Вариант 2): куда слать дайджест. Пароль — «App Password» Gmail.
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")


def send_email(subject, body):
    """Шлёт письмо самому себе через Gmail SMTP (App Password)."""
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = str(Header(subject, "utf-8"))
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = GMAIL_ADDRESS
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx, timeout=60) as s:
        s.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        s.sendmail(GMAIL_ADDRESS, [GMAIL_ADDRESS], msg.as_string())

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
family_notes = []       # сообщения из семейной группы (я + папа)
client = None  # создаётся внутри main(), когда уже есть цикл событий

# Семейные группы для чтения (по ТОЧНОМУ названию, регистр не важен)
FAMILY_TITLES = ["aget family"]

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


async def on_channel(event):
    # ДЁШЕВО: Claude на каждый пост НЕ зовём. Просто копим сырой текст.
    text = event.raw_text or ""
    if not text.strip():
        return
    chat = await event.get_chat()
    chan = getattr(chat, "title", None) or getattr(chat, "username", None) or "канал"
    today_notes.append(f"[{chan}] {text.strip()[:1500]}")
    print(f"+ collected from {chan}", flush=True)


async def on_family(event):
    # Семейный чат (я + папа): копим сырой текст для отдельного письма [AGET-FAMILY].
    text = event.raw_text or ""
    if not text.strip():
        return
    sender = await event.get_sender()
    who = getattr(sender, "first_name", None) or "автор"
    family_notes.append(f"{who}: {text.strip()[:2000]}")
    print(f"+ family msg from {who}", flush=True)


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
        # БЕСПЛАТНО: Claude не зовём. Шлём сырые собранные посты НА ПОЧТУ.
        loop = asyncio.get_running_loop()
        date = datetime.now().strftime("%Y-%m-%d")
        if today_notes:
            body = (f"Собрано постов за сутки: {len(today_notes)}\n\n"
                    + "\n\n— — —\n\n".join(today_notes))
            today_notes.clear()
        else:
            body = "За сутки новых постов в каналах не было."
        subject = f"[AGET-DIGEST] посты за сутки {date}"
        try:
            await loop.run_in_executor(None, send_email, subject, body)
            print("digest emailed", flush=True)
        except Exception as e:
            print("email error:", e, flush=True)
            # запасной вариант: если письмо не ушло — кинем в Telegram «Избранное»
            try:
                for i in range(0, len(body), 3500):
                    await client.send_message("me", body[i:i + 3500])
            except Exception as e2:
                print("tg fallback error:", e2, flush=True)

        # Отдельное письмо по семейному чату (я + папа)
        if family_notes:
            fam_body = (f"Сообщений в семейном чате за сутки: {len(family_notes)}\n\n"
                        + "\n\n— — —\n\n".join(family_notes))
            family_notes.clear()
            try:
                await loop.run_in_executor(
                    None, send_email, f"[AGET-FAMILY] чат за сутки {date}", fam_body)
                print("family emailed", flush=True)
            except Exception as e:
                print("family email error:", e, flush=True)


async def main():
    global client
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()

    resolved = []
    for s in LEARN_SOURCES:
        try:
            resolved.append(await client.get_entity(s))
        except Exception as e:
            print(f"skip source {s}: {e}", flush=True)

    # семейные группы — ищем по названию среди диалогов
    family = []
    wanted = [t.strip().lower() for t in FAMILY_TITLES]
    async for d in client.iter_dialogs():
        title = (getattr(d, "name", "") or "").strip().lower()
        if title in wanted:
            family.append(d.entity)
            print(f"family group found: {d.name}", flush=True)

    client.add_event_handler(on_channel, events.NewMessage(chats=resolved))
    if family:
        client.add_event_handler(on_family, events.NewMessage(chats=family))
    asyncio.create_task(digest_loop())
    print(f"AGET cloud collector running. Sources: {len(resolved)}, "
          f"family groups: {len(family)}. Digest at {DIGEST_HOUR}:00 {DIGEST_TZ}.", flush=True)
    await client.run_until_disconnected()


if __name__ == "__main__":
    threading.Thread(target=run_http, daemon=True).start()
    asyncio.run(main())
