"""Аналіз заявок: прозора оцінка критичності (правила, без зовнішніх API).

Принципи безпеки:
* Аналіз може лише ПІДНІМАТИ пріоритет відносно того, що вказав заявник, і ніколи
  не знижує його. «Може зачекати» ставиться лише тоді, коли заявник сам вказав низьку
  терміновість і в тексті немає жодного тривожного сигналу.
* Результат — підказка для людини. Кожна оцінка має пояснення (reasons).
* Модуль не має мережевих викликів: персональні дані нікуди не передаються.
  Пізніше сюди можна додати ML/LLM-класифікатор з тим самим інтерфейсом analyze().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import re
import time
from urllib import request as urlrequest, error as urlerror

# Рівні критичності (від найвищого до найнижчого).
CRITICAL, HIGH, NORMAL, CAN_WAIT = "critical", "high", "normal", "can_wait"
TIER_ORDER = {CRITICAL: 0, HIGH: 1, NORMAL: 2, CAN_WAIT: 3}
TIER_LABELS = {CRITICAL: "Критична", HIGH: "Висока", NORMAL: "Звичайна", CAN_WAIT: "Може зачекати"}

# Корені слів (укр. + рос., бо люди пишуть обома). Порівняння — за підрядком, у нижньому регістрі.
CRITICAL_KEYWORDS = {
    "задих": "ознаки ускладненого дихання",
    "задых": "ознаки ускладненого дихання",
    "не дихає": "ознаки ускладненого дихання",
    "кисню": "потреба в кисні",
    "кислород": "потреба в кисні",
    "кровотеч": "кровотеча",
    "кровоте": "кровотеча",
    "поранен": "поранення",
    "ранен": "поранення",
    "інсульт": "підозра на інсульт",
    "инсульт": "підозра на інсульт",
    "інфаркт": "підозра на інфаркт",
    "инфаркт": "підозра на інфаркт",
    "без свідомост": "втрата свідомості",
    "без сознани": "втрата свідомості",
    "втрата свідомост": "втрата свідомості",
    "інсулін": "потреба в інсуліні",
    "инсулин": "потреба в інсуліні",
    "діабет": "діабет",
    "диабет": "діабет",
    "судом": "судоми",
    "судорог": "судоми",
    "завал": "людина під завалами",
    "під завалами": "людина під завалами",
    "под завалами": "людина під завалами",
    "пожеж": "пожежа",
    "пожар": "пожежа",
    "обстріл": "небезпека обстрілу",
    "обстрел": "небезпека обстрілу",
    "терміново": "заявник пише про терміновість",
    "срочно": "заявник пише про терміновість",
    "негайно": "заявник пише про терміновість",
}
HIGH_KEYWORDS = {
    "дитин": "є дитина",
    "ребен": "є дитина",
    "ребён": "є дитина",
    "немовл": "є немовля",
    "младен": "є немовля",
    "вагітн": "вагітність",
    "беремен": "вагітність",
    "літн": "літня людина",
    "пристар": "літня людина",
    "пожил": "літня людина",
    "лежач": "лежача людина",
    "інвалід": "людина з інвалідністю",
    "инвалид": "людина з інвалідністю",
    "ліки": "потреба в ліках",
    "лекарств": "потреба в ліках",
    "тиск": "проблеми з тиском",
    "температур": "висока температура",
    "без води": "немає води",
    "без воды": "немає води",
    "без світла": "немає світла/опалення",
    "без опален": "немає світла/опалення",
    "холодно": "мерзнуть",
    "не можу вийти": "не може вийти з дому",
    "не можу ходити": "не може ходити",
}

CATEGORY_BONUS = {"evacuation": 12, "medicine": 8, "food": 3, "other": 0}


@dataclass
class Analysis:
    tier: str
    score: int
    reasons: list[str] = field(default_factory=list)
    source: str = "rules"


def _age_hours(created: datetime | None) -> float:
    if created is None:
        return 0.0
    if created.tzinfo is None:  # SQLite зберігає наївні дати.
        created = created.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - created).total_seconds() / 3600)


def _hits(text: str, table: dict[str, str]) -> list[str]:
    found: list[str] = []
    for stem, label in table.items():
        if stem in text and label not in found:
            found.append(label)
    return found


def analyze(*, description: str, category: str, urgency: int, people_count: int,
            vulnerable: bool, created_at: datetime | None = None) -> Analysis:
    text = (description or "").lower()
    reasons: list[str] = []

    critical_hits = _hits(text, CRITICAL_KEYWORDS)
    high_hits = [h for h in _hits(text, HIGH_KEYWORDS) if h not in critical_hits]

    # --- Рівень -----------------------------------------------------------------
    tier = CAN_WAIT if urgency <= 2 else NORMAL
    if urgency >= 4:
        tier = HIGH
        reasons.append(f"заявник вказав терміновість {urgency} із 5")
    if urgency == 5 and (vulnerable or category in ("medicine", "evacuation")):
        tier = CRITICAL
        reasons.append("максимальна терміновість + вразлива група або ліки/евакуація")
    if vulnerable:
        reasons.append("є вразливі люди")
        if urgency >= 3 and tier in (NORMAL, CAN_WAIT):
            tier = HIGH
        elif tier == CAN_WAIT:
            tier = NORMAL
    if category in ("medicine", "evacuation") and urgency >= 3 and tier in (NORMAL, CAN_WAIT):
        tier = HIGH
        reasons.append("медична або евакуаційна допомога")
    if critical_hits:
        tier = CRITICAL
        reasons.extend(critical_hits)
    if high_hits:
        if tier in (NORMAL, CAN_WAIT):
            tier = HIGH
        reasons.extend(high_hits)
    if people_count >= 5 and tier in (NORMAL, CAN_WAIT):
        tier = HIGH
        reasons.append(f"багато людей ({people_count})")
    if tier == CAN_WAIT:
        reasons.append("заявник вказав, що може зачекати; тривожних ознак у тексті не знайдено")
    elif not reasons:
        reasons.append("стандартна заявка")

    # --- Бали для сортування всередині рівня --------------------------------------
    age_hours = _age_hours(created_at)
    score = (
        urgency * 15
        + CATEGORY_BONUS.get(category, 0)
        + (10 if vulnerable else 0)
        + min(people_count, 10) * 2
        + min(int(age_hours // 6) * 2, 24)   # довше чекає — вище
        + (25 if critical_hits else 0)
        + (min(len(high_hits), 3) * 4)
    )
    return Analysis(tier=tier, score=score, reasons=reasons[:6])


# Gemini is optional. Without a key, the local transparent rules remain active.
_GEMINI_CACHE: dict[str, tuple[float, Analysis]] = {}
_GEMINI_CACHE_TTL = 86400  # 24 hours
_AI_PROCESS_STARTED_AT = datetime.now(timezone.utc)


def _safe_description(value: str) -> str:
    """Remove common contact details before sending text to the external model."""
    value = (value or "")[:4000]
    value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-zА-Яа-яІіЇїЄє]{2,}", "[прихована електронна адреса]", value)
    value = re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "[прихований номер телефону]", value)
    value = re.sub(r"(?i)\b(вул\.?|улица|проспект|провулок|переулок|будинок|буд\.?|квартира|кв\.?)\s*[^,;\n]{1,80}", "[прихована адреса]", value)
    return value


def _gemini_analysis(item, base: Analysis) -> Analysis:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return base

    # Do not unexpectedly send an entire historical database to Gemini when an
    # admin opens the queue. Only requests created during this server session
    # are eligible for external AI analysis. Older records keep local analysis.
    created = getattr(item, "created_at", None)
    if created is not None:
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created < _AI_PROCESS_STARTED_AT:
            return base

    description = _safe_description(getattr(item, "description", ""))
    cache_key = json.dumps({
        "description": description,
        "category": getattr(item, "category", "other"),
        "urgency": getattr(item, "urgency", 3),
        "people": getattr(item, "people_count", 1),
        "vulnerable": bool(getattr(item, "vulnerable_people", False)),
    }, ensure_ascii=False, sort_keys=True)
    cached = _GEMINI_CACHE.get(cache_key)
    now = time.time()
    if cached and now - cached[0] < _GEMINI_CACHE_TTL:
        return cached[1]

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    prompt = (
        "Ти допоміжний класифікатор заявок гуманітарної допомоги. Не вигадуй фактів. "
        "Поверни ЛИШЕ JSON без Markdown у форматі "
        '{"tier":"critical|high|normal|can_wait","reasons":["коротка причина"]}. '
        "Критичний — безпосередня загроза життю/безпеці; високий — термінова потреба або вразливі люди; "
        "звичайний — допомога потрібна, але немає ознак негайної небезпеки; може зачекати — лише якщо це явно випливає з тексту. "
        "Не став медичний діагноз і не ухвалюй рішень про лікування чи евакуацію. "
        "Врахуй, що це лише попередня оцінка для перевірки людиною. "
        f"Категорія: {getattr(item, 'category', 'other')}. "
        f"Терміновість від заявника (1-5): {getattr(item, 'urgency', 3)}. "
        f"Кількість людей: {getattr(item, 'people_count', 1)}. "
        f"Є вразливі люди: {'так' if getattr(item, 'vulnerable_people', False) else 'ні'}. "
        f"Опис без контактних даних: {description}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
    }
    req = urlrequest.Request(url, data=json.dumps(payload).encode("utf-8"),
                             headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=8) as response:
            data = json.loads(response.read().decode("utf-8"))
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        result = json.loads(raw)
        tier = result.get("tier")
        if tier not in TIER_ORDER:
            return base
        # AI may raise urgency, but is never allowed to downgrade the transparent baseline.
        chosen = tier if TIER_ORDER[tier] < TIER_ORDER[base.tier] else base.tier
        ai_reasons = result.get("reasons", [])
        if not isinstance(ai_reasons, list):
            ai_reasons = []
        reasons = list(dict.fromkeys((base.reasons + [str(x)[:180] for x in ai_reasons if isinstance(x, str)])))[:6]
        # Keep score coherent with the final tier while preserving the original score ranking.
        score = base.score + (40 if chosen == CRITICAL and base.tier != CRITICAL else
                              20 if chosen == HIGH and base.tier not in (CRITICAL, HIGH) else 0)
        analysis = Analysis(tier=chosen, score=score, reasons=reasons, source="gemini")
        if len(_GEMINI_CACHE) > 1000:
            _GEMINI_CACHE.clear()
        _GEMINI_CACHE[cache_key] = (now, analysis)
        return analysis
    except (urlerror.URLError, TimeoutError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        # Fail safely: keep the local rule-based result if Gemini is unavailable or returns invalid data.
        return base


def analyze_item(item) -> Analysis:
    """Analyze with Gemini when configured; otherwise use transparent local rules."""
    base = analyze(description=item.description, category=item.category, urgency=item.urgency,
                   people_count=item.people_count, vulnerable=bool(item.vulnerable_people),
                   created_at=item.created_at)
    return _gemini_analysis(item, base)
