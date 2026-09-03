from flask import Flask, jsonify, render_template, request
from services.stock_service import StockService

app = Flask(__name__)
stock_service = StockService(api_client=None)  # No need to pass api_client


@app.route('/health', methods=['GET'])
def health():
    """Cheap liveness check for the ECS task and load balancer."""
    return jsonify({"status": "ok"}), 200


@app.route('/', methods=['GET'])
def home():
    return render_template('index.html')


@app.route('/search', methods=['GET'])
def search_stock():
    try:
        query = request.args.get('query', '')
        symbol = stock_service.resolve_symbol(query)
        if not symbol:
            return jsonify({"error": "No NSE equity symbol found for that company name."}), 404
        return jsonify(stock_service.fetch_stock_data(symbol)), 200
    except Exception as exc:
        app.logger.exception("Stock search failed")
        return jsonify({"error": f"Stock search failed: {exc}"}), 502


@app.route('/nifty-50', methods=['GET'])
def get_nifty_50():
    try:
        stocks = stock_service.nifty_50_constituents()
        return jsonify({"stocks": stocks, "count": len(stocks)}), 200
    except Exception as exc:
        app.logger.exception("NIFTY 50 constituent lookup failed")
        return jsonify({"error": f"Unable to load the current NIFTY 50 list: {exc}"}), 502


@app.route('/nifty-next-50', methods=['GET'])
def get_nifty_next_50():
    try:
        stocks = stock_service.nifty_next_50_constituents()
        return jsonify({"stocks": stocks, "count": len(stocks)}), 200
    except Exception as exc:
        app.logger.exception("NIFTY Next 50 constituent lookup failed")
        return jsonify({"error": f"Unable to load the current NIFTY Next 50 list: {exc}"}), 502


@app.route('/interpretation/<symbol>', methods=['GET'])
def get_interpretation(symbol):
    try:
        stock_data = stock_service.fetch_stock_data(symbol)
        interpretation_items = stock_service.generate_interpretation(symbol, stock_data)
        return jsonify({
            "symbol": stock_data["symbol"],
            # Keep a plain-text version for API clients or an older browser
            # page that expects a single interpretation string.
            "interpretation": "\n".join(item["text"] for item in interpretation_items),
            "interpretation_items": interpretation_items,
        }), 200
    except Exception as exc:
        app.logger.exception("Interpretation failed")
        return jsonify({"error": f"Interpretation unavailable: {exc}"}), 502

@app.route('/stock/<symbol>', methods=['GET'])
def get_stock_results(symbol):
    try:
        stock_data = stock_service.fetch_stock_data(symbol)
        return jsonify(stock_data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5050)
