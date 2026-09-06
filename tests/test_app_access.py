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
        return {
            "symbol": symbol,
            "pe_ratio": 24.5,
            "profit_margin": 18.2,
            "analysis": {"available": True, "score": 82},
        }

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

    def get_plan(self, _user_id):
        return {"stock_limit": 10}

    def get_usage(self, _user_id, _plan=None):
        return {
            "stocks_remaining": 10,
            "used_symbols": [],
        }

    def list_wishlist(self, _user_id):
        return [{"symbol": "INFY", "company": "Infosys", "created_at": "2026-09-07T10:00:00+05:30"}]

    def save_to_wishlist(self, _user_id, symbol, company):
        return {"symbol": symbol, "company": company, "created_at": "2026-09-07T10:00:00+05:30"}

    def remove_from_wishlist(self, _user_id, _symbol):
        return None


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


def test_interpretation_endpoint_records_usage(client):
    browser, stock, plan = client
    response = browser.get("/interpretation/HDFCBANK")
    assert response.status_code == 200
    assert stock.fetch_calls == 1
    assert plan.recorded == ["HDFCBANK"]


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


def test_advanced_screener_is_free_and_returns_filter_metrics(client):
    browser, stock, plan = client
    plan.allowed = False
    response = browser.get("/screener-data/nifty-50/RELIANCE")
    assert response.status_code == 200
    assert response.get_json()["metrics"] == {
        "earnings_quality_score": 82,
        "pe_ratio": 24.5,
        "profit_margin": 18.2,
    }
    assert stock.fetch_calls == 1
    assert plan.recorded == []


def test_advanced_screener_rejects_stock_outside_selected_index(client):
    browser, stock, plan = client
    response = browser.get("/screener-data/nifty-50/PIDILITIND")
    assert response.status_code == 404
    assert stock.fetch_calls == 0
    assert plan.recorded == []


def test_wishlist_actions_do_not_consume_stock_quota(client):
    browser, stock, plan = client
    saved = browser.post("/api/wishlist", json={"symbol": "INFY", "company": "Infosys"})
    listed = browser.get("/api/wishlist")
    removed = browser.delete("/api/wishlist/INFY")
    assert saved.status_code == 201
    assert listed.status_code == 200
    assert listed.get_json()["items"][0]["symbol"] == "INFY"
    assert removed.status_code == 200
    assert stock.fetch_calls == 0
    assert plan.recorded == []


def test_robots_txt_allows_home_and_advertises_sitemap(client):
    browser, _stock, _plan = client
    response = browser.get("/robots.txt")
    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert "Allow: /" in response.text
    assert "Disallow: /api/" in response.text
    assert "Sitemap: https://resultlens.in/sitemap.xml" in response.text


def test_sitemap_contains_canonical_homepage(client):
    browser, _stock, _plan = client
    response = browser.get("/sitemap.xml")
    assert response.status_code == 200
    assert response.mimetype == "application/xml"
    assert "<loc>https://resultlens.in/</loc>" in response.text
    assert "<loc>https://resultlens.in/methodology</loc>" in response.text
    assert "<loc>https://resultlens.in/privacy</loc>" in response.text
    assert "<loc>https://resultlens.in/terms</loc>" in response.text
    assert "<loc>https://resultlens.in/refund-policy</loc>" in response.text
    assert "<loc>https://resultlens.in/about</loc>" in response.text
    assert "<loc>https://resultlens.in/stocks</loc>" in response.text
    assert "<loc>https://resultlens.in/stocks/reliance</loc>" in response.text
    assert "<loc>https://resultlens.in/stocks/pidilitind</loc>" in response.text


def test_public_stock_pages_are_indexable_without_fetching_financial_data(client):
    browser, stock, plan = client
    directory = browser.get("/stocks")
    assert directory.status_code == 200
    assert 'href="/stocks/reliance"' in directory.text
    assert stock.fetch_calls == 0
    assert plan.recorded == []

    page = browser.get("/stocks/reliance")
    assert page.status_code == 200
    assert "Reliance Industries Quarterly Results Analysis" in page.text
    assert '<link rel="canonical" href="https://resultlens.in/stocks/reliance">' in page.text
    assert 'href="/?query=RELIANCE"' in page.text
    assert stock.fetch_calls == 0
    assert plan.recorded == []


def test_unknown_public_stock_page_returns_not_found(client):
    browser, stock, plan = client
    response = browser.get("/stocks/not-a-stock")
    assert response.status_code == 404
    assert stock.fetch_calls == 0
    assert plan.recorded == []


def test_homepage_exposes_canonical_search_metadata(client):
    browser, _stock, _plan = client
    response = browser.get("/")
    assert response.status_code == 200
    assert '<link rel="canonical" href="https://resultlens.in/">' in response.text
    assert '<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">' in response.text
    assert '<meta name="description"' in response.text
    assert '"@type": "WebApplication"' in response.text
    assert 'mailto:resultlens.support@gmail.com' in response.text
    assert 'href="/refund-policy"' in response.text
    assert 'href="/stocks"' in response.text
    assert "By purchasing, you agree to our" in response.text


def test_methodology_page_is_public_and_canonical(client):
    browser, _stock, _plan = client
    response = browser.get("/methodology")
    assert response.status_code == 200
    assert '<link rel="canonical" href="https://resultlens.in/methodology">' in response.text
    assert "Operating-company model" in response.text
    assert "Bank and lender model" in response.text


def test_privacy_page_is_public_and_identifies_contact(client):
    browser, _stock, _plan = client
    response = browser.get("/privacy")
    assert response.status_code == 200
    assert '<link rel="canonical" href="https://resultlens.in/privacy">' in response.text
    assert "G-RY1QK5K7ZT" in response.text
    assert "Optional Google Analytics" in response.text
    assert "resultlens.support@gmail.com" in response.text
    assert "Razorpay processes payment credentials" in response.text


@pytest.mark.parametrize(
    ("path", "canonical", "expected_text"),
    [
        ("/terms", "https://resultlens.in/terms", "Financial-information disclaimer"),
        ("/refund-policy", "https://resultlens.in/refund-policy", "one-time purchases valid for 30 days"),
    ],
)
def test_commercial_policy_pages_are_public(client, path, canonical, expected_text):
    browser, _stock, _plan = client
    response = browser.get(path)
    assert response.status_code == 200
    assert f'<link rel="canonical" href="{canonical}">' in response.text
    assert expected_text in response.text


def test_about_page_identifies_product_operator(client):
    browser, _stock, _plan = client
    response = browser.get("/about")
    assert response.status_code == 200
    assert '<link rel="canonical" href="https://resultlens.in/about">' in response.text
    assert "Raj Gopalachari" in response.text
    assert "resultlens.support@gmail.com" in response.text
