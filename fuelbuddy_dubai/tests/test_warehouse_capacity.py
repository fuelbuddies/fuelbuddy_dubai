# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""Warehouse capacity is checked in litres, per tank, across all rows (IDEV-3119).

Needs a site with ERPNext and the Warehouse custom_capacity field (bench run-tests). Receipts and
stock entries are built in memory and never saved: ERPNext's own methods set their stock quantities
(qty x conversion factor), then the hook runs. The test Items, Warehouses and Bins are rolled back.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from fuelbuddy_dubai import warehouse_capacity

IG = 4.546  # litres per imperial gallon (IG)
CAPACITY = 50000  # litres
REFUSED = r"(?i)capacity"  # the stock lane classes an ERP refusal as fixable by this word


class TestCapacityHooks(FrappeTestCase):
	def test_hooks_are_wired(self):
		events = frappe.get_hooks("doc_events")
		self.assertIn(
			"fuelbuddy_dubai.warehouse_capacity.validate_purchase_receipt",
			events["Purchase Receipt"]["validate"],
		)
		self.assertIn(
			"fuelbuddy_dubai.warehouse_capacity.validate_stock_entry",
			events["Stock Entry"]["validate"],
		)


class TestWarehouseCapacity(FrappeTestCase):
	def setUp(self):
		self.company = frappe.db.get_value("Company", {}, "name")
		if not self.company:
			self.skipTest("no Company on this site")
		if not frappe.get_meta("Warehouse").has_field("custom_capacity"):
			self.skipTest("Warehouse has no custom_capacity field")
		item_group = frappe.db.get_value("Item Group", {"is_group": 0}, "name")
		self.litre_item = self._insert(
			"Item",
			item_code="_Test Capacity Litre Item",
			item_name="_Test Capacity Litre Item",
			item_group=item_group,
			stock_uom="Litre",
			is_stock_item=1,
		)
		self.nos_item = self._insert(
			"Item",
			item_code="_Test Capacity Nos Item",
			item_name="_Test Capacity Nos Item",
			item_group=item_group,
			stock_uom="Nos",
			is_stock_item=1,
		)
		self.tank = self._warehouse("_Test Capacity Tank", custom_capacity=CAPACITY)
		self.no_capacity = self._warehouse("_Test Capacity None")
		self.source = self._warehouse("_Test Capacity Source")

	def tearDown(self):
		frappe.db.rollback()

	def _insert(self, doctype, **fields):
		# Test-only master data: skip mandatory custom fields other apps may add. The name comes
		# back from the insert, since Items may be named by a naming series.
		doc = frappe.get_doc({"doctype": doctype, **fields})
		return doc.insert(ignore_permissions=True, ignore_mandatory=True).name

	def _warehouse(self, warehouse_name, **fields):
		return self._insert("Warehouse", warehouse_name=warehouse_name, company=self.company, **fields)

	def _holds(self, litres):
		"""The tank's Bin balance of the Litre item (a Bin only, no ledger)."""
		self._insert("Bin", item_code=self.litre_item, warehouse=self.tank, actual_qty=litres)

	def _receipt(self, *rows, is_return=0):
		"""An unsaved Purchase Receipt. Rows are (warehouse, qty, uom, conversion factor, item)."""
		doc = frappe.get_doc(
			{
				"doctype": "Purchase Receipt",
				"company": self.company,
				"is_return": is_return,
				"items": [
					{"warehouse": wh, "qty": qty, "uom": uom, "conversion_factor": cf, "item_code": item}
					for wh, qty, uom, cf, item in rows
				],
			}
		)
		doc.set_qty_as_per_stock_uom()
		return doc

	def _transfer(self, *rows):
		"""An unsaved Material Transfer. Rows are (source, target, qty, uom, conversion factor)."""
		doc = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Transfer",
				"purpose": "Material Transfer",
				"company": self.company,
				"items": [
					{
						"s_warehouse": source,
						"t_warehouse": target,
						"qty": qty,
						"uom": uom,
						"conversion_factor": cf,
						"item_code": self.litre_item,
					}
					for source, target, qty, uom, cf in rows
				],
			}
		)
		doc.set_transfer_qty()
		return doc

	def _litres(self, warehouse, qty):
		return (warehouse, qty, "Litre", 1, self.litre_item)

	def _gallons(self, warehouse, qty):
		return (warehouse, qty, "IG", IG, self.litre_item)

	# Purchase Receipt

	def test_ig_receipt_is_counted_in_litres(self):
		# 20,000 IG is 90,920 L, over a 50,000 L tank. The old script compared 20,000 with 50,000 and passed it.
		receipt = self._receipt(self._gallons(self.tank, 20000))
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED) as refusal:
			warehouse_capacity.validate_purchase_receipt(receipt)
		message = str(refusal.exception)
		self.assertIn("90920.0 Litre", message)
		self.assertIn("Capacity: 50000.0 Litre", message)
		self.assertIn("Exceeds by 40920.0 Litre", message)

	def test_receipt_that_fills_the_tank_exactly_passes(self):
		self._holds(27270)
		receipt = self._receipt(self._gallons(self.tank, 5000))  # 27,270 + 22,730 L = 50,000 L
		warehouse_capacity.validate_purchase_receipt(receipt)

	def test_receipt_rows_into_one_tank_are_totalled(self):
		# Each row fits on its own; together they are 60,000 L.
		receipt = self._receipt(self._litres(self.tank, 30000), self._litres(self.tank, 30000))
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			warehouse_capacity.validate_purchase_receipt(receipt)

	def test_receipt_counts_the_stock_in_the_tank(self):
		self._holds(46000)
		receipt = self._receipt(self._gallons(self.tank, 1000))  # 46,000 + 4,546 L
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			warehouse_capacity.validate_purchase_receipt(receipt)

	def test_receipt_that_fits_beside_the_stock_passes(self):
		self._holds(45000)
		warehouse_capacity.validate_purchase_receipt(self._receipt(self._gallons(self.tank, 1000)))

	def test_warehouse_without_capacity_is_not_checked(self):
		warehouse_capacity.validate_purchase_receipt(self._receipt(self._litres(self.no_capacity, 100000)))

	def test_item_not_stocked_in_litres_is_not_checked(self):
		receipt = self._receipt((self.tank, 100000, "Nos", 1, self.nos_item))
		warehouse_capacity.validate_purchase_receipt(receipt)

	def test_return_is_not_refused(self):
		self._holds(60000)  # already over capacity: taking stock out must still be allowed
		warehouse_capacity.validate_purchase_receipt(self._receipt(self._litres(self.tank, -1000), is_return=1))

	# Stock Entry

	def test_ig_transfer_is_counted_in_litres(self):
		transfer = self._transfer((self.source, self.tank, 20000, "IG", IG))  # 90,920 L
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			warehouse_capacity.validate_stock_entry(transfer)

	def test_transfer_that_fills_the_tank_exactly_passes(self):
		self._holds(27270)
		transfer = self._transfer((self.source, self.tank, 5000, "IG", IG))  # 27,270 + 22,730 L = 50,000 L
		warehouse_capacity.validate_stock_entry(transfer)

	def test_transfer_rows_into_one_tank_are_totalled(self):
		transfer = self._transfer(
			(self.source, self.tank, 30000, "Litre", 1), (self.source, self.tank, 30000, "Litre", 1)
		)
		with self.assertRaisesRegex(frappe.ValidationError, REFUSED):
			warehouse_capacity.validate_stock_entry(transfer)

	def test_transfer_within_the_tank_adds_nothing(self):
		self._holds(49000)
		# The tank-to-tank row is not counted: 49,000 + 500 L fits.
		transfer = self._transfer((self.tank, self.tank, 30000, "Litre", 1), (self.source, self.tank, 500, "Litre", 1))
		warehouse_capacity.validate_stock_entry(transfer)
