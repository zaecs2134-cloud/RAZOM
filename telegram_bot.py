"""Telegram-бот «Разом»: сповіщення про заявки та команди /queue, /new.

Працює без публічної адреси (long polling) — зручно запускати локально.
Бот НІКОЛИ не надсилає телефон, адресу чи опис заявки: лише категорію, місто,
рівень критичності та посилання на сайт, де дані доступні після входу.

Потрібні змінні в .env:
    TELEGRAM_BOT_TOKEN     — токен від @BotFather
    TELEGRAM_BOT_USERNAME  — ім'я бота без @ (для посилання «Підключити»)
    SITE_URL               — адреса сайту (https://... у production)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from urllib import request as urlrequest

import ai_service

logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "").lstrip("@")
SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:8000").rstrip("/")

TIER_ICON = {"critical": "🔴", "high": "🟠", "normal": "🟡", "can_wait": "🟢"}
CATEGORY_LABEL = {"food": "Продукти", "medicine": "Ліки", "evacuation": "Евакуація", "other": "Інше"}


def enabled() -> bool:
    return bool(TOKEN)


def _api(method: str, payload: dict, timeout: int = 10) -> dict | None:
    req = urlrequest.Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send_message(chat_id: str, text: str, url: str | None = None) -> None:
    """Надіслати повідомлення. Кнопка-посилання — лише для https (Telegram не приймає http/localhost)."""
    if not TOKEN or not chat_id:
        return
    payload: dict = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if url:
        if url.startswith("https://"):
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Відкрити на сайті", "url": url}]]}
        else:
            payload["text"] = f"{text}\n{url}"
    try:
        _api("sendMessage", payload)
    except Exception:
        logger.exception("Telegram sendMessage failed")


def request_link(request_id: int) -> str:
    return f"{SITE_URL.rstrip('/')}/#request-{request_id}"


def request_line(item, analysis: ai_service.Analysis | None = None) -> str:
    a = analysis or ai_service.analyze_item(item)
    icon = TIER_ICON.get(a.tier, "")
    label = ai_service.TIER_LABELS.get(a.tier, a.tier)
    cat = CATEGORY_LABEL.get(item.category, item.category)
    source = "Gemini AI" if a.source == "gemini" else "локальний аналіз"
    return f"{icon} {label} · {cat} · {item.city} · {item.people_count} люд. · №{item.id} · {source}"


# ---------------------------------------------------------------- сповіщення ---
def _app():
    import main  # відкладений імпорт: уникає циклічної залежності
    return main


def notify_admins_new(request_id: int) -> None:
    """Нова заявка → адміністраторам (вони перевіряють її першими)."""
    if not enabled():
        return
    m = _app()
    with m.SessionLocal() as db:
        item = db.get(m.HelpRequest, request_id)
        if item is None:
            return
        chats = db.scalars(m.select(m.User.telegram_chat_id).where(
            m.User.role == m.Role.admin.value, m.User.telegram_chat_id.is_not(None))).all()
        text = "🆕 Нова заявка, потрібна перевірка\n" + request_line(item)
    for chat in chats:
        send_message(chat, text, request_link(request_id))


def notify_volunteers_visible(request_id: int) -> None:
    """Заявка відкрита волонтерам → підтверджені волонтери з підключеним Telegram.

    «Може зачекати» не розсилається (щоб не втомлювати людей), вона доступна в /queue."""
    if not enabled():
        return
    m = _app()
    with m.SessionLocal() as db:
        item = db.get(m.HelpRequest, request_id)
        if item is None or item.status != m.RequestStatus.new.value:
            return
        analysis = ai_service.analyze_item(item)
        if analysis.tier == ai_service.CAN_WAIT:
            return
        chats = db.scalars(m.select(m.User.telegram_chat_id).where(
            m.User.role == m.Role.volunteer.value, m.User.is_verified.is_(True),
            m.User.telegram_chat_id.is_not(None))).all()
        text = "📣 Потрібна допомога\n" + request_line(item, analysis)
    for chat in chats:
        send_message(chat, text, request_link(request_id))


# ------------------------------------------------------------------- команди ---
HELP_TEXT = (
    "Я бот платформи «Разом».\n"
    "/queue — найкритичніші заявки\n"
    "/new — найновіші заявки\n"
    "/unlink — відключити сповіщення\n\n"
    "Щоб підключитися, натисніть «Підключити Telegram» у своєму кабінеті на сайті."
)


def _user_by_chat(db, chat_id: str):
    m = _app()
    return db.scalar(m.select(m.User).where(m.User.telegram_chat_id == chat_id))


def _consume_link_code(db, code: str, chat_id: str) -> str:
    m = _app()
    digest = hashlib.sha256(code.strip().encode()).hexdigest()
    row = db.scalar(m.select(m.TelegramLinkCode).where(m.TelegramLinkCode.code_sha256 == digest))
    now = datetime.now(timezone.utc)
    if row is None or row.used_at is not None:
        return "Код недійсний. Створіть новий на сайті."
    expires = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires < now:
        return "Термін дії коду минув. Створіть новий на сайті."
    user = db.get(m.User, row.user_id)
    if user is None:
        return "Користувача не знайдено."
    # Один чат — один акаунт.
    for other in db.scalars(m.select(m.User).where(m.User.telegram_chat_id == chat_id)).all():
        other.telegram_chat_id = None
    user.telegram_chat_id = chat_id
    row.used_at = now
    db.commit()
    return f"✅ Підключено: {user.full_name}. Тепер ви отримуватимете сповіщення. /queue — поточна черга."


def _queue_text(user, db, mode: str) -> tuple[str, str | None]:
    m = _app()
    if user.role not in (m.Role.admin.value, m.Role.volunteer.value) or (
            user.role == m.Role.volunteer.value and not user.is_verified):
        return "Черга доступна підтвердженим волонтерам та адміністраторам.", None
    status = m.RequestStatus.new
    items = m.visible_requests(user, db, None, None, status)
    pairs = [(it, ai_service.analyze_item(it)) for it in items]
    if mode == "time":
        pairs.sort(key=lambda p: p[0].created_at.replace(tzinfo=None), reverse=True)
        title = "🕒 Найновіші заявки"
    else:
        pairs.sort(key=lambda p: (ai_service.TIER_ORDER[p[1].tier], -p[1].score))
        title = "🚨 Черга за потребою"
    if not pairs:
        return f"{title}\nНаразі відкритих заявок немає.", None
    lines = [f"{request_line(it, a)}\n{request_link(it.id)}" for it, a in pairs[:5]]
    more = f"\n\nПоказано 5 з {len(pairs)}. Повна черга — на сайті." if len(pairs) > 5 else ""
    return title + "\n\n" + "\n\n".join(lines) + more, SITE_URL


def handle_update(update: dict) -> None:
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    if not text or chat.get("type") != "private":  # лише особисті чати
        return
    chat_id = str(chat["id"])
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    m = _app()
    with m.SessionLocal() as db:
        if cmd == "/start":
            reply = _consume_link_code(db, arg, chat_id) if arg.strip() else HELP_TEXT
            send_message(chat_id, reply)
        elif cmd in ("/queue", "/new"):
            user = _user_by_chat(db, chat_id)
            if user is None:
                send_message(chat_id, "Спершу підключіть Telegram у кабінеті на сайті.")
                return
            reply, url = _queue_text(user, db, "time" if cmd == "/new" else "priority")
            send_message(chat_id, reply, url)
        elif cmd == "/unlink":
            user = _user_by_chat(db, chat_id)
            if user:
                user.telegram_chat_id = None
                db.commit()
            send_message(chat_id, "Сповіщення вимкнено.")
        else:
            send_message(chat_id, HELP_TEXT)


# ------------------------------------------------------------------- polling ---
def poll_forever(stop: threading.Event) -> None:
    offset = 0
    while not stop.is_set():
        try:
            data = _api("getUpdates", {"offset": offset, "timeout": 25,
                                       "allowed_updates": ["message"]}, timeout=35)
            for upd in (data or {}).get("result", []):
                offset = upd["update_id"] + 1
                try:
                    handle_update(upd)
                except Exception:
                    logger.exception("Telegram update handling failed")
        except Exception:
            logger.warning("Telegram polling error; retry in 5s", exc_info=True)
            stop.wait(5)
        else:
            time.sleep(0.2)


def start_polling() -> tuple[threading.Thread, threading.Event] | None:
    if not enabled():
        logger.info("TELEGRAM_BOT_TOKEN is empty: Telegram bot is disabled")
        return None
    stop = threading.Event()
    thread = threading.Thread(target=poll_forever, args=(stop,), name="telegram-poll", daemon=True)
    thread.start()
    return thread, stop
