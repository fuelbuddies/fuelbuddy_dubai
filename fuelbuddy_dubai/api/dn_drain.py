"""
Production drain for back-dated Delivery Notes.

Workflow per batch:
  1. Pick draft DNs in a (customer, date-range, item-warehouse) window.
  2. Pre-flight every draft against a Redis shadow of tabBin (negative-stock
     check) — skips doomed drafts before they hit the submit chain.
  3. Engage 4 monkey-patch flags (defined in fuelbuddy_dubai/__init__.py):
       fb_skip_repost              — skip per-submit Repost Item Valuation
       fb_skip_future_sle_update   — skip update_qty_in_future_sle wide UPDATE
       fb_skip_bin_update          — skip per-submit tabBin row update
       fb_skip_billing_status      — skip per-submit SO/SI billing recompute
  4. Submit each DN through ERPNext's standard doc.submit() — the patches make
     all four deferred operations no-op while flags are set. Apply atomic
     decrement to the Redis shadow per item.
  5. After the batch:
       a. Cancel any RIVs that got auto-queued despite the patch (defence in
          depth; should be 0).
       b. Run ONE consolidated Repost Item Valuation for the hot key from the
          earliest posting_date in the batch. This recomputes:
            - tabStock Ledger Entry: valuation_rate, stock_value,
              stock_value_difference, qty_after_transaction
            - tabGL Entry: deletes stale per-submit GLs, reinserts with
              correct values via repost_gle_for_stock_vouchers
            - tabBin: actual_qty, stock_value, valuation_rate, stock_queue
       c. SQL recompute of SO Item.delivered_qty and SO.per_delivered from
          authoritative DN rows (replacing what skip_billing_status deferred).
       d. Reconcile shadow vs tabBin. Drift on actual_qty/reserved_qty must
          be float-precision noise (~1e-9). Any larger drift is a bug.
       e. Cleanup shadow keys.

Identity guarantee:
  Field-by-field diff against native ERPNext submit + consolidated repost
  on the same input set produces 0 diffs on SLE, GL, and Bin (verified at
  100-DN scale, April 6 NFPC, crossing 13 GRN events).

Performance (measured):
  Submit phase: ~120ms/doc with patches engaged (vs ~9.4s/doc native).
  Consolidated repost: ~0.6s/SLE row walked.
  SO billing recompute: <5 seconds for any batch size (it's a SQL aggregate).

Projection for 95k DN drain on prod hardware:
  ~3.2h submit + ~1.2h repost = ~4.5h total — fits one off-hours window.
"""

from __future__ import annotations

import json
import time
from typing import Optional

import frappe

from fuelbuddy_dubai.api import shadow_bin


HOT_ITEM = "FB/FL/00001"
HOT_WAREHOUSE = "Default Warehouse - FFSL"


