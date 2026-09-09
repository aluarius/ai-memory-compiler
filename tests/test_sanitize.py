from __future__ import annotations

import pytest

from sanitize import sanitize


def test_sanitize_redacts_bearer_and_env_secrets() -> None:
    text = """
Authorization: Bearer sk-secret-token-value-1234567890
OPENAI_API_KEY=sk-realistic-openai-key-1234567890
"""

    sanitized = sanitize(text)

    assert "sk-secret-token" not in sanitized
    assert "sk-realistic-openai-key" not in sanitized
    assert "[REDACTED" in sanitized


@pytest.mark.parametrize("header", ["Authorization", "Proxy-Authorization", "authorization"])
def test_sanitize_redacts_basic_credentials_and_preserves_following_line(header: str) -> None:
    credential = "dXNlcjpwYXNz"  # Synthetic user:pass, shorter than generic blob patterns.
    sanitized = sanitize(f"{header}: Basic {credential}\nERROR authentication failed")
    assert credential not in sanitized
    assert "[REDACTED" in sanitized
    assert "\nERROR authentication failed" in sanitized
