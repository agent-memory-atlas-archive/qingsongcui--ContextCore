#!/usr/bin/env python3
from __future__ import annotations
"""ContextCore Universal Host Hook Dispatcher.

Called by external host hook runners (Codex, Cursor, Antigravity) to bridge
events to the local ContextCore loopback server without leaking tokens or
failing closed on connection dropouts.

Lifecycle hooks only acknowledge events and never append context or outage
messages to conversations. Context retrieval uses explicit MCP/host commands.

Security:
- Loopback only (127.0.0.1)
- Reads token from 0600 file, never from command line or URL query
- Never logs credentials
- Flattens payload to top-level canonical fields for direct parse_hook consumption
"""

import json
import ipaddress
import os
import stat
import sys
import urllib.request
import urllib.error
from urllib.parse import urlsplit
from datetime import datetime, timezone
from pathlib import Path

# Mapping of CLI shorthands to official vendor event names
EVENT_NAME_MAP: dict[str, str] = {
    # Codex official hooks
    "prompt": "UserPromptSubmit",
    "UserPromptSubmit": "UserPromptSubmit",
    "session-start": "SessionStart",
    "SessionStart": "SessionStart",
    "stop": "Stop",
    "Stop": "Stop",
    "tool": "PostToolUse",
    "PostToolUse": "PostToolUse",
    "end": "SessionEnd",
    "SessionEnd": "SessionEnd",
    # Cursor official hooks
    "beforeSubmitPrompt": "beforeSubmitPrompt",
    "sessionStart": "sessionStart",
    "afterAgentResponse": "afterAgentResponse",
    "postToolUse": "postToolUse",
    "sessionEnd": "sessionEnd",
    "assistant": "afterAgentResponse",
    # Antigravity official hooks
    "pre-invoke": "PreInvocation",
    "PreInvocation": "PreInvocation",
    # OpenCode plugin events
    "chat.message": "chat.message",
    "chat-message": "chat.message",
    "session.idle": "session.idle",
    "idle": "session.idle",
    # OMP events
    "user_message": "user_message",
    "assistant_message": "assistant_message",
    "intent_detected": "user_message",
    "before_agent_start": "before_agent_start",
    "session_stop": "session_stop",
}

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _loopback_url(base_url: str) -> bool:
    try:
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment:
            return False
        if parsed.path not in ("", "/") or not parsed.hostname:
            return False
        if parsed.port is not None and parsed.port <= 0:
            return False
        return parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def get_token_securely(agent_id: str) -> str | None:
    """Read agent credential from local 0600 file or environment."""
    env_token = os.environ.get("CONTEXTCORE_TOKEN")
    if env_token:
        return env_token.strip()

    cred_path_env = os.environ.get("CONTEXTCORE_CREDENTIAL_PATH")
    if cred_path_env:
        cred_path = Path(cred_path_env).expanduser().resolve()
    else:
        cred_path = Path.home() / ".contextcore" / "credentials" / f"{agent_id}.token"

    if not cred_path.is_file():
        return None

    try:
        file_stat = cred_path.stat()
        mode = file_stat.st_mode
        if (mode & 0o077) != 0:
            # Group or other accessible - reject insecure token
            return None
        return cred_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


import re
from pathlib import Path
from typing import Any, Mapping