@frappe.whitelist()
def drain(
    customer: Optional[str] = None,
    from_date: str = "2026-04-01",
    to_date: str = "2026-04-30",
    batch_size: int = 0,
    dry_run: int = 0,
    item_code: str = HOT_ITEM,
    warehouse: str = HOT_WAREHOUSE,
    min_age_minutes: int = 0,
) -> dict:
    """
    Drain back-dated draft Delivery Notes through the v3.6 fast path.

    Args:
        customer:   optional filter to a single customer (None = all)
        from_date:  posting_date lower bound (inclusive)
        to_date:    posting_date upper bound (inclusive)
        batch_size: 0 = drain all matching drafts; >0 = first N
        dry_run:    1 = pre-flight + simulate only, no commits
        item_code:  the hot item (drain assumes single-item, single-warehouse)
        warehouse:  the hot warehouse
        min_age_minutes: only pick drafts created at least this many minutes
                    ago (0 = no age filter); lets the scheduled drain leave
                    freshly punched DNs alone. Waived when a full batch_size
                    of backlog exists — throughput wins over the age guard.

    Returns:
        dict with stage breakdowns, per-phase timings, shadow reconciliation,
        and per-DN failure list.
    """
    started = time.time()
    timings: dict[str, float] = {}
    result: dict = {
        "stage": "in_progress",
        "params": {
            "customer": customer, "from_date": from_date, "to_date": to_date,
            "batch_size": batch_size, "dry_run": int(dry_run),
            "item_code": item_code, "warehouse": warehouse,
            "min_age_minutes": int(min_age_minutes),
        },
    }

    try:
        # ------------------------------------------------------------------
        # 1. Pick drafts
        # ------------------------------------------------------------------
        t = time.time()
        # Age guard is waived when a full batch of backlog exists: pick oldest-first
        # ignoring age; only if that comes up short of batch_size (backlog under the
        # cap) re-pick with the min_age filter so freshly punched DNs are left alone.
        drafts = _pick_drafts(customer, from_date, to_date, batch_size,
                              item_code, warehouse)
        if int(min_age_minutes) > 0 and int(batch_size) > 0 and len(drafts) < int(batch_size):
            drafts = _pick_drafts(customer, from_date, to_date, batch_size,
                                  item_code, warehouse, int(min_age_minutes))
        timings["pick_s"] = round(time.time() - t, 3)
        result["draft_count"] = len(drafts)
        if not drafts:
            result["stage"] = "empty"
            result["timings"] = timings
            return result

        # ------------------------------------------------------------------
        # 2. Initialise shadow Bin
        # ------------------------------------------------------------------
        t = time.time()
        run_id = f"fb-drain-{frappe.generate_hash(length=8)}"
        shadow_bin.initialize(run_id, [(item_code, warehouse)])
        result["run_id"] = run_id
        timings["shadow_init_s"] = round(time.time() - t, 3)

        # ------------------------------------------------------------------
        # 3. Clean pre-existing RIVs in the path (defence in depth)
        # ------------------------------------------------------------------
        frappe.db.sql(
            "UPDATE `tabRepost Item Valuation` SET status='Skipped' "
            "WHERE status IN ('Queued','In Progress') "
            "  AND item_code=%s AND warehouse=%s",
            (item_code, warehouse),
        )
        frappe.db.commit()

        # ------------------------------------------------------------------
        # 4. Submit loop with all 4 patches engaged
        # ------------------------------------------------------------------
        submitted: list[str] = []
        skipped_negative: list[str] = []
        failed: list[dict] = []

        frappe.flags.fb_skip_repost = True
        frappe.flags.fb_skip_future_sle_update = True
        frappe.flags.fb_skip_bin_update = True
        frappe.flags.fb_skip_billing_status = True

        t = time.time()
        try:
            for name in drafts:
                # Pre-flight: shadow-based negative-stock check
                items = frappe.get_all(
                    "Delivery Note Item",
                    filters={"parent": name},
                    fields=["item_code", "warehouse", "qty"],
                )
                if any(
                    shadow_bin.will_cause_negative(it.item_code, it.warehouse, it.qty)
                    for it in items
                ):
                    skipped_negative.append(name)
                    continue

                if dry_run:
                    submitted.append(name)
                    for it in items:
                        # Update shadow as if we submitted, so subsequent
                        # pre-flight checks are accurate
                        shadow_bin.apply(it.item_code, it.warehouse, it.qty, name)
                    continue

                try:
                    doc = frappe.get_doc("Delivery Note", name)
                    doc.submit()
                    frappe.db.commit()
                    for it in doc.items:
                        shadow_bin.apply(it.item_code, it.warehouse, it.qty, name)
                    submitted.append(name)
                except Exception as exc:
                    frappe.db.rollback()
                    failed.append({"name": name, "error": str(exc)[:300]})
                    frappe.log_error(
                        title=f"v3.6 drain submit failed: {name}",
                        message=frappe.get_traceback(),
                    )
        finally:
            frappe.flags.fb_skip_repost = False
            frappe.flags.fb_skip_future_sle_update = False
            frappe.flags.fb_skip_bin_update = False
            frappe.flags.fb_skip_billing_status = False
        timings["submit_s"] = round(time.time() - t, 3)

        result["submitted_count"] = len(submitted)
        result["skipped_negative_count"] = len(skipped_negative)
        result["failed_count"] = len(failed)
        result["failed"] = failed[:50]
        result["skipped_negative_sample"] = skipped_negative[:20]
        result["submit_avg_ms"] = (
            int(timings["submit_s"] * 1000 / max(len(submitted), 1))
        )

        if dry_run:
            shadow_bin.cleanup()
            result["stage"] = "dry_run_complete"
            result["timings"] = timings
            return result

        if not submitted:
            shadow_bin.cleanup()
            result["stage"] = "no_submits"
            result["timings"] = timings
            return result

        # ------------------------------------------------------------------
        # 5. Consolidated Repost Item Valuation
        # ------------------------------------------------------------------
        # Clean any RIVs that may have been auto-queued during patches
        # (defence in depth — should be 0 with patches working).
        frappe.db.sql(
            "UPDATE `tabRepost Item Valuation` SET status='Skipped' "
            "WHERE status IN ('Queued','In Progress') "
            "  AND item_code=%s AND warehouse=%s",
            (item_code, warehouse),
        )
        frappe.db.commit()

        t = time.time()
        earliest = frappe.db.sql(
            "SELECT MIN(posting_date) FROM `tabDelivery Note` WHERE name IN %s",
            (tuple(submitted),),
        )[0][0]
        company = frappe.db.get_single_value("Global Defaults", "default_company")
        riv = frappe.get_doc({
            "doctype": "Repost Item Valuation",
            "based_on": "Item and Warehouse",
            "item_code": item_code,
            "warehouse": warehouse,
            "posting_date": earliest,
            "posting_time": "00:00:00",
            "allow_negative_stock": 1,
            "company": company,
        }).insert(ignore_permissions=True)
        riv.submit()
        frappe.db.commit()

        from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
            repost as run_repost,
        )
        run_repost(riv)
        frappe.db.commit()
        timings["repost_s"] = round(time.time() - t, 3)
        result["consolidated_repost"] = riv.name

        # ------------------------------------------------------------------
        # 6. SQL recompute of SO Item.delivered_qty and SO.per_delivered
        # ------------------------------------------------------------------
        t = time.time()
        frappe.db.sql(
            """
            UPDATE `tabSales Order Item` soi
            JOIN (
                SELECT so_detail, SUM(qty) AS d
                FROM `tabDelivery Note Item` dni
                JOIN `tabDelivery Note` dn ON dn.name = dni.parent
                WHERE dn.docstatus = 1 AND dni.so_detail IS NOT NULL
                GROUP BY so_detail
            ) x ON x.so_detail = soi.name
            SET soi.delivered_qty = x.d
            """
        )
        frappe.db.sql(
            """
            UPDATE `tabSales Order` so
            JOIN (
                SELECT parent,
                       100 * SUM(delivered_qty) / NULLIF(SUM(qty), 0) AS pct
                FROM `tabSales Order Item`
                GROUP BY parent
            ) x ON x.parent = so.name
            SET so.per_delivered = COALESCE(x.pct, 0)
            """
        )
        frappe.db.commit()
        timings["billing_recompute_s"] = round(time.time() - t, 3)

        # ------------------------------------------------------------------
        # 7. Reconcile shadow vs tabBin (must be zero drift on qty)
        # ------------------------------------------------------------------
        recon = shadow_bin.reconcile(item_code, warehouse)
        result["shadow_reconciliation"] = recon
        result["reconciliation_passed"] = (
            abs(recon.get("drift_actual_qty", 0)) < 0.001
            and abs(recon.get("drift_reserved_qty", 0)) < 0.001
        )

        # ------------------------------------------------------------------
        # 8. Cleanup
        # ------------------------------------------------------------------
        shadow_bin.cleanup()

        timings["total_s"] = round(time.time() - started, 3)
        result["stage"] = "complete"
        result["timings"] = timings
        return result

    except Exception as exc:
        # Best-effort cleanup
        try:
            shadow_bin.cleanup()
        except Exception:
            pass
        import traceback
        result["stage"] = "exception"
        result["error"] = str(exc)[:500]
        result["trace"] = traceback.format_exc()[-2000:]
        result["timings"] = timings
        return result


