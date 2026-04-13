"""
Sanitize conversation context before writing to daily logs.

Masks sensitive data: API keys, tokens, passwords, secrets, private keys,
connection strings, and other credentials that may appear in conversations.
"""

from __future__ import annotations

import re

# Each pattern: (compiled regex, replacement string)
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # API keys (generic: sk-, pk-, key-, api_, etc.)
    (re.compile(r"\b(sk|pk|api|key|token|secret)[_-][A-Za-z0-9_\-]{20,}\b", re.IGNORECASE), "[REDACTED_KEY]"),
    # AWS keys
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_AWS_KEY]"),
    # AWS secret keys
    (re.compile(r"(?<=[\s=:'\"])[A-Za-z0-9/+=]{40}(?=[\s'\",])"), "[REDACTED_AWS_SECRET]"),
    # Bearer tokens
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-.~+/]+=*", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    # Authorization headers
    (re.compile(r"(Authorization:\s*)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    # Passwords in URLs (user:pass@host)
    (re.compile(r"://([^:]+):([^@]{3,})@"), r"://\1:[REDACTED]@"),
    # Password fields in configs/env
    (re.compile(r"(password|passwd|pwd|pass)\s*[=:]\s*\S+", re.IGNORECASE), r"\1=[REDACTED]"),
    # Private keys (PEM blocks)
    (re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
    # SSH private keys (base64 block after -----BEGIN)
    (re.compile(r"(-----BEGIN OPENSSH PRIVATE KEY-----)[\s\S]*?(-----END OPENSSH PRIVATE KEY-----)"), r"\1\n[REDACTED]\n\2"),
    # .env file values for common secret names
    (re.compile(r"^((?:DATABASE_URL|REDIS_URL|MONGO_URI|SECRET_KEY|PRIVATE_KEY|ENCRYPTION_KEY|JWT_SECRET|SESSION_SECRET|ANTHROPIC_API_KEY|OPENAI_API_KEY|GITHUB_TOKEN|SLACK_TOKEN|STRIPE_KEY|SENDGRID_KEY|TWILIO_AUTH)\s*=\s*).+$", re.IGNORECASE | re.MULTILINE), r"\1[REDACTED]"),
    # Generic hex tokens (32+ hex chars, likely a secret)
    (re.compile(r"\b[0-9a-f]{40,}\b"), "[REDACTED_HEX]"),
    # Base64 blobs that look like secrets (64+ chars, no spaces)
    (re.compile(r"(?<=[=: '\"])[A-Za-z0-9+/]{64,}={0,2}(?=[\s'\",});\]])"), "[REDACTED_BASE64]"),
]


def sanitize(text: str) -> str:
    """Apply all redaction patterns to text."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
