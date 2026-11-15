"""Additional opt-in RAZOM features. No database migrations or new packages.

Routes are registered from the bottom of main.py, after its models and dependencies
are declared, so the existing production setup (including Telegram webhook) is kept.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

from fastapi import APIRouter, Query
from sqlalchemy import select

from main import (
    CurrentUser, Db, GuestReceipt, HelpRequest, RequestStatus, Resource,
    Role, ai_service, require,
)

router = APIRouter(tags=["RAZOM — додаткові функції"])

# Approximate city CENTRES only. These are not personal/geocoded addresses.
# The offline lookup intentionally leaves unknown settlements unplotted.
CITY_COORDS = {
    "київ": (50.4501, 30.5234), "львів": (49.8397, 24.0297),
    "одеса": (46.4825, 30.7233), "харків": (49.9935, 36.2304),
    "дніпро": (48.4647, 35.0462), "запоріжжя": (47.8388, 35.1396),
    "миколаїв": (46.9750, 31.9946), "херсон": (46.6354, 32.6169),
    "чернігів": (51.4982, 31.2893), "ніжин": (51.0480, 31.8860),
    "бахмач": (51.1833, 32.8333), "корюківка": (51.7680, 32.2480),
    "прилуки": (50.5932, 32.3876), "суми": (50.9077, 34.7981),
    "конотоп": (51.2370, 33.2020), "полтава": (49.5883, 34.5514),
    "черкаси": (49.4444, 32.0598), "кропивницький": (48.5079, 32.2623),
    "вінниця": (49.2331, 28.4682), "житомир": (50.2547, 28.6587),
    "рівне": (50.6199, 26.2516), "луцьк": (50.7472, 25.3254),
    "тернопіль": (49.5535, 25.5948), "хмельницький": (49.4229, 26.9871),
    "чернівці": (48.2915, 25.9403), "івано-франківськ": (48.9226, 24.7111),
    "ужгород": (48.6208, 22.2879), "мукачево": (48.4403, 22.7178),
    "біла церква": (49.7956, 30.1167), "бровари": (50.5110, 30.7909),
    "бориспіль": (50.3482, 30.9561), "кременчук": (49.0681, 33.4204),
    "кривий ріг": (47.9105, 33.3918), "кам'янець-подільський": (48.6746, 26.5852),
    "слов'янськ": (48.8532, 37.6054), "краматорськ": (48.7389, 37.5843),
    "павлоград": (48.5343, 35.8707), "баштанка": (47.4072, 32.4387),
}
CITY_ALIASES = {
    "киев": "київ", "харьков": "харків", "днепр": "дніпро",
    "одесса": "одеса", "николаев": "миколаїв", "чернигов": "чернігів",
    "нежин": "ніжин", "тернополь": "тернопіль",
    "ивано франковск": "івано-франківськ",
}


def city_key(text: str) -> str:
    value = unicodedata.normalize("NFKC", text or "").strip().casefold()
    value = value.replace("’", "'").replace("ʼ", "'")
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"^(м\.|місто|город)\s+", "", value)
    return CITY_ALIASES.get(value, value)


def location(city: str):
    return CITY_COORDS.get(city_key(city))


def haversine_km(point_a, point_b) -> float:
    lat1, lon1 = (math.radians(x) for x in point_a)
    lat2, lon2 = (math.radians(x) for x in point_b)
    delta_lat, delta_lon = lat2 - lat1, lon2 - lon1
    a = (math.sin(delta_lat / 2) ** 2 +
         math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2)
    return 6371.0088 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def comparable(a: HelpRequest, b: HelpRequest):
    """Duplicate *candidate*; never silently reject a person's request."""
    if a.id == b.id or a.category != b.category or city_key(a.city) != city_key(b.city):
        return None
    ad, bd = normalize_text(a.description), normalize_text(b.description)
    if len(ad) < 12 or len(bd) < 12:
        return None
    ratio = SequenceMatcher(None, ad, bd).ratio()
    digits_a = re.sub(r"\D", "", a.phone or "")
    digits_b = re.sub(r"\D", "", b.phone or "")
    same_phone = len(digits_a) >= 7 and digits_a == digits_b
    # Matching phone by itself is not enough: demo users may share a test number.
    if ratio < (0.78 if same_phone else 0.88):
        return None
    return {
        "request_a": min(a.id, b.id), "request_b": max(a.id, b.id),
        "similarity_percent": round(100 * ratio),
        "city": a.city, "category": a.category,
        "same_contact": same_phone,
        "reason": "Схожий опис, однакові місто та категорія" +
                  ("; збігається контакт" if same_phone else ""),
    }