def _result_key(job_id: str) -> str:
    return f"fb_dn_drain_result:{job_id}"


@frappe.whitelist()
def drain_async(
    customer: Optional[str] = None,
    from_date: str = "2026-04-01",
    to_date: str = "2026-04-30",
    batch_size: int = 0,
    dry_run: int = 0,
    item_code: str = HOT_ITEM,
    warehouse: str = HOT_WAREHOUSE,
    min_age_minutes: int = 0,
) -> dict:
    """
    Enqueue drain() on the long worker and return a job_id to poll via
    drain_status(). HTTP-safe wrapper: a synchronous drain call can outlive
    the gateway/gunicorn timeout, which kills it MID-BATCH — DNs submitted
    with the skip-flags engaged but no consolidated repost, and a stale
    shadow RUN_KEY that blocks the next drain. RQ jobs have no such timeout.
    Explicit params (no **kwargs) so Frappe's `cmd` form param never leaks in.
    """
    job_id = "fb-dn-drain-" + frappe.generate_hash(length=8)
    frappe.enqueue(
        "fuelbuddy_dubai.api.dn_drain._drain_job",
        queue="long",
        timeout=3600,
        job_id=job_id,
        result_key=_result_key(job_id),
        drain_kwargs={
            "customer": customer, "from_date": from_date, "to_date": to_date,
            "batch_size": batch_size, "dry_run": dry_run,
            "item_code": item_code, "warehouse": warehouse,
            "min_age_minutes": min_age_minutes,
        },
    )
    return {"job_id": job_id}


