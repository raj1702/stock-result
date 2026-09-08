import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.earnings_truth import build_earnings_truth
from services.stock_service import StockService


def number(value):
    return float(value)


def result(revenue, pat, margin, operating=12):
    keys = {"revenue": revenue, "profit": pat, "profit_margin": margin,
            "operating_profit": operating, "operating_margin": margin}
    return {
        "quarters": [{"label": f"Q{i} 2026"} for i in range(1, 6)],
        "rows": [{"key": key, "values": [10, 11, 12, 13, 14]}
                 for key in keys],
        "comparisons": {"yoy": [{"key": key, "values": [None] * 4 + [value]}
                                  for key, value in keys.items()]},
    }


def test_detects_broad_based_growth():
    truth = build_earnings_truth(result(18, 25, 3), is_lender=False, number=number)
    assert truth["pattern"] == "Broad-based earnings growth"
    assert truth["sustainability"]["rating"] == "Strong"
    assert truth["confidence"]["score"] == 100


def test_detects_margin_led_growth():
    truth = build_earnings_truth(result(2, 35, 8), is_lender=False, number=number)
    assert truth["pattern"] == "Margin-led profit growth"
    assert truth["tone"] == "caution"


def test_sustained_loss_recovery_is_not_treated_like_persistent_loss():
    data = result(18, 25, 3)
    next(row for row in data["rows"] if row["key"] == "profit")["values"] = [-100, -80, -55, -30, -10]

    truth = build_earnings_truth(data, is_lender=False, number=number)

    assert truth["pattern"] == "Consistent loss recovery"
    assert truth["sustainability"]["score"] == 70
    assert "continuously" in truth["summary"]


def test_negative_but_continuously_improving_margins_receive_recovery_credit():
    series = {
        "revenue": [100, 110, 120, 130, 140],
        "profit": [-100, -80, -55, -30, -10],
        "profit_margin": [-100, -72, -46, -23, -7],
        "operating_profit": [-90, -65, -42, -20, -5],
        "operating_margin": [-90, -59, -35, -15, -4],
    }
    quarterly = {
        "quarters": [{"label": f"Q{i} 2026"} for i in range(1, 6)],
        "rows": [{"key": key, "values": values} for key, values in series.items()],
        "comparisons": {"yoy": []},
    }

    analysis = StockService()._earnings_quality_analysis(quarterly)
    components = {item["key"]: item for item in analysis["components"]}

    assert analysis["rating"] == "Strong"
    assert components["profit"]["earned"] == 25
    assert components["profit_margin"]["earned"] == 17
    assert components["operating_margin"]["earned"] == 12.8
    assert "continuous improvement" in components["profit_margin"]["explanation"]
