"""
Bulk cancel for submitted Delivery Notes (returns + originals) — the
cancel-side sibling of dn_drain.

Why not plain doc.cancel() in a loop: DeliveryNote.update_billing_status on
cancel walks every Delivery Note billed against the same SO line and calls
update_billing_percentage + notify_update per sibling. On the hot SO lines
that is 30k+ sibling DNs — measured ~10 minutes PER CANCEL on the prod dump.
With fb_skip_billing_status + fb_skip_repost + fb_skip_future_sle_update
engaged (the dn_drain monkey-patch flags in fuelbuddy_dubai/__init__.py),
a cancel drops to ~100ms. The deferred work is recovered after the batch:
  - one consolidated Repost Item Valuation per touched (item, warehouse)
    from the earliest cancelled posting_date (fixes SLE/GL/Bin, same as
    dn_drain step 5), run inline
  - SQL recompute of SO Item.delivered_qty / SO.per_delivered, scoped to
    the SO lines the cancelled DNs touched (dn_drain step 6, plus LEFT JOIN
    so a line whose every DN got cancelled correctly drops to 0)

Ordering: pass returns (FB/RTN/...) BEFORE their originals — a DN with an
active return against it cannot be cancelled. The batch runs in ONE RQ job,
in input order, committing per doc, so ordering holds regardless of how many
long workers exist. Re-runnable: already-cancelled docs are skipped, so
re-POST the same list to retry failures.

Usage (Administrator/System Manager):
  POST /api/method/fuelbuddy_dubai.api.dn_cancel.cancel_async
      names   = whitespace/comma separated DN names (returns first)
      dry_run = 1 (default) validate + report only; 0 = run
  -> {"job_id": ...}; poll cancel_status(job_id=...) for progress/result.

  curl -s -X POST "$BASE/api/method/fuelbuddy_dubai.api.dn_cancel.cancel_async" \\
    -H "Authorization: token $KEY:$SECRET" \\
    --data-urlencode "names@names.txt" --data "dry_run=0"
"""

from __future__ import annotations

import json
import time

import frappe


@frappe.whitelist()
def cancel_batch(names: str = "", dry_run: int = 1) -> dict:
    """Synchronous batch cancel. Called from the RQ job — for HTTP use
    cancel_async, a sync call this long dies at the gateway timeout."""
    frappe.only_for("System Manager")
    started = time.time()

    ordered = _parse_names(names)
    if not ordered:
        frappe.throw("names is required")

    rows = frappe.get_all(
        "Delivery Note",
        filters={"name": ["in", ordered]},
        fields=["name", "docstatus"],
        limit_page_length=0,
    )
    status = {r.name: r.docstatus for r in rows}
    to_cancel = [n for n in ordered if status.get(n) == 1]

    result: dict = {
        "input": len(ordered),
        "to_cancel": len(to_cancel),
        "already_cancelled": sum(1 for n in ordered if status.get(n) == 2),
        "draft_skipped": sum(1 for n in ordered if status.get(n) == 0),
        "missing_count": sum(1 for n in ordered if n not in status),
        "missing_sample": [n for n in ordered if n not in status][:20],
    }
    if int(dry_run) or not to_cancel:
        result["stage"] = "dry_run" if int(dry_run) else "nothing_to_cancel"
        return result

    cancelled: list[str] = []
    failed: list[dict] = []

    frappe.flags.fb_skip_billing_status = True
    frappe.flags.fb_skip_repost = True
    frappe.flags.fb_skip_future_sle_update = True
    t = time.time()
    try:
        for name in to_cancel:
            try:
                doc = frappe.get_doc("Delivery Note", name)
                doc.flags.ignore_permissions = True
                doc.cancel()
                frappe.db.commit()
                cancelled.append(name)
            except Exception as exc:
                frappe.db.rollback()
                failed.append({"name": name, "error": str(exc)[:300]})
                frappe.log_error(
                    title=f"dn_cancel failed: {name}",
                    message=frappe.get_traceback(),
                )
            if len(cancelled) % 25 == 0:
                _set_progress(f"{len(cancelled) + len(failed)}/{len(to_cancel)} cancelled")
    finally:
        frappe.flags.fb_skip_billing_status = False
        frappe.flags.fb_skip_repost = False
        frappe.flags.fb_skip_future_sle_update = False

    result["cancelled_count"] = len(cancelled)
    result["failed_count"] = len(failed)
    result["failed"] = failed[:50]
    result["cancel_s"] = round(time.time() - t, 3)
    result["cancel_avg_ms"] = int(result["cancel_s"] * 1000 / max(len(cancelled), 1))

    if cancelled:
        _set_progress("cancels done, running consolidated repost")
        t = time.time()
        result["reposts"] = _consolidated_repost(cancelled)
        result["repost_s"] = round(time.time() - t, 3)

        t = time.time()
        _recompute_so_delivered(cancelled)
        result["so_recompute_s"] = round(time.time() - t, 3)

    result["total_s"] = round(time.time() - started, 3)
    result["stage"] = "complete"
    return result