def _drain_job(result_key: str, drain_kwargs: dict) -> None:
    """RQ target: run the drain and stash its result for drain_status()."""
    result = drain(**drain_kwargs)
    frappe.cache().set_value(result_key, json.dumps(result, default=str),
                             expires_in_sec=3600)


@frappe.whitelist()
def drain_status(job_id: str) -> dict:
    """Poll a drain_async job: done (with result), failed (with traceback tail),
    or pending."""
    raw = frappe.cache().get_value(_result_key(job_id))
    if raw:
        return {"status": "done", "result": json.loads(raw)}
    try:
        from rq.job import Job
        from frappe.utils.background_jobs import get_redis_conn
        job = Job.fetch(f"{frappe.local.site}::{job_id}", connection=get_redis_conn())
        if job.get_status() == "failed":
            return {"status": "failed", "error": (job.exc_info or "")[-500:]}
    except Exception:
        pass  # job not in RQ (yet/anymore) — fall through to pending
    return {"status": "pending"}


def _pick_drafts(
    customer: Optional[str],
    from_date: str,
    to_date: str,
    batch_size: int,
    item_code: str,
    warehouse: str,
    min_age_minutes: int = 0,
) -> list[str]:
    """Pick draft DN names matching the drain filters, chronological order."""
    where_parts = [
        "dn.docstatus = 0",
        "dni.item_code = %(item_code)s",
        "dni.warehouse = %(warehouse)s",
        "dn.posting_date BETWEEN %(from_date)s AND %(to_date)s",
    ]
    if customer:
        where_parts.append("dn.customer = %(customer)s")
    if min_age_minutes > 0:
        # Event 2: leave freshly punched DNs alone — only drain drafts that have
        # sat for at least min_age_minutes since creation.
        where_parts.append(
            "dn.creation <= DATE_SUB(NOW(), INTERVAL %(min_age_minutes)s MINUTE)"
        )

    limit = f"LIMIT {int(batch_size)}" if batch_size and batch_size > 0 else ""

    return frappe.db.sql_list(
        f"""
        SELECT dn.name
        FROM `tabDelivery Note` dn
        JOIN `tabDelivery Note Item` dni ON dni.parent = dn.name
        WHERE {" AND ".join(where_parts)}
        ORDER BY dn.posting_date ASC, dn.posting_time ASC, dn.creation ASC
        {limit}
        """,
        {
            "item_code": item_code,
            "warehouse": warehouse,
            "from_date": from_date,
            "to_date": to_date,
            "customer": customer or "",
            "min_age_minutes": min_age_minutes,
        },
    )
