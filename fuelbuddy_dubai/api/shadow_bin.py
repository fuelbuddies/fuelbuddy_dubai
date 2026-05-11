"""
Redis-backed shadow of `tabBin` for the back-dated Delivery Note drain.

During drain, Patch 3 in fuelbuddy_dubai/__init__.py skips the per-submit
tabBin row update (the single hottest contention point — one row per
(item, warehouse) hit by every submit). The drain code is then responsible
for:

  1. Mirroring the relevant Bin rows into Redis at start
  2. Decrementing the shadow per-submit (atomic HINCRBYFLOAT)
  3. Pre-flighting negative-stock checks against the shadow
  4. Reconciling shadow vs tabBin AFTER the consolidated Repost Item Valuation
     has rewritten tabBin from the final SLE state

Three-way correctness invariant:
    Σ(SLE.actual_qty for our voucher_nos)
    == (initial shadow.actual_qty − final shadow.actual_qty)
    == (initial tabBin.actual_qty − final tabBin.actual_qty after repost)

Drift between any two of these three is a bug — the audit log
(`fb_drain:bin_mutations:*` LIST in Redis) shows exactly which mutation
caused the divergence.
"""

from __future__ import annotations

import json
import time

import frappe
import redis


KEY_PREFIX = "fb_drain:bin:"
MUT_PREFIX = "fb_drain:bin_mutations:"
RUN_KEY = "fb_drain:run:active"
SHADOW_TTL_S = 24 * 60 * 60   # 24h hard ceiling


def _raw_redis() -> redis.Redis:
    """Get a raw redis-py connection that bypasses Frappe's RedisWrapper.
    We need raw access because RedisWrapper pickles values, which breaks
    atomic counters (hincrbyfloat). Our shadow Bin needs numeric atomic
    operations, so we deliberately bypass pickling."""
    url = frappe.conf.get("redis_cache") or "redis://redis-cache:6379"
    if url.startswith("redis://"):
        return redis.from_url(url, decode_responses=True)
    return redis.Redis(host=url.split(":")[0], port=int(url.split(":")[1]),
                       decode_responses=True)


def _k(item: str, wh: str) -> str:
    return f"{KEY_PREFIX}{item}:{wh}"


def _mk(item: str, wh: str) -> str:
    return f"{MUT_PREFIX}{item}:{wh}"


def initialize(run_id: str, pairs: list[tuple[str, str]]) -> dict:
    """Snapshot tabBin into Redis for each (item, warehouse) the drain will touch."""
    r = _raw_redis()

    existing = r.get(RUN_KEY)
    if existing:
        if isinstance(existing, bytes):
            existing = existing.decode()
        if existing != run_id:
            frappe.throw(f"Another drain is already active: run_id={existing}")

    r.set(RUN_KEY, run_id, ex=SHADOW_TTL_S)

    summary = []
    for item, wh in pairs:
        bin_data = frappe.db.get_value(
            "Bin",
            {"item_code": item, "warehouse": wh},
            [
                "actual_qty", "reserved_qty", "ordered_qty", "indented_qty",
                "planned_qty", "projected_qty", "stock_value", "valuation_rate",
                "stock_uom",
            ],
            as_dict=True,
        )
        if not bin_data:
            # No Bin row — drain shouldn't touch this pair anyway, but record empty
            bin_data = {
                "actual_qty": 0, "reserved_qty": 0, "ordered_qty": 0,
                "indented_qty": 0, "planned_qty": 0, "projected_qty": 0,
                "stock_value": 0, "valuation_rate": 0, "stock_uom": "",
            }
        bin_data["drain_run_id"] = run_id
        bin_data["initialized_at"] = frappe.utils.now_datetime().isoformat()

        key = _k(item, wh)
        r.hset(key, mapping={k: str(v) for k, v in bin_data.items()})
        r.expire(key, SHADOW_TTL_S)
        summary.append({"item": item, "warehouse": wh, **bin_data})

    return {"run_id": run_id, "pairs": summary}


def get(item: str, wh: str) -> dict:
    """Return current shadow values for a pair as floats."""
    r = _raw_redis()
    raw = r.hgetall(_k(item, wh))
    out = {}
    for k, v in raw.items():
        if isinstance(k, bytes):
            k = k.decode()
        if isinstance(v, bytes):
            v = v.decode()
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            out[k] = v
    return out


def will_cause_negative(item: str, wh: str, qty: float) -> bool:
    """Pre-flight: would subtracting `qty` make actual_qty go negative?"""
    r = _raw_redis()
    raw = r.hget(_k(item, wh), "actual_qty")
    if raw is None:
        return True
    if isinstance(raw, bytes):
        raw = raw.decode()
    return (float(raw) - float(qty)) < 0


def apply(item: str, wh: str, qty: float, dn_name: str) -> None:
    """Atomically decrement shadow Bin for an outgoing DN qty."""
    r = _raw_redis()
    key = _k(item, wh)

    rate_raw = r.hget(key, "valuation_rate")
    if isinstance(rate_raw, bytes):
        rate_raw = rate_raw.decode()
    rate = float(rate_raw or 0)

    r.hincrbyfloat(key, "actual_qty", -float(qty))
    r.hincrbyfloat(key, "reserved_qty", -float(qty))
    r.hincrbyfloat(key, "stock_value", -float(qty) * rate)

    r.rpush(
        _mk(item, wh),
        json.dumps({
            "ts": time.time(),
            "dn": dn_name,
            "delta_qty": -float(qty),
            "delta_value": -float(qty) * rate,
        }),
    )
    r.expire(_mk(item, wh), SHADOW_TTL_S)


def reconcile(item: str, wh: str) -> dict:
    """After consolidated repost, compare shadow vs actual tabBin."""
    bin_now = frappe.db.get_value(
        "Bin",
        {"item_code": item, "warehouse": wh},
        ["actual_qty", "stock_value", "valuation_rate", "reserved_qty"],
        as_dict=True,
    ) or {}
    shadow_now = get(item, wh)
    drift = {
        "drift_actual_qty": shadow_now.get("actual_qty", 0) - (bin_now.get("actual_qty") or 0),
        "drift_stock_value": shadow_now.get("stock_value", 0) - (bin_now.get("stock_value") or 0),
        "drift_reserved_qty": shadow_now.get("reserved_qty", 0) - (bin_now.get("reserved_qty") or 0),
    }
    return {
        "item": item,
        "warehouse": wh,
        "shadow": shadow_now,
        "bin": dict(bin_now),
        **drift,
    }


def cleanup() -> int:
    """Remove all shadow keys after a successful drain."""
    r = _raw_redis()
    cleared = 0
    for k in r.scan_iter(match=f"{KEY_PREFIX}*"):
        r.delete(k)
        cleared += 1
    for k in r.scan_iter(match=f"{MUT_PREFIX}*"):
        r.delete(k)
        cleared += 1
    r.delete(RUN_KEY)
    return cleared
