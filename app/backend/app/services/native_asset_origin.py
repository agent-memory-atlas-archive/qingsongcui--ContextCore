"""Relate a saved native resource to its physical source before writing any host."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from app.integrations.config_transactions import ConfigurationError, canonical
from app.services.management_service import HOSTS

FIELDS = {'mcp': 'mcp_path', 'skill': 'skill_dir', 'prompt': 'prompt_path'}
BLOCKED = {'native_read_only', 'native_source_changed', 'native_source_missing'}


def _root(host, kind):
    return host.get(FIELDS[kind]) or (host.get('prompt_dir') if kind == 'prompt' else None)


def _read(manager, path):
    from app.services.native_catalog import _read as read_native
    return read_native(path, 4 * 1024 * 1024)


def _inspect(manager, host, kind, row, cache, *, owned=False):
    root = _root(host, kind)
    if not root or os.path.normpath(root) != row['relative_path']:
        return None
    target = manager.home / root
    key = row['entry_name']
    if '/' in key or key in ('', '.', '..') or '\x00' in key:
        return {'status': 'native_read_only'}
    if kind == 'skill':
        marker = target / key / 'SKILL.md'
        try:
            manager.transactions.safe_path(str(marker))
            status = 'native_read_only' if marker.is_file() else 'native_source_missing'
        except ConfigurationError:
            status = 'native_read_only'
        return {'status': status}
    if kind == 'prompt' and not host.get('prompt_path'):
        target = target / key
    code = {'path': str(target), 'kind': 'file' if kind == 'prompt' else ('toml_table' if host.get('format') == 'toml' else host.get('format'))}
    if kind == 'mcp':
        code['pointer'] = [host.get('container', 'mcp_servers'), key]
    if owned:
        return {**code, 'status': 'managed'}
    try:
        if target not in cache:
            raw = _read(manager, target)
            if kind == 'mcp' and raw is not None:
                from app.services.native_catalog import _document
                document = _document(raw, host.get('format'))
                collection = document.get(code['pointer'][0], {})
                cache[target] = collection if isinstance(collection, dict) else {}
            else:
                cache[target] = raw
        raw = cache[target]
    except (OSError, ValueError, TypeError):
        return {**code, 'status': 'native_read_only'}
    if kind == 'mcp':
        value = raw.get(key) if isinstance(raw, dict) else None
        checksum = hashlib.sha256(canonical(value).encode()).hexdigest() if value is not None else None
    else:
        checksum = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if not checksum:
        # Reinstalling a formerly managed resource reuses its original location.
        status = 'removed' if row['was_managed'] else 'native_source_missing'
    elif checksum != row['value_hash']:
        status = 'native_source_changed'
    elif kind == 'mcp' and (code['kind'] not in ('json','toml_table') or not row['portable_entry']):
        status = 'native_read_only'
    else:
        status = 'native_available'
    if status == 'native_available':
        code['takeover_expected_hash'] = checksum if kind == 'mcp' else hashlib.sha256(canonical(raw.decode()).encode()).hexdigest()
    return {**code, 'status': status}


def _legacy_matches(manager, host, kind, source):
    path = dict(source).get('path')
    root = _root(host, kind)
    if not root:
        return False
    if path:
        return os.path.normpath(path) == str(manager.home / root)
    prior = dict(source).get('host_id')
    return prior in HOSTS and root == _root(HOSTS[prior], kind)


def descriptors(manager, host_id, resources, limit=500):
    cache, result = {}, {}
    host = manager.host_config(host_id)
    with manager.connect() as conn:
        rows = conn.execute("""SELECT o.*,e.kind,json_extract(e.data,'$.kind') asset_kind FROM management_native_origins o
            JOIN management_entities e ON e.id=o.entity_id WHERE e.kind='asset' AND o.relative_path IN (?,?,?)
            ORDER BY o.entity_id LIMIT ?""", (*[_root(host, kind) for kind in FIELDS], limit + 1)).fetchall()
        legacy = conn.execute("""SELECT e.id,json_extract(e.data,'$.kind') kind,json_extract(e.data,'$.source.host_id') host_id,json_extract(e.data,'$.source.path') path
            FROM management_entities e WHERE e.kind='asset' AND json_extract(e.data,'$.source.type')='host_config'
            AND NOT EXISTS(SELECT 1 FROM management_native_origins o WHERE o.entity_id=e.id)
            ORDER BY e.id LIMIT ?""", (limit+1,)).fetchall()
    for row in rows[:limit]:
        if resources.get(row['entity_id'], {}).get('paths'):
            continue
        view = _inspect(manager, host, row['asset_kind'], row, cache)
        if view:
            result[row['entity_id']] = {'kind': 'asset', 'paths': 0, 'status': view['status'], 'native_origin': True}
    for row in legacy[:limit]:
        if row['id'] not in resources and row['kind'] in FIELDS and _legacy_matches(manager, host, row['kind'], row):
            result[row['id']] = {'kind':'asset','paths':0,'status':'native_read_only','native_origin':True}
    return result, len(rows) > limit or len(legacy) > limit


def plan_target(manager, host_id, asset, operation):
    kind = asset['kind']
    host = manager.host_config(host_id)
    with manager.connect() as conn:
        row = conn.execute('SELECT * FROM management_native_origins WHERE entity_id=?', (asset['id'],)).fetchone()
        if not row:
            source = asset.get('source') or {}
            if source.get('type') == 'host_config' and _legacy_matches(manager, host, kind, source):
                raise ConfigurationError('Native source is outside managed ownership; distribute to another assistant or read it again', 409)
            return None
        owned = conn.execute("SELECT 1 FROM management_owned WHERE owner=? AND path=?",
                             (asset['id'], str(manager.home / row['relative_path'] / row['entry_name']
                              if kind == 'prompt' and not host.get('prompt_path') else manager.home / row['relative_path']))).fetchone()
    result = _inspect(manager, host, kind, row, {}, owned=bool(owned))
    if not result:
        return None
    if result['status'] in BLOCKED:
        raise ConfigurationError('Native source already belongs to this assistant or changed; use its native editor or distribute to another assistant', 409)
    if operation == 'uninstall' and not owned:
        raise ConfigurationError('The original native resource has not been taken under management', 409)
    if kind == 'skill' or (kind == 'mcp' and result['kind'] not in ('json','toml_table')):
        raise ConfigurationError('Native source has no lossless in-place renderer for this format', 409)
    result.update(owner=asset['id'], operation=operation, native_origin=asset['id'])
    if result['status'] == 'native_available':
        result['takeover_selected_field'] = True
    return result


def record(conn, entity_id, source, fingerprint, portable):
    path, name = source
    conn.execute("""INSERT INTO management_native_origins(entity_id,relative_path,entry_name,value_hash,portable_entry,was_managed)
        VALUES(?,?,?,?,?,0) ON CONFLICT(entity_id) DO UPDATE SET relative_path=excluded.relative_path,
        entry_name=excluded.entry_name,value_hash=excluded.value_hash,portable_entry=excluded.portable_entry""",
        (entity_id, path, name, fingerprint, int(portable)))