def _parse_names(names) -> list[str]:
    """Whitespace/comma separated (or JSON list) -> deduped, order kept."""
    if isinstance(names, str):
        names = json.loads(names) if names.strip().startswith("[") else names.replace(",", " ").split()
    return list(dict.fromkeys(names))


def _consolidated_repost(cancelled: list[str]) -> list[str]:
    """One inline Repost Item Valuation per (item, warehouse) touched, from
    the earliest cancelled posting_date — dn_drain step 5."""
    keys = frappe.db.sql(
        """
        SELECT dni.item_code, dni.warehouse, MIN(dn.posting_date)
        FROM `tabDelivery Note Item` dni
        JOIN `tabDelivery Note` dn ON dn.name = dni.parent
        WHERE dn.name IN %s
        GROUP BY dni.item_code, dni.warehouse
        """,
        (tuple(cancelled),),
    )
    company = frappe.db.get_single_value("Global Defaults", "default_company")
    from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
        repost as run_repost,
    )

    rivs = []
    for item_code, warehouse, from_date in keys:
        riv = frappe.get_doc({
            "doctype": "Repost Item Valuation",
            "based_on": "Item and Warehouse",
            "item_code": item_code,
            "warehouse": warehouse,
            "posting_date": from_date,
            "posting_time": "00:00:00",
            "allow_negative_stock": 1,
            "company": company,
        }).insert(ignore_permissions=True)
        riv.submit()
        frappe.db.commit()
        run_repost(riv)
        frappe.db.commit()
        rivs.append(riv.name)
    return rivs


def _recompute_so_delivered(cancelled: list[str]) -> None:
    """dn_drain step 6, scoped to the SO lines the cancelled DNs touched.
    LEFT JOIN (unlike drain) so a line with no submitted DN rows left is
    reset to 0 instead of keeping its stale delivered_qty."""
    so_details = frappe.db.sql_list(
        "SELECT DISTINCT so_detail FROM `tabDelivery Note Item` "
        "WHERE parent IN %s AND so_detail IS NOT NULL AND so_detail != ''",
        (tuple(cancelled),),
    )
    if not so_details:
        return
    frappe.db.sql(
        """
        UPDATE `tabSales Order Item` soi
        LEFT JOIN (
            SELECT so_detail, SUM(qty) AS d
            FROM `tabDelivery Note Item` dni
            JOIN `tabDelivery Note` dn ON dn.name = dni.parent
            WHERE dn.docstatus = 1 AND dni.so_detail IN %(so_details)s
            GROUP BY so_detail
        ) x ON x.so_detail = soi.name
        SET soi.delivered_qty = COALESCE(x.d, 0)
        WHERE soi.name IN %(so_details)s
        """,
        {"so_details": so_details},
    )
    parents = frappe.db.sql_list(
        "SELECT DISTINCT parent FROM `tabSales Order Item` WHERE name IN %s",
        (tuple(so_details),),
    )
    frappe.db.sql(
        """
        UPDATE `tabSales Order` so
        JOIN (
            SELECT parent,
                   100 * SUM(delivered_qty) / NULLIF(SUM(qty), 0) AS pct
            FROM `tabSales Order Item`
            WHERE parent IN %(parents)s
            GROUP BY parent
        ) x ON x.parent = so.name
        SET so.per_delivered = COALESCE(x.pct, 0)
        WHERE so.name IN %(parents)s
        """,
        {"parents": parents},
    )
    frappe.db.commit()


# ---------------------------------------------------------------------------
# Async wrapper — mirrors dn_drain.drain_async / drain_status
# ---------------------------------------------------------------------------

def _result_key(job_id: str) -> str:
    return f"fb_dn_cancel_result:{job_id}"


def _set_progress(msg: str) -> None:
    job_id = getattr(frappe.flags, "fb_dn_cancel_job_id", None)
    if job_id:
        frappe.cache().set_value(_result_key(job_id) + ":progress", msg,
                                 expires_in_sec=86400)


@frappe.whitelist()
def cancel_async(names: str = "", dry_run: int = 1) -> dict:
    """Enqueue cancel_batch on the long worker; poll cancel_status(job_id)."""
    frappe.only_for("System Manager")
    job_id = "fb-dn-cancel-" + frappe.generate_hash(length=8)
    frappe.enqueue(
        "fuelbuddy_dubai.api.dn_cancel._cancel_job",
        queue="long",
        timeout=14400,
        job_id=job_id,
        result_key=_result_key(job_id),
        cancel_kwargs={"names": names, "dry_run": dry_run},
    )
    return {"job_id": job_id}


def _cancel_job(result_key: str, cancel_kwargs: dict) -> None:
    """RQ target: run the batch and stash its result for cancel_status()."""
    frappe.flags.fb_dn_cancel_job_id = result_key.split(":", 1)[1]
    result = cancel_batch(**cancel_kwargs)
    frappe.cache().set_value(result_key, json.dumps(result, default=str),
                             expires_in_sec=86400)


@frappe.whitelist()
def cancel_status(job_id: str) -> dict:
    """Poll a cancel_async job: done (with result), failed (with traceback
    tail), or pending (with last progress marker)."""
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
    return {"status": "pending",
            "progress": frappe.cache().get_value(_result_key(job_id) + ":progress")}
