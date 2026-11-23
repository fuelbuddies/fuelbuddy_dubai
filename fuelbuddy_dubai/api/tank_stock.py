# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Read-only stock ledger of tank warehouses, for the app's stock reconciler (IDEV-3553).

The reconciler (IDEV-3549) compares ERP's Stock Ledger Entries in each tank warehouse with the
app's own ledger at ERP's 9 decimal places. `ledger` gives it, in one read:

- the Stock Ledger Entries of one item in the given warehouses, modified at or after
  `modified_since` and posted at or after `posted_since`, each with the keys of the voucher behind it;
- each warehouse's Bin balance;
- whether each warehouse is busy, meaning ERP's figures for it may still change.

Quantities are strings. Frappe's database layer turns DECIMAL into a Python float, which cannot hold
every decimal(21,9) value (12345678901.123456789 comes back as 12345678901.123457), so each quantity
is cast to text in SQL and never passes through a float. Timestamps are cast the same way, so
`modified` always carries six fractional digits and can be passed back as `modified_since` as is.

Cancelled entries are returned and flagged (is_cancelled = 1), never left out. Cancelling a voucher
marks its entries cancelled, bumping their `modified`, and adds reversing entries that are flagged
too. An incremental reader learns of a cancellation only by seeing those rows. ERPNext's own
balances ignore is_cancelled = 1 entries.

`modified_since` is inclusive (>=): entries that share the cursor's timestamp come back again rather
than being skipped. `name` identifies an entry across reads.

`posted_since` is inclusive (>=) too, on `posting_datetime`: the reconciler reads only from the
release cut-over on. Both bounds apply together when both are given.

Access: whoever may read Stock Ledger Entry, as ERP's own permissions grant it.

What `modified` does not show: a back-dated posting shifts qty_after_transaction on every later
entry without touching their `modified` (erpnext stock_ledger.update_qty_in_future_sle). The Repost
Item Valuation it queues rewrites those entries and does bump `modified`; until it has run, the
warehouse reports repost_pending.

Busy, per warehouse:

- repost_pending: a submitted Repost Item Valuation, Queued or In Progress, covers this item in the
  warehouse. Either it is based on Item and Warehouse with this item and warehouse, or it is based on
  a Transaction whose voucher has a Stock Ledger Entry for this item in the warehouse (cancelled
  entries included: a cancelled voucher still owes its repost). It is the rule dn_drain uses to find
  the reposts it must not lose.
- drain_active: the DN drain's run key (shadow_bin.RUN_KEY) is held. The drain submits with Bin
  updates, future-entry qty updates and reposts switched off (fuelbuddy_dubai/__init__.py), so while
  it runs neither Bin nor qty_after_transaction can be trusted. The key names no warehouse, so it
  marks every warehouse busy.

Read-only: SELECTs and one Redis EXISTS.
"""

from __future__ import annotations

import json

import frappe
from frappe import _
from frappe.utils import get_datetime

from fuelbuddy_dubai.api import shadow_bin
from fuelbuddy_dubai.api.dn_drain import PENDING_RIV_STATUSES

# Voucher fields returned with each entry, and the voucher types that carry them. A field is read
# only from the tables that have its column: custom_invoiced_item_id is a Custom Field made on the
# production desk, and custom_app_op_key comes with fuelbuddy_crm (patches/add_app_op_key).
VOUCHER_FIELDS = {
	"custom_invoiced_item_id": ("Delivery Note",),
	"custom_app_op_key": ("Purchase Receipt", "Stock Entry"),
	"is_return": ("Delivery Note", "Purchase Receipt", "Stock Entry", "Sales Invoice", "Purchase Invoice"),
}


@frappe.whitelist()
def ledger(warehouses, item: str, modified_since: str | None = None, posted_since: str | None = None) -> dict:
	"""Stock Ledger Entries, Bin balances and busy flags of `item` in the tank `warehouses`.

	Args:
	    warehouses: warehouse names, as a list or a JSON array (one name alone is accepted too).
	    item: the item code.
	    modified_since: only entries whose `modified` is at or after this ERP-local timestamp, as
	        an entry's `modified` gives it. Empty returns every entry.
	    posted_since: only entries whose `posting_datetime` is at or after this ERP-local
	        timestamp, in the same format. Empty applies no posting bound.

	Returns:
	    {"item", "entries": [...], "warehouses": [{"warehouse", "bin_actual_qty", "repost_pending",
	    "pending_reposts", "drain_active", "busy"}]}, warehouses in the order given.
	"""
	frappe.has_permission("Stock Ledger Entry", "read", throw=True)
	warehouses = _parse_warehouses(warehouses)
	_validate(item, warehouses)
	since = _parse_since(modified_since, "modified_since", "`modified`")
	posted = _parse_since(posted_since, "posted_since", "`posting_datetime`")

	entries = _entries(item, warehouses, since, posted)
	bins = _bin_actual_qty(item, warehouses)
	pending = _pending_reposts(item, warehouses)
	drain_active = bool(shadow_bin._raw_redis().exists(shadow_bin.RUN_KEY))

	return {
		"item": item,
		"entries": entries,
		"warehouses": [
			{
				"warehouse": warehouse,
				"bin_actual_qty": bins.get(warehouse),
				"repost_pending": bool(pending.get(warehouse)),
				"pending_reposts": pending.get(warehouse, []),
				"drain_active": drain_active,
				"busy": bool(pending.get(warehouse)) or drain_active,
			}
			for warehouse in warehouses
		],
	}


def _parse_warehouses(warehouses) -> list[str]:
	if isinstance(warehouses, str):
		warehouses = json.loads(warehouses) if warehouses.strip().startswith("[") else [warehouses]
	if not isinstance(warehouses, list | tuple) or not all(
		isinstance(w, str) and w.strip() for w in warehouses
	):
		frappe.throw(_("warehouses must be a list of warehouse names"))
	warehouses = list(dict.fromkeys(w.strip() for w in warehouses))
	if not warehouses:
		frappe.throw(_("warehouses is required"))
	return warehouses


def _validate(item: str, warehouses: list[str]) -> None:
	"""Refuse an unknown item or warehouse, or a group warehouse, rather than return nothing for it."""
	if not item or not frappe.db.exists("Item", item):
		frappe.throw(_("Unknown item: {0}").format(item))
	found = dict(
		frappe.db.sql(
			"SELECT name, is_group FROM `tabWarehouse` WHERE name IN %(warehouses)s",
			{"warehouses": tuple(warehouses)},
		)
	)
	unknown = [w for w in warehouses if w not in found]
	groups = [w for w in warehouses if found.get(w)]
	if unknown:
		frappe.throw(_("Unknown warehouse(s): {0}").format(", ".join(unknown)))
	if groups:
		frappe.throw(_("Group warehouse(s) hold no stock entries: {0}").format(", ".join(groups)))


def _parse_since(value, arg: str, column: str):
	if not value:
		return None
	try:
		since = get_datetime(value)
	except Exception:
		since = None
	if since is None or since.tzinfo is not None:
		frappe.throw(
			_("{0} must be an ERP-local timestamp without a time zone, as {1} gives it").format(arg, column)
		)
	return since


def _voucher_columns() -> tuple[list[str], list[str]]:
	"""The select expression of each voucher field, and the voucher joins they need.

	Each join is keyed on voucher_type as well as voucher_no, so at most one matches an entry and
	COALESCE picks that voucher's value. A field with no column anywhere reads as NULL."""
	doctypes = sorted({dt for dts in VOUCHER_FIELDS.values() for dt in dts})
	alias = {dt: f"v{i}" for i, dt in enumerate(doctypes)}
	columns, joined = [], set()
	for field, dts in VOUCHER_FIELDS.items():
		present = [dt for dt in dts if frappe.db.has_column(dt, field)]
		joined.update(present)
		exprs = [f"{alias[dt]}.`{field}`" for dt in present]
		expr = "NULL" if not exprs else exprs[0] if len(exprs) == 1 else f"COALESCE({', '.join(exprs)})"
		columns.append(f"{expr} AS `{field}`")
	joins = [
		f"LEFT JOIN `tab{dt}` {alias[dt]} "
		f"ON sle.voucher_type = {frappe.db.escape(dt)} AND {alias[dt]}.name = sle.voucher_no"
		for dt in doctypes
		if dt in joined
	]
	return columns, joins


