import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from services.earnings_truth import build_earnings_truth


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
