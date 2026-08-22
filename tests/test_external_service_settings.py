import json

import pytest

from arxiv_updater import external_services
from arxiv_updater.config import get_settings
from arxiv_updater.external_services import CREDENTIAL_SERVICE_NAME, CredentialStoreError


class FakeCredentialBackend:
    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}

    def get_password(self, service_name: str, username: str) -> str | None:
        return self.passwords.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        self.passwords[(service_name, username)] = password


@pytest.fixture()
def credential_backend(monkeypatch):
    backend = FakeCredentialBackend()
    monkeypatch.setattr(external_services, "_system_backend", lambda: backend)
    get_settings.cache_clear()
    yield backend
    get_settings.cache_clear()


def test_settings_renders_ios_switches_without_returning_secrets(
    app_client, credential_backend
):
    client, _, _ = app_client

    response = client.get("/settings")

    assert response.status_code == 200
    assert response.text.count('class="ios-switch"') == 10
    assert response.text.count('data-service-toggle') == 2
    assert response.text.count("data-auto-submit") == 8
    assert 'onchange="this.form.requestSubmit()"' not in response.text
    assert response.text.count('type="password"') == 2
    assert "Windows 凭据管理器或 macOS Keychain" in response.text
    assert "value=\"test-secret\"" not in response.text


def test_deepseek_switch_saves_disables_and_clears_credential(
    app_client, credential_backend
):
    client, _, _ = app_client

    response = client.post(
        "/settings/services/deepseek",
        data={"enabled": "on", "api_key": "test-secret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert get_settings().deepseek_api_key == "test-secret"
    page = client.get("/settings")
    assert "test-secret" not in page.text
    assert "已启用" in page.text

    response = client.post(
        "/settings/services/deepseek",
        data={"api_key": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert get_settings().deepseek_api_key == ""
    payload = json.loads(
        credential_backend.passwords[(CREDENTIAL_SERVICE_NAME, "deepseek")]
    )
    assert payload["enabled"] is False
    assert payload["api_key"] == "test-secret"

    response = client.post(
        "/settings/services/deepseek/clear",
        follow_redirects=False,
    )
    assert response.status_code == 303
    payload = json.loads(
        credential_backend.passwords[(CREDENTIAL_SERVICE_NAME, "deepseek")]
    )
    assert payload == {"version": 1, "enabled": False, "api_key": ""}


def test_enabling_service_without_key_returns_a_safe_validation_error(
    app_client, credential_backend
):
    client, _, _ = app_client

    response = client.post(
        "/settings/services/deepseek",
        data={"enabled": "on", "api_key": ""},
    )

    assert response.status_code == 422
    assert "开启服务前请先输入 API key" in response.text
    assert (CREDENTIAL_SERVICE_NAME, "deepseek") not in credential_backend.passwords


def test_serpapi_switch_is_the_scholar_schedule_gate(app_client, credential_backend):
    client, session_factory, models = app_client
    page = client.get("/settings")
    assert "SerpAPI 未启用" in page.text
    with session_factory() as db:
        schedule = db.get(models.SourceSchedule, "scholar")
        assert schedule is not None and schedule.enabled is False

    response = client.post(
        "/settings/services/serpapi",
        data={"enabled": "on", "api_key": "serp-secret"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_factory() as db:
        schedule = db.get(models.SourceSchedule, "scholar")
        assert schedule is not None and schedule.enabled is True
        assert schedule.next_due_at is not None

    response = client.post(
        "/settings/services/serpapi",
        data={"api_key": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with session_factory() as db:
        schedule = db.get(models.SourceSchedule, "scholar")
        assert schedule is not None and schedule.enabled is False
        assert schedule.next_due_at is None
    blocked = client.post(
        "/settings/authors",
        data={"profile_url": "https://scholar.google.com/citations?user=author1234"},
    )
    assert blocked.status_code == 422
    assert "请先开启 SerpAPI" in blocked.text


def test_credential_store_write_failure_does_not_expose_the_key(
    app_client, credential_backend, monkeypatch
):
    client, _, _ = app_client

    def fail_save(*args, **kwargs):
        raise CredentialStoreError("安全存储失败")

    monkeypatch.setattr("arxiv_updater.web.save_external_service", fail_save)
    response = client.post(
        "/settings/services/deepseek",
        data={"enabled": "on", "api_key": "never-echo-this"},
    )

    assert response.status_code == 422
    assert "安全存储失败" in response.text
    assert "never-echo-this" not in response.text


def test_cross_site_browser_posts_cannot_change_credentials(
    app_client, credential_backend
):
    client, _, _ = app_client

    response = client.post(
        "/settings/services/deepseek",
        data={"enabled": "on", "api_key": "cross-site-secret"},
        headers={"Origin": "https://malicious.example", "Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    assert "cross-site-secret" not in response.text
    assert (CREDENTIAL_SERVICE_NAME, "deepseek") not in credential_backend.passwords
