# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The drain leaves the stock lane's draft Delivery Notes alone.

Needs a site with ERPNext (bench run-tests). Everything it creates is rolled back.
"""

import frappe
from frappe.tests.utils import FrappeTestCase

from fuelbuddy_dubai.api import dn_drain

LANE_FLAG = "custom_app_lane_owned"
DAY = "2026-01-15"


class TestDrainSkipsLaneOwned(FrappeTestCase):
	def setUp(self):
		self.company = frappe.db.get_value("Company", {}, "name")
		if not self.company:
			self.skipTest("no Company on this site")
		abbr = frappe.db.get_value("Company", self.company, "abbr")
		self.item = self._ensure(
			"Item",
			"_Test Drain Lane Item",
			item_code="_Test Drain Lane Item",
			item_group=frappe.db.get_value("Item Group", {"is_group": 0}, "name"),
			stock_uom="Nos",
			is_stock_item=1,
		)
		self.warehouse = self._ensure(
			"Warehouse",
			f"_Test Drain Lane WH - {abbr}",
			warehouse_name="_Test Drain Lane WH",
			company=self.company,
		)
		self.customer = self._ensure(
			"Customer",
			"_Test Drain Lane Customer",
			customer_name="_Test Drain Lane Customer",
		)

	def tearDown(self):
		frappe.db.rollback()

	def _ensure(self, doctype, name, **fields):
		if not frappe.db.exists(doctype, name):
			# Test-only master data: skip mandatory custom fields other apps may add.
			frappe.get_doc({"doctype": doctype, **fields}).insert(
				ignore_permissions=True, ignore_mandatory=True
			)
		return name

	def _draft(self, lane_owned):
		doc = frappe.get_doc(
			{
				"doctype": "Delivery Note",
				"company": self.company,
				"customer": self.customer,
				"posting_date": DAY,
				"set_posting_time": 1,
				"items": [
					{
						"item_code": self.item,
						"qty": 1,
						"rate": 1,
						"warehouse": self.warehouse,
					}
				],
			}
		)
		if lane_owned:
			doc.set(LANE_FLAG, 1)
		doc.insert(ignore_permissions=True, ignore_mandatory=True)
		return doc.name

	def _picked(self):
		return set(
			dn_drain._pick_drafts(None, DAY, DAY, 0, self.item, self.warehouse)
		)

	def test_skips_lane_owned_drafts(self):
		if not frappe.db.has_column("Delivery Note", LANE_FLAG):
			self.skipTest(f"Delivery Note has no {LANE_FLAG} column (fuelbuddy_crm not installed)")
		mine = self._draft(lane_owned=False)
		lanes = self._draft(lane_owned=True)
		picked = self._picked()
		self.assertIn(mine, picked)
		self.assertNotIn(lanes, picked)

	def test_picks_drafts_without_the_column(self):
		mine = self._draft(lane_owned=False)
		self.assertIn(mine, self._picked())
