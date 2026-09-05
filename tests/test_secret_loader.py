import json

from src.services import secret_loader


class FakeSecretsManager:
    def get_secret_value(self, SecretId):
        assert SecretId == "earnings-assistant/prod/app"
        return {"SecretString": json.dumps({
            key: f"value-for-{key.lower()}"
            for key in secret_loader.SUPPORTED_SECRET_KEYS
        })}


def test_loader_overrides_server_environment(monkeypatch):
    monkeypatch.setenv("AWS_SECRETS_MANAGER_SECRET_ID", "earnings-assistant/prod/app")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "old-server-value")
    monkeypatch.setattr(
        secret_loader.boto3,
        "client",
        lambda service, region_name: FakeSecretsManager(),
    )

    assert secret_loader.load_runtime_secrets()
    assert secret_loader.os.environ["RAZORPAY_KEY_SECRET"] == "value-for-razorpay_key_secret"


def test_loader_is_disabled_for_local_env(monkeypatch):
    monkeypatch.delenv("AWS_SECRETS_MANAGER_SECRET_ID", raising=False)
    assert not secret_loader.load_runtime_secrets()
