from __future__ import annotations

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
