from datetime import datetime
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from services.stock_service import StockService  # noqa: E402
from services import stock_service as stock_service_module  # noqa: E402


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.text = payload if isinstance(payload, str) else ""

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        return FakeResponse(self.payload)


def test_calendar_keeps_all_board_meeting_events():
    service = StockService()
    service._nse_session = FakeSession([
        {
            "bm_symbol": "ALPHA", "bm_date": "08-Sep-2026",
            "bm_purpose": "Financial Results", "bm_desc": "Quarterly financial results",
            "sm_name": "Alpha Limited",
        },
        {
            "bm_symbol": "ALPHA", "bm_date": "08-Sep-2026",
            "bm_purpose": "Board Meeting Intimation", "bm_desc": "To consider unaudited financial results",
            "sm_name": "Alpha Limited",
        },
        {
            "bm_symbol": "BETA", "bm_date": "09-Sep-2026",
            "bm_purpose": "Board Meeting Intimation", "bm_desc": "To consider quarterly results",
            "sm_name": "Beta Limited",
        },
        {
            "bm_symbol": "OTHER", "bm_date": "09-Sep-2026",
            "bm_purpose": "Fund Raising", "bm_desc": "To consider fund raising",
            "sm_name": "Other Limited",
        },
    ])

    calendar = service.results_calendar(datetime(2026, 9, 8, 10, 0))

    assert [item["symbol"] for item in calendar["days"][0]["results"]] == ["ALPHA", "ALPHA"]
    assert [item["symbol"] for item in calendar["days"][1]["results"]] == ["BETA", "OTHER"]
    assert calendar["days"][0]["count"] == 2
    assert calendar["days"][1]["count"] == 2
    assert calendar["days"][1]["results"][1]["purpose"] == "Fund Raising"
    assert calendar["days"][0]["results"][0]["category"] == "results"
    assert calendar["days"][0]["results"][1]["category"] == "results"
    assert calendar["days"][1]["results"][1]["category"] == "other"
    assert service._nse_session.calls[0][1]["from_date"] == "08-09-2026"
    assert service._nse_session.calls[0][1]["to_date"] == "09-09-2026"


def test_calendar_can_fetch_the_next_seven_days_for_the_full_page():
    service = StockService()
    service._nse_session = FakeSession([
        {
            "bm_symbol": "WEEK", "bm_date": "16-Sep-2026",
            "bm_purpose": "Board Meeting", "bm_desc": "Business update",
            "sm_name": "Week Limited",
        },
    ])

    calendar = service.results_calendar(datetime(2026, 9, 8, 10, 0), days=9)

    assert len(calendar["days"]) == 9
    assert calendar["days"][8]["results"][0]["symbol"] == "WEEK"
    assert service._nse_session.calls[0][1]["to_date"] == "16-09-2026"


def test_wishlist_news_uses_exact_nse_instrument_keys(monkeypatch):
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "test-token")
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params, headers, timeout))
        if url == StockService.EQUITY_MASTER_URL:
            return FakeResponse("SYMBOL,NAME OF COMPANY,ISIN NUMBER\nINFY,Infosys Limited,INE009A01021\n")
        return FakeResponse({
            "status": "success",
            "data": {
                "NSE_EQ|INE009A01021": [{
                    "heading": "Infosys announces a new contract",
                    "summary": "A recent company update.",
                    "article_link": "https://upstox.com/news/example/",
                    "published_time": 1788813000000,
                }],
            },
        })

    monkeypatch.setattr(stock_service_module.requests, "get", fake_get)
    service = StockService()
    result = service.wishlist_news([{"symbol": "INFY", "company": "Infosys Limited"}])

    assert result["count"] == 1
    assert result["items"][0]["symbols"] == ["INFY"]
    assert calls[1][1]["instrument_keys"] == "NSE_EQ|INE009A01021"
    assert calls[1][2]["Authorization"] == "Bearer test-token"


def test_wishlist_news_deduplicates_matching_headlines(monkeypatch):
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "test-token")

    def fake_get(url, params=None, headers=None, timeout=None):
        if url == StockService.EQUITY_MASTER_URL:
            return FakeResponse("SYMBOL,NAME OF COMPANY,ISIN NUMBER\nINFY,Infosys Limited,INE009A01021\n")
        return FakeResponse({
            "status": "success",
            "data": {
                "NSE_EQ|INE009A01021": [
                    {
                        "heading": "Infosys announces a new contract",
                        "article_link": "https://example.com/story?source=one",
                        "published_time": 1788813000000,
                    },
                    {
                        "heading": "Infosys announces a new contract!",
                        "article_link": "https://example.com/story?source=two",
                        "published_time": 1788813000000,
                    },
                ],
            },
        })

    monkeypatch.setattr(stock_service_module.requests, "get", fake_get)
    result = StockService().wishlist_news([{"symbol": "INFY", "company": "Infosys Limited"}])

    assert result["count"] == 1


def test_upcoming_stock_events_are_filtered_to_requested_symbol():
    service = StockService()
    service._nse_session = FakeSession([
        {
            "bm_symbol": "INFY", "bm_date": "18-Sep-2026",
            "bm_purpose": "Financial Results", "bm_desc": "Quarterly results",
        },
        {
            "bm_symbol": "OTHER", "bm_date": "18-Sep-2026",
            "bm_purpose": "Fund Raising", "bm_desc": "Other company",
        },
    ])

    result = service.upcoming_stock_events("INFY", now=datetime(2026, 9, 9, 10, 0))

    assert result["count"] == 1
    assert result["items"][0]["purpose"] == "Financial Results"
    assert service._nse_session.calls[0][1]["symbol"] == "INFY"
    assert service._nse_session.calls[0][1]["to_date"] == "09-10-2026"
