from __future__ import annotations

import os
from pathlib import Path
import re
from collections.abc import Mapping
from typing import Any

from .base import AgentAdapterError

CURSOR_HOOK_ENV_KEYS = (
    "CURSOR_VERSION",
    "CURSOR_PROJECT_DIR",
    "CURSOR_TRANSCRIPT_PATH",
    "CURSOR_USER_EMAIL",
    "CURSOR_CODE_REMOTE",
)
GROK_HOOK_ENV_KEYS = (
    "GROK_HOOK_EVENT",
    "GROK_HOOK_NAME",
    "GROK_SESSION_ID",
    "GROK_WORKSPACE_ROOT",
)
CURSOR_CAMEL_HOOK_EVENTS = frozenset({
    "beforeSubmitPrompt",
    "sessionStart",
    "sessionEnd",
    "afterAgentResponse",
    "afterAgentThought",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "beforeTabFileRead",
    "afterTabFileEdit",
    "preCompact",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "subagentStart",
    "subagentStop",
})


def _path_segments(path_str: str) -> tuple[tuple[str, ...], ...]:
    raw = str(path_str or "").strip()
    if not raw:
        return ()
    expanded = os.path.expanduser(raw)
    candidates = [raw, expanded]
    try:
        candidates.append(os.path.realpath(expanded))
    except OSError:
        pass
    seen: list[tuple[str, ...]] = []
    for cand in candidates:
        parts = tuple(seg.lower() for seg in re.split(r"[\\/]+", cand) if seg)
        if parts and parts not in seen:
            seen.append(parts)
    return tuple(seen)


def _has_adjacent_segments(parts: tuple[str, ...], seq: tuple[str, ...]) -> bool:
    n = len(seq)
    if n == 0 or len(parts) < n:
        return False
    return any(parts[i:i + n] == seq for i in range(len(parts) - n + 1))


def is_claude_code_transcript_path(
    path_str: str,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Verify path resolves to a .jsonl file inside CLAUDE_CONFIG_DIR or ~/.claude."""
    raw = str(path_str or "").strip()
    if not raw:
        return False
    env_map = os.environ if env is None else env
    cfg_dir = str(env_map.get("CLAUDE_CONFIG_DIR") or "").strip()
    root_candidate = Path(os.path.expanduser(cfg_dir)) if cfg_dir else (Path.home() / ".claude")
    try:
        root_real = Path(os.path.realpath(str(root_candidate)))
        target_real = Path(os.path.realpath(os.path.expanduser(raw)))
        target_real.relative_to(root_real)
    except (OSError, ValueError):
        return False
    return target_real.suffix.lower() == ".jsonl"


def infer_host_from_transcript_path(
    path_str: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Infer the owning host from verified Claude root or exact path segments (never bare '.claude')."""
    if is_claude_code_transcript_path(path_str, env=env):
        return "claude-code"
    for parts in _path_segments(path_str):
        if (
            ".cursor" in parts
            or "agent-transcripts" in parts
            or _has_adjacent_segments(parts, ("application support", "cursor"))
            or _has_adjacent_segments(parts, ("cursor", "user"))
        ):
            return "cursor"
        if ".grok" in parts:
            return "grok-build"
        if ".codex" in parts or _has_adjacent_segments(parts, ("application support", "codex")):
            return "codex"
        if ".omp" in parts:
            return "omp"
        if (
            _has_adjacent_segments(parts, (".local", "share", "opencode"))
            or _has_adjacent_segments(parts, (".config", "opencode"))
            or parts[-1] == "opencode.db"
        ):
            return "opencode"
        if ".gemini" in parts and any(seg in {"antigravity", "antigravity-cli", "antigravity-ide"} for seg in parts):
            return "antigravity"
    return None


def _first_payload_path(payload_map: Mapping[str, Any], env_map: Mapping[str, str], host_lower: str) -> str | None:
    candidates = [
        payload_map.get("transcript_path"),
        payload_map.get("transcriptPath"),
        payload_map.get("artifactDirectoryPath"),
    ]
    if host_lower != "cursor":
        candidates.append(env_map.get("CURSOR_TRANSCRIPT_PATH"))
    for cand in candidates:
        cand_str = str(cand or "").strip()
        if cand_str:
            return cand_str
    return None


def detect_foreign_hook_detail(
    declared_host: str,
    payload: Any,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str | None] | None:
    """Return (actual_host, offending_path) if payload or supplied env contradicts declared_host."""
    host_lower = str(declared_host or "").strip().lower()
    env_map = env if isinstance(env, Mapping) else {}
    payload_map = payload if isinstance(payload, Mapping) else {}

    if host_lower != "cursor" and any(str(env_map.get(k) or "").strip() for k in CURSOR_HOOK_ENV_KEYS):
        cursor_path = str(env_map.get("CURSOR_TRANSCRIPT_PATH") or "").strip() or _first_payload_path(
            payload_map, env_map, host_lower
        )
        return ("cursor", cursor_path)
    if host_lower != "grok-build" and any(str(env_map.get(k) or "").strip() for k in GROK_HOOK_ENV_KEYS):
        return ("grok-build", _first_payload_path(payload_map, env_map, host_lower))

    path_candidates = [
        payload_map.get("transcript_path"),
        payload_map.get("transcriptPath"),
        payload_map.get("artifactDirectoryPath"),
    ]
    if host_lower != "cursor":
        path_candidates.append(env_map.get("CURSOR_TRANSCRIPT_PATH"))
    for cand in path_candidates:
        cand_str = str(cand or "").strip()
        if not cand_str:
            continue
        inferred = infer_host_from_transcript_path(cand_str, env=env)
        if inferred is not None and inferred != host_lower:
            return (inferred, cand_str)
        if host_lower == "claude-code" and any(".claude" in parts for parts in _path_segments(cand_str)):
            if not is_claude_code_transcript_path(cand_str, env=env):
                return ("outside-claude-root", cand_str)

    if host_lower != "cursor" and str(payload_map.get("cursor_version") or "").strip():
        return ("cursor", _first_payload_path(payload_map, env_map, host_lower))

    if host_lower == "claude-code":
        hook_event_name = str(payload_map.get("hook_event_name") or "").strip()
        if str(payload_map.get("generation_id") or "").strip() or hook_event_name in CURSOR_CAMEL_HOOK_EVENTS:
            return ("cursor", _first_payload_path(payload_map, env_map, host_lower))

    return None


def detect_foreign_hook_host(
    declared_host: str,
    payload: Any,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return detected foreign host ID if payload or supplied env contradicts declared_host."""
    detail = detect_foreign_hook_detail(declared_host, payload, env=env)
    return detail[0] if detail is not None else None


def raise_if_foreign_hook_host(
    declared_host: str,
    payload: Any,
    env: Mapping[str, str] | None = None,
) -> None:
    """Raise AgentAdapterError if the hook event originates from a different host."""
    detail = detect_foreign_hook_detail(declared_host, payload, env=env)
    if detail is None:
        return
    actual_host, offending_path = detail
    host_lower = str(declared_host or "").strip().lower() or "claude-code"
    raise AgentAdapterError(
        host=host_lower,
        code="unsupported_format",
        path=offending_path,
        detail=f"Hook event appears to originate from {actual_host}, not {host_lower}",
    )
