"""Verify that the current desktop OS keyring can round-trip a temporary value."""

from __future__ import annotations

import secrets
import uuid

import keyring

service_name = f"arXiv Updater release smoke {uuid.uuid4()}"
username = "release-smoke"
temporary_value = secrets.token_urlsafe(32)
created = False

try:
    keyring.set_password(service_name, username, temporary_value)
    created = True
    assert keyring.get_password(service_name, username) == temporary_value
finally:
    if created:
        keyring.delete_password(service_name, username)

backend = keyring.get_keyring()
backend_name = f"{type(backend).__module__}.{type(backend).__name__}"
print(f"System credential round trip passed with {backend_name}")
