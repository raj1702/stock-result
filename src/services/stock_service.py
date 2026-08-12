"""NSE-backed stock-data service."""

import logging
import csv
import os
from difflib import SequenceMatcher
from io import StringIO
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Mapping, Optional
from xml.etree import ElementTree

import requests
from jugaad_data.nse import NSELive

from models import StockData


logger = logging.getLogger(__name__)


class StockService:
    """Fetch quote and financial metrics from NSE's current public filings."""

    XBRL_HEADERS = {
        "Accept": "application/xml,text/xml,text/html,*/*",
        "Referer": "https://www.nseindia.com/",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    EQUITY_MASTER_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
    UPSTOX_BASE_URL = "https://api.upstox.com/v2/fundamentals"
    FUNDAMENTALS_CACHE_TTL = timedelta(hours=24)
    # BSE-only securities that a user may reasonably search by their commonly
    # known ticker. Upstox Fundamentals is ISIN-based and supports this path.
    BSE_ONLY_ISINS = {"NSDL": "INE301O01023"}

    def __init__(self, api_client=None):
        # NSELive connects during construction, so defer it until a request is
        # made; Flask can then start even during an NSE outage.
        self.nse_live = api_client
        # Cache successful Upstox responses for a day. This prevents repeated
        # searches from consuming API quota while the Flask process is running.
        self._fundamentals_cache = {}
        self._fundamentals_cache_lock = Lock()

    def fetch_stock_data(self, symbol: str) -> StockData:
        """Return NSE quote, valuation, margin, revenue-growth, and profit-growth metrics."""
        nse_symbol = self._normalise_symbol(symbol)
        known_isin = self.BSE_ONLY_ISINS.get(nse_symbol)
        fallback = {}
        try:
            quote = self._get_quote(nse_symbol)
            pe_ratio = self._number(
                quote.get("secInfo", {}).get("pdSymbolPe")
                or quote.get("metadata", {}).get("pdSymbolPe")
            )
        except Exception as exc:
            logger.warning("Unable to fetch NSE quote for %s: %s", nse_symbol, exc)
            if not known_isin:
                return self._empty_data(nse_symbol)
            # NSDL is BSE-only, so there is no NSE quote to read. Continue to
            # the ISIN-based Upstox Fundamentals fallback instead.
            quote = {}
            pe_ratio = 0.0

        try:
            (
                latest,
                previous,
                ocf_latest,
                ocf_previous,
                fcf_latest,
                fcf_previous,
                net_debt_latest,
                net_debt_previous,
                loan_book_latest,
                loan_book_previous,
                interest_income_latest,
                interest_income_previous,
            ) = self._financial_filing_metrics(nse_symbol)
            # Some NSE symbols return an empty filing payload instead of an
            # error. Treat that as unavailable data so the AI fallback runs.
            if not latest.get("revenue") or not previous.get("revenue"):
                raise RuntimeError("NSE filing has no comparable revenue data")
        except Exception as exc:
            logger.warning("Unable to fetch NSE financial filings for %s: %s", nse_symbol, exc)
            # The quote response normally includes the exact ISIN. Prefer it
            # over downloading a separate NSE security-master CSV, which can
            # be intermittently unavailable or delayed after a listing.
            quote_isin = str(quote.get("metadata", {}).get("isin", "")).strip()
            fallback = self._upstox_fundamentals(
                nse_symbol, quote_isin or known_isin or None
            )
            latest, previous = fallback.get("latest", {}), fallback.get("previous", {})
            ocf_latest, ocf_previous = fallback.get("ocf_latest", {}), fallback.get("ocf_previous", {})
            fcf_latest, fcf_previous = fallback.get("fcf_latest", {}), fallback.get("fcf_previous", {})
            net_debt_latest = fallback.get("net_debt_latest", {})
            net_debt_previous = fallback.get("net_debt_previous", {})
            loan_book_latest, loan_book_previous = {}, {}
            interest_income_latest, interest_income_previous = {}, {}
            pe_ratio = self._number(fallback.get("pe_ratio")) or pe_ratio

        current_revenue = self._number(latest.get("revenue"))
        current_profit = self._number(latest.get("profit"))
        previous_revenue = self._number(previous.get("revenue"))
        previous_profit = self._number(previous.get("profit"))
        profit_margin = (current_profit / current_revenue) * 100 if current_revenue else 0.0
        previous_profit_margin = (
            (previous_profit / previous_revenue) * 100 if previous_revenue else 0.0
        )
        profit_margin_yoy_change = profit_margin - previous_profit_margin
        yoy_revenue = self._growth(current_revenue, previous_revenue)
        yoy_profit = self._growth(current_profit, previous_profit)
        operating_cash_flow_yoy = self._growth(
            ocf_latest.get("operating_cash_flow"),
            ocf_previous.get("operating_cash_flow"),
        )
        free_cash_flow_yoy = self._growth(
            fcf_latest.get("free_cash_flow"), fcf_previous.get("free_cash_flow")
        )
        net_debt_yoy = self._growth(
            net_debt_latest.get("net_debt"), net_debt_previous.get("net_debt")
        )
        total_borrowings = self._number(net_debt_latest.get("total_borrowings"))
        equity = self._number(net_debt_latest.get("equity"))
        debt_to_equity = total_borrowings / equity if equity else 0.0
        metrics = {
            "symbol": nse_symbol,
            "pe_ratio": round(pe_ratio, 2),
            "profit_margin": round(profit_margin, 2),
            "profit_margin_yoy_change": round(profit_margin_yoy_change, 2),
            "yoy_revenue": round(yoy_revenue, 2),
            "yoy_profit": round(yoy_profit, 2),
            "operating_cash_flow_yoy": round(operating_cash_flow_yoy, 2),
            "free_cash_flow_yoy": round(free_cash_flow_yoy, 2),
            "net_debt_yoy": round(net_debt_yoy, 2),
            "debt_to_equity": round(debt_to_equity, 2),
            "data_note": "Source of truth: NSE corporate financial filings.",
        }
        if fallback.get("latest", {}).get("upstox_generated"):
            # Do not represent line items that Upstox did not supply as zero.
            # Zero is a meaningful financial value, whereas these are unknown.
            if not fcf_latest or not fcf_previous:
                metrics.pop("free_cash_flow_yoy")
            if not net_debt_latest or not net_debt_previous:
                metrics.pop("net_debt_yoy")
                metrics.pop("debt_to_equity")
            metrics["data_note"] = "Source of truth: Upstox Fundamentals."
        elif fallback.get("error"):
            metrics["data_note"] = (
                "Source of truth: NSE quote only — comparable NSE financial filings "
                f"and the Upstox fallback were unavailable: {fallback['error']}"
            )
        if self._is_financial_company(quote):
            metrics.pop("net_debt_yoy", None)
            metrics.pop("debt_to_equity", None)
        if self._is_lender(quote):
            metrics["loan_book_yoy"] = round(
                self._growth(
                    loan_book_latest.get("loan_book"), loan_book_previous.get("loan_book")
                ),
                2,
            )
            metrics["interest_income_yoy"] = round(
                self._growth(
                    interest_income_latest.get("interest_income"),
                    interest_income_previous.get("interest_income"),
                ),
                2,
            )
        return metrics

    def _upstox_fundamentals(self, symbol: str, isin: Optional[str] = None) -> dict:
        """Fetch deterministic raw fundamentals from Upstox and calculate locally."""
        cache_key = f"upstox:{symbol}"
        with self._fundamentals_cache_lock:
            cached = self._fundamentals_cache.get(cache_key)
            if cached and datetime.utcnow() - cached[0] < self.FUNDAMENTALS_CACHE_TTL:
                return self._copy_fallback(cached[1])
        token = os.getenv("UPSTOX_ACCESS_TOKEN")
        if not token:
            return {"error": "UPSTOX_ACCESS_TOKEN is not set"}
        try:
            isin = isin or self._isin_for_symbol(symbol)
            if not isin:
                return {"error": f"No ISIN found for {symbol}"}
            income = self._upstox_get(isin, "income-statement", {
                "type": "consolidated", "time_period": "quarterly"
            })
            cash_flow = self._upstox_get(isin, "cash-flow", {
                "type": "consolidated", "fs": "true"
            })
            balance_sheet = self._upstox_get(isin, "balance-sheet", {
                "type": "consolidated", "fs": "true"
            })
            ratios = self._upstox_get(isin, "key-ratios")

            income_data = income["data"]
            revenue = self._upstox_category_pair(income_data.get("income_statement", []), "revenue")
            profit = self._upstox_category_pair(income_data.get("income_statement", []), "net_profit")
            statement_period = "quarterly"
            # Some companies have fewer than five quarterly observations in
            # this API. Without the same quarter from the prior year, use the
            # annual statement instead of incorrectly substituting QoQ data.
            if not revenue or not profit:
                income = self._upstox_get(isin, "income-statement", {
                    "type": "consolidated", "time_period": "yearly"
                })
                income_data = income["data"]
                revenue = self._upstox_category_pair(
                    income_data.get("income_statement", []), "revenue"
                )
                profit = self._upstox_category_pair(
                    income_data.get("income_statement", []), "net_profit"
                )
                statement_period = "annual"
            operating_cash_flow = self._upstox_category_pair(
                cash_flow["data"].get("cash_flow", []), "operating"
            )
            free_cash_flow = self._upstox_line_pair(
                cash_flow["data"].get("full_statement", []), ("free cash flow",)
            )
            borrowings = self._upstox_line_pair(
                balance_sheet["data"].get("full_statement", []), ("borrowings", "total debt")
            )
            cash = self._upstox_line_pair(
                balance_sheet["data"].get("full_statement", []), ("cash and cash equivalents", "cash & cash equivalents")
            )
            equity = self._upstox_line_pair(
                balance_sheet["data"].get("full_statement", []), ("total equity", "shareholders equity")
            ) or self._upstox_balance_equity_pair(balance_sheet["data"].get("history", []))
            pe_ratio = next(
                (self._number(item.get("company_value")) for item in ratios.get("data", [])
                 if str(item.get("name", "")).upper() == "P/E"),
                0.0,
            )
            if not revenue or not profit:
                return {"error": "Upstox returned no comparable revenue/profit periods"}
            result = {
                "latest": {
                    "revenue": revenue[0],
                    "profit": profit[0],
                    "upstox_generated": True,
                    "statement_period": statement_period,
                },
                "previous": {"revenue": revenue[1], "profit": profit[1]},
                "ocf_latest": {"operating_cash_flow": operating_cash_flow[0]} if operating_cash_flow else {},
                "ocf_previous": {"operating_cash_flow": operating_cash_flow[1]} if operating_cash_flow else {},
                "fcf_latest": {"free_cash_flow": free_cash_flow[0]} if free_cash_flow else {},
                "fcf_previous": {"free_cash_flow": free_cash_flow[1]} if free_cash_flow else {},
                "net_debt_latest": {
                    "net_debt": borrowings[0] - cash[0],
                    "total_borrowings": borrowings[0],
                    "equity": equity[0] if equity else 0.0,
                } if borrowings and cash else {},
                "net_debt_previous": {
                    "net_debt": borrowings[1] - cash[1]
                } if borrowings and cash else {},
                "pe_ratio": pe_ratio,
            }
            with self._fundamentals_cache_lock:
                self._fundamentals_cache[cache_key] = (datetime.utcnow(), result)
            return self._copy_fallback(result)
        except (requests.RequestException, KeyError, TypeError, ValueError) as exc:
            logger.warning("Upstox fundamentals failed for %s: %s", symbol, exc)
            return {"error": str(exc)[:180]}

    def _upstox_get(self, isin: str, endpoint: str, params: Optional[dict] = None) -> Mapping[str, Any]:
        response = requests.get(
            f"{self.UPSTOX_BASE_URL}/{isin}/{endpoint}",
            params=params,
            headers={"Accept": "application/json", "Authorization": f"Bearer {os.environ['UPSTOX_ACCESS_TOKEN']}"},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise ValueError(str(payload))
        return payload

    def _isin_for_symbol(self, symbol: str) -> Optional[str]:
        response = requests.get(self.EQUITY_MASTER_URL, headers=self.XBRL_HEADERS, timeout=20)
        response.raise_for_status()
        for row in csv.DictReader(StringIO(response.text)):
            # NSE CSV headers can contain leading/trailing whitespace.
            normalised_row = {str(key).strip().upper(): value for key, value in row.items()}
            if str(normalised_row.get("SYMBOL", "")).strip().upper() == symbol:
                return str(normalised_row.get("ISIN NUMBER", "")).strip() or None
        return None

    def _upstox_category_pair(self, categories: list, category: str):
        history = next(
            (item.get("history", []) for item in categories if item.get("category") == category), []
        )
        return self._upstox_history_pair(history)

    def _upstox_line_pair(self, lines: list, names: tuple):
        history = next(
            (item.get("history", []) for item in lines
             if any(name in str(item.get("particular", "")).lower() for name in names)),
            [],
        )
        return self._upstox_history_pair(history)

    def _upstox_balance_equity_pair(self, history: list):
        """Derive total equity as assets minus liabilities when supplied."""
        if len(history) < 2:
            return None
        latest = history[0]
        try:
            latest_period = datetime.strptime(str(latest.get("period", "")), "%b %Y")
        except ValueError:
            return None
        previous = next(
            (
                item for item in history[1:]
                if self._is_same_month_previous_year(
                    latest_period, str(item.get("period", ""))
                )
            ),
            None,
        )
        if previous is None:
            return None
        return (
            (self._number(latest.get("total_asset")) - self._number(latest.get("total_liability"))) * 10_000_000,
            (self._number(previous.get("total_asset")) - self._number(previous.get("total_liability"))) * 10_000_000,
        )

    def _upstox_history_pair(self, history: list):
        if len(history) < 2:
            return None
        latest = history[0]
        try:
            latest_period = datetime.strptime(str(latest.get("period", "")), "%b %Y")
        except ValueError:
            return None
        # With the quarterly endpoint, history[1] is the preceding quarter.
        # YoY must instead compare the same reporting month in the prior year
        # (e.g. Mar 2026 with Mar 2025), never a QoQ comparison.
        previous = next(
            (
                item for item in history[1:]
                if self._is_same_month_previous_year(
                    latest_period, str(item.get("period", ""))
                )
            ),
            None,
        )
        if previous is None:
            return None
        # Upstox returns values in ₹ crore. Convert to INR so all existing
        # Python calculations continue to use a single unit.
        return (self._number(latest.get("value")) * 10_000_000,
                self._number(previous.get("value")) * 10_000_000)

    @staticmethod
    def _is_same_month_previous_year(latest: datetime, period: str) -> bool:
        try:
            candidate = datetime.strptime(period, "%b %Y")
        except ValueError:
            return False
        return (
            candidate.month == latest.month
            and candidate.year == latest.year - 1
        )

    @staticmethod
    def _copy_fallback(fallback: Mapping[str, Any]) -> dict:
        """Return copies of nested cached fallback values."""
        return {
            key: dict(value) if isinstance(value, Mapping) else value
            for key, value in fallback.items()
        }

    def resolve_symbol(self, query: str) -> Optional[str]:
        """Resolve an NSE symbol from a stock symbol or company-name search."""
        lookup = self._normalise_lookup(query)
        if not lookup:
            return None
        if lookup in self.BSE_ONLY_ISINS:
            return lookup

        # A compact, single-word query is often an NSE ticker. Verify it with
        # NSE before doing fuzzy name matching, so NSDL cannot be redirected
        # to the different symbol NDL merely because their names are similar.
        direct_symbol = self._normalise_symbol(query)
        if " " not in direct_symbol and direct_symbol.isalnum():
            try:
                direct_quote = self._get_quote(direct_symbol)
                returned_symbol = str(
                    direct_quote.get("metadata", {}).get("symbol")
                    or direct_quote.get("info", {}).get("symbol", "")
                ).upper()
                if returned_symbol == direct_symbol:
                    return direct_symbol
            except Exception:
                pass

        try:
            response = requests.get(
                self.EQUITY_MASTER_URL, headers=self.XBRL_HEADERS, timeout=20
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            logger.warning("Unable to download NSE equity master list: %s", exc)
            return None

        matches = []
        fuzzy_matches = []
        for row in csv.DictReader(StringIO(response.text)):
            symbol = str(row.get("SYMBOL", "")).strip().upper()
            company_name = str(row.get("NAME OF COMPANY", "")).strip()
            if not symbol or str(row.get(" SERIES", row.get("SERIES", ""))).strip() not in {"EQ", "BE"}:
                continue
            symbol_lookup = self._normalise_lookup(symbol)
            company_lookup = self._normalise_lookup(company_name)
            if lookup == symbol_lookup or lookup == company_lookup:
                return symbol
            if lookup in company_lookup or lookup in symbol_lookup:
                matches.append((0, len(company_lookup), symbol))
                continue
            query_words = lookup.split()
            if query_words and all(word in company_lookup for word in query_words):
                matches.append((1, len(company_lookup), symbol))
                continue

            # Compare against both the meaningful company name and individual
            # name words. The latter makes a short typo such as "relience"
            # match "Reliance Industries Limited".
            company_words = self._meaningful_lookup_words(company_lookup)
            company_core = " ".join(company_words)
            symbol_score = SequenceMatcher(None, lookup, symbol_lookup).ratio()
            score = max(
                SequenceMatcher(None, lookup, company_core).ratio(),
                symbol_score,
                *(SequenceMatcher(None, lookup, word).ratio() for word in company_words),
            )
            # Short tickers are easy to confuse (NSDL vs NDL). Keep fuzzy
            # matching permissive for company names, but make it stricter for
            # compact symbol-like searches.
            fuzzy_threshold = 0.88 if len(lookup) <= 5 and " " not in lookup else 0.72
            if len(lookup) >= 4 and score >= fuzzy_threshold:
                # If several company names share a close word (for example,
                # Reliance Industries and Reliance Power), prefer the symbol
                # that is also closest to the entered text.
                fuzzy_matches.append((-score, -symbol_score, len(company_lookup), symbol))

        if matches:
            return min(matches)[2]
        if query.strip().isupper() and " " not in query.strip():
            return None
        return min(fuzzy_matches)[-1] if fuzzy_matches else None

    def generate_interpretation(self, symbol: str, metrics: Mapping[str, Any]):
        """Create an exact, rule-based explanation from the NSE comparison values."""
        source_note = str(metrics.get("data_note", ""))
        comparison_data = (
            self._fallback_comparison(symbol, "upstox")
            if "Upstox Fundamentals" in source_note
            else {}
            if "Upstox fallback were unavailable" in source_note
            else self._interpretation_data(symbol)
        )
        descriptions = {
            "revenue": "Revenue",
            "profit": "Profit",
            "operating_cash_flow": "Operating cash flow",
            "free_cash_flow": "Free cash flow",
            "net_debt": "Net debt",
            "loan_book": "Loan book",
            "interest_income": "Interest income",
        }
        items = []
        for key, label in descriptions.items():
            values = comparison_data.get(key)
            if not values:
                continue
            change = values["yoy_percent"]
            direction = "increased" if change > 0 else "decreased" if change < 0 else "was unchanged"
            is_debt = key == "net_debt"
            tone = "neutral" if not change else (
                "positive" if (change > 0) != is_debt else "negative"
            )
            items.append({
                "tone": tone,
                "text": (
                    f"{label} {direction} from ₹{values['previous_year_crore']:,.2f} crore "
                    f"to ₹{values['current_crore']:,.2f} crore ({change:+.2f}% YoY)."
                ),
            })

        revenue = comparison_data.get("revenue")
        profit = comparison_data.get("profit")
        if revenue and profit and revenue["current_crore"] and revenue["previous_year_crore"]:
            current_margin = profit["current_crore"] / revenue["current_crore"] * 100
            previous_margin = profit["previous_year_crore"] / revenue["previous_year_crore"] * 100
            margin_change = current_margin - previous_margin
            direction = "increased" if margin_change > 0 else "decreased" if margin_change < 0 else "was unchanged"
            items.append({
                "tone": "positive" if margin_change > 0 else "negative" if margin_change < 0 else "neutral",
                "text": (
                    f"Profit margin {direction} from {previous_margin:.2f}% to {current_margin:.2f}% "
                    f"({margin_change:+.2f} percentage points YoY)."
                ),
            })

        if "loan_book_yoy" in metrics:
            items.append({
                "tone": "neutral",
                "text": (
                    "For a bank or NBFC, loan-book and interest-income trends are generally more useful "
                    "than free-cash-flow trends."
                ),
            })
        return items or [{
            "tone": "neutral",
            "text": "Comparable NSE filing values are unavailable for an interpretation.",
        }]

    def _fallback_comparison(
        self, symbol: str, source: str
    ) -> Mapping[str, Mapping[str, float]]:
        """Build interpretation comparisons from cached fallback source values."""
        cache_key = source if ":" in source else f"{source}:{symbol}"
        with self._fundamentals_cache_lock:
            cached = self._fundamentals_cache.get(cache_key)
            fallback = cached[1] if cached else {}
        pairs = {
            "revenue": (fallback.get("latest", {}), fallback.get("previous", {}), "revenue"),
            "profit": (fallback.get("latest", {}), fallback.get("previous", {}), "profit"),
            "operating_cash_flow": (
                fallback.get("ocf_latest", {}), fallback.get("ocf_previous", {}), "operating_cash_flow"
            ),
            "free_cash_flow": (
                fallback.get("fcf_latest", {}), fallback.get("fcf_previous", {}), "free_cash_flow"
            ),
            "net_debt": (
                fallback.get("net_debt_latest", {}), fallback.get("net_debt_previous", {}), "net_debt"
            ),
        }
        result = {}
        for label, (current, prior, key) in pairs.items():
            current_value, prior_value = current.get(key), prior.get(key)
            if current_value is None or prior_value is None:
                continue
            result[label] = {
                "current_crore": round(self._number(current_value) / 10_000_000, 2),
                "previous_year_crore": round(self._number(prior_value) / 10_000_000, 2),
                "yoy_percent": round(self._growth(current_value, prior_value), 2),
            }
        return result

    def _interpretation_data(self, symbol: str) -> Mapping[str, Mapping[str, float]]:
        (
            latest,
            previous,
            ocf_latest,
            ocf_previous,
            fcf_latest,
            fcf_previous,
            net_debt_latest,
            net_debt_previous,
            loan_book_latest,
            loan_book_previous,
            interest_income_latest,
            interest_income_previous,
        ) = self._financial_filing_metrics(symbol)
        pairs = {
            "revenue": (latest, previous, "revenue"),
            "profit": (latest, previous, "profit"),
            "operating_cash_flow": (ocf_latest, ocf_previous, "operating_cash_flow"),
            "free_cash_flow": (fcf_latest, fcf_previous, "free_cash_flow"),
            "net_debt": (net_debt_latest, net_debt_previous, "net_debt"),
            "loan_book": (loan_book_latest, loan_book_previous, "loan_book"),
            "interest_income": (interest_income_latest, interest_income_previous, "interest_income"),
        }
        result = {}
        for label, (current, prior, key) in pairs.items():
            current_value = current.get(key)
            prior_value = prior.get(key)
            if current_value is None or prior_value is None:
                continue
            if not current_value or not prior_value:
                continue
            result[label] = {
                "current_crore": round(self._number(current_value) / 10_000_000, 2),
                "previous_year_crore": round(self._number(prior_value) / 10_000_000, 2),
                "yoy_percent": round(self._growth(current_value, prior_value), 2),
            }
        return result

    def _get_quote(self, symbol: str) -> Mapping[str, Any]:
        if self.nse_live is None:
            self.nse_live = NSELive()
        return self.nse_live.stock_quote(symbol)

    def _financial_filing_metrics(
        self, symbol: str
    ) -> tuple:
        response = self.nse_live.corporate_integrated_filing(
            index="equities", symbol=symbol, size=20
        )
        filings = response.get("data", [])
        if not isinstance(filings, list):
            raise ValueError("NSE returned no financial filings")

        consolidated = [f for f in filings if f.get("consolidated") == "Consolidated"]
        candidates = consolidated or filings
        latest_filing = candidates[0] if candidates else None
        if not latest_filing:
            raise ValueError("No financial filing found")

        latest_date = self._filing_date(latest_filing)
        previous_filing = next(
            (
                filing
                for filing in candidates[1:]
                if self._is_previous_year_period(latest_date, self._filing_date(filing))
            ),
            None,
        )
        if not previous_filing:
            raise ValueError("No comparable prior-year financial filing found")

        metrics_cache = {}

        def metrics_for(filing: Mapping[str, Any]) -> Mapping[str, float]:
            url = str(filing.get("xbrl"))
            if url not in metrics_cache:
                try:
                    metrics_cache[url] = self._xbrl_metrics(filing)
                except (requests.RequestException, ElementTree.ParseError) as exc:
                    # NSE sometimes retains a filing record after the linked
                    # XBRL file has been removed. Skip that record instead of
                    # discarding every metric for the company.
                    logger.warning("Unable to read NSE XBRL filing %s: %s", url, exc)
                    metrics_cache[url] = {}
            return metrics_cache[url]

        latest_metrics = metrics_for(latest_filing)
        previous_metrics = metrics_for(previous_filing)
        ocf_latest, ocf_previous = self._latest_metric_pair(
            candidates, metrics_for, "operating_cash_flow"
        )
        fcf_latest, fcf_previous = self._latest_metric_pair(
            candidates, metrics_for, "free_cash_flow"
        )
        net_debt_latest, net_debt_previous = self._latest_metric_pair(
            candidates, metrics_for, "net_debt"
        )
        loan_book_latest, loan_book_previous = self._latest_metric_pair(
            candidates, metrics_for, "loan_book"
        )
        interest_income_latest, interest_income_previous = self._latest_metric_pair(
            candidates, metrics_for, "interest_income"
        )
        return (
            latest_metrics,
            previous_metrics,
            ocf_latest,
            ocf_previous,
            fcf_latest,
            fcf_previous,
            net_debt_latest,
            net_debt_previous,
            loan_book_latest,
            loan_book_previous,
            interest_income_latest,
            interest_income_previous,
        )

    def _latest_metric_pair(self, filings, metrics_for, metric_name):
        """Find the newest same-period filing pair that reports a given metric."""
        latest_available = {}
        for filing in filings:
            current_metrics = metrics_for(filing)
            if not current_metrics.get(metric_name):
                continue
            if not latest_available:
                latest_available = current_metrics
            filing_date = self._filing_date(filing)
            previous_filing = next(
                (
                    candidate
                    for candidate in filings
                    if self._is_previous_year_period(filing_date, self._filing_date(candidate))
                ),
                None,
            )
            if previous_filing:
                previous_metrics = metrics_for(previous_filing)
                if previous_metrics.get(metric_name):
                    return current_metrics, previous_metrics
        # Retain the newest current figure for metrics such as debt-to-equity,
        # even if NSE no longer hosts the comparable prior-year XBRL file.
        return latest_available, {}

    def _xbrl_metrics(self, filing: Mapping[str, Any]) -> Mapping[str, float]:
        xbrl_url = filing.get("xbrl")
        if not xbrl_url:
            raise ValueError("Financial filing has no XBRL document")
        response = requests.get(xbrl_url, headers=self.XBRL_HEADERS, timeout=20)
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)

        # An NSE XBRL file contains several facts with the same tag: current
        # quarter, year-to-date, prior-year comparative and often segments.
        # XML order is not a financial-data rule, so never use the first tag.
        # Select facts whose XBRL context belongs to this filing's reported
        # period instead.
        values = self._period_values(root, self._filing_date(filing))
        metric_tags = {
            "RevenueFromOperations",
            "OperatingIncome",
            "Income",
            "GrossPremiumIncome",
            "ProfitLossForPeriod",
            "ProfitLossAfterTax",
            "ProfitLossAfterTaxBeforeExtraordinaryItems",
            "ProfitLossAfterTaxAndExtraordinaryItems",
            "ProfitLossForThePeriod",
            "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
            "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
            "BasicEarningsLossPerShareFromContinuingOperations",
            "CashFlowsFromUsedInOperatingActivities",
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
            "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
            "PurchaseOfIntangibleAssetsUnderDevelopment",
            "BorrowingsNoncurrent",
            "BorrowingsCurrent",
            "Borrowings",
            "CashAndCashEquivalents",
            "Equity",
            "Loans",
            "Advances",
            "InterestEarned",
        }
        values = {name: value for name, value in values.items() if name in metric_tags}

        return {
            "revenue": (
                values.get("RevenueFromOperations", 0.0)
                or values.get("OperatingIncome", 0.0)
                or values.get("Income", 0.0)
                or values.get("GrossPremiumIncome", 0.0)
            ),
            "profit": (
                values.get("ProfitLossForPeriod", 0.0)
                or values.get("ProfitLossAfterTax", 0.0)
                or values.get("ProfitLossForThePeriod", 0.0)
                or values.get(
                    "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates", 0.0
                )
                or values.get("ProfitLossAfterTaxAndExtraordinaryItems", 0.0)
                or values.get("ProfitLossAfterTaxBeforeExtraordinaryItems", 0.0)
            ),
            "eps": (
                values.get("BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations")
                or values.get("BasicEarningsLossPerShareFromContinuingOperations", 0.0)
            ),
            "operating_cash_flow": values.get("CashFlowsFromUsedInOperatingActivities", 0.0),
            "free_cash_flow": (
                values.get("CashFlowsFromUsedInOperatingActivities", 0.0)
                - values.get("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities", 0.0)
                - values.get("PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities", 0.0)
                - values.get("PurchaseOfIntangibleAssetsUnderDevelopment", 0.0)
            ),
            "total_borrowings": (
                values.get("Borrowings", 0.0)
                or values.get("BorrowingsNoncurrent", 0.0) + values.get("BorrowingsCurrent", 0.0)
            ),
            "equity": values.get("Equity", 0.0),
            "loan_book": values.get("Loans", 0.0) or values.get("Advances", 0.0),
            "interest_income": values.get("InterestEarned", 0.0),
            "net_debt": (
                (
                    values.get("Borrowings", 0.0)
                    or values.get("BorrowingsNoncurrent", 0.0)
                    + values.get("BorrowingsCurrent", 0.0)
                )
                - values.get("CashAndCashEquivalents", 0.0)
            ),
        }

    def _period_values(
        self, root: ElementTree.Element, report_date: Optional[datetime]
    ) -> Mapping[str, float]:
        """Return only top-level XBRL facts matching the filing's reporting date."""
        if not report_date:
            return {}

        contexts = {}
        for context in root.iter():
            if self._tag_name(context) != "context":
                continue
            context_id = context.get("id")
            period = next((item for item in context if self._tag_name(item) == "period"), None)
            if not context_id or period is None:
                continue
            start = self._context_date(period, "startDate")
            end = self._context_date(period, "endDate")
            instant = self._context_date(period, "instant")
            has_dimension = any(
                self._tag_name(item) in {"scenario", "segment", "explicitMember", "typedMember"}
                for item in context.iter()
            )
            contexts[context_id] = (start, end or instant, bool(instant), has_dimension)

        candidates = {}
        target_date = report_date.date()
        for element in root.iter():
            context_id = element.get("contextRef")
            if not context_id or not element.text or context_id not in contexts:
                continue
            start, end, is_instant, has_dimension = contexts[context_id]
            if end != target_date:
                continue
            name = self._tag_name(element)
            value = self._number(element.text)
            duration_days = (end - start).days if start and not is_instant else 0
            # Prefer primary (non-segment) facts. For duration facts ending on
            # the same date, prefer the shortest period: quarterly rather than
            # a cumulative year-to-date number.
            rank = (1 if has_dimension else 0, duration_days)
            current = candidates.get(name)
            if current is None or rank < current[0]:
                candidates[name] = (rank, value)
        return {name: candidate[1] for name, candidate in candidates.items()}

    @staticmethod
    def _context_date(period: ElementTree.Element, name: str):
        element = next((item for item in period if StockService._tag_name(item) == name), None)
        if element is None or not element.text:
            return None
        try:
            return datetime.strptime(element.text.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    @staticmethod
    def _tag_name(element: ElementTree.Element) -> str:
        return element.tag.rsplit("}", 1)[-1]

    @staticmethod
    def _filing_date(filing: Mapping[str, Any]) -> Optional[datetime]:
        try:
            return datetime.strptime(str(filing.get("qe_Date")), "%d-%b-%Y")
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_previous_year_period(
        latest_date: Optional[datetime], candidate_date: Optional[datetime]
    ) -> bool:
        return bool(
            latest_date
            and candidate_date
            and latest_date.month == candidate_date.month
            and latest_date.day == candidate_date.day
            and latest_date.year - candidate_date.year == 1
        )

    @staticmethod
    def _growth(current: Any, previous: Any) -> float:
        current_value = StockService._number(current)
        previous_value = StockService._number(previous)
        # Use the absolute prior value so that a loss-to-profit turnaround is
        # positive and a larger loss is negative. Dividing by a negative prior
        # value would reverse those signals.
        return (
            ((current_value - previous_value) / abs(previous_value)) * 100
            if previous_value
            else 0.0
        )

    @staticmethod
    def _is_financial_company(quote: Mapping[str, Any]) -> bool:
        return StockService._quote_industry_contains(
            quote, ("NBFC", "FINANCE", "BANK", "INSURANCE")
        )

    @staticmethod
    def _is_lender(quote: Mapping[str, Any]) -> bool:
        return StockService._quote_industry_contains(quote, ("NBFC", "FINANCE", "BANK")) and not StockService._quote_industry_contains(quote, ("INSURANCE",))

    @staticmethod
    def _quote_industry_contains(quote: Mapping[str, Any], terms: tuple) -> bool:
        details = quote.get("secInfo", {})
        text = " ".join(
            str(details.get(key, ""))
            for key in ("basicIndustry", "industryInfo", "sector", "macro")
        ).upper()
        return any(term in text for term in terms)

    @staticmethod
    def _normalise_symbol(symbol: str) -> str:
        return symbol.strip().upper().removesuffix(".NS")

    @staticmethod
    def _normalise_lookup(value: str) -> str:
        return " ".join(
            "".join(character if character.isalnum() else " " for character in value.upper()).split()
        )

    @staticmethod
    def _meaningful_lookup_words(value: str):
        ignored_words = {"LIMITED", "LTD", "INDIA", "INDIAN", "THE", "AND", "COMPANY", "CO"}
        return [word for word in value.split() if word not in ignored_words]

    @staticmethod
    def _number(value: Any) -> float:
        if value is None or value == "":
            return 0.0
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _empty_data(symbol: str) -> StockData:
        return {
            "symbol": symbol,
            "pe_ratio": 0.0,
            "profit_margin": 0.0,
            "profit_margin_yoy_change": 0.0,
            "yoy_revenue": 0.0,
            "yoy_profit": 0.0,
            "operating_cash_flow_yoy": 0.0,
            "free_cash_flow_yoy": 0.0,
            "net_debt_yoy": 0.0,
            "debt_to_equity": 0.0,
        }
