"""Small redaction helpers for user-visible and persisted error messages."""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

REDACTED = "[REDACTED]"
SENSITIVE_QUERY_PARAMETER = re.compile(
    r"(?i)([?&](?:api_key|apikey|access_token|token|key)=)[^&\s'\"]+"
)
BEARER_TOKEN = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+")


def redact_sensitive_text(value: object, secrets: Iterable[str] = ()) -> str:
    """Redact known secrets plus common credential-bearing URL/header forms."""

    text = str(value)
    for secret in secrets:
        if not secret:
            continue
        for representation in {secret, quote(secret, safe=""), quote_plus(secret)}:
            if representation:
                text = text.replace(representation, REDACTED)
    text = SENSITIVE_QUERY_PARAMETER.sub(rf"\1{REDACTED}", text)
    return BEARER_TOKEN.sub(rf"\1{REDACTED}", text)
