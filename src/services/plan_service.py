import os
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError


TIER_ORDER = ("free", "bronze", "silver", "gold")
TIER_LIMITS = {
    "free": 10,
    "bronze": 25,
    "silver": 40,
    "gold": None,
}
TIER_DURATION = timedelta(days=30)
INDIA_TIMEZONE = ZoneInfo("Asia/Kolkata")
DISPOSABLE_EMAIL_DOMAINS = {
    "10minutemail.com", "guerrillamail.com", "mailinator.com", "tempmail.com",
    "temp-mail.org", "yopmail.com",
}


class PlanService:
    """Store and resolve a user's referral-earned plan in DynamoDB."""

    def __init__(self):
        region = os.getenv("AWS_REGION", "ap-south-1")
        table_name = os.getenv("DYNAMODB_TABLE_NAME", "earnings-assistant")
        self.referral_secret = os.getenv("REFERRAL_CODE_SECRET") or os.getenv("FLASK_SECRET_KEY", "")
        if not self.referral_secret:
            raise ValueError("REFERRAL_CODE_SECRET or FLASK_SECRET_KEY must be configured")
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self.table_name = table_name
        self.dynamodb_client = self.table.meta.client

    @staticmethod
    def _now():
        return datetime.now(INDIA_TIMEZONE)

    @staticmethod
    def _parse_datetime(value):
        return datetime.fromisoformat(value) if value else None

    @staticmethod
    def _format_datetime(value):
        return value.isoformat() if value else None

    def ensure_user(self, user):
        plan, _created = self.ensure_user_with_status(user)
        return plan

    def ensure_user_with_status(self, user):
        """Create the default Free profile once and report whether it was new."""
        user_id = user.get("sub")
        if not user_id:
            raise ValueError("A Cognito user ID is required")

        now = self._now()
        created = False
        try:
            self.table.put_item(
                Item={
                    "PK": f"USER#{user_id}",
                    "SK": "PROFILE",
                    "email": user.get("email") or "",
                    "plan": "free",
                    "successful_referrals": 0,
                    "created_at": self._format_datetime(now),
                    "updated_at": self._format_datetime(now),
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
            created = True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        self._ensure_email_identity(user_id, user.get("email"))
        self._ensure_referral_code(user_id)
        return self.get_plan(user_id), created

    def _email_fingerprint(self, email):
        normalized = (email or "").strip().lower()
        if not normalized:
            return None
        return hmac.new(
            self.referral_secret.encode("utf-8"),
            normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _ensure_email_identity(self, user_id, email):
        fingerprint = self._email_fingerprint(email)
        if not fingerprint:
            return
        try:
            self.table.put_item(
                Item={
                    "PK": f"EMAIL#{fingerprint}",
                    "SK": "IDENTITY",
                    "user_id": user_id,
                    "created_at": self._format_datetime(self._now()),
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            identity = self.table.get_item(
                Key={"PK": f"EMAIL#{fingerprint}", "SK": "IDENTITY"},
                ConsistentRead=True,
            ).get("Item", {})
            if identity.get("user_id") != user_id:
                self.table.update_item(
                    Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
                    UpdateExpression="SET identity_conflict = :value",
                    ExpressionAttributeValues={":value": True},
                )

    def hash_network_address(self, address):
        if not address:
            return None
        return hmac.new(
            self.referral_secret.encode("utf-8"),
            address.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def update_login_context(self, user_id, network_hash):
        if not network_hash:
            return
        self.table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
            UpdateExpression="SET last_login_network_hash = :network, updated_at = :updated",
            ExpressionAttributeValues={
                ":network": network_hash,
                ":updated": self._format_datetime(self._now()),
            },
        )

    def _ensure_referral_code(self, user_id):
        """Backfill a stable, non-sequential referral code and its lookup item."""
        digest = hmac.new(
            self.referral_secret.encode("utf-8"),
            user_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        referral_code = digest[:10].upper()
        now = self._format_datetime(self._now())

        lookup = self.table.get_item(
            Key={"PK": f"REFERRAL#{referral_code}", "SK": "LOOKUP"},
            ConsistentRead=True,
        ).get("Item")
        if lookup and lookup.get("referrer_user_id") != user_id:
            raise RuntimeError("Referral-code collision detected")

        self.table.put_item(Item={
            "PK": f"REFERRAL#{referral_code}",
            "SK": "LOOKUP",
            "referrer_user_id": user_id,
            "created_at": lookup.get("created_at", now) if lookup else now,
        })
        self.table.update_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
            UpdateExpression="SET referral_code = if_not_exists(referral_code, :code)",
            ExpressionAttributeValues={":code": referral_code},
        )
        return referral_code

    def capture_referral(self, referral_code, current_user_id=None):
        """Validate a share code before preserving it in the browser session."""
        code = (referral_code or "").strip().upper()
        if not code:
            return None
        lookup = self.table.get_item(
            Key={"PK": f"REFERRAL#{code}", "SK": "LOOKUP"},
            ConsistentRead=True,
        ).get("Item")
        if not lookup or lookup.get("referrer_user_id") == current_user_id:
            return None
        return code

    def register_referral(self, referred_user_id, referral_code, email=None, network_hash=None):
        """Bind one inviter to a user; qualification happens after first analysis."""
        code = self.capture_referral(referral_code, current_user_id=referred_user_id)
        if not code:
            return False
        lookup = self.table.get_item(
            Key={"PK": f"REFERRAL#{code}", "SK": "LOOKUP"},
            ConsistentRead=True,
        )["Item"]
        referred_profile = self.table.get_item(
            Key={"PK": f"USER#{referred_user_id}", "SK": "PROFILE"},
            ConsistentRead=True,
        ).get("Item", {})
        if referred_profile.get("identity_conflict"):
            return False
        email_domain = (email or "").strip().lower().rpartition("@")[2]
        if email_domain in DISPOSABLE_EMAIL_DOMAINS:
            return False
        referrer_profile = self.table.get_item(
            Key={"PK": f"USER#{lookup['referrer_user_id']}", "SK": "PROFILE"},
            ConsistentRead=True,
        ).get("Item", {})
        review_flags = []
        if network_hash and network_hash == referrer_profile.get("last_login_network_hash"):
            review_flags.append("shared_network")
        now = self._format_datetime(self._now())
        try:
            self.table.put_item(
                Item={
                    "PK": f"USER#{referred_user_id}",
                    "SK": "REFERRAL",
                    "referral_code": code,
                    "referrer_user_id": lookup["referrer_user_id"],
                    "status": "registered",
                    "registered_at": now,
                    **({"review_flags": review_flags} if review_flags else {}),
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                return False
            raise
        self.table.update_item(
            Key={"PK": f"USER#{referred_user_id}", "SK": "PROFILE"},
            UpdateExpression="SET referred_by = if_not_exists(referred_by, :referrer)",
            ExpressionAttributeValues={":referrer": lookup["referrer_user_id"]},
        )
        return True

    def qualify_referral(self, referred_user_id):
        """Qualify once and atomically upgrade the inviter by one tier."""
        referral_key = {"PK": f"USER#{referred_user_id}", "SK": "REFERRAL"}
        for _ in range(4):
            referral = self.table.get_item(
                Key=referral_key,
                ConsistentRead=True,
            ).get("Item")
            if not referral or referral.get("status") != "registered":
                return False

            referrer_id = referral["referrer_user_id"]
            plan = self.get_plan(referrer_id)
            if not plan:
                raise RuntimeError("Referrer profile is unavailable")

            current_tier = plan["plan"]
            current_index = TIER_ORDER.index(current_tier)
            next_tier = TIER_ORDER[min(current_index + 1, len(TIER_ORDER) - 1)]
            current_count = plan["successful_referrals"]
            current_quota_cycle = plan["quota_cycle"]
            now = self._now()
            now_text = self._format_datetime(now)

            referral_values = {
                ":registered": "registered",
                ":qualified": "qualified",
                ":qualified_at": now_text,
                ":rewarded_tier": next_tier,
            }
            profile_values = {
                ":current_plan": current_tier,
                ":expected_count": current_count,
                ":next_count": current_count + 1,
                ":next_quota_cycle": current_quota_cycle + 1,
                ":updated_at": now_text,
            }
            if current_tier == "gold":
                profile_values.update({
                    ":started_at": now_text,
                    ":expires_at": self._format_datetime(now + TIER_DURATION),
                })
                profile_update = (
                    "SET tier_started_at = :started_at, tier_expires_at = :expires_at, "
                    "updated_at = :updated_at, successful_referrals = :next_count, "
                    "quota_cycle = :next_quota_cycle REMOVE stock_limit_override"
                )
            else:
                profile_values.update({
                    ":next_plan": next_tier,
                    ":started_at": now_text,
                    ":expires_at": self._format_datetime(now + TIER_DURATION),
                })
                profile_update = (
                    "SET #plan = :next_plan, tier_started_at = :started_at, "
                    "tier_expires_at = :expires_at, updated_at = :updated_at, "
                    "successful_referrals = :next_count, quota_cycle = :next_quota_cycle "
                    "REMOVE stock_limit_override"
                )

            try:
                self.dynamodb_client.transact_write_items(TransactItems=[
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": referral_key,
                            "UpdateExpression": (
                                "SET #status = :qualified, qualified_at = :qualified_at, "
                                "rewarded_tier = :rewarded_tier"
                            ),
                            "ConditionExpression": "#status = :registered",
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": referral_values,
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.table_name,
                            "Key": {
                                "PK": f"USER#{referrer_id}",
                                "SK": "PROFILE",
                            },
                            "UpdateExpression": profile_update,
                            "ConditionExpression": (
                                "#plan = :current_plan AND successful_referrals = :expected_count"
                            ),
                            "ExpressionAttributeNames": {"#plan": "plan"},
                            "ExpressionAttributeValues": profile_values,
                        }
                    },
                ])
                return True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                    raise
        raise RuntimeError("Referral qualification was busy; please retry")

    def get_plan(self, user_id):
        response = self.table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": "PROFILE"},
            ConsistentRead=True,
        )
        profile = response.get("Item")
        if not profile:
            return None

        profile = self._apply_expired_downgrades(profile)
        tier = profile.get("plan", "free")
        tier_index = TIER_ORDER.index(tier)
        next_tier = TIER_ORDER[tier_index + 1] if tier_index < len(TIER_ORDER) - 1 else None
        configured_limit = TIER_LIMITS[tier]
        effective_limit = profile.get("stock_limit_override", configured_limit)
        return {
            "plan": tier,
            "stock_limit": int(effective_limit) if effective_limit is not None else None,
            "stock_limit_override": (
                int(profile["stock_limit_override"])
                if "stock_limit_override" in profile else None
            ),
            "successful_referrals": int(profile.get("successful_referrals", 0)),
            "quota_cycle": int(profile.get("quota_cycle", 0)),
            "referral_code": profile.get("referral_code") or self._ensure_referral_code(user_id),
            "expires_at": profile.get("tier_expires_at"),
            "next_tier": next_tier,
            "referrals_needed_for_next_tier": 1,
            "referral_action": "renew" if tier == "gold" else "upgrade",
        }

    def get_usage(self, user_id, plan=None):
        plan = plan or self.get_plan(user_id)
        month = self._now().strftime("%Y-%m")
        quota_cycle = plan.get("quota_cycle", 0)
        usage_key = f"USAGE#{month}" if quota_cycle == 0 else f"USAGE#{month}#CYCLE#{quota_cycle}"
        item = self.table.get_item(
            Key={"PK": f"USER#{user_id}", "SK": usage_key},
            ConsistentRead=True,
        ).get("Item", {})
        used = len(item.get("symbols", set()))
        limit = plan["stock_limit"]
        return {
            "usage_month": month,
            "quota_cycle": quota_cycle,
            "stocks_used": used,
            "stocks_remaining": None if limit is None else max(limit - used, 0),
            "used_symbols": sorted(item.get("symbols", set())),
        }

    def record_stock_usage(self, user_id, symbol):
        """Count each distinct stock once per calendar month and enforce the tier limit."""
        plan = self.get_plan(user_id)
        month = self._now().strftime("%Y-%m")
        quota_cycle = plan.get("quota_cycle", 0)
        usage_key = f"USAGE#{month}" if quota_cycle == 0 else f"USAGE#{month}#CYCLE#{quota_cycle}"
        now = self._format_datetime(self._now())
        key = {"PK": f"USER#{user_id}", "SK": usage_key}
        values = {":symbol": {symbol.upper()}, ":updated_at": now}
        kwargs = {
            "Key": key,
            "UpdateExpression": "SET updated_at = :updated_at ADD symbols :symbol",
            "ExpressionAttributeValues": values,
        }
        if plan["stock_limit"] is not None:
            values[":limit"] = plan["stock_limit"]
            values[":single_symbol"] = symbol.upper()
            kwargs["ConditionExpression"] = (
                "attribute_not_exists(symbols) OR contains(symbols, :single_symbol) "
                "OR size(symbols) < :limit"
            )
        try:
            self.table.update_item(**kwargs)
            allowed = True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            allowed = False
        return {"allowed": allowed, **self.get_usage(user_id, plan)}

    def can_access_stock(self, user_id, symbol):
        plan = self.get_plan(user_id)
        usage = self.get_usage(user_id, plan)
        allowed = (
            plan["stock_limit"] is None
            or symbol.upper() in usage["used_symbols"]
            or usage["stocks_remaining"] > 0
        )
        return {"allowed": allowed, **usage}

    def _apply_expired_downgrades(self, profile):
        tier = profile.get("plan", "free")
        expiry = self._parse_datetime(profile.get("tier_expires_at"))
        original_tier = tier
        original_expiry = profile.get("tier_expires_at")
        now = self._now()
        changed = False

        while tier != "free" and expiry and now >= expiry:
            tier = TIER_ORDER[TIER_ORDER.index(tier) - 1]
            expiry = None if tier == "free" else expiry + TIER_DURATION
            changed = True

        if changed:
            profile["plan"] = tier
            profile["quota_cycle"] = int(profile.get("quota_cycle", 0)) + 1
            if expiry:
                profile["tier_expires_at"] = self._format_datetime(expiry)
            else:
                profile.pop("tier_expires_at", None)
            profile["updated_at"] = self._format_datetime(now)

            expression = (
                "SET #plan = :plan, quota_cycle = :quota_cycle, updated_at = :updated_at "
                "REMOVE stock_limit_override"
            )
            values = {
                ":plan": tier,
                ":quota_cycle": profile["quota_cycle"],
                ":updated_at": profile["updated_at"],
            }
            if expiry:
                expression = expression.replace(
                    " REMOVE stock_limit_override",
                    ", tier_expires_at = :expiry REMOVE stock_limit_override",
                )
                values[":expiry"] = profile["tier_expires_at"]
            else:
                expression += ", tier_expires_at"

            values.update({":original_plan": original_tier, ":original_expiry": original_expiry})
            try:
                self.table.update_item(
                    Key={"PK": profile["PK"], "SK": profile["SK"]},
                    UpdateExpression=expression,
                    ConditionExpression=(
                        "#plan = :original_plan AND tier_expires_at = :original_expiry"
                    ),
                    ExpressionAttributeNames={"#plan": "plan"},
                    ExpressionAttributeValues=values,
                )
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
                return self.table.get_item(
                    Key={"PK": profile["PK"], "SK": profile["SK"]},
                    ConsistentRead=True,
                ).get("Item", profile)

        return profile
