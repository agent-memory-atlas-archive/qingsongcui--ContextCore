"""Explicit corrections to historical local estimates using an effective price version."""
import hashlib
from collections import defaultdict
from decimal import Decimal

from fastapi import HTTPException

from app.database import db, decode, encode, log_audit, now_iso
from app.services.usage_service import _cost, ensure_schema


def _affected(conn, price):
    # Use the same precedence as usage_service._price, including same-start corrections.
    return conn.execute("""
        SELECT a.id, a.data FROM usage_v2_attempts a
        WHERE a.provider_id=:provider_id AND a.started_at >= :effective_from
          AND (:effective_to IS NULL OR a.started_at < :effective_to)
          AND COALESCE(NULLIF(json_extract(a.data, '$.billing_model'), ''), a.model)=:model
          AND :id=(
              SELECT p.id FROM usage_v2_prices p
              WHERE p.provider_id=:provider_id AND p.model=:model AND p.effective_from<=a.started_at
                AND (p.effective_to IS NULL OR p.effective_to>a.started_at)
              ORDER BY p.effective_from DESC, p.rowid DESC LIMIT 1)
        ORDER BY a.id
    """, price)


def _changes(conn, price):
    for row in _affected(conn, price):
        data = decode(row["data"], {})
        before = data.get("cost") or {}
        after = _cost(data.get("usage") or {}, price)
        if before != after:
            yield row["id"], before, after


def _totals(cost, amounts):
    if cost.get("amount") is None:
        return 1
    amounts[cost["currency"]] += Decimal(cost["amount"])
    return 0


def price_correction(price_id, expected_fingerprint=None):
    with db() as conn:
        ensure_schema(conn)
        applying = expected_fingerprint is not None
        conn.execute("BEGIN IMMEDIATE" if applying else "BEGIN")
        row = conn.execute("SELECT data FROM usage_v2_prices WHERE id=?", (price_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Price version not found")
        price = decode(row["data"], {})
        digest = hashlib.sha256(price_id.encode())
        before_amounts, after_amounts = defaultdict(Decimal), defaultdict(Decimal)
        count = before_unknown = after_unknown = 0
        for aid, before, after in _changes(conn, price):
            digest.update(encode([aid, before, after]).encode())
            count += 1
            before_unknown += _totals(before, before_amounts)
            after_unknown += _totals(after, after_amounts)
        fingerprint = digest.hexdigest()
        if applying:
            if fingerprint != expected_fingerprint:
                raise HTTPException(409, "Estimates changed since preview. Preview again.")
            conn.execute("""CREATE TABLE IF NOT EXISTS usage_price_corrections(
                attempt_id TEXT NOT NULL, corrected_at TEXT NOT NULL, before_cost TEXT NOT NULL,
                after_cost TEXT NOT NULL)""")
            timestamp = now_iso()
            for aid, before, after in _changes(conn, price):
                conn.execute("""INSERT INTO usage_price_corrections VALUES(?,?,?,?)""",
                             (aid, timestamp, encode(before), encode(after)))
                conn.execute("""UPDATE usage_v2_attempts SET data=json_set(data, '$.cost', json(?))
                                WHERE id=?""", (encode(after), aid))
            if count:
                log_audit("user", "usage.price.correct", price_id, conn=conn)
        return {"price_id": price_id, "count": count, "fingerprint": fingerprint,
                "applied": applying, "before_unknown": before_unknown, "after_unknown": after_unknown,
                "before": {k: format(v, "f") for k, v in before_amounts.items()},
                "after": {k: format(v, "f") for k, v in after_amounts.items()}}
