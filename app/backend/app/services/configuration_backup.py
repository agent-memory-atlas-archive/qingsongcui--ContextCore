"""Restore portable Senrelay definitions with stable references and one SQLite commit."""
from __future__ import annotations

import copy
import hashlib
import re
from contextlib import nullcontext
from datetime import datetime, timezone
from uuid import uuid4

from app.integrations.config_transactions import ConfigurationError, canonical, reject_secrets
from app.services.management_service import ENTITY_KINDS, configuration_locked

_ID = re.compile(r'[a-z]+_[A-Za-z0-9_-]{1,90}')
_PRIVATE = {'credential_ref', 'oauth_flow', 'token_type', 'expires_at', 'authorization_expires_at'}
_HEADER = {'id', 'version', 'updated_at', 'created_at', 'has_credential', 'previous_credential_cleanup'}
_MAX_BYTES = 32 * 1024 * 1024


def _entries(document):
    if not isinstance(document, dict) or document.get('format') != 'senrelay-management' or type(document.get('version')) is not int or document['version'] != 1:
        raise ConfigurationError('Select a Senrelay management backup, version 1', 422)
    rows = document.get('objects')
    if not isinstance(rows, list) or len(rows) > 20000 or len(canonical(document).encode()) > _MAX_BYTES:
        raise ConfigurationError('Backup exceeds 32 MiB or 20000 definitions', 422)
    entries = {}
    for row in rows:
        if not isinstance(row, dict) or row.get('entity_kind') not in ENTITY_KINDS or not isinstance(row.get('data'), dict):
            raise ConfigurationError('Invalid backup definition', 422)
        data, kind = row['data'], row['entity_kind']
        identifier = data.get('id')
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or not identifier.startswith(kind + '_') or identifier in entries:
            raise ConfigurationError('Backup identifiers must be unique Senrelay resource identifiers', 422)
        if any(key in data for key in _PRIVATE) or 'keychain:senrelay:' in canonical(data).casefold():
            raise ConfigurationError('Backup includes a local credential or OAuth session', 422)
        data = {key: val for key, val in data.items() if key not in _HEADER}
        if kind == 'asset' and data.get('mcp_account_bindings'):
            raise ConfigurationError('Reattach MCP accounts locally after restore', 422)
        if kind == 'account':
            data.pop('quota', None)
            data['status'] = 'unconfigured' if data.get('kind') == 'api_key' else 'unsupported'
        reject_secrets(data)
        entries[identifier] = (kind, data)
    return entries


def _equivalent(kind, data, old):
    # The export deliberately drops private and volatile state. Compare only the
    # portable part so restoring one's own backup never discards credentials.
    public = {key: val for key, val in old.items() if key not in _PRIVATE | _HEADER}
    if kind == 'account':
        public.pop('quota', None)
        public['status'] = data['status']
    if kind == 'provider' and (old.get('credential_ref') or old.get('account_id') or old.get('credential_source') == 'account'):
        public['credential_reattach_required'] = True
    return public == data


