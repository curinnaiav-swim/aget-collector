# -*- coding: utf-8 -*-
"""
AGET Digest (one-shot) — запускается РАЗ В ДЕНЬ по расписанию (GitHub Actions).

Ключевое отличие от старого userbot_cloud.py:
- НЕ висит 24/7 и НЕ копит посты в памяти (это и ломалось при засыпании сервера),
- при каждом запуске сам ПОДГРУЖАЕТ ИСТОРИЮ за последние 24 часа из каждого канала,
- собирает сырьё и шлёт письмо [AGET-DIGEST] себе на почту,
- завершается. Никакого сервера держать не надо.

Секреты — из переменных окружения (GitHub Actions Secrets):
  API_ID, API_HASH, AGET_SESSION, GMAIL_ADDRESS, GMAIL_APP_PASSWORD
Опц.: LOOKBACK_HOURS (24), MAX_PER_CHAT (60)
"""

import os
import ssl
import smtplib
import asyncio
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
SESSION = os.environ.get("AGET_SESSION", "")

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
MAX_PER_CHAT = int(os.environ.get("MAX_PER_CHAT", "60"))

# Небольшой запас, чтобы при сдвиге запуска (GitHub Actions может опаздывать)
# не терять посты на стыке суток. Небольшое перекрытие безопаснее пропусков.
BUFFER_HOURS = 1

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

# Семейные группы для чтения (по ТОЧНОМУ названию, регистр не важен)
FAMILY_TITLES = ["aget family"]


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


async def collect_from(client, entity, cutoff, max_items):
    """Тянет сообщения за окно cutoff..now, новые сверху. Возвращает список текстов."""
    out = []
    try:
        async for msg in client.iter_messages(entity, limit=max_items):
            if msg.date and msg.date < cutoff:
                break
            text = (msg.text or msg.raw_text or "").strip()
            if text:
                out.append(text[:1500])
    except Exception as e:
        print(f"  ! error reading {entity}: {e}", flush=True)
    return out


async def main():
    if not (API_ID and API_HASH and SESSION and GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        raise SystemExit("Не заданы секреты: API_ID/API_HASH/AGET_SESSION/GMAIL_ADDRESS/GMAIL_APP_PASSWORD")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS + BUFFER_HOURS)
    date = datetime.now().strftime("%Y-%m-%d")

    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()

    # --- каналы про SMM/ИИ ---
    notes = []
    for s in LEARN_SOURCES:
        try:
            ent = await client.get_entity(s)
        except Exception as e:
            print(f"skip source {s}: {e}", flush=True)
            continue
        title = getattr(ent, "title", None) or getattr(ent, "username", None) or str(s)
        posts = await collect_from(client, ent, cutoff, MAX_PER_CHAT)
        print(f"+ {title}: {len(posts)} posts", flush=True)
        for p in posts:
            notes.append(f"[{title}] {p}")

    # --- семейный чат (я + папа) ---
    fam_notes = []
    wanted = [t.strip().lower() for t in FAMILY_TITLES]
    async for d in client.iter_dialogs():
        title = (getattr(d, "name", "") or "").strip().lower()
        if title in wanted:
            print(f"family group found: {d.name}", flush=True)
            msgs = await collect_from(client, d.entity, cutoff, MAX_PER_CHAT)
            for m in msgs:
                fam_notes.append(m)

    # --- письмо с сырьём каналов ([AGET-DIGEST]) ---
    if notes:
        body = (f"Собрано постов за сутки: {len(notes)}\n\n"
                + "\n\n— — —\n\n".join(notes))
    else:
        body = "За сутки новых постов в каналах не было."
    subject = f"[AGET-DIGEST] посты за сутки {date}"
    try:
        send_email(subject, body)
        print("digest emailed", flush=True)
    except Exception as e:
        print("email error:", e, flush=True)
        # запасной вариант: если письмо не ушло — кинем в Telegram «Избранное»
        try:
            for i in range(0, len(body), 3500):
                await client.send_message("me", body[i:i + 3500])
        except Exception as e2:
            print("tg fallback error:", e2, flush=True)

    # --- отдельное письмо по семейному чату ([AGET-FAMILY]) ---
    if fam_notes:
        fam_body = (f"Сообщений в семейном чате за сутки: {len(fam_notes)}\n\n"
                    + "\n\n— — —\n\n".join(fam_notes))
        try:
            send_email(f"[AGET-FAMILY] чат за сутки {date}", fam_body)
            print("family emailed", flush=True)
        except Exception as e:
            print("family email error:", e, flush=True)

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
