#!/usr/bin/env python3
"""Safely grant plans or reset quota for one Cognito user."""

import argparse
import os
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
IST = ZoneInfo("Asia/Kolkata")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-sub", required=True, help="Cognito sub, not an email address")
    parser.add_argument("--tier", choices=("free", "bronze", "silver", "gold"))
    parser.add_argument("--quota", type=int, help="Optional custom stock quota; must be positive")
    parser.add_argument("--days", type=int, default=30, help="Validity for a paid-tier grant")
    parser.add_argument("--reset-quota", action="store_true", help="Start a fresh quota cycle")
    parser.add_argument("--reason", required=True, help="Reason stored in the audit record")
    parser.add_argument("--apply", action="store_true", help="Actually write; otherwise dry-run")
    args = parser.parse_args()
    if not any((args.tier, args.quota is not None, args.reset_quota)):
        parser.error("choose --tier, --quota, or --reset-quota")
    if args.quota is not None and args.quota <= 0:
        parser.error("--quota must be positive")
    if args.days <= 0:
        parser.error("--days must be positive")
    return args


def main():
    args = parse_args()
    region = os.getenv("AWS_REGION", "ap-south-1")
    table_name = os.getenv("DYNAMODB_TABLE_NAME", "earnings-assistant")
    now = datetime.now(IST)
    summary = {
        "user_sub": args.user_sub,
        "tier": args.tier,
        "custom_quota": args.quota,
        "reset_quota": args.reset_quota or bool(args.tier) or args.quota is not None,
        "valid_days": args.days if args.tier and args.tier != "free" else None,
        "reason": args.reason,
        "table": table_name,
        "region": region,
    }
    print("Planned admin action:", summary)
    if not args.apply:
        print("Dry run only. Add --apply after checking the user sub and action.")
        return 0

    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    key = {"PK": f"USER#{args.user_sub}", "SK": "PROFILE"}
    if "Item" not in table.get_item(Key=key, ConsistentRead=True):
        print("User profile not found; no changes made.", file=sys.stderr)
        return 2

    names = {}
    values = {":one": 1, ":updated": now.isoformat()}
    set_parts = ["updated_at = :updated"]
    remove_parts = []
    if args.tier:
        names["#plan"] = "plan"
        values[":tier"] = args.tier
        set_parts.extend(["#plan = :tier", "tier_started_at = :updated"])
        if args.tier == "free":
            remove_parts.append("tier_expires_at")
        else:
            values[":expiry"] = (now + timedelta(days=args.days)).isoformat()
            set_parts.append("tier_expires_at = :expiry")
    if args.quota is not None:
        values[":quota"] = args.quota
        set_parts.append("stock_limit_override = :quota")
    elif args.tier:
        remove_parts.append("stock_limit_override")

    expression = f"SET {', '.join(set_parts)} ADD quota_cycle :one"
    if remove_parts:
        expression += f" REMOVE {', '.join(remove_parts)}"
    update = {
        "Key": key,
        "UpdateExpression": expression,
        "ExpressionAttributeValues": values,
        "ConditionExpression": "attribute_exists(PK)",
    }
    if names:
        update["ExpressionAttributeNames"] = names
    table.update_item(**update)
    table.put_item(Item={
        "PK": f"USER#{args.user_sub}",
        "SK": f"ADMIN#{now.isoformat()}#{uuid.uuid4().hex[:8]}",
        "action": "manual_plan_adjustment",
        "details": summary,
        "created_at": now.isoformat(),
    })
    print("Admin action applied and audited.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