@configuration_locked
def restore(manager, document, replace_existing=False, preview_id=None, confirmed=False):
    if type(replace_existing) is not bool or type(confirmed) is not bool:
        raise ConfigurationError('Invalid backup restore options', 422)
    apply = preview_id is not None
    if apply and (not confirmed or not isinstance(preview_id, str) or not re.fullmatch(r'[a-f0-9]{96}', preview_id)):
        raise ConfigurationError('Confirm a current backup preview first', 422)
    entries = _entries(document)
    file_hash = hashlib.sha256(canonical(document).encode()).hexdigest()
    with manager.connect() as conn:
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        if apply:
            conn.execute('CREATE TABLE IF NOT EXISTS management_backup_receipts(id TEXT PRIMARY KEY, file_hash TEXT NOT NULL, replace_existing INTEGER NOT NULL, changed INTEGER NOT NULL)')
            prior = conn.execute('SELECT * FROM management_backup_receipts WHERE id=?', (preview_id,)).fetchone()
            if prior:
                if prior['file_hash'] != file_hash or prior['replace_existing'] != replace_existing:
                    raise ConfigurationError('Backup differs from the reviewed preview')
                return {'status': 'restored', 'changed': prior['changed'], 'preview_id': preview_id}
        changes, prepared, revisions = [], {}, []
        for identifier, (kind, data) in entries.items():
            row = conn.execute('SELECT * FROM management_entities WHERE id=?', (identifier,)).fetchone()
            old = manager._row(row) if row else None
            if row and row['kind'] != kind:
                raise ConfigurationError('Backup resource kind conflicts with an existing definition')
            protected = old and (old.get('kind') != data.get('kind') or any(old.get(key) for key in _PRIVATE) or
                                 (kind == 'provider' and old.get('account_id')) or old.get('mcp_account_bindings') or 'keychain:senrelay:' in canonical(old))
            if not old:
                action = 'create'
            elif _equivalent(kind, data, old):
                action = 'unchanged'
            elif protected:
                action = 'keep_protected'
            else:
                action = 'replace' if replace_existing else 'keep'
            project_detached = False
            if action in ('create', 'replace') and data.get('project_id'):
                project_table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone()
                if not project_table or not conn.execute('SELECT 1 FROM projects WHERE id=?', (data['project_id'],)).fetchone():
                    data = {**data, 'project_id': None}
                    project_detached = True
            if action in ('create', 'replace'):
                prepared[identifier] = (kind, data)
            changes.append({'id': identifier, 'kind': kind, 'name': str(data.get('name') or data.get('host_id') or identifier)[:200],
                            'action': action, 'project_detached': project_detached,
                            'reattach_required': bool(data.get('credential_reattach_required') or (not old and kind == 'account'))})
            # An export can recreate the same stable ID at version one after
            # deletion. Bind approval to the actual state shown in the preview.
            state_hash = hashlib.sha256(canonical(old).encode()).hexdigest() if old else None
            revisions.append((identifier, state_hash, action, project_detached))
        nonce = preview_id[:32] if apply else uuid4().hex
        token = nonce + hashlib.sha256(canonical([nonce, file_hash, replace_existing, revisions]).encode()).hexdigest()
        if apply and preview_id != token:
            raise ConfigurationError('Local definitions changed since the backup preview')
        # Validate references against the final graph, including cycles between
        # accounts and providers. Never bind to partially inserted rows.
        view = copy.copy(manager)
        view.connect = lambda: nullcontext(conn)
        def lookup(kind, entity_id):
            if entity_id in prepared:
                actual_kind, value = prepared[entity_id]
                if actual_kind != kind:
                    raise ConfigurationError('Backup reference points to the wrong resource kind', 422)
                return {**value, 'id': entity_id}
            return manager._row(conn.execute('SELECT * FROM management_entities WHERE kind=? AND id=?', (kind, entity_id)).fetchone())
        view.get = lookup
        now = datetime.now(timezone.utc).isoformat()
        for identifier, (kind, value) in prepared.items():
            value = view._validate(kind, value, entity_id=identifier)
            if not apply:
                continue
            conn.execute('''INSERT INTO management_entities VALUES(?,?,?,1,?,?)
                ON CONFLICT(id) DO UPDATE SET data=excluded.data,version=version+1,updated_at=excluded.updated_at''',
                         (identifier, kind, canonical(value), now, now))
            if kind == 'asset':
                conn.execute('INSERT INTO management_asset_versions SELECT id,version,data,updated_at FROM management_entities WHERE id=?', (identifier,))
        if apply:
            conn.execute('INSERT INTO management_backup_receipts VALUES(?,?,?,?)', (token, file_hash, replace_existing, len(prepared)))
            conn.execute('INSERT INTO audit_logs VALUES(?,?,?,?,?,?)', ('backup_' + token, 'user', 'restore_management_backup', 'management', 'restored', now))
        return {'status': 'restored' if apply else 'preview', 'preview_id': token, 'changed': len(prepared), 'items': changes}
