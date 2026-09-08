"""Deterministic explanations of reported quarterly results."""

import math
from typing import Any, Callable, Mapping


def build_earnings_truth(data: Mapping[str, Any], *, is_lender: bool,
                         number: Callable[[Any], float]) -> dict:
    rows = {row.get("key"): row for row in data.get("rows", [])
            if isinstance(row, Mapping) and row.get("key")}
    comparisons = data.get("comparisons", {}).get("yoy", [])
    if not data.get("quarters") or not rows:
        return {"available": False}

    def yoy(key):
        row = next((item for item in comparisons if item.get("key") == key), {})
        values = row.get("values", [])
        value = values[-1] if values else None
        return number(value) if value is not None and math.isfinite(number(value)) else None

    primary_key = "net_interest_income" if is_lender else "revenue"
    operating_key = "loan_book" if is_lender else "operating_profit"
    primary_label = "NII" if is_lender else "Revenue"
    operating_label = "Loan book" if is_lender else "Operating profit"
    pat, primary = yoy("profit"), yoy(primary_key)
    operating, margin = yoy(operating_key), yoy("profit_margin")
    operating_margin = yoy("operating_margin")
    profit_values = [number(value) for value in rows.get("profit", {}).get("values", [])
                     if value is not None and math.isfinite(number(value))]
    latest_pat = profit_values[-1] if profit_values else None
    profit_transitions = list(zip(profit_values, profit_values[1:]))
    sustained_loss_recovery = (
        latest_pat is not None and latest_pat < 0
        and len(profit_transitions) >= 2
        and all(current > previous for previous, current in profit_transitions)
    )
    pattern, tone = "Mixed earnings signals", "neutral"
    summary = "Available metrics do not yet show one dominant earnings driver."
    drivers, watchpoints = [], []
    if latest_pat is not None and latest_pat < 0:
        narrowing = len(profit_values) > 1 and profit_values[-1] > profit_values[-2]
        if narrowing:
            pattern, tone = "Consistent loss recovery" if sustained_loss_recovery else "Loss recovery in progress", "caution"
            summary = (
                "The company remains loss-making, but PAT has improved continuously across the reported periods."
                if sustained_loss_recovery else
                "The company remains loss-making, although its latest loss narrowed."
            )
            drivers.append(
                "PAT is on a sustained recovery path, although positive earnings are not yet established."
                if sustained_loss_recovery else
                "PAT improved sequentially, but positive earnings are not established."
            )
            watchpoints.append("Check whether PAT turns positive without sacrificing revenue momentum.")
        else:
            pattern, tone = "Loss pressure", "negative"
            summary = "The latest period remains loss-making without a clear sequential recovery."
            drivers.append("Negative PAT limits the reliability of growth conclusions.")
            watchpoints.append("Look for sustained loss reduction and a path to positive margins.")
    elif pat is not None and primary is not None:
        gap = pat - primary
        if pat > 0 and primary > 0 and abs(gap) <= 15:
            pattern, tone = "Broad-based earnings growth", "positive"
            summary = f"{primary_label} and PAT grew together, providing broad support."
            drivers.append(f"{primary_label} grew {primary:.2f}% and PAT grew {pat:.2f}% YoY.")
            watchpoints.append(f"Check whether {primary_label} and PAT remain aligned next quarter.")
        elif pat > 0 and (primary <= 0 or gap > 15) and any(
                value is not None and value > 0 for value in (margin, operating_margin)):
            pattern, tone = "Margin-led profit growth", "caution"
            summary = f"PAT materially outpaced {primary_label.lower()} and appears margin-supported."
            drivers.append(f"PAT grew {pat:.2f}% versus {primary_label} growth of {primary:.2f}% YoY.")
            margin_parts = []
            if margin is not None and margin > 0:
                margin_parts.append(f"profit margin improved {margin:.2f}%")
            if operating_margin is not None and operating_margin > 0:
                margin_parts.append(f"operating margin improved {operating_margin:.2f}%")
            drivers.append(f"{' and '.join(margin_parts).capitalize()} YoY, indicating that margin expansion contributed to PAT growth.")
            watchpoints.append(f"Look for stronger {primary_label.lower()} growth with stable margins.")
        elif pat > 0 and (primary <= 0 or gap > 15):
            pattern, tone = "Profit growth lacks topline support", "caution"
            summary = f"PAT outpaced {primary_label.lower()} without clear margin support."
            drivers.append("Tax, other income or exceptional items may have influenced PAT.")
            watchpoints.append("Check whether operating performance catches up with PAT.")
        elif primary > 0 and pat < 0:
            pattern, tone = "Growth with profitability pressure", "negative"
            summary = f"{primary_label} grew, but PAT declined, weakening earnings conversion."
            drivers.append(f"{primary_label} grew {primary:.2f}% while PAT changed {pat:.2f}% YoY.")
            watchpoints.append("Check costs, provisions and margins for the source of pressure.")
        elif primary < 0 and pat < 0:
            pattern, tone = "Broad earnings contraction", "negative"
            summary = f"Both {primary_label.lower()} and PAT declined in the latest comparison."

    if operating is not None and primary is not None:
        if operating < primary - 10:
            drivers.append(f"{operating_label} trailed {primary_label}, weakening conversion.")
            watchpoints.append(f"Monitor whether {operating_label.lower()} catches up.")
        elif operating > 0:
            drivers.append(f"{operating_label} growth supports the reported trend.")

    if margin is not None and pattern != "Margin-led profit growth":
        if margin > 0:
            drivers.append(f"Profit margin improved {margin:.2f}% YoY, strengthening earnings conversion.")
        elif margin < 0:
            drivers.append(f"Profit margin declined {abs(margin):.2f}% YoY, weakening earnings conversion.")
            watchpoints.append("Check whether profit margin stabilises in the next result.")
    if not drivers and any(value is not None for value in (primary, pat, operating, margin)):
        drivers.append("The available YoY metrics are mixed and do not confirm a single durable trend.")

    named_values = ((primary_label, primary), ("PAT", pat),
                    (operating_label, operating), ("Profit margin", margin))
    evidence = [{"label": label, "value": f"{value:+.2f}% YoY",
                 "tone": "positive" if value > 0 else "negative" if value < 0 else "neutral"}
                for label, value in named_values if value is not None]
    confidence_score = round(len(evidence) / 4 * 100)
    sustainability = 50
    for value, points in ((primary, 15), (pat, 15), (operating, 10), (margin, 10)):
        sustainability += points if value is not None and value > 0 else -points if value is not None and value < 0 else 0
    if pattern in ("Margin-led profit growth", "Profit growth lacks topline support"):
        sustainability -= 15
    if latest_pat is not None and latest_pat < 0:
        # Sustained multi-period recovery is materially stronger than a single
        # narrowing quarter, while still retaining a discount until PAT is positive.
        sustainability = min(sustainability, 70 if sustained_loss_recovery else 40)
    sustainability = max(0, min(100, sustainability))
    missing = [label for label, value in named_values if value is None]
    sustainability_reason = (
        "Revenue or NII, PAT, operating performance and margin direction are combined; "
        "unsupported or margin-led PAT growth is discounted."
    )
    confidence_reason = (
        "All core YoY signals are available."
        if not missing else f"Unavailable latest YoY evidence: {', '.join(missing)}."
    )
    return {
        "available": bool(evidence), "pattern": pattern, "tone": tone,
        "summary": summary, "drivers": drivers[:5], "evidence": evidence,
        "sustainability": {"score": sustainability, "rating": _rating(sustainability),
                           "explanation": sustainability_reason},
        "confidence": {"score": confidence_score, "rating": _confidence(confidence_score),
                       "explanation": confidence_reason},
        "watchpoints": list(dict.fromkeys(watchpoints))[:3],
    }


def _rating(score):
    return "Strong" if score >= 75 else "Moderate" if score >= 50 else "Fragile"


def _confidence(score):
    return "High" if score >= 75 else "Moderate" if score >= 50 else "Limited"
