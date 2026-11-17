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
       a. Park any RIVs that got auto-queued despite the patch (defence in
          depth; should be 0) — INHERITING their anchors, see below.
       b. Run ONE consolidated Repost Item Valuation for the hot key from the
          earliest of (this batch's earliest posting_date, every anchor we
          parked). This recomputes:
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

Anchor inheritance (IDEV-3156):
  Parking a pending RIV without adopting its anchor silently destroys a
  valuation repair that ERPNext had already queued. Production had 1,534
  Skipped RIVs with anchors from 2026-06-01 to 2026-09-01 against only 3
  Completed, and 22 unhealed valuation-chain breaks as the result.

  The invariant this module now holds:

      the consolidated repost is NEVER anchored later than any repost it
      supersedes

  `resolve_repost_anchor` is the pure statement of that rule and is unit
  tested without a site; `_quiesce_pending_rivs` is its DB shell.

  'In Progress' rows are a special case: we cannot tell a genuinely-running
  repost from a zombie (one that died mid-walk and was left In Progress) from
  SQL alone, so we adopt their anchor but leave their status untouched and
  surface them in the result for a human to clear.

  A parked row is only safe once the consolidated repost that inherits its
  anchor exists (IDEV-3266 review). So parking never happens on a dry run, and
  a run that parks and then ends WITHOUT submitting the consolidated repost
  (no drafts submitted, or an exception) puts the parked rows back to Queued.
  A Transaction-based repost whose voucher also moves stock on another
  item/warehouse is never parked: the consolidated repost covers only the hot
  key, so parking it would drop the other pairs' repost. Its anchor is still
  adopted; it stays Queued (reported as riv_left_queued_multi_pair).

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
from frappe.utils import getdate

from fuelbuddy_dubai.api import shadow_bin
from fuelbuddy_dubai.api.repost_anchor import AnchorTooEarly, resolve_repost_anchor


HOT_ITEM = "FB/FL/00001"
HOT_WAREHOUSE = "Default Warehouse - FFSL"

# Statuses that represent a repost ERPNext still owes us.
PENDING_RIV_STATUSES = ("Queued", "In Progress")


def _quiesce_pending_rivs(item_code: str, warehouse: str, park: bool = True) -> dict:
    """Park Queued RIVs for the hot key and report every pending anchor.

    park=False (dry run) only reads: anchors and would-be-parked rows are
    reported, nothing is updated.

    Parking matters because an independent repost walking the same
    item+warehouse while the drain submits will contend for the same rows —
    that contention is what surfaces as
    `QueryTimeoutError (1205, 'Lock wait timeout exceeded')`.

    'In Progress' rows are reported but NOT touched: from SQL we cannot
    distinguish a live worker from a repost that died mid-walk, and parking a
    live one would abandon a partial walk. Their anchors are still adopted.
    """
    # based_on='Transaction' rows carry voucher_type/voucher_no and leave
    # item_code/warehouse NULL, so filtering on item+warehouse alone is blind
    # to them. In production that blind spot covered exactly the four reposts
    # sitting on the three broken June DNs. Resolve those through the stock
    # ledger instead (is_cancelled deliberately NOT filtered — a cancelled
    # voucher still owes a repost).
    rows = frappe.db.sql(
        """
        SELECT riv.name, riv.posting_date, riv.status, riv.based_on,
               (riv.based_on = 'Transaction' AND EXISTS (
                    SELECT 1 FROM `tabStock Ledger Entry` other
                    WHERE other.voucher_type = riv.voucher_type
                      AND other.voucher_no   = riv.voucher_no
                      AND (other.item_code <> %(item_code)s OR other.warehouse <> %(warehouse)s)
               )) AS touches_other_pairs
        FROM `tabRepost Item Valuation` riv
        WHERE riv.status IN %(statuses)s
          AND riv.docstatus = 1
          AND (
                (riv.item_code = %(item_code)s AND riv.warehouse = %(warehouse)s)
             OR (
                  riv.based_on = 'Transaction'
                  AND EXISTS (
                        SELECT 1 FROM `tabStock Ledger Entry` sle
                        WHERE sle.voucher_type = riv.voucher_type
                          AND sle.voucher_no   = riv.voucher_no
                          AND sle.item_code    = %(item_code)s
                          AND sle.warehouse    = %(warehouse)s
                  )
                )
          )
        """,
        {
            "statuses": PENDING_RIV_STATUSES,
            "item_code": item_code,
            "warehouse": warehouse,
        },
        as_dict=True,
    )

    queued = [r for r in rows if r.status == "Queued"]
    parkable = [r for r in queued if not r.touches_other_pairs]
    multi_pair = [r for r in queued if r.touches_other_pairs]
    in_progress = [r for r in rows if r.status == "In Progress"]

    if park and parkable:
        frappe.db.sql(
            "UPDATE `tabRepost Item Valuation` SET status='Skipped' WHERE name IN %s",
            (tuple(r.name for r in parkable),),
        )
        frappe.db.commit()

    return {
        # every pending anchor, parked or not — the caller MUST fold these in
        "anchors": [r.posting_date for r in rows if r.posting_date],
        "parked": [r.name for r in parkable] if park else [],
        "would_park": [] if park else [r.name for r in parkable],
        "left_queued_multi_pair": [r.name for r in multi_pair],
        "in_progress_untouched": [r.name for r in in_progress],
    }