@router.get("/admin/duplicates")
def suspected_duplicates(user: CurrentUser, db: Db, days: int = Query(30, ge=1, le=90)):
    require(user, Role.admin)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    entries = db.scalars(select(HelpRequest).where(
        HelpRequest.created_at >= cutoff,
        HelpRequest.status != RequestStatus.cancelled.value,
    ).order_by(HelpRequest.created_at.desc()).limit(501)).all()
    truncated = len(entries) > 500
    entries = entries[:500]
    groups = defaultdict(list)
    for req in entries:
        groups[(city_key(req.city), req.category)].append(req)
    candidates = []
    for items in groups.values():
        for index, item in enumerate(items):
            for other in items[index + 1:]:
                result = comparable(item, other)
                if result:
                    candidates.append(result)
    candidates.sort(key=lambda x: (-x["similarity_percent"], x["request_a"], x["request_b"]))
    return {"matches": candidates[:100], "checked_requests": len(entries),
            "truncated": truncated or len(candidates) > 100,
            "note": "Це лише можливі дублікати. Рішення ухвалює адміністратор."}


def _created_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


@router.get("/admin/forecast")
def needs_forecast(user: CurrentUser, db: Db):
    """Illustrative seven-day trend; returns null until history is sufficient."""
    require(user, Role.admin)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=28)
    items = db.scalars(select(HelpRequest).where(
        HelpRequest.created_at >= cutoff,
        HelpRequest.status != RequestStatus.cancelled.value,
    ).limit(10001)).all()
    truncated = len(items) > 10000
    items = items[:10000]
    categories = ("food", "medicine", "evacuation", "other")
    by_category = []
    for category in categories:
        category_items = [i for i in items if i.category == category]
        last7 = sum(_created_utc(i.created_at) >= now - timedelta(days=7) for i in category_items)
        previous7 = sum(now - timedelta(days=14) <= _created_utc(i.created_at) < now - timedelta(days=7) for i in category_items)
        # Two full weekly windows plus enough independent observations needed.
        earliest = min((_created_utc(i.created_at) for i in category_items), default=now)
        sufficient = len(category_items) >= 10 and (now - earliest).days >= 14 and not truncated
        projected = round(0.65 * last7 + 0.35 * previous7) if sufficient else None
        trend = ("зростання" if last7 > previous7 else
                 "зниження" if last7 < previous7 else "без змін")
        by_category.append({
            "category": category, "requests_28d": len(category_items),
            "last_7d": last7, "previous_7d": previous7,
            "forecast_next_7d": projected, "trend": trend,
            "enough_data": sufficient,
        })
    return {
        "period": "Наступні 7 днів", "method": "Зважене середнє за попередні два тижні (65% / 35%)",
        "note": "Орієнтовна статистична оцінка, не гарантія попиту і не прогноз надзвичайних подій.",
        "truncated": truncated, "categories": by_category,
    }