def _entries(item: str, warehouses: list[str], since, posted) -> list[dict]:
	columns, joins = _voucher_columns()
	modified = "AND sle.modified >= %(since)s" if since else ""
	posting = "AND sle.posting_datetime >= %(posted)s" if posted else ""
	return frappe.db.sql(
		f"""
		SELECT
			sle.name,
			sle.voucher_type,
			sle.voucher_no,
			sle.voucher_detail_no,
			sle.warehouse,
			CAST(sle.posting_datetime AS CHAR) AS posting_datetime,
			CAST(sle.creation AS CHAR) AS creation,
			CAST(sle.modified AS CHAR) AS modified,
			sle.is_cancelled,
			CAST(sle.actual_qty AS CHAR) AS actual_qty,
			CAST(sle.qty_after_transaction AS CHAR) AS qty_after_transaction,
			{", ".join(columns)}
		FROM `tabStock Ledger Entry` sle
		{" ".join(joins)}
		WHERE sle.item_code = %(item)s
			AND sle.warehouse IN %(warehouses)s
			{modified}
			{posting}
		ORDER BY sle.warehouse, sle.posting_datetime, sle.creation, sle.name
		""",
		{"item": item, "warehouses": tuple(warehouses), "since": since, "posted": posted},
		as_dict=True,
	)


def _bin_actual_qty(item: str, warehouses: list[str]) -> dict[str, str]:
	"""Bin.actual_qty as text per warehouse. A warehouse ERP has never stocked has no Bin row."""
	return dict(
		frappe.db.sql(
			"""
			SELECT warehouse, CAST(actual_qty AS CHAR)
			FROM `tabBin`
			WHERE item_code = %(item)s AND warehouse IN %(warehouses)s
			""",
			{"item": item, "warehouses": tuple(warehouses)},
		)
	)


def _pending_reposts(item: str, warehouses: list[str]) -> dict[str, list[str]]:
	"""Queued or In Progress reposts covering `item` in each warehouse (dn_drain's rule)."""
	rows = frappe.db.sql(
		"""
		SELECT riv.name, riv.warehouse
		FROM `tabRepost Item Valuation` riv
		WHERE riv.docstatus = 1
			AND riv.status IN %(statuses)s
			AND riv.item_code = %(item)s
			AND riv.warehouse IN %(warehouses)s
		UNION
		SELECT riv.name, sle.warehouse
		FROM `tabRepost Item Valuation` riv
		JOIN `tabStock Ledger Entry` sle
			ON sle.voucher_type = riv.voucher_type AND sle.voucher_no = riv.voucher_no
		WHERE riv.docstatus = 1
			AND riv.status IN %(statuses)s
			AND riv.based_on = 'Transaction'
			AND sle.item_code = %(item)s
			AND sle.warehouse IN %(warehouses)s
		""",
		{"statuses": PENDING_RIV_STATUSES, "item": item, "warehouses": tuple(warehouses)},
	)
	pending: dict[str, list[str]] = {}
	for name, warehouse in sorted(rows):
		pending.setdefault(warehouse, []).append(name)
	return pending
