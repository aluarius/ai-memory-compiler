"""Shared transcript/session helpers for conversation ingestion."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class SessionMetadata:
    """Normalized metadata for a captured AI session."""

    session_id: str
    agent: str
    provider: str
    model: str | None = None
    cwd: str | None = None
    transcript_path: str | None = None
    source: str | None = None


@dataclass(slots=True)
class TranscriptParseResult:
    """Normalized transcript parsing output."""

    context: str
    turn_count: int
    format: str
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    cwd: str | None = None
    source: str | None = None


def _normalize_claude_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                text_parts.append(block)
        return "\n".join(part for part in text_parts if part)
    return ""


def _normalize_codex_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in {"input_text", "output_text", "text"}:
                text_parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                text_parts.append(block)
        return "\n".join(part for part in text_parts if part)
    return ""


def _trim_context(turns: list[str], *, max_turns: int, max_chars: int) -> tuple[str, int]:
    recent = turns[-max_turns:]
    context = "\n".join(recent)

    if len(context) > max_chars:
        context = context[-max_chars:]
        boundary = context.find("\n**")
        if boundary > 0:
            context = context[boundary + 1 :]

    return context, len(recent)


_FORMAT_PROBE_LINES = 50


def detect_transcript_format(transcript_path: Path) -> str:
    """Detect the transcript format by probing the first non-empty lines.

    Newer Claude Code CLI versions prepend metadata entries (permission-mode,
    attachment, queue-operation, last-prompt, ...) before the first real
    message, so we scan up to _FORMAT_PROBE_LINES entries until we see a
    decisive marker.
    """
    saw_json = False
    with open(transcript_path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= _FORMAT_PROBE_LINES:
                break
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                if not saw_json:
                    return "text"
                continue
            saw_json = True

            if not isinstance(entry, dict):
                continue

            if entry.get("type") == "session_meta" and isinstance(entry.get("payload"), dict):
                return "codex_jsonl"
            if "message" in entry or entry.get("role") in {"user", "assistant"}:
                return "claude_jsonl"

    if saw_json:
        return "jsonl"
    return "empty"


def _parse_claude_jsonl(
    transcript_path: Path,
    *,
    max_turns: int,
    max_chars: int,
) -> TranscriptParseResult:
    turns: list[str] = []

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg = entry.get("message", {})
            if isinstance(msg, dict):
                role = msg.get("role", "")
                content = msg.get("content", "")
            else:
                role = entry.get("role", "")
                content = entry.get("content", "")

            if role not in ("user", "assistant"):
                continue

            text = _normalize_claude_content(content).strip()
            if not text:
                continue

            label = "User" if role == "user" else "Assistant"
            turns.append(f"**{label}:** {text}\n")

    context, count = _trim_context(turns, max_turns=max_turns, max_chars=max_chars)
    return TranscriptParseResult(context=context, turn_count=count, format="claude_jsonl")


def _parse_codex_jsonl(
    transcript_path: Path,
    *,
    max_turns: int,
    max_chars: int,
) -> TranscriptParseResult:
    turns: list[str] = []
    session_id: str | None = None
    provider: str | None = None
    model: str | None = None
    cwd: str | None = None
    source: str | None = None

    with open(transcript_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type")
            payload = entry.get("payload", {})

            if entry_type == "session_meta" and isinstance(payload, dict):
                session_id = payload.get("id") or session_id
                provider = payload.get("model_provider") or provider
                cwd = payload.get("cwd") or cwd
                raw_source = payload.get("source")
                if isinstance(raw_source, str):
                    source = raw_source
                elif isinstance(raw_source, dict) and raw_source:
                    source = ",".join(f"{key}={value}" for key, value in sorted(raw_source.items()))
                continue

            if entry_type == "turn_context" and isinstance(payload, dict):
                model = payload.get("model") or model
                cwd = payload.get("cwd") or cwd
                continue

            if entry_type != "response_item" or not isinstance(payload, dict):
                continue

            if payload.get("type") != "message":
                continue

            role = payload.get("role", "")
            if role not in {"user", "assistant"}:
                continue

            text = _normalize_codex_content(payload.get("content", "")).strip()
            if not text:
                continue

            label = "User" if role == "user" else "Assistant"
            turns.append(f"**{label}:** {text}\n")

    context, count = _trim_context(turns, max_turns=max_turns, max_chars=max_chars)
    return TranscriptParseResult(
        context=context,
        turn_count=count,
        format="codex_jsonl",
        session_id=session_id,
        provider=provider,
        model=model,
        cwd=cwd,
        source=source,
    )


def parse_transcript(
    transcript_path: Path,
    *,
    max_turns: int,
    max_chars: int,
) -> TranscriptParseResult:
    """Parse a supported transcript file into normalized context."""
    transcript_format = detect_transcript_format(transcript_path)

    if transcript_format == "codex_jsonl":
        return _parse_codex_jsonl(transcript_path, max_turns=max_turns, max_chars=max_chars)
    if transcript_format == "claude_jsonl":
        return _parse_claude_jsonl(transcript_path, max_turns=max_turns, max_chars=max_chars)

    content = transcript_path.read_text(encoding="utf-8").strip()
    trimmed = content[-max_chars:] if content else ""
    return TranscriptParseResult(
        context=trimmed,
        turn_count=1 if trimmed else 0,
        format=transcript_format,
    )


def extract_conversation_context(
    transcript_path: Path,
    *,
    max_turns: int,
    max_chars: int,
) -> tuple[str, int]:
    """Read a supported transcript and extract the last N user/assistant turns."""
    parsed = parse_transcript(transcript_path, max_turns=max_turns, max_chars=max_chars)
    return parsed.context, parsed.turn_count


def format_session_header(metadata: SessionMetadata) -> str:
    """Render a compact source metadata line for daily log entries."""
    bits = [
        f"agent={metadata.agent}",
        f"provider={metadata.provider}",
        f"session={metadata.session_id}",
    ]

    if metadata.model:
        bits.append(f"model={metadata.model}")
    if metadata.cwd:
        bits.append(f"cwd={metadata.cwd}")

    return "_Source: " + " | ".join(bits) + "_"