_CURSOR_HOOK_ENV_KEYS = (
    "CURSOR_VERSION",
    "CURSOR_PROJECT_DIR",
    "CURSOR_TRANSCRIPT_PATH",
    "CURSOR_USER_EMAIL",
    "CURSOR_CODE_REMOTE",
)
_GROK_HOOK_ENV_KEYS = (
    "GROK_HOOK_EVENT",
    "GROK_HOOK_NAME",
    "GROK_SESSION_ID",
    "GROK_WORKSPACE_ROOT",
)
_CURSOR_CAMEL_HOOK_EVENTS = frozenset({
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


def _infer_host_from_transcript_path(
    path_str: str,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Infer owning host from verified Claude root or exact path segments (never bare '.claude')."""
    if _is_claude_code_transcript_path(path_str, env=env):
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


def _is_claude_code_transcript_path(path_str: str, env: Mapping[str, str] | None = None) -> bool:
    """Verify a Claude Code transcript path resolves to a .jsonl file inside CLAUDE_CONFIG_DIR or ~/.claude."""
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


def detect_foreign_hook_host(
    declared_host: str,
    payload: Any,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Return a non-empty reason/host tag if env, payload, or path contradicts declared_host."""
    host_lower = str(declared_host or "").strip().lower()
    env_map = os.environ if env is None else env

    if host_lower != "cursor" and any(str(env_map.get(k) or "").strip() for k in _CURSOR_HOOK_ENV_KEYS):
        return "cursor"
    if host_lower != "grok-build" and any(str(env_map.get(k) or "").strip() for k in _GROK_HOOK_ENV_KEYS):
        return "grok-build"

    payload_map = payload if isinstance(payload, Mapping) else {}
    if host_lower != "cursor":
        if str(payload_map.get("cursor_version") or "").strip():
            return "cursor"
        if host_lower == "claude-code":
            hook_event_name = str(payload_map.get("hook_event_name") or "").strip()
            if str(payload_map.get("generation_id") or "").strip() or hook_event_name in _CURSOR_CAMEL_HOOK_EVENTS:
                return "cursor"

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
        inferred = _infer_host_from_transcript_path(cand_str, env=env_map)
        if inferred is not None and inferred != host_lower:
            return inferred

    if host_lower == "claude-code":
        transcript_str = str(payload_map.get("transcript_path") or payload_map.get("transcriptPath") or "").strip()
        if transcript_str and not _is_claude_code_transcript_path(transcript_str, env=env_map):
            return "outside-claude-root"

    return None


def post_to_contextcore(
    agent_id: str,
    event_arg: str,
    raw_payload: dict,
    base_url: str = "http://127.0.0.1:8420",
    timeout: float = 3.0,
    read_response: bool = True,
    *,
    preserve_missing_cwd: bool = False,
) -> dict | None:
    """Dispatch hook event to ContextCore loopback endpoint.

    Flattens raw payload fields so parse_hook in adapters can access prompt,
    last_assistant_message, text, tool_name, etc. directly at top-level.
    """
    if agent_id not in {"codex", "omp", "cursor", "antigravity", "claude-code", "opencode"} or not _loopback_url(base_url):
        return None
    if isinstance(raw_payload, dict) and detect_foreign_hook_host(agent_id, raw_payload):
        return None
    canonical_event = EVENT_NAME_MAP.get(event_arg, event_arg)
    url = f"{base_url.rstrip('/')}/api/connections/{agent_id}/hook"
    token = get_token_securely(agent_id)
    if not token:
        return None

    headers = {
        "Content-Type": "application/json",
        "X-ContextCore-Host": agent_id,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Flatten payload to top level
    body: dict = dict(raw_payload) if isinstance(raw_payload, dict) else {"raw": str(raw_payload)}
    body["event"] = canonical_event
    body["hook_event_name"] = canonical_event
    body["hookEventName"] = canonical_event
    body["context_request"] = bool(raw_payload.get("context_request", False))

    inner: dict = {}
    if isinstance(raw_payload, dict):
        for nested_key in ("payload", "data", "hook_event", "event"):
            candidate = raw_payload.get(nested_key)
            if isinstance(candidate, dict):
                inner = candidate
                break

    for field_name in (
        "conversationId",
        "conversation_id",
        "transcriptPath",
        "transcript_path",
        "artifactDirectoryPath",
        "artifact_directory",
        "surface",
        "client",
        "prompt",
        "user_prompt",
        "userPrompt",
        "user_message",
        "assistant_message",
        "messages",
        "workspacePaths",
        "workspace_roots",
    ):
        if field_name not in body and field_name in inner:
            body[field_name] = inner[field_name]

    if agent_id == "antigravity" and not body.get("surface"):
        env_surface = os.environ.get("ANTIGRAVITY_SURFACE") or os.environ.get("AGY_SURFACE")
        if env_surface:
            body["surface"] = env_surface.strip()

    if not body.get("session_id"):
        body["session_id"] = (
            raw_payload.get("session_id")
            or raw_payload.get("sessionId")
            or raw_payload.get("conversationId")
            or raw_payload.get("conversation_id")
            or inner.get("session_id")
            or inner.get("sessionId")
            or inner.get("conversationId")
            or inner.get("conversation_id")
            or ""
        )
    if not body["session_id"]:
        return None

    if not body.get("cwd"):
        workspace_roots = raw_payload.get("workspace_roots") or raw_payload.get("workspacePaths") or inner.get("workspace_roots") or inner.get("workspacePaths")
        if isinstance(workspace_roots, list) and workspace_roots:
            body["cwd"] = str(workspace_roots[0])
        elif inner.get("cwd"):
            body["cwd"] = str(inner["cwd"])
        elif not preserve_missing_cwd and hasattr(os, "getcwd"):
            try:
                body["cwd"] = os.getcwd()
            except OSError:
                pass

    if not body.get("timestamp"):
        body["timestamp"] = datetime.now(timezone.utc).isoformat()

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(req, timeout=timeout) as resp:
            if resp.status == 200:
                if not read_response:
                    return {}
                raw = resp.read(65537)
                if len(raw) > 65536:
                    return None
                return json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        # Gracefully handle server downtime or connection failure
        return None
    return None


def format_host_response(agent_id: str, event_name: str, resp_data: dict | None) -> dict:
    """Format response adhering to host's official schema."""
    canonical_event = EVENT_NAME_MAP.get(event_name, event_name)

    # Hooks report lifecycle events only. Context and outage diagnostics must
    # never be pushed into an unrelated conversation, even when the server
    # returns a pack/card. Explicit MCP/commands remain the retrieval surface.
    if agent_id == "cursor" and canonical_event == "beforeSubmitPrompt":
        return {"continue": True}
    return {}


HOSTS = frozenset({'codex', 'omp', 'cursor', 'antigravity', 'claude-code', 'opencode'})
MAX_NOTIFICATION_BYTES = 65536
MAX_QUEUED_PER_HOST = 256
_ENQUEUE_SEQ = 0


def _read_host_stdin(limit: int, timeout: float = 0.05) -> str:
    """Read a bounded payload without waiting for the host to close stdin."""
    import select
    import time
    try:
        if sys.stdin.isatty():
            return ""
        fd = sys.stdin.fileno()
    except (OSError, AttributeError, ValueError):
        return ""
    chunks: list[bytes] = []
    total = 0
    deadline = time.monotonic() + timeout
    blocking = True
    got_data = False
    try:
        blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
        while total <= limit:
            wait = 0 if got_data else max(0, deadline - time.monotonic())
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                break
            chunk = os.read(fd, min(8192, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            got_data = True
    except (OSError, ValueError):
        pass
    finally:
        try:
            os.set_blocking(fd, blocking)
        except (OSError, ValueError):
            pass
    return b"".join(chunks)[:limit].decode("utf-8", "replace")


def _notification_dir(*, create: bool = False) -> Path:
    directory = Path.home()
    for name in ('.contextcore', 'notifications'):
        directory = directory / name
        if create:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & (0o022 if name == '.contextcore' else 0o077):
            raise OSError('Unsafe notification directory')
    return directory


def _notification_slot(agent_id, *, create=False):
    """Open a fixed, private slot without following links or waiting for a lock."""
    import fcntl
    if agent_id not in HOSTS:
        raise ValueError('Unknown host')
    directory = _notification_dir(create=create)
    flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
    fd = os.open(directory / f'{agent_id}.json', flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError('Unsafe notification file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _infer_host_from_filename(name: str) -> str | None:
    stem = name[:-5] if name.endswith('.json') else name
    if stem in HOSTS:
        return stem
    for host in sorted(HOSTS, key=len, reverse=True):
        if stem.startswith(f'{host}-'):
            return host
    return None


def _queue_sort_key(path: Path) -> tuple[int, int, str]:
    host = _infer_host_from_filename(path.name)
    if host and path.name.startswith(f'{host}-') and path.name.endswith('.json'):
        rest = path.name[len(host) + 1 : -5]
        parts = rest.split('-')
        if len(parts) >= 2 and parts[0].isdigit():
            seq = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
            return (int(parts[0]), seq, path.name)
    try:
        st = path.lstat()
        return (st.st_mtime_ns, 0, path.name)
    except OSError:
        return (0, 0, path.name)


def _safe_unlink(path: Path) -> None:
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid():
            path.unlink()
    except OSError:
        pass


def enqueue_notification(agent_id, event_arg, payload, base_url='http://127.0.0.1:8420'):
    """Write a unique per-event notification file with client-side cwd and surface; no network here."""
    global _ENQUEUE_SEQ
    import hashlib
    import time
    if agent_id not in HOSTS or not _loopback_url(base_url) or not isinstance(payload, dict):
        return False
    if detect_foreign_hook_host(agent_id, payload):
        return False
    try:
        token = get_token_securely(agent_id)
        if not token:
            return False
        directory = _notification_dir(create=True)
        # Reject if legacy slot path was replaced with a symlink
        legacy_slot = directory / f'{agent_id}.json'
        try:
            legacy_info = legacy_slot.lstat()
            if not stat.S_ISREG(legacy_info.st_mode) or legacy_info.st_uid != os.getuid() or legacy_info.st_mode & 0o077:
                return False
        except FileNotFoundError:
            pass

        payload_copy = {**payload, 'context_request': False}
        if not payload_copy.get('cwd'):
            inner = payload_copy.get('payload') if isinstance(payload_copy.get('payload'), dict) else {}
            ws_roots = (
                payload_copy.get('workspace_roots')
                or payload_copy.get('workspacePaths')
                or inner.get('workspace_roots')
                or inner.get('workspacePaths')
            )
            if isinstance(ws_roots, list) and ws_roots:
                payload_copy['cwd'] = str(ws_roots[0])
            elif inner.get('cwd'):
                payload_copy['cwd'] = str(inner['cwd'])
            elif hasattr(os, 'getcwd'):
                try:
                    payload_copy.setdefault('cwd', os.getcwd())
                except OSError:
                    pass
        if agent_id == 'antigravity' and not payload_copy.get('surface'):
            env_surface = os.environ.get('ANTIGRAVITY_SURFACE') or os.environ.get('AGY_SURFACE')
            if env_surface and env_surface.strip():
                payload_copy['surface'] = env_surface.strip()

        now_ns = time.time_ns()
        _ENQUEUE_SEQ = (_ENQUEUE_SEQ + 1) & 0xFFFF
        data = json.dumps(
            {
                'agent_id': agent_id,
                'event': event_arg,
                'payload': payload_copy,
                'base_url': base_url.rstrip('/'),
                'created_at': now_ns / 1_000_000_000.0,
                'created_at_ns': now_ns,
                'credential_hash': hashlib.sha256(token.encode()).hexdigest(),
            },
            ensure_ascii=False,
        ).encode('utf-8')
        if len(data) > MAX_NOTIFICATION_BYTES:
            return False

        target = directory / f'{agent_id}-{now_ns:020d}-{os.getpid()}-{_ENQUEUE_SEQ:04d}.json'
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError('Unsafe notification file')
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        except BaseException:
            os.close(fd)
            _safe_unlink(target)
            raise
        os.close(fd)

        # Bound per-host queue depth by evicting oldest queued files if flooded
        host_files = sorted(directory.glob(f'{agent_id}-*.json'), key=_queue_sort_key)
        if len(host_files) > MAX_QUEUED_PER_HOST:
            for stale_file in host_files[: len(host_files) - MAX_QUEUED_PER_HOST]:
                _safe_unlink(stale_file)
        return True
    except (OSError, ValueError, TypeError):
        return False


def drain_notifications(base_url='http://127.0.0.1:8420'):
    """Iterate queued notification files in chronological order and unlink after delivery."""
    import hashlib
    import time
    delivered = 0
    try:
        directory = _notification_dir(create=False)
    except OSError:
        return 0

    candidates = sorted(
        (p for p in directory.iterdir() if p.name.endswith('.json')),
        key=_queue_sort_key,
    )
    target_base = base_url.rstrip('/')
    for entry in candidates:
        is_legacy_slot = entry.stem in HOSTS
        try:
            fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    continue
                raw = os.read(fd, MAX_NOTIFICATION_BYTES + 1)
            finally:
                os.close(fd)

            if not raw:
                if not is_legacy_slot:
                    _safe_unlink(entry)
                continue

            try:
                item = json.loads(raw) if len(raw) <= MAX_NOTIFICATION_BYTES else None
            except (ValueError, UnicodeError):
                item = None

            if not isinstance(item, dict) or not isinstance(item.get('payload'), dict):
                if is_legacy_slot:
                    slot_fd = _notification_slot(entry.stem)
                    try:
                        os.ftruncate(slot_fd, 0)
                    finally:
                        os.close(slot_fd)
                else:
                    _safe_unlink(entry)
                continue

            host = item.get('agent_id') or _infer_host_from_filename(entry.name)
            if host not in HOSTS:
                if not is_legacy_slot:
                    _safe_unlink(entry)
                continue

            if item.get('base_url') != target_base:
                continue

            age = time.time() - float(item.get('created_at', 0))
            if age < 0 or age > 60:
                if is_legacy_slot:
                    slot_fd = _notification_slot(host)
                    try:
                        os.ftruncate(slot_fd, 0)
                    finally:
                        os.close(slot_fd)
                else:
                    _safe_unlink(entry)
                continue

            token = get_token_securely(host)
            if not token or hashlib.sha256(token.encode()).hexdigest() != item.get('credential_hash'):
                if is_legacy_slot:
                    slot_fd = _notification_slot(host)
                    try:
                        os.ftruncate(slot_fd, 0)
                    finally:
                        os.close(slot_fd)
                else:
                    _safe_unlink(entry)
                continue

            if not detect_foreign_hook_host(host, item.get('payload')):
                post_to_contextcore(
                    host,
                    item.get('event', ''),
                    {**item['payload'], 'context_request': False},
                    base_url=base_url,
                    timeout=2.0,
                    read_response=False,
                    preserve_missing_cwd=True,
                )
            if is_legacy_slot:
                slot_fd = _notification_slot(host)
                try:
                    os.ftruncate(slot_fd, 0)
                finally:
                    os.close(slot_fd)
            else:
                _safe_unlink(entry)
            delivered += 1
        except (OSError, ValueError, TypeError):
            continue
    return delivered


def main() -> int:
    args = list(sys.argv[1:])
    base_url = os.environ.get("CONTEXTCORE_BASE_URL", "http://127.0.0.1:8420")
    if "--base-url" in args:
        idx = args.index("--base-url")
        if idx + 1 < len(args):
            base_url = args[idx + 1]
            del args[idx:idx + 2]
    agent_id = args[0] if len(args) > 0 else "codex"
    event_arg = args[1] if len(args) > 1 else "unknown"

    raw_input = _read_host_stdin(MAX_NOTIFICATION_BYTES + 1)

    payload = {}
    if len(raw_input) <= MAX_NOTIFICATION_BYTES and raw_input.strip():
        try:
            payload = json.loads(raw_input)
        except json.JSONDecodeError:
            payload = {"raw": raw_input}

    if not detect_foreign_hook_host(agent_id, payload, env=os.environ):
        enqueue_notification(agent_id, event_arg, payload, base_url=base_url)
    output = format_host_response(agent_id, event_arg, None)

    sys.stdout.write(json.dumps(output))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