def _unpark_rivs(names: list) -> list:
    """Put rows this run parked back to Queued (only while still 'Skipped').

    For a run that parked reposts but never submitted the consolidated repost
    that inherits their anchors: without this their valuation repair is lost.
    """
    if not names:
        return []
    frappe.db.sql(
        "UPDATE `tabRepost Item Valuation` SET status='Queued' WHERE name IN %s AND status='Skipped'",
        (tuple(names),),
    )
    frappe.db.commit()
    return list(names)


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
    anchor_floor: Optional[str] = None,
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
        anchor_floor: earliest posting_date the consolidated repost may be
                    anchored at. Inheritance is unbounded by nature and
                    pending anchors reach back to 2026-01-27 (~513k rows,
                    days of walking), so pass this to cap it. When the
                    resolved anchor is earlier, the drain leaves a Queued
                    repost at that anchor and returns rather than starting
                    a walk nobody sized.

    Returns:
        dict with stage breakdowns, per-phase timings, shadow reconciliation,
        and per-DN failure list.
    """
    started = time.time()
    timings: dict[str, float] = {}
    # Set before anything can fail, so the exception path knows whether this
    # call ever held the run key (cleanup is compare-and-delete on it).
    run_id: Optional[str] = None
    # RIVs this call parked, and whether the consolidated repost that inherits
    # their anchors was submitted: until it is, parked rows must be restorable.
    parked: list = []
    consolidated_submitted = False
    result: dict = {
        "stage": "in_progress",
        "params": {
            "customer": customer, "from_date": from_date, "to_date": to_date,
            "batch_size": batch_size, "dry_run": int(dry_run),
            "item_code": item_code, "warehouse": warehouse,
            "min_age_minutes": int(min_age_minutes),
            "anchor_floor": anchor_floor,
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
        # 3. Quiesce pre-existing RIVs in the path, ADOPTING their anchors
        # ------------------------------------------------------------------
        inherited_anchors: list = []
        # A dry run only reads: nothing is parked, so nothing can be lost.
        quiesced_pre = _quiesce_pending_rivs(item_code, warehouse, park=not dry_run)
        inherited_anchors += quiesced_pre["anchors"]
        parked += quiesced_pre["parked"]
        result["riv_parked_pre"] = quiesced_pre["parked"]
        result["riv_would_park_pre"] = quiesced_pre["would_park"]
        result["riv_left_queued_multi_pair"] = quiesced_pre["left_queued_multi_pair"]
        result["riv_in_progress_untouched"] = quiesced_pre["in_progress_untouched"]

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
                # stock_qty, never qty: `qty` is in the LINE's UOM, so an
                # imperial-gallon line understates stock movement 4.546x and
                # both the negative-stock check and the shadow drift silently
                # (IDEV-3156). tabBin is in stock UOM, so stock_qty is what
                # reconciles.
                items = frappe.get_all(
                    "Delivery Note Item",
                    filters={"parent": name},
                    fields=["item_code", "warehouse", "stock_qty"],
                )
                if any(
                    shadow_bin.will_cause_negative(
                        it.item_code, it.warehouse, it.stock_qty
                    )
                    for it in items
                ):
                    skipped_negative.append(name)
                    continue

                if dry_run:
                    submitted.append(name)
                    for it in items:
                        # Update shadow as if we submitted, so subsequent
                        # pre-flight checks are accurate
                        shadow_bin.apply(
                            it.item_code, it.warehouse, it.stock_qty, name
                        )
                    continue

                try:
                    doc = frappe.get_doc("Delivery Note", name)
                    doc.submit()
                    frappe.db.commit()
                    for it in doc.items:
                        shadow_bin.apply(
                            it.item_code, it.warehouse, it.stock_qty, name
                        )
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
            shadow_bin.cleanup(run_id)
            result["stage"] = "dry_run_complete"
            result["timings"] = timings
            return result

        if not submitted:
            # Nothing to repost for, so no consolidated repost will inherit the
            # parked anchors: hand them back to ERPNext's own queue.
            result["riv_unparked"] = _unpark_rivs(parked)
            shadow_bin.cleanup(run_id)
            result["stage"] = "no_submits"
            result["timings"] = timings
            return result

        # ------------------------------------------------------------------
        # 5. Consolidated Repost Item Valuation
        # ------------------------------------------------------------------
        # Park anything auto-queued during the patches (should be 0), again
        # adopting anchors so nothing is lost.
        quiesced_post = _quiesce_pending_rivs(item_code, warehouse)
        inherited_anchors += quiesced_post["anchors"]
        parked += quiesced_post["parked"]
        result["riv_parked_post"] = quiesced_post["parked"]
        result["riv_left_queued_multi_pair"] = sorted(
            set(result["riv_left_queued_multi_pair"]) | set(quiesced_post["left_queued_multi_pair"])
        )
        result["riv_in_progress_untouched"] = sorted(
            set(result["riv_in_progress_untouched"])
            | set(quiesced_post["in_progress_untouched"])
        )

        t = time.time()
        batch_earliest = frappe.db.sql(
            "SELECT MIN(posting_date) FROM `tabDelivery Note` WHERE name IN %s",
            (tuple(submitted),),
        )[0][0]

        # THE fix (IDEV-3156): never anchor later than anything superseded.
        # anchor_floor caps how far inheritance may reach back — without it a
        # single restarted Failed repost (anchors go back to 2026-01-27) would
        # commit this call to a ~513k-row, multi-day inline walk.
        try:
            anchor = resolve_repost_anchor(
                batch_earliest,
                inherited_anchors,
                anchor_floor=getdate(anchor_floor) if anchor_floor else None,
            )
        except AnchorTooEarly as exc:
            # The DNs are submitted and their own values are correct; only the
            # consolidated walk is outstanding. Leave a Queued RIV at the
            # inherited anchor so nothing is lost, and hand the decision back.
            riv = frappe.get_doc({
                "doctype": "Repost Item Valuation",
                "based_on": "Item and Warehouse",
                "item_code": item_code,
                "warehouse": warehouse,
                "posting_date": exc.anchor,
                "posting_time": "00:00:00",
                "allow_negative_stock": 1,
                "company": frappe.db.get_single_value(
                    "Global Defaults", "default_company"
                ),
            }).insert(ignore_permissions=True)
            riv.submit()
            frappe.db.commit()
            consolidated_submitted = True
            shadow_bin.cleanup(run_id)
            result["stage"] = "repost_deferred_anchor_too_early"
            result["repost_anchor"] = str(exc.anchor)
            result["anchor_floor"] = str(exc.floor)
            result["consolidated_repost"] = riv.name
            result["error"] = str(exc)
            result["timings"] = timings
            return result
        result["repost_anchor"] = str(anchor)
        result["repost_anchor_batch_earliest"] = str(batch_earliest)
        result["repost_anchor_inherited"] = anchor != batch_earliest

        company = frappe.db.get_single_value("Global Defaults", "default_company")
        riv = frappe.get_doc({
            "doctype": "Repost Item Valuation",
            "based_on": "Item and Warehouse",
            "item_code": item_code,
            "warehouse": warehouse,
            "posting_date": anchor,
            # midnight: earlier than any real entry on the anchor date, so the
            # walk cannot skip same-day rows
            "posting_time": "00:00:00",
            "allow_negative_stock": 1,
            "company": company,
        }).insert(ignore_permissions=True)
        riv.submit()
        frappe.db.commit()
        consolidated_submitted = True

        from erpnext.stock.doctype.repost_item_valuation.repost_item_valuation import (
            repost as run_repost,
        )

        # RepostItemValuation.on_submit is a no-op outside tests
        # (repost_item_valuation.py:218-232), so submitting does NOT enqueue —
        # the hourly repost_entries() scheduler is the only thing that runs
        # reposts. It selects status IN ('Queued','In Progress') with no lock
        # (:497-505, :476-494), so it can pick this row up between our submit
        # and this call and then run a second walk over the same
        # item+warehouse. Two concurrent walks are what produce
        # QueryTimeoutError (1205, 'Lock wait timeout exceeded'), so only run
        # inline if the row is still ours.
        riv.reload()
        if riv.status == "Queued":
            run_repost(riv)
            frappe.db.commit()
            riv.reload()
            result["repost_ran_inline"] = True
        else:
            result["repost_ran_inline"] = False
        result["repost_status"] = riv.status
        timings["repost_s"] = round(time.time() - t, 3)
        result["consolidated_repost"] = riv.name

        # ------------------------------------------------------------------
        # 6. SQL recompute of SO Item.delivered_qty and SO.per_delivered
        # ------------------------------------------------------------------
        t = time.time()
        # Scoped to the Sales Orders this batch actually touched. The previous
        # unscoped version rewrote every SO Item and every SO in the database
        # on each batch, and its INNER JOIN meant an SO Item whose only DN had
        # been cancelled was never reset — it kept a stale delivered_qty.
        # LEFT JOIN + COALESCE resets those to 0 (IDEV-3156).
        affected_sos = frappe.db.sql_list(
            """
            SELECT DISTINCT soi.parent
            FROM `tabDelivery Note Item` dni
            JOIN `tabSales Order Item` soi ON soi.name = dni.so_detail
            WHERE dni.parent IN %s AND dni.so_detail IS NOT NULL
            """,
            (tuple(submitted),),
        )
        result["affected_sales_orders"] = len(affected_sos)

        if affected_sos:
            frappe.db.sql(
                """
                UPDATE `tabSales Order Item` soi
                LEFT JOIN (
                    SELECT dni.so_detail, SUM(dni.qty) AS d
                    FROM `tabDelivery Note Item` dni
                    JOIN `tabDelivery Note` dn ON dn.name = dni.parent
                    WHERE dn.docstatus = 1 AND dni.so_detail IS NOT NULL
                    GROUP BY dni.so_detail
                ) x ON x.so_detail = soi.name
                SET soi.delivered_qty = COALESCE(x.d, 0)
                WHERE soi.parent IN %s
                """,
                (tuple(affected_sos),),
            )
            frappe.db.sql(
                """
                UPDATE `tabSales Order` so
                LEFT JOIN (
                    SELECT parent,
                           100 * SUM(delivered_qty) / NULLIF(SUM(qty), 0) AS pct
                    FROM `tabSales Order Item`
                    GROUP BY parent
                ) x ON x.parent = so.name
                SET so.per_delivered = COALESCE(x.pct, 0)
                WHERE so.name IN %s
                """,
                (tuple(affected_sos),),
            )
            frappe.db.commit()
        timings["billing_recompute_s"] = round(time.time() - t, 3)

        # ------------------------------------------------------------------
        # 6b. Restore Delivery Note billing status
        # ------------------------------------------------------------------
        # fb_skip_billing_status suppressed DeliveryNote.update_billing_status
        # per submit, and step 6 only restores the SALES ORDER side. Without
        # this, per_billed and status stay stale — production has 79,169 DNs
        # reading "To Bill" whose litres are in fact invoiced (IDEV-3156).
        # Runs with flags cleared so the real method executes. Per-document
        # and therefore the slowest step here; it is timed separately so the
        # cost is visible.
        t = time.time()
        billing_failed: list[dict] = []
        for name in submitted:
            try:
                frappe.get_doc("Delivery Note", name).update_billing_status()
                frappe.db.commit()
            except Exception as exc:
                frappe.db.rollback()
                billing_failed.append({"name": name, "error": str(exc)[:300]})
        frappe.db.commit()
        timings["dn_billing_status_s"] = round(time.time() - t, 3)
        result["dn_billing_status_failed_count"] = len(billing_failed)
        result["dn_billing_status_failed"] = billing_failed[:20]

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
        shadow_bin.cleanup(run_id)

        timings["total_s"] = round(time.time() - started, 3)
        result["stage"] = "complete"
        result["timings"] = timings
        return result

    except Exception as exc:
        # Parked reposts whose anchors no consolidated repost inherited yet go
        # back to Queued, or the run would silently discard them.
        if parked and not consolidated_submitted:
            try:
                frappe.db.rollback()
                result["riv_unparked"] = _unpark_rivs(parked)
            except Exception:
                result["riv_unpark_failed"] = list(parked)
        # Best-effort cleanup
        try:
            shadow_bin.cleanup(run_id)
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
    anchor_floor: Optional[str] = None,
) -> dict:
    """
    Enqueue drain() on the long worker and return a job_id to poll via
    drain_status(). HTTP-safe wrapper: a synchronous drain call can outlive
    the gateway/gunicorn timeout, which kills it MID-BATCH — DNs submitted
    with the skip-flags engaged but no consolidated repost, and a stale
    shadow RUN_KEY that blocks the next drain. RQ jobs have no such timeout.
    Explicit params (no **kwargs) so Frappe's `cmd` form param never leaks in.
    anchor_floor is passed through untouched; see drain() (IDEV-3156).
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
            "anchor_floor": anchor_floor,
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
    if frappe.db.has_column("Delivery Note", "custom_app_lane_owned"):
        # Leave the stock lane's drafts alone: the lane submits them itself, one at a
        # time in fill-time order. The column comes with fuelbuddy_crm; until it exists
        # no note is lane-owned.
        where_parts.append("IFNULL(dn.custom_app_lane_owned, 0) = 0")
    if min_age_minutes > 0:
        # Event 2: leave freshly punched DNs alone — only drain drafts that have
        # sat for at least min_age_minutes since creation.
        where_parts.append(
            "dn.creation <= DATE_SUB(NOW(), INTERVAL %(min_age_minutes)s MINUTE)"
        )

    limit = f"LIMIT {int(batch_size)}" if batch_size and batch_size > 0 else ""

    # DISTINCT: a multi-line DN would otherwise appear once per matching line
    # and be submitted twice, the second attempt landing in `failed`.
    return frappe.db.sql_list(
        f"""
        SELECT DISTINCT dn.name, dn.posting_date, dn.posting_time, dn.creation
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
