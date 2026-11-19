"""Undo the latest managed change from its exact private snapshot and ownership receipt."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.integrations.config_transactions import ConfigurationError, canonical, digest
from app.services.management_service import configuration_locked


def remember_before(conn, plan):
    host = plan['payload']['metadata'].get('host_id')
    if not host:
        return
    previous = conn.execute('SELECT data FROM management_activations WHERE host_id=?', (host,)).fetchone()
    active = json.loads(previous[0]) if previous else {}
    # A saved credential must never be reinstated after the user rotated it
    # or removed the account. Retain only stable definition versions.
    deps = dict(plan['payload']['metadata'].get('entity_versions', {}))
    if active.get('last_plan_id'):
        row = conn.execute("SELECT json_extract(payload,'$.metadata.entity_versions') FROM management_plans WHERE id=?",
                           (active['last_plan_id'],)).fetchone()
        if row and row[0]:
            deps.update(json.loads(row[0]))
    if active.get('provider_id') and active['provider_id'] not in deps:
        row = conn.execute("""SELECT json_extract(payload,'$.metadata.entity_versions') FROM management_plans
            WHERE status='applied' AND json_extract(payload,'$.metadata.host_id')=?
            AND json_extract(payload,'$.metadata.activate_provider') AND json_extract(payload,'$.metadata.provider_id')=?
            ORDER BY updated_at DESC,id DESC LIMIT 1""", (host, active['provider_id'])).fetchone()
        if row and row[0]:
            deps.update(json.loads(row[0]))
    conn.execute('INSERT INTO management_undo_states VALUES(?,?,?)', (plan['id'], previous[0] if previous else None, canonical(deps)))


def _eligible(manager, source):
    meta = source['payload']['metadata']
    if source['status'] != 'applied' or not meta.get('host_id') or meta.get('host_routing') or meta.get('undo_plan_id') or source['payload'].get('renderer'):
        raise ConfigurationError('Choose an applied native configuration change; connection modes have their own restore action', 409)
    with manager.connect() as conn:
        latest = conn.execute("""SELECT id FROM management_plans WHERE json_extract(payload,'$.metadata.host_id')=?
            AND status IN ('applied','applying','recovery_required') ORDER BY updated_at DESC,id DESC LIMIT 1""", (meta['host_id'],)).fetchone()
        available = conn.execute("SELECT 1 FROM sqlite_master WHERE name='management_undo_states'").fetchone()
        saved = conn.execute('SELECT dependencies FROM management_undo_states WHERE plan_id=?', (source['id'],)).fetchone() if available else None
    if not latest or latest[0] != source['id']:
        raise ConfigurationError('Only the most recent applied change on this assistant can be undone', 409)
    if not saved:
        raise ConfigurationError('This older change has no undo receipt; use the original configuration restore', 409)
    versions = json.loads(saved[0])
    with manager.connect() as conn:
        for key, version in versions.items():
            current = conn.execute('SELECT version FROM management_entities WHERE id=?', (key,)).fetchone()
            if not current or current[0] != version:
                raise ConfigurationError('Previous definitions or credentials changed; create a fresh switch instead', 409)
    return versions


def _check_files(manager, source):
    tx = manager.transactions
    for item in source['payload']['files']:
        if 'after_state' in item:
            raw, state = tx._skill_read(item['path'], [item['after_state']])
            if state != item['after_state']:
                raise ConfigurationError('Skill changed after apply', 409)
        else:
            raw = tx._read(Path(item['path']))
        if (digest(raw) if raw is not None else None) != item['after_hash']:
            raise ConfigurationError('Native file changed after apply; review it before undo', 409)
        for change in item['changes']:
            owned = tx._owned(item['path'], change['key'])
            expected = None if change['operation'] == 'uninstall' else {
                'path': item['path'], 'item_key': change['key'], 'owner': change['owner'], 'value_hash': change['value_hash']}
            if owned != expected:
                raise ConfigurationError('Configuration ownership changed after apply', 409)


def snapshot_bytes(tx, item):
    raw = None
    if item.get('snapshot'):
        path = Path(item['snapshot'])
        if path.parent != tx.state_dir or any(part.is_symlink() for part in [path, *path.parents]):
            raise ConfigurationError('Invalid undo snapshot', 409)
        try:
            raw = path.read_bytes()
        except OSError:
            raise ConfigurationError('Undo snapshot is unavailable', 409) from None
    if (digest(raw) if raw is not None else None) != item['before_hash']:
        raise ConfigurationError('Undo snapshot failed integrity verification', 409)
    return raw


@configuration_locked
def preview(manager, plan_id):
    tx = manager.transactions
    source = tx.get(plan_id)
    deps = _eligible(manager, source)
    from app.services.host_config_roots import check_plan
    check_plan(manager, source)
    _check_files(manager, source)
    meta = source['payload']['metadata']
    files = []
    for item in source['payload']['files']:
        snapshot_bytes(tx, item)
        specs, changes = [], []
        for original in item['specs']:
            key = tx._item_key(original)
            owned = tx._owned(item['path'], key)
            spec = {k: original[k] for k in ('kind', 'path', 'pointer', 'marker', 'native_identity', 'managed_skill') if k in original}
            if original.get('native_origin'):
                spec.update(native_origin=original['native_origin'], native_was_removed=original.get('status') == 'removed')
            specs.append({**spec, 'original_ownership': owned})
            prior = original.get('original_ownership')
            change = {'path': item['path'], 'key': key, 'operation': 'uninstall'}
            if prior:
                change.update(operation='install', owner=prior['owner'], value_hash=prior['value_hash'])
            changes.append(change)
        file = {'path': item['path'], 'before_hash': item['after_hash'], 'after_hash': item['before_hash'],
                'specs': specs, 'stage': 'pending', 'undo_changes': changes,
                'diff': 'Restore this file to the checked snapshot from immediately before the selected change.'}
        if 'before_state' in item:
            file.update(before_state=item['after_state'], after_state=item['before_state'],
                        before_registry=tx._skill_registry(item['path']), undo_registry=item['before_registry'])
        files.append(file)
    metadata = {'host_id': meta['host_id'], 'operation': 'undo', 'scope': meta.get('scope'), 'entity_versions': deps,
                'undo_plan_id': plan_id, 'configuration_changes': [
                    {**change, 'operation': 'undo'} for change in meta.get('configuration_changes', [])]}
    for key in ('root_scope', 'shared_configuration', 'selection_config_only', 'configuration_notice'):
        if key in meta:
            metadata[key] = meta[key]
    payload = {'files': files, 'metadata': metadata, 'renderer': 'managed_undo', 'readback': 'not_run',
               'host_reload': 'not_run', 'real_request': 'not_run'}
    new_id = 'plan_' + uuid4().hex
    now = datetime.now(timezone.utc).isoformat()
    with manager.connect() as conn:
        conn.execute("INSERT INTO management_plans VALUES(?,1,'planned',?,?,?)", (new_id, canonical(payload), now, now))
    return tx.get(new_id)


def render(manager, plan):
    source = manager.transactions.get(plan['payload']['metadata']['undo_plan_id'])
    _eligible(manager, source)
    from app.services.host_config_roots import check_plan
    check_plan(manager, source)
    _check_files(manager, source)
    return {item['path']: snapshot_bytes(manager.transactions, item) for item in source['payload']['files']}


def restore_receipt(conn, plan):
    meta = plan['payload']['metadata']
    baseline = conn.execute('SELECT selection FROM management_undo_states WHERE plan_id=?', (meta['undo_plan_id'],)).fetchone()
    if not baseline:
        raise ConfigurationError('Undo receipt disappeared', 409)
    for item in plan['payload']['files']:
        for spec in item['specs']:
            if spec.get('native_origin'):
                conn.execute('UPDATE management_native_origins SET was_managed=? WHERE entity_id=?',
                             (int(spec['native_was_removed']), spec['native_origin']))
    if baseline[0] is None:
        conn.execute('DELETE FROM management_activations WHERE host_id=?', (meta['host_id'],))
    else:
        conn.execute('INSERT OR REPLACE INTO management_activations VALUES(?,?,?)',
                     (meta['host_id'], baseline[0], datetime.now(timezone.utc).isoformat()))
