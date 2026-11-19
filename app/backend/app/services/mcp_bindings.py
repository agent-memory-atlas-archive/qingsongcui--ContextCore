"""Bind reviewed MCP credential fields to explicit Senrelay API-key accounts."""
from __future__ import annotations

import copy
import re

from app.integrations.config_transactions import ConfigurationError


def requires_review(asset):
    return bool(asset.get('native_review_required') or asset.get('credential_reattach_required'))


def settings(manager, asset, *, resolve=False):
    config = copy.deepcopy(asset.get('config', {})) if resolve else asset.get('config', {})
    bindings = asset.get('mcp_account_bindings', [])
    if not isinstance(bindings, list) or len(bindings) > 32:
        raise ConfigurationError('Select up to 32 MCP account fields', 422)
    dependencies = {}
    seen = set()
    for item in bindings:
        if not isinstance(item, dict) or set(item) != {'account_id', 'name', 'location', 'bearer'}:
            raise ConfigurationError('Invalid MCP account binding', 422)
        name = item['name']
        location = 'env' if config.get('transport', 'stdio') == 'stdio' else 'headers'
        pattern = r'[A-Za-z_][A-Za-z0-9_]{0,127}' if location == 'env' else r'[A-Za-z][A-Za-z0-9_-]{0,126}'
        if (item['location'] != location or not isinstance(name, str) or not re.fullmatch(pattern, name)
                or type(item['bearer']) is not bool or not isinstance(item['account_id'], str)):
            raise ConfigurationError('MCP credential field must match the selected transport', 422)
        normal = name if location == 'env' else name.lower()
        existing = {key if location == 'env' else key.lower() for key in asset['config'].get(location, {})}
        if normal in seen or normal in existing:
            raise ConfigurationError('MCP credential field is already configured', 422)
        seen.add(normal)
        account = manager.get('account', item['account_id'])
        if account.get('kind') != 'api_key' or not account.get('credential_ref'):
            raise ConfigurationError('Bind a locally configured API-key account for this MCP field', 409)
        dependencies[account['id']] = account['version']
        if resolve:
            config.setdefault(location, {})[name] = ('Bearer ' if item['bearer'] else '') + '{{credential:' + account['credential_ref'] + '}}'
    return config, dependencies
