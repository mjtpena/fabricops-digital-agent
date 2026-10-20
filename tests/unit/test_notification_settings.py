"""Settings, .env.example, Bicep and the docs agree on notifications and the web UI; webhook URLs stay secrets."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from orqentis.config.settings import DEFAULT_TEAMS_WEBHOOK_HOSTS, NotifierKind, Settings
from tests.conftest import REPO, make_settings

pytestmark = pytest.mark.security

SIGNATURE = "SUPERSECRETSIGNATURE0123456789"
TEAMS_URL = f"https://prod-12.westus.logic.azure.com:443/workflows/abc/triggers/manual/paths/invoke?api-version=1&sig={SIGNATURE}"
BASE = "https://fabricops.example.com"
NEW_SETTINGS = {
    "public_base_url",
    "notifier",
    "notify_teams_webhook_url",
    "notify_webhook_url",
    "notify_webhook_signing_secret",
    "notify_allowed_hosts",
    "notify_timeout_seconds",
    "notify_max_attempts",
    "notify_backoff_base_seconds",
    "notify_backoff_max_seconds",
    "notify_outbox_size",
    "ui_enabled",
    "ui_sign_in_path",
    "ui_csrf_secret",
}


def settings(tmp_path: Path, **values: Any) -> Settings:
    return make_settings(tmp_path, **values)


def text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------- defaults


def test_by_default_nothing_is_sent_and_the_ui_is_on(tmp_path: Path) -> None:
    s = settings(tmp_path)
    assert s.notifier is NotifierKind.NULL and s.ui_enabled and s.public_base_url is None
    assert s.notify_teams_webhook_url is None and s.notify_webhook_url is None
    assert s.notify_allowed_host_patterns == ()


def test_the_new_settings_are_the_ones_these_tests_know_about() -> None:
    declared = {
        n for n in Settings.model_fields if n.startswith(("notify_", "ui_")) or n in {"public_base_url", "notifier"}
    }
    assert declared == NEW_SETTINGS


# ---------------------------------------------------------------------------------------- the notifier


def test_the_teams_notifier_needs_its_secret_url_and_the_public_base_url(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="ORQENTIS_NOTIFY_TEAMS_WEBHOOK_URL"):
        settings(tmp_path, notifier="teams_workflows", public_base_url=BASE)
    with pytest.raises(ValidationError, match="ORQENTIS_PUBLIC_BASE_URL"):
        settings(tmp_path, notifier="teams_workflows", notify_teams_webhook_url=TEAMS_URL)
    ok = settings(tmp_path, notifier="teams_workflows", notify_teams_webhook_url=TEAMS_URL, public_base_url=BASE)
    assert ok.notifier is NotifierKind.TEAMS_WORKFLOWS
    assert ok.notify_allowed_host_patterns == DEFAULT_TEAMS_WEBHOOK_HOSTS


def test_the_generic_webhook_needs_its_url_and_signing_is_optional(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="ORQENTIS_NOTIFY_WEBHOOK_URL"):
        settings(tmp_path, notifier="webhook", public_base_url=BASE)
    plain = settings(
        tmp_path, notifier="webhook", notify_webhook_url="https://hooks.example.com/in", public_base_url=BASE
    )
    assert plain.notify_webhook_signing_secret is None and plain.notify_allowed_host_patterns == ()
    signed = settings(
        tmp_path,
        notifier="webhook",
        notify_webhook_url="https://hooks.example.com/in",
        notify_webhook_signing_secret="k",
        public_base_url=BASE,
    )
    assert signed.notify_webhook_signing_secret is not None


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://prod-12.westus.logic.azure.com/x?sig=" + SIGNATURE, "https"),
        ("https://user:pw@prod-12.westus.logic.azure.com/x?sig=" + SIGNATURE, "credentials"),
        ("https://10.0.0.5/x?sig=" + SIGNATURE, "DNS name"),
        ("https://169.254.169.254/latest?sig=" + SIGNATURE, "DNS name"),
        ("https://evil.example.com/x?sig=" + SIGNATURE, "not in the allowed hosts"),
        ("https://logic.azure.com.evil.example/x?sig=" + SIGNATURE, "not in the allowed hosts"),
        ("not a url " + SIGNATURE, "forbidden characters"),
    ],
)
def test_an_unacceptable_webhook_url_stops_the_start_without_echoing_the_secret(
    tmp_path: Path, url: str, why: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        settings(tmp_path, notifier="teams_workflows", notify_teams_webhook_url=url, public_base_url=BASE)
    message = str(caught.value)
    assert why in message
    assert SIGNATURE not in message and "sig=" not in message  # the URL is a secret: not in the error either


def test_the_allow_list_replaces_the_default(tmp_path: Path) -> None:
    custom = settings(
        tmp_path,
        notifier="teams_workflows",
        notify_teams_webhook_url="https://flows.contoso.com/x?sig=1234567890",
        notify_allowed_hosts=" Flows.Contoso.com , *.logic.azure.com ,, ",
        public_base_url=BASE,
    )
    assert custom.notify_allowed_host_patterns == ("flows.contoso.com", "*.logic.azure.com")
    with pytest.raises(ValidationError, match="not in the allowed hosts"):
        settings(
            tmp_path,
            notifier="webhook",
            notify_webhook_url="https://x.example/y",
            notify_allowed_hosts="a.example",
            public_base_url=BASE,
        )


def test_webhook_secrets_are_masked_wherever_a_settings_object_is_printed(tmp_path: Path) -> None:
    s = settings(
        tmp_path,
        notifier="webhook",
        notify_webhook_url=f"https://hooks.example.com/in?sig={SIGNATURE}",
        notify_webhook_signing_secret="hmac-key-value",
        ui_csrf_secret="csrf-key-value",
        public_base_url=BASE,
    )
    rendered = repr(s) + str(s) + s.model_dump_json() + str(s.model_dump())
    for secret in (SIGNATURE, "hmac-key-value", "csrf-key-value"):
        assert secret not in rendered


def test_retry_settings_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="BACKOFF_MAX"):
        settings(tmp_path, notify_backoff_base_seconds=30, notify_backoff_max_seconds=10)
    for bad in ({"notify_max_attempts": 0}, {"notify_outbox_size": 0}, {"notify_timeout_seconds": 0}):
        with pytest.raises(ValidationError):
            settings(tmp_path, **bad)


def test_the_notifier_is_read_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORQENTIS_NOTIFIER", "teams_workflows")
    monkeypatch.setenv("ORQENTIS_NOTIFY_TEAMS_WEBHOOK_URL", TEAMS_URL)
    monkeypatch.setenv("ORQENTIS_PUBLIC_BASE_URL", BASE)
    s = Settings(_env_file=None, data_dir=tmp_path)  # type: ignore[call-arg]
    assert s.notifier is NotifierKind.TEAMS_WORKFLOWS and s.public_base_url == BASE
    assert s.notify_teams_webhook_url is not None and s.notify_teams_webhook_url.get_secret_value() == TEAMS_URL


# ----------------------------------------------------------------------------- base URL and sign-in path


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Fabricops.Example.com", "https://fabricops.example.com"),
        ("https://fabricops.example.com/", "https://fabricops.example.com"),
        ("https://fabricops.example.com:8443", "https://fabricops.example.com:8443"),
        ("http://localhost:8000", "http://localhost:8000"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("", None),
        ("   ", None),
    ],
)
def test_public_base_url_is_normalised_to_an_origin(tmp_path: Path, raw: str, expected: str | None) -> None:
    assert settings(tmp_path, public_base_url=raw).public_base_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "http://fabricops.example.com",
        "https://fabricops.example.com/ui",
        "https://fabricops.example.com?x=1",
        "https://user@fabricops.example.com",
        "fabricops.example.com",
        "ftp://fabricops.example.com",
        "https:///x",
    ],
)
def test_public_base_url_rejects_anything_that_is_not_an_https_origin(tmp_path: Path, raw: str) -> None:
    with pytest.raises(ValidationError):
        settings(tmp_path, public_base_url=raw)


@pytest.mark.parametrize("raw", ["/.auth/login/aad", "/login", "  /.auth/login/aad  "])
def test_sign_in_path_accepts_absolute_paths(tmp_path: Path, raw: str) -> None:
    assert settings(tmp_path, ui_sign_in_path=raw).ui_sign_in_path == raw.strip()


@pytest.mark.parametrize(
    "raw", ["https://evil.example/login", "//evil.example/login", "login", "/a?b=1", "/a#b", "/a\\b"]
)
def test_sign_in_path_rejects_anything_that_could_leave_the_site(tmp_path: Path, raw: str) -> None:
    with pytest.raises(ValidationError):
        settings(tmp_path, ui_sign_in_path=raw)


# ------------------------------------------------------------------------------------ files and docs


def test_every_new_setting_is_documented_in_env_example_with_no_value_for_secrets() -> None:
    example = text(".env.example")
    for name in NEW_SETTINGS:
        assert f"ORQENTIS_{name.upper()}" in example, name
    # The webhook URLs, the signing key and the CSRF key appear only as commented, empty placeholders.
    for name in ("notify_teams_webhook_url", "notify_webhook_url", "notify_webhook_signing_secret", "ui_csrf_secret"):
        lines = [line for line in example.splitlines() if f"ORQENTIS_{name.upper()}=" in line]
        assert lines, name
        for line in lines:
            assert line.startswith("# "), line
            assert line.split("=", 1)[1].split("#", 1)[0].strip() == "", line


def test_bicep_has_no_parameter_that_takes_a_webhook_url_value() -> None:
    main = text("infra/bicep/main.bicep")
    params = dict(re.findall(r"^param (\w+) (\w+)", main, flags=re.MULTILINE))
    webhook_params = {name for name in params if name.lower().startswith(("notify", "uisignin", "uicsrf"))}
    assert webhook_params >= {
        "notifyTeamsWebhookSecretUri",
        "notifyWebhookSecretUri",
        "notifyWebhookSigningSecretUri",
        "notifyAllowedHosts",
        "uiSignInClientSecretUri",
        "uiCsrfSecretUri",
    }
    for name in webhook_params - {"notifyAllowedHosts"}:
        assert name.endswith("SecretUri"), f"{name} must be a Key Vault secret URI parameter, never a value"
    assert not [n for n in params if re.search(r"(?i)webhook.*url$", n) and not n.endswith("SecretUri")]


def test_bicep_exposes_the_secret_urls_only_as_container_app_secret_references() -> None:
    main = text("infra/bicep/main.bicep")
    assert "map(envVaultReferences, s => { name: s.envName, secretRef: s.secretName })" in main
    assert "keyVaultUrl: s.uri, identity: readerIdentity.id" in main
    for variable in (
        "ORQENTIS_NOTIFY_TEAMS_WEBHOOK_URL",
        "ORQENTIS_NOTIFY_WEBHOOK_URL",
        "ORQENTIS_NOTIFY_WEBHOOK_SIGNING_SECRET",
        "ORQENTIS_UI_CSRF_SECRET",
    ):
        assert f"envName: '{variable}'" in main
        assert not re.search(rf"name: '{variable}',\s*value:", main), f"{variable} must be a secretRef, never a value"
    # The reader identity may read exactly these secrets (the same per-secret role assignment as the Fabric credential).
    assert "map(configuredVaultReferences, s => length(split(s.uri, '/')) > 4" in main
    assert "module notificationInputCheck 'modules/input-check.bicep'" in main
    assert "notificationInputCheck" in main.split("dependsOn: [")[-1]


def test_bicep_configures_browser_sign_in_with_a_key_vault_client_secret_and_no_token_store() -> None:
    main = text("infra/bicep/main.bicep")
    assert "clientSecretSettingName: uiSignInEnabled ? uiSignInSecretName : null" in main
    assert "unauthenticatedClientAction: 'Return401'" in main  # API semantics unchanged: no token, no service
    assert "tokenStore: {\n            enabled: false" in main.replace("\r\n", "\n")
    assert "allowedExternalRedirectUrls: []" in main


def test_every_new_bicep_parameter_is_documented_in_the_infra_readme() -> None:
    readme = text("infra/bicep/README.md")
    for param in (
        "notifierKind",
        "notifyTeamsWebhookSecretUri",
        "notifyWebhookSecretUri",
        "notifyWebhookSigningSecretUri",
        "notifyAllowedHosts",
        "publicBaseUrl",
        "uiEnabled",
        "uiSignInClientSecretUri",
        "uiCsrfSecretUri",
    ):
        assert f"`{param}`" in readme, param
    assert "federated" in readme and "OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID" in readme  # the choice is explained


def test_the_teams_guide_the_adr_and_the_threat_model_exist_and_are_linked() -> None:
    assert "ADR-0018" in text("docs/adr/0018-notifications-and-web-ui.md")
    assert "0018-notifications-and-web-ui.md" in text("docs/adr/README.md")
    guide = text("docs/teams-notifications.md")
    assert "When a Teams webhook request is received" in guide and "Key Vault" in guide
    assert "teams-notifications.md" in text("README.md") and "teams-notifications.md" in text("infra/bicep/README.md")
    threat_model = text("docs/threat-model.md")
    for threat in ("SSRF", "CSRF", "XSS", "notification"):
        assert threat in threat_model, threat


def test_the_template_files_are_inside_the_package_so_the_wheel_and_the_image_carry_them() -> None:
    package = REPO / "src" / "orqentis" / "ui"
    assert (package / "static" / "ui.css").is_file() and len(list((package / "templates").glob("*.html"))) >= 8
    assert "jinja2" in text("pyproject.toml").lower()
