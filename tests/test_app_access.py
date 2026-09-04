import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import app as app_module  # noqa: E402


class FakeStockService:
    def __init__(self):
        self.fetch_calls = 0

    def resolve_symbol(self, query):
        return query.upper() if query else None

    def fetch_stock_data(self, symbol):
        self.fetch_calls += 1
        return {"symbol": symbol, "analysis": {"available": True}}

    def generate_interpretation(self, _symbol, _data):
        return [{"text": "Healthy"}]

    def nifty_50_constituents(self):
        return [{"symbol": "RELIANCE", "company": "Reliance Industries"}]

    def nifty_next_50_constituents(self):
        return [{"symbol": "PIDILITIND", "company": "Pidilite Industries"}]


class FakePlanService:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.recorded = []
        self.qualified = []

    def can_access_stock(self, _user_id, _symbol):
        return {
            "allowed": self.allowed,
            "usage_month": "2026-09",
            "stocks_used": 10 if not self.allowed else 0,
            "stocks_remaining": 0 if not self.allowed else 10,
            "used_symbols": [],
        }

    def record_stock_usage(self, _user_id, symbol):
        self.recorded.append(symbol)
        return {
            "allowed": self.allowed,
            "usage_month": "2026-09",
            "stocks_used": 1,
            "stocks_remaining": 9,
            "used_symbols": [symbol],
        }

    def qualify_referral(self, user_id):
        self.qualified.append(user_id)

    def get_plan(self, _user_id):
        return {"stock_limit": 10}

    def get_usage(self, _user_id, _plan=None):
        return {
            "stocks_remaining": 10,
            "used_symbols": [],
        }


@pytest.fixture
def client(monkeypatch):
    stock = FakeStockService()
    plan = FakePlanService()
    monkeypatch.setattr(app_module, "stock_service", stock)
    monkeypatch.setattr(app_module, "plan_service", plan)
    app_module.app.config.update(TESTING=True, SECRET_KEY="test-secret")
    client = app_module.app.test_client()
    with client.session_transaction() as session:
        session["user"] = {"sub": "user-1", "email": "user@example.com"}
    return client, stock, plan


def test_direct_stock_endpoint_cannot_bypass_limit(client):
    browser, stock, plan = client
    plan.allowed = False
    response = browser.get("/stock/RELIANCE")
    assert response.status_code == 403
    assert response.get_json()["plan_limit_reached"] is True
    assert stock.fetch_calls == 0


def test_interpretation_endpoint_records_usage_and_qualifies_referral(client):
    browser, stock, plan = client
    response = browser.get("/interpretation/HDFCBANK")
    assert response.status_code == 200
    assert stock.fetch_calls == 1
    assert plan.recorded == ["HDFCBANK"]
    assert plan.qualified == ["user-1"]


def test_search_access_checks_quota_before_cached_result(client):
    browser, _stock, plan = client
    plan.allowed = False
    response = browser.get("/search-access?query=INFY")
    assert response.status_code == 403
    assert plan.recorded == []


def test_screening_result_does_not_consume_quota(client):
    browser, stock, plan = client
    response = browser.get("/screening/nifty-50/RELIANCE")
    assert response.status_code == 200
    assert stock.fetch_calls == 1
    assert plan.recorded == []
    assert plan.qualified == []