@router.get("/admin/geography")
def geography(user: CurrentUser, db: Db):
    """RAZOM 2.6: admin-only city aggregates with evacuation counts and request IDs.

    Never return a street address, phone number or an applicant's exact position.
    The UI may resolve *settlement names only* to approximate centres.
    """
    require(user, Role.admin)
    items = db.scalars(select(HelpRequest).where(
        HelpRequest.status.in_((RequestStatus.new.value, RequestStatus.assigned.value))
    ).limit(10001)).all()
    resources = db.scalars(select(Resource).where(Resource.quantity > 0).limit(10001)).all()
    per_city = defaultdict(lambda: {
        "requests": 0, "resources": 0,
        "evacuation_requests": 0, "evacuation_request_ids": [],
    })
    for item in items[:10000]:
        counts = per_city[city_key(item.city)]
        counts["requests"] += 1
        if item.category == "evacuation":
            counts["evacuation_requests"] += 1
            if len(counts["evacuation_request_ids"]) < 30:
                counts["evacuation_request_ids"].append(item.id)
    for resource in resources[:10000]:
        per_city[city_key(resource.city)]["resources"] += 1
    points, missing = [], []
    for city, counts in sorted(per_city.items()):
        coords = location(city)
        record = {"city": city, **counts}
        if coords:
            points.append({**record, "lat": coords[0], "lng": coords[1]})
        else:
            missing.append(record)
    return {
        "points": points, "unmapped": missing,
        "truncated": len(items) > 10000 or len(resources) > 10000,
        "note": "Точки позначають центри населених пунктів, НЕ адреси заявників. Червоний колір — є потреба в евакуації.",
    }


_CATEGORY_RESOURCE = {"food": "food", "medicine": "medicine", "evacuation": "transport", "other": "other"}


def ranked_matches(items, resources, *, max_distance_km=250, local_only=False):
    results = []
    for item in items:
        analysis = ai_service.analyze_item(item)
        target = location(item.city)
        for resource in resources:
            if resource.quantity <= 0 or resource.category != _CATEGORY_RESOURCE.get(item.category):
                continue
            same_city = city_key(resource.city) == city_key(item.city)
            origin = location(resource.city)
            distance = (haversine_km(origin, target) if origin and target else None)
            if local_only and not same_city:
                continue
            if not same_city and (distance is None or distance > max_distance_km):
                continue
            score = (analysis.score + (35 if same_city else max(0, 30 - int(distance / 15)))
                     + min(10, resource.quantity))
            results.append({
                "request_id": item.id, "request_title": item.title,
                "request_city": item.city, "request_category": item.category,
                "priority_tier": analysis.tier, "priority_score": analysis.score,
                "resource_id": resource.id, "resource_name": resource.name,
                "resource_city": resource.city, "resource_quantity": resource.quantity,
                "resource_unit": resource.unit,
                "match_score": score, "same_city": same_city,
                "distance_km_straight": round(distance, 1) if distance is not None else None,
                "can_allocate_now": same_city,
                "note": ("Те саме місто; виділення після взяття заявки в роботу." if same_city
                         else "Інше місто; перевезення треба погодити. Відстань лише по прямій."),
                "from_lat": origin[0] if origin else None,
                "from_lng": origin[1] if origin else None,
                "to_lat": target[0] if target else None,
                "to_lng": target[1] if target else None,
            })
    return sorted(results, key=lambda x: (-x["match_score"], x["request_id"], x["resource_id"]))[:100]


def _eligible_requests(db):
    unreviewed = select(GuestReceipt.request_id).where(GuestReceipt.reviewed_at.is_(None))
    return db.scalars(select(HelpRequest).where(
        HelpRequest.status == RequestStatus.new.value,
        HelpRequest.id.not_in(unreviewed),
    ).order_by(HelpRequest.created_at.desc()).limit(300)).all()


@router.get("/smart-matches")
def smart_matches(user: CurrentUser, db: Db):
    """Smart priority-ranked same-city matches for own stock (no private data)."""
    require(user, Role.volunteer, verified=True)
    resources = db.scalars(select(Resource).where(
        Resource.owner_id == user.id, Resource.quantity > 0
    ).limit(200)).all()
    results = ranked_matches(_eligible_requests(db), resources, local_only=True)
    return {"matches": results, "note": "Рекомендації; остаточне рішення — за волонтером."}


@router.get("/logistics/matches")
def logistics_matches(user: CurrentUser, db: Db):
    require(user, Role.volunteer, Role.admin, verified=True)
    resource_q = select(Resource).where(Resource.quantity > 0)
    if user.role == Role.volunteer.value:
        resource_q = resource_q.where(Resource.owner_id == user.id)
    resources = db.scalars(resource_q.limit(200)).all()
    results = ranked_matches(_eligible_requests(db), resources)
    return {
        "matches": results,
        "note": "Це попередні відповідності, а не готові маршрути. Відстані наведено по прямій; час поїздки не обчислюється.",
    }
