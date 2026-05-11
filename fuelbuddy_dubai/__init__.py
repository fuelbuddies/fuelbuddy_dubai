__version__ = "0.0.1"


# =============================================================================
# Drain-time monkey-patches for ERPNext stock + delivery controllers
# =============================================================================
#
# Four guarded patches that allow the back-dated Delivery Note drain pipeline
# (fuelbuddy_dubai.api.dn_drain.drain) to suppress per-submit work that is
# the dominant bottleneck during bulk back-dated submission.
#
# Each patch is a NO-OP unless its corresponding `frappe.flags.fb_skip_*`
# attribute is explicitly set to True. The drain code sets all four for the
# duration of the submit loop, then resets them. Default ERPNext behaviour
# for every other code path (UI submits, scheduled jobs, REST API, manual
# `bench execute`, etc.) is completely unaffected.
#
# The four deferred operations are recovered after the batch:
#   - update_qty_in_future_sle, repost_future_sle_and_gle, update_bin_qty
#       → recovered by a single consolidated Repost Item Valuation at
#         end-of-batch, which recomputes SLE qty_after_transaction +
#         valuation_rate + stock_value + stock_queue and rewrites GL Entry
#         rows (via repost_gle_for_stock_vouchers).
#   - update_billing_status
#       → recovered by a SQL UPDATE on tabSales Order Item.delivered_qty
#         and tabSales Order.per_delivered at end-of-batch.
#
# Identity vs. native ERPNext submit + consolidated repost has been verified
# at 100-DN scale (April 6 NFPC, crossing 13 GRN events): zero diffs on
# Stock Ledger Entry, GL Entry, and tabBin financial fields.
#
# See docs/back_dated_dn_drain.md for the full operational guide.
# =============================================================================

import frappe
import erpnext.controllers.stock_controller as _sc
import erpnext.stock.stock_ledger as _sl
import erpnext.stock.doctype.delivery_note.delivery_note as _dn_mod


# ---- Patch 1: skip per-submit Repost Item Valuation enqueue ----------------
_orig_repost = _sc.StockController.repost_future_sle_and_gle


def _patched_repost(self, force=False, via_landed_cost_voucher=False):
    if getattr(frappe.flags, "fb_skip_repost", False):
        return
    return _orig_repost(self, force, via_landed_cost_voucher)


_sc.StockController.repost_future_sle_and_gle = _patched_repost


# ---- Patch 2: skip per-submit wide UPDATE on future SLE rows ----------------
_orig_update_future_sle = _sl.update_qty_in_future_sle


def _patched_update_future_sle(args, allow_negative_stock=False):
    if getattr(frappe.flags, "fb_skip_future_sle_update", False):
        return
    return _orig_update_future_sle(args, allow_negative_stock)


_sl.update_qty_in_future_sle = _patched_update_future_sle


# ---- Patch 3: skip per-submit tabBin row update -----------------------------
# stock_ledger.py does `from erpnext.stock.doctype.bin.bin import update_qty as
# update_bin_qty` at module load time, so we patch the alias on stock_ledger,
# not the source module.
_orig_update_bin_qty = _sl.update_bin_qty


def _patched_update_bin_qty(bin_name, args):
    if getattr(frappe.flags, "fb_skip_bin_update", False):
        return
    return _orig_update_bin_qty(bin_name, args)


_sl.update_bin_qty = _patched_update_bin_qty


# ---- Patch 4: skip per-submit update_billing_status on Delivery Note --------
# The Sales Order .per_delivered / SO Item .delivered_qty / SO .status fields
# are pure tracking/display — NO impact on valuation, COGS, GL Entry, or PnL.
# Drain code recomputes them via SQL aggregate at end-of-batch.
_orig_update_billing_status = _dn_mod.DeliveryNote.update_billing_status


def _patched_update_billing_status(self, update_modified=True):
    if getattr(frappe.flags, "fb_skip_billing_status", False):
        return
    return _orig_update_billing_status(self, update_modified)


_dn_mod.DeliveryNote.update_billing_status = _patched_update_billing_status
