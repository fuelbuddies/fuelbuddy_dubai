# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Warehouse capacity checks on Purchase Receipt and Stock Entry (IDEV-3119).

These replace the desk Server Scripts "Capacity on Purchase Receipt" and "Capacity for Stock entry"
(Before Save, which is the validate event). Those scripts must be switched off when this deploys.

A warehouse's ``custom_capacity`` is in litres, and so is the Bin balance of an item whose stock unit
is Litre. The scripts added ``qty``, which is in the transaction unit (IG on diesel receipts), so an IG
receipt was under-counted 4.546 times. They also checked each row on its own, so two rows into one
tank could overfill it, and the stock entry script counted rows whose source was the same as the
target.

Here, for items whose stock unit is Litre, a document's rows are totalled per (warehouse, item) in
the stock unit: ``stock_qty`` on a receipt, ``transfer_qty`` into the target warehouse on a stock
entry. ERPNext sets both (qty x conversion factor) in the document's own validate, which runs before
these hooks. The Bin balance plus that total must not exceed the capacity. Warehouses without a
capacity are not checked.

The refusal is a plain ValidationError whose message contains "Capacity": the stock lane
(erp-functions laneReply.util.js) classifies an ERP refusal as fixable by that word.
"""

import frappe
from frappe.utils import flt

CAPACITY_FIELD = "custom_capacity"
LITRE = "Litre"


def validate_purchase_receipt(doc, method=None):
	"""Purchase Receipt validate hook: what the rows put into their warehouses."""
	rows = [(row.warehouse, row.item_code, row.stock_qty) for row in doc.get("items")]
	_check_capacity(rows, "receive")


def validate_stock_entry(doc, method=None):
	"""Stock Entry validate hook: what the rows put into their target warehouses."""
	rows = [
		(row.t_warehouse, row.item_code, row.transfer_qty)
		for row in doc.get("items")
		if row.t_warehouse != row.s_warehouse  # moving stock within one warehouse adds nothing
	]
	_check_capacity(rows, "transfer/receive")


def _check_capacity(rows, action):
	"""Refuse when a warehouse's Bin balance plus this document's total exceeds its capacity.

	``rows`` are (warehouse, item_code, quantity in the item's stock unit)."""
	adding = {}
	for warehouse, item_code, stock_qty in rows:
		if warehouse and item_code:
			key = (warehouse, item_code)
			adding[key] = adding.get(key, 0) + flt(stock_qty)

	if not adding or not frappe.get_meta("Warehouse").has_field(CAPACITY_FIELD):
		return

	for (warehouse, item_code), qty in adding.items():
		if qty <= 0:
			continue  # a return takes stock out, it cannot overfill
		if frappe.db.get_value("Item", item_code, "stock_uom") != LITRE:
			continue
		capacity = flt(frappe.db.get_value("Warehouse", warehouse, CAPACITY_FIELD))
		if not capacity:
			continue
		current = flt(
			frappe.db.get_value("Bin", {"warehouse": warehouse, "item_code": item_code}, "actual_qty")
		)
		new_total = current + qty
		# To the millilitre, so float noise cannot refuse a tank filled exactly.
		if flt(new_total, 3) > flt(capacity, 3):
			frappe.throw(
				f"Cannot {action} {flt(qty, 3)} {LITRE} of {item_code} into {warehouse}. "
				f"Current Balance: {flt(current, 3)} {LITRE}, New Total: {flt(new_total, 3)} {LITRE}, "
				f"Capacity: {flt(capacity, 3)} {LITRE}. Exceeds by {flt(new_total - capacity, 3)} {LITRE}."
			)
