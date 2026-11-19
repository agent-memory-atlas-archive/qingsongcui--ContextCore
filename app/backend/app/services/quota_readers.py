"""Explicit, read-only quota adapters. Never return raw provider or app-server messages."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import signal
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

import httpx


class QuotaUnavailable(Exception):
    def __init__(self, status):
        self.status = status


# Matching is on the entire configured base URL. Never forward credentials to
# user-supplied quota URLs, alternate origins, proxies or redirects.
BASES = {
    "https://api.deepseek.com": "deepseek", "https://api.deepseek.com/v1": "deepseek",
    "https://openrouter.ai/api/v1": "openrouter", "https://api.moonshot.cn/v1": "moonshot",
    "https://api.anthropic.com": "anthropic", "https://api.anthropic.com/v1": "anthropic",
    "https://api.openai.com/v1": "openai", "https://api.siliconflow.cn/v1": "siliconflow",
}
ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/user/balance",
    "openrouter": "https://openrouter.ai/api/v1/key",
    "moonshot": "https://api.moonshot.cn/v1/users/me/balance",
}
DASHBOARDS = {
    "codex": "https://chatgpt.com/codex/settings/usage",
    "claude": "https://claude.ai/settings/usage",
    "openai": "https://platform.openai.com/usage",
    "anthropic": "https://platform.claude.com/settings/billing",
    "deepseek": "https://platform.deepseek.com/usage",
    "openrouter": "https://openrouter.ai/settings/credits",
    "moonshot": "https://platform.moonshot.cn/console/account",
    "siliconflow": "https://cloud.siliconflow.cn/",
}


def decimal_amount(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)) or len(str(value)) > 48:
        raise QuotaUnavailable("unreadable")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise QuotaUnavailable("unreadable") from None
    if not amount.is_finite() or abs(amount.as_tuple().exponent) > 32:
        raise QuotaUnavailable("unreadable")
    return format(amount, "f")


def http_status(code):
    if code in (401, 403):
        return "unauthorized"
    if code == 429:
        return "rate_limited"
    if code in (404, 405):
        return "unsupported"
    return "offline" if code >= 500 else "unreadable"


async def _get_official_json(client, url):
    try:
        async with asyncio.timeout(12), client.stream("GET", url) as response:
            if response.status_code != 200:
                raise QuotaUnavailable(http_status(response.status_code))
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 65536:
                    raise QuotaUnavailable("unreadable")
                body.extend(chunk)
        return json.loads(body, parse_float=Decimal)
    except (httpx.HTTPError, TimeoutError):
        raise QuotaUnavailable("offline") from None
    except (ValueError, RecursionError):
        raise QuotaUnavailable("unreadable") from None


async def read_balance(source, key, transport=None):
    if not isinstance(key, str) or not key.isascii() or not key.isprintable() or not key.strip():
        raise QuotaUnavailable("unauthorized")
    account_balance, account_error = None, None
    try:
        async with httpx.AsyncClient(timeout=8, trust_env=False, follow_redirects=False, transport=transport,
                                     headers={"Authorization": f"Bearer {key}"}) as client:
            result = await _get_official_json(client, ENDPOINTS[source])
            # The credits endpoint is restricted to management keys. Keep a key's
            # separate budget even when the account-level query is denied.
            if source == "openrouter" and result["data"].get("is_management_key") is True:
                try:
                    credits = (await _get_official_json(client, "https://openrouter.ai/api/v1/credits"))["data"]
                    bought = Decimal(decimal_amount(credits["total_credits"]))
                    spent = Decimal(decimal_amount(credits["total_usage"]))
                    with localcontext() as context:
                        context.prec = 128
                        account_balance = {"unit": "USD", "amount": format(bought - spent, "f"), "scope": "account"}
                except QuotaUnavailable as exc:
                    account_error = exc.status
                except (KeyError, ValueError, TypeError):
                    account_error = "unreadable"
        snapshot = {"windows": [], "balances": []}
        if source == "deepseek":
            for item in result["balance_infos"]:
                unit = item["currency"]
                if unit not in ("USD", "CNY"):
                    raise QuotaUnavailable("unreadable")
                snapshot["balances"].append({"unit": unit, "amount": decimal_amount(item["total_balance"])})
        elif source == "moonshot":
            if result.get("code") != 0 or result.get("status") is not True:
                raise QuotaUnavailable("unreadable")
            snapshot["balances"].append({"unit": "CNY", "amount": decimal_amount(result["data"]["available_balance"])})
        else:
            remaining = result["data"]["limit_remaining"]
            if remaining is None and "limit" in result["data"] and result["data"]["limit"] is None:
                snapshot["no_key_limit"] = True
            else:
                snapshot["balances"].append({"unit": "USD", "amount": decimal_amount(remaining)})
        if account_balance is not None:
            snapshot["balances"].append(account_balance)
        if account_error:
            snapshot["account_balance_status"] = account_error
        if not snapshot["balances"] and not snapshot.get("no_key_limit"):
            raise QuotaUnavailable("unreadable")
        return snapshot
    except (httpx.HTTPError, TimeoutError):
        raise QuotaUnavailable("offline") from None
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise QuotaUnavailable("unreadable") from None


def codex_command(home):
    found = shutil.which("codex")
    locations = [home / ".local/bin/codex", Path("/opt/homebrew/bin/codex"),
                 Path("/usr/local/bin/codex"), Path("/Applications/Codex.app/Contents/Resources/codex")]
    if found and Path(found).is_absolute():
        locations.insert(0, Path(found))
    for path in locations:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise QuotaUnavailable("not_installed")


def _codex_snapshot(identity, result):
    snapshot = {"windows": [], "balances": []}
    if isinstance(result.get("ordinaryUsageAllowed"), bool):
        snapshot["ordinary_usage_allowed"] = result["ordinaryUsageAllowed"]
    plan = identity.get("planType")
    if isinstance(plan, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", plan):
        snapshot["plan"] = plan
    email = identity.get("email")
    if isinstance(email, str) and re.fullmatch(r"[^\s@]+@[\w.-]{1,120}", email):
        user, domain = email.rsplit("@", 1)
        snapshot["identity_label"] = user[:1] + "…@" + domain
    multi = result.get("rateLimitsByLimitId")
    buckets = multi if isinstance(multi, dict) and multi else {"codex": result["rateLimits"]}
    if len(buckets) > 64:
        raise QuotaUnavailable("unreadable")
    for bucket, data in buckets.items():
        if not isinstance(data, dict) or not re.fullmatch(r"[\w. -]{1,100}", bucket):
            raise QuotaUnavailable("unreadable")
        for rank in ("primary", "secondary"):
            window = data.get(rank)
            if window is None:
                continue
            used = window["usedPercent"]
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or not 0 <= used <= 100:
                raise QuotaUnavailable("unreadable")
            minutes, reset = window.get("windowDurationMins"), window.get("resetsAt")
            if minutes is not None and (type(minutes) is not int or minutes < 1):
                raise QuotaUnavailable("unreadable")
            if reset is not None and (type(reset) is not int or not 0 <= reset < 253402300800):
                raise QuotaUnavailable("unreadable")
            reset_time = datetime.fromtimestamp(reset, timezone.utc).isoformat() if reset is not None else None
            snapshot["windows"].append({"label": bucket, "rank": rank, "used_percent": used,
                                        "minutes": minutes, "resets_at": reset_time})
        credits = data.get("credits")
        if isinstance(credits, dict):
            if credits.get("unlimited") is True:
                snapshot["credits_unlimited"] = True
            elif credits.get("balance") is not None:
                balance = {"unit": "credits", "amount": decimal_amount(credits["balance"])}
                if balance not in snapshot["balances"]:
                    snapshot["balances"].append(balance)
    if not snapshot["balances"] and not snapshot["windows"] and not snapshot.get("credits_unlimited"):
        raise QuotaUnavailable("unreadable")
    return snapshot


async def read_codex(home, *, isolated=False):
    executable = codex_command(home)
    profile = str(home / ".codex") if isolated else os.environ.get("CODEX_HOME", str(home / ".codex"))
    env = {"HOME": str(home), "CODEX_HOME": profile, "PATH": os.environ.get("PATH", os.defpath)}
    try:
        proc = await asyncio.create_subprocess_exec(
            executable, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, cwd=home, env=env, start_new_session=True, limit=131072,
        )
    except OSError:
        raise QuotaUnavailable("not_installed") from None

    async def send(message):
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        await proc.stdin.drain()

    async def call(request_id, method, params=None):
        message = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await send(message)
        for _ in range(64):
            line = await proc.stdout.readline()
            if not line:
                break
            reply = json.loads(line)
            if reply.get("id") != request_id or "method" in reply:
                continue
            if "error" in reply:
                error = reply["error"]
                if error.get("code") == -32601:
                    raise QuotaUnavailable("unsupported")
                # Some app-server versions return upstream status only inside the message.
                message = str(error.get("message", "")).lower()
                if "429" in message or "too many requests" in message:
                    raise QuotaUnavailable("rate_limited")
                if "401" in message or "403" in message or "not logged in" in message:
                    raise QuotaUnavailable("unauthorized")
                if any(reason in message for reason in ("timed out", "timeout", "error sending request", "connection refused")):
                    raise QuotaUnavailable("offline")
                raise QuotaUnavailable("unreadable")
            return reply["result"]
        raise QuotaUnavailable("unreadable")

    try:
        async with asyncio.timeout(12):
            await call(1, "initialize", {"clientInfo": {"name": "senrelay", "title": "Senrelay", "version": "1"}})
            await send({"method": "initialized"})
            account = (await call(2, "account/read", {"refreshToken": False})).get("account")
            if account is None:
                raise QuotaUnavailable("unauthorized")
            if account.get("type") != "chatgpt":
                raise QuotaUnavailable("unsupported")
            result = await call(3, "account/rateLimits/read")
            after = (await call(4, "account/read", {"refreshToken": False})).get("account")
            if after != account:
                raise QuotaUnavailable("identity_changed")
            return _codex_snapshot(account, result)
    except TimeoutError:
        raise QuotaUnavailable("offline") from None
    except (ValueError, OSError, KeyError, TypeError, AttributeError, RecursionError):
        raise QuotaUnavailable("unreadable") from None
    finally:
        # Give app-server an EOF first so it can close its own native state.
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 1)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                await asyncio.wait_for(proc.wait(), 1)
            except TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            except ProcessLookupError:
                pass
            await proc.wait()
