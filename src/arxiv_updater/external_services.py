"""Secure, per-Windows-user configuration for optional external services."""

from __future__ import annotations

import importlib
import json
from dataclasses import dataclass, field
from types import ModuleType
from typing import Literal, Protocol, cast


def _load_keyring_module() -> ModuleType | None:
    try:
        return importlib.import_module("keyring")
    except ImportError:  # pragma: no cover - keyring is installed only on Windows
        return None


_keyring = _load_keyring_module()

ServiceName = Literal["deepseek", "serpapi"]
CredentialSource = Literal["credential_manager", "none"]

SUPPORTED_SERVICES: tuple[ServiceName, ...] = ("deepseek", "serpapi")
CREDENTIAL_SERVICE_NAME = "arXiv Updater optional API services"
PAYLOAD_VERSION = 1
MAX_API_KEY_LENGTH = 1024


class CredentialBackend(Protocol):
    def get_password(self, service_name: str, username: str) -> str | None: ...

    def set_password(self, service_name: str, username: str, password: str) -> None: ...


class CredentialStoreError(RuntimeError):
    """The operating-system credential store could not be read or updated."""


class CredentialValidationError(ValueError):
    """A requested service state is incomplete or unsafe to persist."""


@dataclass(frozen=True, slots=True)
class ExternalServiceState:
    service: ServiceName
    requested_enabled: bool
    api_key: str = field(repr=False)
    source: CredentialSource = "none"
    storage_available: bool = True

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    @property
    def enabled(self) -> bool:
        return self.requested_enabled and self.has_api_key

    @property
    def effective_api_key(self) -> str:
        return self.api_key if self.enabled else ""


@dataclass(frozen=True, slots=True)
class ExternalServiceView:
    service: ServiceName
    requested_enabled: bool
    enabled: bool
    has_api_key: bool
    source: CredentialSource
    storage_available: bool


def public_service_view(state: ExternalServiceState) -> ExternalServiceView:
    """Return a template-safe view that cannot expose the API key."""

    return ExternalServiceView(
        service=state.service,
        requested_enabled=state.requested_enabled,
        enabled=state.enabled,
        has_api_key=state.has_api_key,
        source=state.source,
        storage_available=state.storage_available,
    )


def _system_backend() -> CredentialBackend:
    if _keyring is None:
        raise CredentialStoreError("当前系统没有可用的安全凭据存储。")
    return cast(CredentialBackend, _keyring)


def _normalize_api_key(value: str) -> str:
    api_key = value.strip()
    if len(api_key) > MAX_API_KEY_LENGTH:
        raise CredentialValidationError("API key 长度异常，请检查后重试。")
    if any(ord(character) < 32 for character in api_key):
        raise CredentialValidationError("API key 不能包含换行符或控制字符。")
    return api_key


def _managed_state(service: ServiceName, raw: str) -> ExternalServiceState:
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("version") != PAYLOAD_VERSION:
            raise ValueError
        enabled = payload.get("enabled")
        api_key = payload.get("api_key")
        if not isinstance(enabled, bool) or not isinstance(api_key, str):
            raise ValueError
        api_key = _normalize_api_key(api_key)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CredentialStoreError("安全凭据格式无法识别，请清除后重新保存。") from exc
    return ExternalServiceState(
        service=service,
        requested_enabled=enabled,
        api_key=api_key,
        source="credential_manager",
    )


def load_external_service(
    service: ServiceName,
    *,
    backend: CredentialBackend | None = None,
) -> ExternalServiceState:
    """Resolve one service exclusively from the OS credential store."""

    try:
        active_backend = backend or _system_backend()
        raw = active_backend.get_password(CREDENTIAL_SERVICE_NAME, service)
    except Exception:
        return ExternalServiceState(
            service=service,
            requested_enabled=False,
            api_key="",
            source="none",
            storage_available=False,
        )
    if raw is None:
        return ExternalServiceState(
            service=service,
            requested_enabled=False,
            api_key="",
            source="none",
        )
    try:
        return _managed_state(service, raw)
    except CredentialStoreError:
        return ExternalServiceState(
            service=service,
            requested_enabled=False,
            api_key="",
            source="credential_manager",
            storage_available=True,
        )


def save_external_service(
    service: ServiceName,
    *,
    enabled: bool,
    new_api_key: str = "",
    backend: CredentialBackend | None = None,
) -> ExternalServiceState:
    """Persist a switch state and optional replacement key in the OS credential store."""

    try:
        active_backend = backend or _system_backend()
        current = load_external_service(service, backend=active_backend)
        replacement = _normalize_api_key(new_api_key)
        api_key = replacement or current.api_key
        if enabled and not api_key:
            raise CredentialValidationError("开启服务前请先输入 API key。")
        payload = json.dumps(
            {"version": PAYLOAD_VERSION, "enabled": enabled, "api_key": api_key},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        active_backend.set_password(CREDENTIAL_SERVICE_NAME, service, payload)
    except CredentialValidationError:
        raise
    except Exception as exc:
        raise CredentialStoreError("无法写入 Windows 凭据管理器，请稍后重试。") from exc
    return ExternalServiceState(
        service=service,
        requested_enabled=enabled,
        api_key=api_key,
        source="credential_manager",
    )


def clear_external_service(
    service: ServiceName,
    *,
    backend: CredentialBackend | None = None,
) -> ExternalServiceState:
    """Disable one service and remove its managed secret."""

    try:
        active_backend = backend or _system_backend()
        payload = json.dumps(
            {"version": PAYLOAD_VERSION, "enabled": False, "api_key": ""},
            separators=(",", ":"),
        )
        active_backend.set_password(CREDENTIAL_SERVICE_NAME, service, payload)
    except Exception as exc:
        raise CredentialStoreError("无法写入 Windows 凭据管理器，请稍后重试。") from exc
    return ExternalServiceState(
        service=service,
        requested_enabled=False,
        api_key="",
        source="credential_manager",
    )
