import json
import os

import boto3


SUPPORTED_SECRET_KEYS = {
    "FLASK_SECRET_KEY",
    "COGNITO_CLIENT_SECRET",
    "UPSTOX_ACCESS_TOKEN",
    "RAZORPAY_KEY_ID",
    "RAZORPAY_KEY_SECRET",
    "RAZORPAY_WEBHOOK_SECRET",
}


def load_runtime_secrets():
    """Load production credentials once, before application services start."""
    secret_id = os.getenv("AWS_SECRETS_MANAGER_SECRET_ID", "").strip()
    if not secret_id:
        return False

    region = os.getenv("AWS_REGION", "ap-south-1")
    response = boto3.client("secretsmanager", region_name=region).get_secret_value(
        SecretId=secret_id
    )
    try:
        secret_values = json.loads(response["SecretString"])
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Secrets Manager value must be a JSON object") from exc
    if not isinstance(secret_values, dict):
        raise RuntimeError("Secrets Manager value must be a JSON object")

    missing = sorted(key for key in SUPPORTED_SECRET_KEYS if not secret_values.get(key))
    if missing:
        raise RuntimeError(f"Secrets Manager is missing required keys: {', '.join(missing)}")
    for key in SUPPORTED_SECRET_KEYS:
        os.environ[key] = str(secret_values[key])
    return True
