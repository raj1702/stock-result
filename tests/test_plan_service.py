from concurrent.futures import ThreadPoolExecutor
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
        monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-that-stays-stable")
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


def user(user_id, email=None):
    return {"sub": user_id, "email": email or f"{user_id}@example.com"}


def register_referred_user(service, referrer_id, referred_id):
    referrer = service.ensure_user(user(referrer_id))
    service.ensure_user(user(referred_id))
    assert service.register_referral(
        referred_id,
        referrer["referral_code"],
        email=f"{referred_id}@example.com",
    )


def test_free_plan_counts_distinct_stocks_and_enforces_limit(plan_service):
    plan_service.ensure_user(user("owner"))
    for index in range(10):
        assert plan_service.record_stock_usage("owner", f"STOCK{index}")["allowed"]
    assert plan_service.record_stock_usage("owner", "STOCK0")["allowed"]
    blocked = plan_service.record_stock_usage("owner", "STOCK10")
    assert not blocked["allowed"]
    assert blocked["stocks_used"] == 10
    assert blocked["stocks_remaining"] == 0


def test_usage_resets_at_start_of_new_ist_month(plan_service):
    plan_service._now = lambda: datetime(2026, 9, 30, 23, 59, tzinfo=INDIA_TIMEZONE)
    plan_service.ensure_user(user("owner"))
    plan_service.record_stock_usage("owner", "RELIANCE")
    assert plan_service.get_usage("owner")["stocks_used"] == 1

    plan_service._now = lambda: datetime(2026, 10, 1, 0, 1, tzinfo=INDIA_TIMEZONE)
    assert plan_service.get_usage("owner")["stocks_used"] == 0
    assert plan_service.get_usage("owner")["stocks_remaining"] == 10


def test_qualification_is_idempotent(plan_service):
    register_referred_user(plan_service, "inviter", "friend")
    assert plan_service.qualify_referral("friend")
    assert not plan_service.qualify_referral("friend")
    plan = plan_service.get_plan("inviter")
    assert plan["plan"] == "bronze"
    assert plan["successful_referrals"] == 1


def test_upgrade_starts_with_fresh_tier_quota(plan_service):
    register_referred_user(plan_service, "inviter", "friend")
    for index in range(10):
        assert plan_service.record_stock_usage("inviter", f"STOCK{index}")["allowed"]
    assert plan_service.get_usage("inviter")["stocks_remaining"] == 0

    assert plan_service.qualify_referral("friend")
    plan = plan_service.get_plan("inviter")
    usage = plan_service.get_usage("inviter", plan)
    assert plan["plan"] == "bronze"
    assert plan["quota_cycle"] == 1
    assert usage["stocks_used"] == 0
    assert usage["stocks_remaining"] == 25


def test_parallel_qualification_only_rewards_once(plan_service):
    register_referred_user(plan_service, "inviter", "friend")
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _index: plan_service.qualify_referral("friend"), range(4)))
    assert results.count(True) == 1
    assert plan_service.get_plan("inviter")["successful_referrals"] == 1


def test_two_parallel_referrals_advance_two_tiers(plan_service):
    register_referred_user(plan_service, "inviter", "friend-one")
    inviter = plan_service.get_plan("inviter")
    plan_service.ensure_user(user("friend-two"))
    assert plan_service.register_referral(
        "friend-two",
        inviter["referral_code"],
        email="friend-two@example.com",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            plan_service.qualify_referral,
            ("friend-one", "friend-two"),
        ))
    assert results.count(True) == 2
    plan = plan_service.get_plan("inviter")
    assert plan["plan"] == "silver"
    assert plan["successful_referrals"] == 2


def test_gold_referral_renews_gold_for_thirty_days(plan_service):
    now = datetime(2026, 9, 5, 12, 0, tzinfo=INDIA_TIMEZONE)
    plan_service._now = lambda: now
    register_referred_user(plan_service, "inviter", "friend")
    plan_service.table.update_item(
        Key={"PK": "USER#inviter", "SK": "PROFILE"},
        UpdateExpression="SET #plan = :gold, tier_expires_at = :old",
        ExpressionAttributeNames={"#plan": "plan"},
        ExpressionAttributeValues={
            ":gold": "gold",
            ":old": (now + timedelta(days=2)).isoformat(),
        },
    )
    assert plan_service.qualify_referral("friend")
    plan = plan_service.get_plan("inviter")
    assert plan["plan"] == "gold"
    assert datetime.fromisoformat(plan["expires_at"]) == now + timedelta(days=30)


def test_expired_tiers_step_down_every_thirty_days(plan_service):
    now = datetime(2026, 9, 5, 12, 0, tzinfo=INDIA_TIMEZONE)
    plan_service._now = lambda: now
    plan_service.ensure_user(user("owner"))
    plan_service.table.update_item(
        Key={"PK": "USER#owner", "SK": "PROFILE"},
        UpdateExpression="SET #plan = :gold, tier_expires_at = :expiry",
        ExpressionAttributeNames={"#plan": "plan"},
        ExpressionAttributeValues={
            ":gold": "gold",
            ":expiry": (now - timedelta(days=31)).isoformat(),
        },
    )
    plan = plan_service.get_plan("owner")
    assert plan["plan"] == "bronze"
    assert plan["quota_cycle"] == 1
    assert datetime.fromisoformat(plan["expires_at"]) == now + timedelta(days=29)


def test_disposable_email_cannot_register_referral(plan_service):
    inviter = plan_service.ensure_user(user("inviter"))
    plan_service.ensure_user(user("friend", "friend@mailinator.com"))
    assert not plan_service.register_referral(
        "friend",
        inviter["referral_code"],
        email="friend@mailinator.com",
    )
