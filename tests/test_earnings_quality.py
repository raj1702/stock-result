from services.stock_service import StockService


def quarterly_results(rows):
    return {
        "quarters": [{"label": label} for label in ("Q1", "Q2", "Q3", "Q4")],
        "rows": [{"key": key, "values": values} for key, values in rows.items()],
        "comparisons": {"yoy": []},
    }


def test_aligned_continuous_recovery_is_not_penalized_for_negative_values():
    data = quarterly_results({
        "revenue": [100, 110, 120, 130],
        "profit": [-100, -80, -55, -25],
        "profit_margin": [-100, -73, -46, -19],
        "operating_profit": [-90, -65, -40, -15],
        "operating_margin": [-90, -59, -33, -12],
    })

    analysis = StockService()._earnings_quality_analysis(data)

    assert analysis["score"] == 100
    assert all(component["earned"] == component["weight"] for component in analysis["components"])
    assert any("broad and uninterrupted" in insight for insight in analysis["insights"])


def test_each_continuously_improving_component_gets_full_credit_independently():
    data = quarterly_results({
        "revenue": [100, 110, 105, 130],
        "profit": [-100, -80, -55, -25],
        "profit_margin": [-100, -73, -46, -19],
        "operating_profit": [-90, -65, -40, -15],
        "operating_margin": [-90, -59, -33, -12],
    })

    analysis = StockService()._earnings_quality_analysis(data)

    assert analysis["score"] < 100
    components = {component["key"]: component for component in analysis["components"]}
    assert components["profit_margin"]["earned"] == 20
    assert components["operating_margin"]["earned"] == 15
    assert components["revenue"]["earned"] < components["revenue"]["weight"]
