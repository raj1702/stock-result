from datetime import datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from src.services.plan_service import INDIA_TIMEZONE, PlanService


@pytest.fixture
def plan_service(monkeypatch):
    with mock_aws():
        monkeypatch.setenv("AWS_REGION", "ap-south-1")
        monkeypatch.setenv("DYNAMODB_TABLE_NAME", "earnings-assistant")
        dynamodb = boto3.resource("dynamodb", region_name="ap-south-1")
        dynamodb.create_table(
            TableName="earnings-assistant",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield PlanService()


def user(user_id):
    return {"sub": user_id, "email": f"{user_id}@example.com"}


def payment_order(service, user_id, tier, order_id="order_1"):
    service.table.put_item(Item={
        "PK": f"PAYMENT#{order_id}", "SK": "ORDER", "user_id": user_id,
        "tier": tier, "status": "created", "amount": 2500, "currency": "INR",
    })
    return service.table.get_item(
        Key={"PK": f"PAYMENT#{order_id}", "SK": "ORDER"}
    )["Item"]


def test_free_plan_counts_distinct_stocks_and_enforces_limit(plan_service):
    plan_service.ensure_user(user("owner"))
    for index in range(10):
        assert plan_service.record_stock_usage("owner", f"STOCK{index}")["allowed"]
    assert plan_service.record_stock_usage("owner", "STOCK0")["allowed"]
    blocked = plan_service.record_stock_usage("owner", "STOCK10")
    assert not blocked["allowed"]
    assert blocked["stocks_remaining"] == 0


def test_paid_upgrade_starts_with_fresh_quota(plan_service):
    plan_service.ensure_user(user("owner"))
    plan_service.record_stock_usage("owner", "RELIANCE")
    plan = plan_service.activate_paid_plan(
        payment_order(plan_service, "owner", "bronze"), {"id": "pay_1"}
    )
    usage = plan_service.get_usage("owner", plan)
    assert plan["plan"] == "bronze"
    assert usage["stocks_used"] == 0
    assert usage["stocks_remaining"] == 25


def test_same_tier_renewal_extends_existing_expiry(plan_service):
    now = datetime(2026, 9, 5, 12, 0, tzinfo=INDIA_TIMEZONE)
    plan_service._now = lambda: now
    plan_service.ensure_user(user("owner"))
    first = plan_service.activate_paid_plan(
        payment_order(plan_service, "owner", "bronze", "order_1"), {"id": "pay_1"}
    )
    second = plan_service.activate_paid_plan(
        payment_order(plan_service, "owner", "bronze", "order_2"), {"id": "pay_2"}
    )
    assert datetime.fromisoformat(second["expires_at"]) == now + timedelta(days=60)
    assert first["quota_cycle"] + 1 == second["quota_cycle"]


def test_paid_activation_is_idempotent(plan_service):
    plan_service.ensure_user(user("owner"))
    order = payment_order(plan_service, "owner", "silver")
    first = plan_service.activate_paid_plan(order, {"id": "pay_1"})
    second = plan_service.activate_paid_plan(order, {"id": "pay_1"})
    assert first["quota_cycle"] == second["quota_cycle"]


def test_expired_tiers_step_down_every_thirty_days(plan_service):
    now = datetime(2026, 9, 5, 12, 0, tzinfo=INDIA_TIMEZONE)
    plan_service._now = lambda: now
    plan_service.ensure_user(user("owner"))
    plan_service.table.update_item(
        Key={"PK": "USER#owner", "SK": "PROFILE"},
        UpdateExpression="SET #plan = :gold, tier_expires_at = :expiry",
        ExpressionAttributeNames={"#plan": "plan"},
        ExpressionAttributeValues={
            ":gold": "gold", ":expiry": (now - timedelta(days=31)).isoformat(),
        },
    )
    plan = plan_service.get_plan("owner")
    assert plan["plan"] == "bronze"
    assert datetime.fromisoformat(plan["expires_at"]) == now + timedelta(days=29)
