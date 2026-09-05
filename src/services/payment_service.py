import os
import uuid
from datetime import datetime

import boto3
import razorpay
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")
PAID_PLANS = {
    "bronze": {"amount": 2500, "price_rupees": 25, "valid_days": 30},
    "silver": {"amount": 5000, "price_rupees": 50, "valid_days": 30},
    "gold": {"amount": 25000, "price_rupees": 250, "valid_days": 30},
}


class PaymentService:
    def __init__(self):
        self.key_id = os.getenv("RAZORPAY_KEY_ID", "")
        self.key_secret = os.getenv("RAZORPAY_KEY_SECRET", "")
        self.webhook_secret = os.getenv("RAZORPAY_WEBHOOK_SECRET", "")
        self.mode = os.getenv("RAZORPAY_MODE", "test").lower()
        table_name = os.getenv("DYNAMODB_TABLE_NAME", "earnings-assistant")
        region = os.getenv("AWS_REGION", "ap-south-1")
        self.table = boto3.resource("dynamodb", region_name=region).Table(table_name)
        self.client = (
            razorpay.Client(auth=(self.key_id, self.key_secret))
            if self.key_id and self.key_secret else None
        )

    @property
    def ready(self):
        return bool(self.client)

    def public_plans(self):
        return {
            tier: {
                "price_rupees": details["price_rupees"],
                "valid_days": details["valid_days"],
            }
            for tier, details in PAID_PLANS.items()
        }

    def create_order(self, user, tier):
        if not self.ready:
            raise RuntimeError("Razorpay is not configured")
        tier = (tier or "").lower()
        plan = PAID_PLANS.get(tier)
        if not plan:
            raise ValueError("Unknown paid plan")
        receipt = f"ea_{uuid.uuid4().hex[:24]}"
        order = self.client.order.create({
            "amount": plan["amount"],
            "currency": "INR",
            "receipt": receipt,
            "notes": {"tier": tier, "user_sub": user["sub"]},
        })
        now = datetime.now(IST).isoformat()
        self.table.put_item(
            Item={
                "PK": f"PAYMENT#{order['id']}",
                "SK": "ORDER",
                "user_id": user["sub"],
                "email": user.get("email") or "",
                "tier": tier,
                "amount": plan["amount"],
                "currency": "INR",
                "status": "created",
                "receipt": receipt,
                "mode": self.mode,
                "created_at": now,
                "updated_at": now,
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
        return {
            "key_id": self.key_id,
            "order_id": order["id"],
            "amount": plan["amount"],
            "currency": "INR",
            "tier": tier,
            "price_rupees": plan["price_rupees"],
            "name": "NSE · BSE Earnings Decision Assistant",
            "description": f"{tier.title()} plan · 30 days",
            "prefill": {"email": user.get("email") or ""},
        }

    def verify_checkout_payment(self, user_id, payload):
        required = ("razorpay_order_id", "razorpay_payment_id", "razorpay_signature")
        if not all(payload.get(field) for field in required):
            raise ValueError("Incomplete payment response")
        order = self.get_order(payload["razorpay_order_id"])
        if not order or order.get("user_id") != user_id:
            raise ValueError("Payment order does not belong to this user")
        self.client.utility.verify_payment_signature({field: payload[field] for field in required})
        payment = self.client.payment.fetch(payload["razorpay_payment_id"])
        self._validate_captured_payment(order, payment)
        return order, payment

    def get_order(self, order_id):
        return self.table.get_item(
            Key={"PK": f"PAYMENT#{order_id}", "SK": "ORDER"},
            ConsistentRead=True,
        ).get("Item")

    @staticmethod
    def _validate_captured_payment(order, payment):
        if payment.get("status") != "captured":
            raise ValueError("Payment has not been captured")
        if payment.get("order_id") != order["PK"].removeprefix("PAYMENT#"):
            raise ValueError("Payment order mismatch")
        if int(payment.get("amount", -1)) != int(order["amount"]):
            raise ValueError("Payment amount mismatch")
        if payment.get("currency") != order["currency"]:
            raise ValueError("Payment currency mismatch")

    def verify_webhook(self, raw_body, signature):
        if not self.webhook_secret:
            raise RuntimeError("Razorpay webhook secret is not configured")
        self.client.utility.verify_webhook_signature(
            raw_body.decode("utf-8"), signature, self.webhook_secret
        )

    def captured_payment_from_webhook(self, event):
        if event.get("event") != "payment.captured":
            return None, None
        payment = event.get("payload", {}).get("payment", {}).get("entity", {})
        order = self.get_order(payment.get("order_id", ""))
        if not order:
            return None, None
        self._validate_captured_payment(order, payment)
        return order, payment
