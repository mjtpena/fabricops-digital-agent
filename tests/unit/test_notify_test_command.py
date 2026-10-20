"""``orqentis notify-test``: one test notification through the configured notifier, never printing the URL."""

from __future__ import annotations

import pytest

from orqentis import cli
from orqentis.notifications import factory
from orqentis.notifications.model import NotificationKind
from tests.support.notifications import RecordingNotifier, permanent

pytestmark = pytest.mark.security

URL = "https://prod-12.westus.logic.azure.com/workflows/abc/triggers/manual/paths/invoke?sig=SUPERSECRETSIGNATURE"


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    monkeypatch.setenv("ORQENTIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ORQENTIS_NOTIFIER", "teams_workflows")
    monkeypatch.setenv("ORQENTIS_NOTIFY_TEAMS_WEBHOOK_URL", URL)
    monkeypatch.setenv("ORQENTIS_PUBLIC_BASE_URL", "https://fabricops.example.com")
    monkeypatch.setenv("ORQENTIS_UI_SIGN_IN_PATH", "/.auth/login/aad")


def test_without_a_notifier_there_is_nothing_to_test(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["notify-test"]) == 2
    assert "nothing to test" in capsys.readouterr().out


def test_a_delivered_test_prints_the_host_and_the_link_but_never_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(monkeypatch, tmp_path)
    notifier = RecordingNotifier()
    monkeypatch.setattr(factory, "build_notifier", lambda settings: notifier)
    assert cli.main(["notify-test"]) == 0
    output = capsys.readouterr().out
    assert "delivered to hooks.example.test (HTTP 202)" in output
    assert "https://fabricops.example.com/.auth/login/aad?post_login_redirect_uri=%2Fui%2Fincidents" in output
    (message,) = notifier.sent
    assert message.kind is NotificationKind.TEST and message.headline == "FabricOps test notification"
    assert dict(message.facts)["Destination"] == "hooks.example.test"
    assert "SUPERSECRETSIGNATURE" not in output


def test_a_refused_test_exits_non_zero_with_the_code_and_not_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        factory, "build_notifier", lambda settings: RecordingNotifier([permanent("DESTINATION_NOT_PUBLIC")])
    )
    assert cli.main(["notify-test"]) == 1
    output = capsys.readouterr().out
    assert "not delivered" in output and "DESTINATION_NOT_PUBLIC" in output and "SUPERSECRETSIGNATURE" not in output
