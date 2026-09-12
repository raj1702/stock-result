from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from services import stock_service as stock_service_module  # noqa: E402
from services.stock_service import StockService  # noqa: E402


def test_microcap_uses_full_published_list_and_an_independent_cache(monkeypatch):
    # The published list can temporarily exceed the number in the index name.
    header = "Company Name,Industry,Symbol,Series,ISIN Code\n"
    microcap_csv = header + "\n".join(
        f"Company {index:03},Services,MICRO{index:03},EQ,ISIN{index:03}"
        for index in reversed(range(254))
    ) + "\nCompany 000,Services,MICRO000,EQ,ISIN000\nBlank,Services,,EQ,\n"
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(url)
        payload = microcap_csv if url == StockService.MICROCAP_250_CONSTITUENTS_URL else (
            header + "Smallcap Company,Services,SMALL,EQ,ISIN\n"
        )
        return SimpleNamespace(text=payload, raise_for_status=lambda: None)

    monkeypatch.setattr(stock_service_module.requests, "get", fake_get)
    service = StockService()
    stocks = service.microcap_250_constituents()
    assert len(stocks) == 254
    assert stocks[0] == {"symbol": "MICRO000", "company": "Company 000"}
    assert stocks[-1]["symbol"] == "MICRO253"
    stocks[0]["company"] = "Changed by caller"
    assert service.microcap_250_constituents()[0]["company"] == "Company 000"
    assert len(calls) == 1
    assert service.smallcap_250_constituents()[0]["symbol"] == "SMALL"
    assert len(service.microcap_250_constituents()) == 254
    assert len(calls) == 2

    _, cached_stocks = service._index_constituents_cache["microcap-250"]
    service._index_constituents_cache["microcap-250"] = (
        datetime.utcnow() - timedelta(hours=2), cached_stocks,
    )
    assert len(service.microcap_250_constituents()) == 254
    assert calls.count(StockService.MICROCAP_250_CONSTITUENTS_URL) == 2
