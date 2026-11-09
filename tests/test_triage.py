"""Тести аналізу заявок (без мережі та БД)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_service as ai


def run(desc, cat="food", urgency=3, people=1, vulnerable=False):
    return ai.analyze(description=desc, category=cat, urgency=urgency,
                      people_count=people, vulnerable=vulnerable)


def test_can_wait_only_without_signals():
    assert run("Потрібні продукти", urgency=1).tier == ai.CAN_WAIT
    assert run("Потрібен інсулін", cat="medicine", urgency=1).tier == ai.CRITICAL


def test_never_lowers_self_reported_urgency():
    assert run("Усе добре", urgency=4).tier in (ai.HIGH, ai.CRITICAL)


def test_critical_keywords_ru_and_uk():
    assert run("Нужна эвакуация, обстрел", cat="evacuation", urgency=3).tier == ai.CRITICAL
    assert run("Сильна кровотеча", cat="medicine", urgency=2).tier == ai.CRITICAL


def test_vulnerable_raises_tier():
    assert run("Потрібна вода", urgency=3, vulnerable=True).tier == ai.HIGH


def test_reasons_always_present():
    assert run("Потрібна допомога").reasons
