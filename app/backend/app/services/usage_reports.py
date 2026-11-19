"""Filtered, metadata-only workbench views over the observed attempt ledger."""
import csv
import io
import json
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException

from app.database import db
from app.services import usage_service as ledger


def timezone_for(name):
    try:
        return ZoneInfo(name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise HTTPException(422, "Unknown timezone") from exc


def _labels(conn, dimension, values):
    if dimension == "host_id":
        from app.services.management_service import HOSTS
        # Native Codex records without surface evidence share this identity.
        return {v: "Codex (CLI / Desktop)" if v == "codex" else HOSTS.get(v, {}).get("name", v) for v in values}
    ids = [v for v in values if v is not None]
    if not ids:
        return {}
    encoded = json.dumps(ids)
    if dimension == "project_id":
        return dict(conn.execute("SELECT id,name FROM projects WHERE id IN (SELECT value FROM json_each(?))", (encoded,)))
    if dimension not in ("provider_id", "account_id"):
        return {}
    labels = {}
    kind = "provider" if dimension == "provider_id" else "account"
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='management_entities'").fetchone():
        rows = conn.execute("SELECT id,json_extract(data,'$.name') FROM management_entities WHERE kind=? "
                            "AND id IN (SELECT value FROM json_each(?))", (kind, encoded))
        labels.update((key, value) for key, value in rows if value)
    missing = [v for v in ids if v not in labels]
    if missing:
        # A deleted or unbound definition can still have a dispatch-time name.
        field = "$." + kind + "_name"
        query = (f"SELECT value,(SELECT json_extract(data,'{field}') FROM usage_v2_attempts "
                 f"WHERE {dimension}=v.value AND json_type(data,'{field}')='text' "
                 "ORDER BY started_at DESC,id DESC LIMIT 1) FROM json_each(?) v")
        labels.update((key, value) for key, value in conn.execute(query, (json.dumps(missing),)) if value)
    return labels


METERING = "json_extract(data,'$.usage.input_tokens','$.usage.output_tokens','$.cost.amount','$.cost.currency')"


def breakdown(filters, dimension):
    if dimension not in ledger.DIMENSIONS:
        raise HTTPException(422, "Unknown breakdown dimension")
    where, params = ledger._where(filters)
    with db() as conn:
        ledger.ensure_schema(conn)
        conn.execute("BEGIN")
        key = ("breakdown", dimension, ledger._cache_key(conn, where, params))
        items = ledger._cached(key)
        if items is None:
            # Stream sorted compact rows. Only distinct buckets are held in
            # memory; no per-provider sets of every request or JSON bodies.
            cursor = conn.execute(f"SELECT {ledger.dimension_sql(dimension)} AS bucket,logical_request_id,{METERING},{ledger.UNATTRIBUTED_SAMPLE} FROM usage_v2_attempts"
                                  + where + " ORDER BY bucket,logical_request_id", params)
            buckets = {}
            previous = None
            for value, rid, amounts, sample in cursor:
                if value not in buckets:
                    buckets[value] = ledger._totals()
                pair = (value, rid)
                ledger._count_attempt(buckets[value], pair != previous, *json.loads(amounts), sample=sample)
                previous = pair
            items = [{"value": value, **ledger._format_totals(bucket)} for value, bucket in buckets.items()]
            items.sort(key=lambda i: (-i["logical_requests"], -i["attempts"], i["value"] or ""))
            ledger._remember(key, items)
        labels = _labels(conn, dimension, (i["value"] for i in items))
        return {"dimension": dimension, "items": [{**i, "label": labels.get(i["value"], i["value"])} for i in items]}


def filter_options(filters):
    where, params = ledger._where(filters)
    with db() as conn:
        ledger.ensure_schema(conn)
        conn.execute("BEGIN")
        key = ("options", ledger._cache_key(conn, where, params))
        values = ledger._cached(key)
        if values is None:
            dimensions = (*ledger.DIMENSIONS, "status")
            columns = ",".join(ledger.dimension_sql(key) for key in dimensions)
            query = f"SELECT {columns},json_extract(data,'$.origins') FROM usage_v2_attempts" + where
            options = [set() for _ in dimensions]
            origins = set()
            for row in conn.execute(query, params):
                for index, group in enumerate(options):
                    group.add(row[index])
                if row[-1]:
                    origins.update(json.loads(row[-1]))
            values = {key: list(group) for key, group in zip(dimensions, options)}
            values["origin"] = list(origins)
            ledger._remember(key, values)
        result = {}
        for dimension, options in values.items():
            labels = _labels(conn, dimension, options)
            entries = [{"value": v, "label": labels.get(v, v)} for v in options]
            result[dimension] = sorted(entries, key=lambda item: (item["value"] is None, (item["label"] or "").casefold()))
        return result


def logical_requests(filters, limit, offset):
    where, params = ledger._where(filters)
    with db() as conn:
        ledger.ensure_schema(conn)
        conn.execute("BEGIN")
        key = ("logical_count", ledger._cache_key(conn, where, params))
        total = ledger._cached(key)
        if total is None:
            total = conn.execute("SELECT COUNT(DISTINCT logical_request_id) FROM usage_v2_attempts" + where, params).fetchone()[0]
            ledger._remember(key, total)
        page = conn.execute("SELECT logical_request_id,MIN(started_at) AS first_time FROM usage_v2_attempts"
                            + where + " GROUP BY logical_request_id ORDER BY first_time DESC,logical_request_id DESC LIMIT ? OFFSET ?",
                            (*params, limit, offset))
        groups = {rid: {"id": rid, "logical_request_id": rid, "started_at": start, **ledger._totals()}
                  for rid, start in page}
        if groups:
            # Never decode every attempt just to obtain one page. The attempt
            # order comes from dispatch when recorded, then native timestamps.
            query = f"SELECT logical_request_id,host_id,{ledger.dimension_sql('model')},status,{METERING},{ledger.UNATTRIBUTED_SAMPLE} FROM usage_v2_attempts" + where
            query += (" AND " if where else " WHERE ") + "logical_request_id IN (SELECT value FROM json_each(?))"
            query += " ORDER BY logical_request_id," + ledger.ATTEMPT_ORDER
            for rid, host, model, status, amounts, sample in conn.execute(query, (*params, json.dumps(list(groups)))):
                group = groups[rid]
                ledger._count_attempt(group, group["attempts"] == 0, *json.loads(amounts), sample=sample)
                group.update(host_id=host or group.get("host_id"), model=model, status="unknown" if sample else status,
                             unattributed_sample=bool(sample))
            finals = conn.execute(
                "SELECT id,(SELECT status FROM usage_v2_attempts WHERE logical_request_id=r.id ORDER BY "
                + ledger.FINAL_ATTEMPT_ORDER + " LIMIT 1) FROM usage_v2_requests r WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(list(groups)),))
            for rid, status in finals:
                groups[rid]["request_status"] = "unknown" if groups[rid]["unattributed_sample"] else status
        return {"items": [ledger._format_totals(v) for v in groups.values()], "limit": limit, "offset": offset, "total": total}


def attempts(logical_id, limit, offset):
    with db() as conn:
        ledger.ensure_schema(conn)
        conn.execute("BEGIN")
        total = conn.execute("SELECT COUNT(*) FROM usage_v2_attempts WHERE logical_request_id=?", (logical_id,)).fetchone()[0]
        query = f"SELECT data,{ledger.UNATTRIBUTED_SAMPLE} FROM usage_v2_attempts WHERE logical_request_id=? ORDER BY " + ledger.ATTEMPT_ORDER + " LIMIT ? OFFSET ?"
        rows = conn.execute(query, (logical_id, limit, offset))
        items = []
        for raw, sample in rows:
            record = json.loads(raw)
            if sample:
                record.update(unattributed_sample=True, status="unknown")
            items.append(record)
        return {"items": items, "total": total, "limit": limit, "offset": offset}


def _csv_cell(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_csv(filters, tz):
    """Spool one WAL read snapshot. HTTP backpressure never keeps its reader open."""
    zone = timezone_for(tz)
    where, params = ledger._where(filters)
    output = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
    try:
        output.write(b"\xef\xbb\xbf")
        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)

        def write(values):
            writer.writerow([_csv_cell(v) for v in values])
            output.write(buffer.getvalue().encode("utf-8"))
            buffer.seek(0)
            buffer.truncate(0)

        extra = ("attempt_number", "requested_model", "actual_model", "route_version", "error_code",
                 "usage.input_tokens", "usage.output_tokens", "usage.cache_read_tokens",
                 "usage.cache_write_tokens", "usage.reasoning_tokens", "cost.amount", "cost.currency",
                 "cost.status", "origins")
        write(("logical_request_id", "attempt_id", "started_at", "host_id", "project_id",
               "provider_id", "account_id", "model", "status", "record_kind", *extra))
        fields = ",".join(f"'$.{path}'" for path in extra)
        with db() as conn:
            ledger.ensure_schema(conn)
            conn.execute("BEGIN")
            rows = conn.execute(f"SELECT logical_request_id,id,started_at,host_id,project_id,provider_id,account_id,model,status,{ledger.UNATTRIBUTED_SAMPLE},"
                                + f"json_extract(data,{fields}) FROM usage_v2_attempts"
                                + where + " ORDER BY started_at DESC,id DESC", params)
            for row in rows:
                metadata = json.loads(row[-1])
                metadata[-1] = "|".join(metadata[-1] or [])
                stamp = datetime.fromisoformat(row[2]).astimezone(zone).isoformat()
                sample = bool(row[-2])
                write((*row[:2], stamp, *row[3:8], "unknown" if sample else row[8],
                       "usage_sample" if sample else "attempt", *metadata))
        output.seek(0)
        return output
    except BaseException:
        output.close()
        raise
