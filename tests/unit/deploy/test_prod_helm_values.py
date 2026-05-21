"""T094 — production Helm values are present and shaped for public ingress + secrets."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_prod_values_define_public_ingress_and_tls() -> None:
    values = yaml.safe_load(Path("deploy/helm/alertbot/values-prod.yaml").read_text())

    assert values["ingress"]["enabled"] is True
    assert values["ingress"]["hosts"][0]["host"] == "alertbot.hashkeychain.net"
    assert values["ingress"]["tls"][0]["secretName"] == "alertbot-prod-tls"


def test_prod_values_reference_sealed_secret_keys_without_secret_values() -> None:
    values_text = Path("deploy/helm/alertbot/values-prod.yaml").read_text()
    values = yaml.safe_load(values_text)

    for key in [
        "larkAppSecret",
        "larkEncryptKey",
        "larkVerifyToken",
        "fdWebhookSecret",
        "fdApiToken",
        "amToken",
        "databaseUrl",
    ]:
        assert key in values["secrets"]
    assert "actual-" not in values_text.lower()
    assert "password" not in values_text.lower()
