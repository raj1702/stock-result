import os
from datetime import timedelta
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from services.plan_service import PlanService
from services.stock_service import StockService

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("COGNITO_REDIRECT_URI", "").startswith("https://"),
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)
stock_service = StockService(api_client=None)  # No need to pass api_client
plan_service = PlanService()

COGNITO_REGION = os.getenv("COGNITO_REGION", "ap-south-1")
COGNITO_USER_POOL_ID = os.getenv("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.getenv("COGNITO_CLIENT_ID", "")
COGNITO_CLIENT_SECRET = os.getenv("COGNITO_CLIENT_SECRET", "")
COGNITO_DOMAIN = os.getenv("COGNITO_DOMAIN", "").rstrip("/")
COGNITO_REDIRECT_URI = os.getenv("COGNITO_REDIRECT_URI", "")
COGNITO_LOGOUT_URI = os.getenv("COGNITO_LOGOUT_URI", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

oauth = OAuth(app)
cognito = None
if all((COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, COGNITO_CLIENT_SECRET, COGNITO_DOMAIN)):
    issuer = (
        f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
        f"{COGNITO_USER_POOL_ID}"
    )
    cognito = oauth.register(
        name="cognito",
        client_id=COGNITO_CLIENT_ID,
        client_secret=COGNITO_CLIENT_SECRET,
        authorize_url=f"{COGNITO_DOMAIN}/oauth2/authorize",
        access_token_url=f"{COGNITO_DOMAIN}/oauth2/token",
        jwks_uri=f"{issuer}/.well-known/jwks.json",
        client_kwargs={"scope": "openid email profile"},
    )


def _authentication_ready():
    return bool(cognito and app.secret_key and COGNITO_REDIRECT_URI and COGNITO_LOGOUT_URI)


def _guest_stock_access(symbol, *, record=False, limit=2, session_key="guest_stock_symbols"):
    """Allow two distinct stocks per guest browser before requiring sign-in."""
    if session.get("user") or not _authentication_ready():
        return None
    viewed = list(dict.fromkeys(session.get(session_key, [])))
    if symbol in viewed:
        return None
    if len(viewed) >= limit:
        return jsonify({
            "auth_required": True,
            "error": "Sign in to continue exploring stock results.",
            "login_url": "/login?next=/",
            "signup_url": "/signup?next=/",
        }), 401
    if record:
        viewed.append(symbol)
        session.permanent = True
        session[session_key] = viewed
    return None


def _qualify_current_user_referral():
    user = session.get("user")
    if not user:
        return
    try:
        plan_service.qualify_referral(user["sub"])
    except Exception:
        app.logger.exception("Unable to qualify referral after stock analysis")


def _plan_limit_response(usage):
    return jsonify({
        "plan_limit_reached": True,
        "error": "Your monthly stock limit is used. Refer a friend to upgrade your plan.",
        **usage,
    }), 403


def _stock_access_gate(symbol, *, screening_preview=False):
    guest_options = {
        "limit": 3,
        "session_key": "guest_screening_symbols",
    } if screening_preview else {}
    blocked = _guest_stock_access(symbol, **guest_options)
    if blocked:
        return blocked
    user = session.get("user")
    if not user:
        return None
    usage = plan_service.can_access_stock(user["sub"], symbol)
    return None if usage["allowed"] else _plan_limit_response(usage)


def _complete_stock_access(symbol, *, screening_preview=False):
    guest_options = {
        "limit": 3,
        "session_key": "guest_screening_symbols",
    } if screening_preview else {}
    _guest_stock_access(symbol, record=True, **guest_options)
    user = session.get("user")
    if not user:
        return None
    usage = plan_service.record_stock_usage(user["sub"], symbol)
    if not usage["allowed"]:
        return _plan_limit_response(usage)
    _qualify_current_user_referral()
    return None


@app.route('/health', methods=['GET'])
def health():
    """Cheap liveness check for the ECS task and load balancer."""
    return jsonify({"status": "ok"}), 200


@app.route('/', methods=['GET'])
def home():
    referral_code = request.args.get("ref")
    if referral_code:
        try:
            valid_code = plan_service.capture_referral(
                referral_code,
                current_user_id=(session.get("user") or {}).get("sub"),
            )
            if valid_code and not session.get("user"):
                session.permanent = True
                session["pending_referral_code"] = valid_code
        except Exception:
            app.logger.exception("Unable to capture referral code")
    return render_template('index.html', current_user=session.get("user"))


@app.route('/login', methods=['GET'])
def login():
    if not _authentication_ready():
        return jsonify({"error": "Authentication is not configured on this server."}), 503
    requested_path = request.args.get("next", "/")
    session["post_login_path"] = (
        requested_path if requested_path.startswith("/") and not requested_path.startswith("//") else "/"
    )
    # Do not silently reuse the last Cognito/Google identity. Cognito handles
    # `login` locally and forwards `select_account` to federated providers.
    return cognito.authorize_redirect(
        COGNITO_REDIRECT_URI,
        prompt="login select_account",
    )


@app.route('/signup', methods=['GET'])
def signup():
    if not _authentication_ready():
        return jsonify({"error": "Authentication is not configured on this server."}), 503
    requested_path = request.args.get("next", "/")
    session["post_login_path"] = (
        requested_path if requested_path.startswith("/") and not requested_path.startswith("//") else "/"
    )
    # Authlib creates and stores the OAuth state/nonce before generating the
    # authorization URL. Cognito's `screen_hint` is ignored for local user-pool
    # accounts, so preserve those secure parameters and open its dedicated
    # managed-login registration endpoint instead.
    authorization_response = cognito.authorize_redirect(COGNITO_REDIRECT_URI)
    signup_url = authorization_response.headers["Location"].replace(
        f"{COGNITO_DOMAIN}/oauth2/authorize",
        f"{COGNITO_DOMAIN}/signup",
        1,
    )
    return redirect(signup_url)


@app.route('/auth/callback', methods=['GET'])
def auth_callback():
    if not _authentication_ready():
        return jsonify({"error": "Authentication is not configured on this server."}), 503
    try:
        token = cognito.authorize_access_token()
        user = token.get("userinfo")
        if not user:
            raise ValueError("Cognito returned no verified OpenID user information")
    except Exception:
        app.logger.exception("Cognito callback failed")
        return redirect(url_for("home", auth_error="signin_failed"))
    post_login_path = session.get("post_login_path", "/")
    pending_referral_code = session.get("pending_referral_code")
    session.clear()
    session.permanent = True
    session["user"] = {
        "sub": user.get("sub"),
        "email": user.get("email"),
        "name": user.get("name") or user.get("email"),
    }
    try:
        plan, created = plan_service.ensure_user_with_status(session["user"])
        network_hash = plan_service.hash_network_address(request.remote_addr)
        plan_service.update_login_context(session["user"]["sub"], network_hash)
        if pending_referral_code and created:
            plan_service.register_referral(
                session["user"]["sub"],
                pending_referral_code,
                email=session["user"].get("email"),
                network_hash=network_hash,
            )
    except Exception:
        # Authentication should still succeed during a temporary DynamoDB issue.
        # Plan-protected actions will fail closed when they check entitlement.
        app.logger.exception("Unable to initialise the user plan")
    return redirect(post_login_path)


@app.route('/auth/status', methods=['GET'])
def auth_status():
    user = session.get("user")
    return jsonify({"authenticated": bool(user), "user": user if user else None})


@app.route('/api/plan', methods=['GET'])
def plan_status():
    user = session.get("user")
    if not user:
        return jsonify({
            "authenticated": False,
            "error": "Sign in to view your plan.",
            "login_url": "/login?next=/",
        }), 401
    try:
        plan = plan_service.ensure_user(user)
        usage = plan_service.get_usage(user["sub"], plan)
        referral_path = url_for("home", ref=plan["referral_code"])
        referral_url = f"{PUBLIC_BASE_URL}{referral_path}" if PUBLIC_BASE_URL else url_for(
            "home", ref=plan["referral_code"], _external=True
        )
        return jsonify({
            "authenticated": True,
            **plan,
            **usage,
            "referral_url": referral_url,
        }), 200
    except Exception:
        app.logger.exception("Unable to load the user plan")
        return jsonify({"error": "Your plan is temporarily unavailable."}), 503


@app.route('/logout', methods=['GET'])
def logout():
    session.clear()
    if not COGNITO_DOMAIN or not COGNITO_CLIENT_ID or not COGNITO_LOGOUT_URI:
        return redirect(url_for("home"))
    query = urlencode({"client_id": COGNITO_CLIENT_ID, "logout_uri": COGNITO_LOGOUT_URI})
    return redirect(f"{COGNITO_DOMAIN}/logout?{query}")


@app.route('/search', methods=['GET'])
def search_stock():
    try:
        query = request.args.get('query', '')
        symbol = stock_service.resolve_symbol(query)
        if not symbol:
            return jsonify({"error": "No NSE equity symbol found for that company name."}), 404
        blocked = _stock_access_gate(symbol)
        if blocked:
            return blocked
        stock_data = stock_service.fetch_stock_data(symbol)
        limit_error = _complete_stock_access(symbol)
        if limit_error:
            return limit_error
        return jsonify(stock_data), 200
    except Exception as exc:
        app.logger.exception("Stock search failed")
        return jsonify({"error": f"Stock search failed: {exc}"}), 502


@app.route('/search-access', methods=['GET'])
def search_access():
    """Apply the guest limit before serving a browser-cached stock result."""
    try:
        symbol = stock_service.resolve_symbol(request.args.get('query', ''))
        if not symbol:
            return jsonify({"error": "No NSE equity symbol found for that company name."}), 404
        blocked = _stock_access_gate(symbol)
        if blocked:
            return blocked
        limit_error = _complete_stock_access(symbol)
        if limit_error:
            return limit_error
        return jsonify({"allowed": True, "symbol": symbol}), 200
    except Exception as exc:
        app.logger.exception("Stock access check failed")
        return jsonify({"error": f"Unable to check stock access: {exc}"}), 502


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


@app.route('/screening/<index_name>/<symbol>', methods=['GET'])
def get_screening_result(index_name, symbol):
    """Return a quota-limited screener row without consuming stock allowance."""
    try:
        constituent_loaders = {
            "nifty-50": stock_service.nifty_50_constituents,
            "nifty-next-50": stock_service.nifty_next_50_constituents,
        }
        loader = constituent_loaders.get(index_name)
        if not loader:
            return jsonify({"error": "Unknown stock index."}), 404

        constituents = loader()
        symbol = symbol.upper()
        user = session.get("user")
        if user:
            plan = plan_service.get_plan(user["sub"])
            usage = plan_service.get_usage(user["sub"], plan)
            used = set(usage["used_symbols"])
            remaining = usage["stocks_remaining"]
            visible_symbols = []
            for stock in constituents:
                candidate = stock["symbol"].upper()
                if plan["stock_limit"] is None or candidate in used:
                    visible_symbols.append(candidate)
                elif remaining > 0:
                    visible_symbols.append(candidate)
                    remaining -= 1
        else:
            visible_symbols = [stock["symbol"].upper() for stock in constituents[:3]]

        if symbol not in visible_symbols:
            return jsonify({
                "screening_locked": True,
                "error": "This screening row is locked by your current plan.",
            }), 403

        return jsonify(stock_service.fetch_stock_data(symbol)), 200
    except Exception as exc:
        app.logger.exception("Screening result failed")
        return jsonify({"error": f"Screening result unavailable: {exc}"}), 502


@app.route('/interpretation/<symbol>', methods=['GET'])
def get_interpretation(symbol):
    try:
        symbol = symbol.upper()
        blocked = _stock_access_gate(symbol)
        if blocked:
            return blocked
        stock_data = stock_service.fetch_stock_data(symbol)
        limit_error = _complete_stock_access(symbol)
        if limit_error:
            return limit_error
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
        symbol = symbol.upper()
        screening_preview = request.args.get("preview") == "screening" and not session.get("user")
        blocked = _stock_access_gate(symbol, screening_preview=screening_preview)
        if blocked:
            return blocked
        stock_data = stock_service.fetch_stock_data(symbol)
        limit_error = _complete_stock_access(symbol, screening_preview=screening_preview)
        if limit_error:
            return limit_error
        return jsonify(stock_data), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5050)
