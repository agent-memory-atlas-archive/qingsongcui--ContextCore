"""Track only the resource entries introduced by a profile on native files."""
from __future__ import annotations

import json

from app.integrations.config_transactions import canonical


def exclusive_resources(active, plan):
    selected = set(plan['payload']['metadata'].get('bundle_assets', []))
    inherited = set(active.get('bundle_exclusive_assets', []))
    preexisting = set()
    for file in plan['payload']['files']:
        for spec in file.get('specs', []):
            if spec.get('original_ownership'):
                preexisting.add(spec['owner'])
    return sorted(selected.intersection(inherited).union(selected - preexisting))


def release_independently_managed(conn, plan):
    """A later explicit edit through any surface sharing a file keeps its resource."""
    meta = plan['payload']['metadata']
    touched = {(file['path'], spec.get('owner')) for file in plan['payload']['files']
               for spec in file.get('specs', []) if str(spec.get('owner', '')).startswith('asset_')}
    if not touched:
        return
    touched_ids = {owner for _, owner in touched}
    for active in conn.execute("""SELECT host_id,json_extract(data,'$.bundle_exclusive_assets') ids,
            json_extract(data,'$.bundle_plan_id') plan_id FROM management_activations
            WHERE json_array_length(data,'$.bundle_exclusive_assets')>0""").fetchall():
        if meta.get('bundle_id') and meta['host_id'] == active['host_id']:
            continue
        exclusive = set(json.loads(active['ids']))
        candidates = exclusive & touched_ids
        if not candidates:
            continue
        if meta['host_id'] == active['host_id']:
            released = candidates
        else:
            recorded = conn.execute("""SELECT json_extract(f.value,'$.path') path,json_extract(s.value,'$.owner') owner
                FROM management_plans p,json_each(p.payload,'$.files') f,json_each(f.value,'$.specs') s WHERE p.id=?""",
                                    (active['plan_id'],))
            released = {row['owner'] for row in recorded if row['owner'] in candidates and (row['path'], row['owner']) in touched}
        if released:
            conn.execute("""UPDATE management_activations SET data=json_set(data,'$.bundle_exclusive_assets',json(?))
                WHERE host_id=?""", (canonical(sorted(exclusive - released)), active['host_id']))
