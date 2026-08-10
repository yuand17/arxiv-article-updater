import json

import pytest

from arxiv_updater.external_services import (
    CREDENTIAL_SERVICE_NAME,
    CredentialValidationError,
    clear_external_service,
    load_external_service,
    public_service_view,
    save_external_service,
)
from arxiv_updater.security import REDACTED, redact_sensitive_text


class FakeCredentialBackend:
    def __init__(self) -> None:
        self.passwords: dict[tuple[str, str], str] = {}
        self.fail_reads = False
        self.fail_writes = False

    def get_password(self, service_name: str, username: str) -> str | None:
        if self.fail_reads:
            raise RuntimeError("credential read failed")
        return self.passwords.get((service_name, username))

    def set_password(self, service_name: str, username: str, password: str) -> None:
        if self.fail_writes:
            raise RuntimeError("credential write failed")
        self.passwords[(service_name, username)] = password


def test_environment_key_is_a_backward_compatible_default():
    backend = FakeCredentialBackend()

    state = load_external_service("deepseek", "legacy-key", backend=backend)

    assert state.enabled is True
    assert state.effective_api_key == "legacy-key"
    assert state.source == "environment"
    assert "legacy-key" not in repr(state)
    assert not hasattr(public_service_view(state), "api_key")


def test_switch_state_and_key_are_saved_without_echoing_the_key():
    backend = FakeCredentialBackend()

    enabled = save_external_service(
        "deepseek",
        enabled=True,
        new_api_key="new-secret-key",
        backend=backend,
    )
    disabled = save_external_service("deepseek", enabled=False, backend=backend)
    restored = load_external_service("deepseek", backend=backend)

    assert enabled.enabled is True
    assert disabled.enabled is False
    assert disabled.has_api_key is True
    assert restored.enabled is False
    assert restored.has_api_key is True
    assert restored.effective_api_key == ""
    payload = json.loads(backend.passwords[(CREDENTIAL_SERVICE_NAME, "deepseek")])
    assert payload["enabled"] is False
    assert payload["api_key"] == "new-secret-key"


def test_clear_writes_a_tombstone_that_masks_a_legacy_environment_key():
    backend = FakeCredentialBackend()
    save_external_service(
        "serpapi",
        enabled=True,
        new_api_key="managed-key",
        backend=backend,
    )

    clear_external_service("serpapi", backend=backend)
    state = load_external_service("serpapi", "legacy-key", backend=backend)

    assert state.source == "credential_manager"
    assert state.has_api_key is False
    assert state.enabled is False
    assert "managed-key" not in backend.passwords[(CREDENTIAL_SERVICE_NAME, "serpapi")]


def test_enabling_without_any_key_is_rejected():
    with pytest.raises(CredentialValidationError, match="请先输入"):
        save_external_service(
            "deepseek",
            enabled=True,
            backend=FakeCredentialBackend(),
        )


def test_read_failure_keeps_legacy_configuration_but_blocks_settings_writes():
    backend = FakeCredentialBackend()
    backend.fail_reads = True

    state = load_external_service("deepseek", "legacy-key", backend=backend)

    assert state.enabled is True
    assert state.storage_available is False


def test_sensitive_errors_redact_known_keys_and_query_parameters():
    message = (
        "GET https://serpapi.com/search.json?engine=x&api_key=secret-value "
        "Authorization: Bearer bearer-value"
    )

    redacted = redact_sensitive_text(message, ("secret-value", "bearer-value"))

    assert "secret-value" not in redacted
    assert "bearer-value" not in redacted
    assert redacted.count(REDACTED) >= 2
