"""Safe offline checks for integration entrypoints."""
import os
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ai_service
import telegram_bot

def test_telegram_link_contains_request_anchor(monkeypatch):
    monkeypatch.setattr(telegram_bot, "SITE_URL", "https://example.org/")
    assert telegram_bot.request_link(42) == "https://example.org/#request-42"

def test_gemini_without_key_falls_back(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    item = SimpleNamespace(description="Потрібна допомога", category="food", urgency=3,
                           people_count=1, vulnerable_people=False, created_at=None)
    result = ai_service.analyze_item(item)
    assert result.source == "rules"
    assert result.tier in ai_service.TIER_ORDER
