# Copyright (c) 2026, Fuelbuddy and contributors
# For license information, please see license.txt

"""The tank stock ledger read (IDEV-3553).

Most tests write synthetic rows straight into the tables the read selects from, with quantities
given as decimal strings, so they hold at 9 decimal places whatever the site's float precision. One
test posts and cancels a real Stock Entry to show how ERPNext flags a cancellation. The drain-key
tests need the site's redis_cache. Everything is rolled back.

    bench --site <site> run-tests --module fuelbuddy_dubai.tests.test_tank_stock
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from fuelbuddy_dubai.api import shadow_bin, tank_stock

NINE_DP = re.compile(r"^-?\d+\.\d{9}$")
WRITE = re.compile(r"^\s*(insert|update|delete|replace|alter|create|drop|truncate|lock)\b", re.I)


def _insert(table, **row):
	columns = ", ".join(f"`{c}`" for c in row)
	values = ", ".join(f"%({c})s" for c in row)
	frappe.db.sql(f"INSERT INTO `{table}` ({columns}) VALUES ({values})", row)


class TankStockCase(FrappeTestCase):
	def setUp(self):
		self.tag = frappe.generate_hash(length=8).upper()
		self.item = self._item(f"_T-TSL-ITEM-{self.tag}")
		self.tank_a = self._warehouse(f"_T-TSL Tank A {self.tag}")
		self.tank_b = self._warehouse(f"_T-TSL Tank B {self.tag}")

	def tearDown(self):
		frappe.db.rollback()

	# ---- synthetic rows ----------------------------------------------------------------------
	def _item(self, code):
		_insert("tabItem", name=code, item_code=code, item_name=code, stock_uom="Litre", is_stock_item=1)
		return code

	def _warehouse(self, name, is_group=0):
		_insert("tabWarehouse", name=name, warehouse_name=name, is_group=is_group)
		return name

	def _sle(
		self,
		warehouse,
		actual_qty,
		qty_after="0",
		*,
		voucher_type="Stock Entry",
		voucher_no=None,
		modified="2026-11-20 10:00:00.000000",
		posting="2026-11-20 09:00:00.000000",
		is_cancelled=0,
		item=None,
	):
		name = f"_T-TSL-SLE-{frappe.generate_hash(length=10)}"
		_insert(
			"tabStock Ledger Entry",
			name=name,
			item_code=item or self.item,
			warehouse=warehouse,
			voucher_type=voucher_type,
			voucher_no=voucher_no or f"_T-TSL-V-{self.tag}",
			voucher_detail_no=f"{name}-row",
			posting_date=posting[:10],
			posting_time=posting[11:],
			posting_datetime=posting,
			creation=modified,
			modified=modified,
			actual_qty=actual_qty,
			qty_after_transaction=qty_after,
			is_cancelled=is_cancelled,
			docstatus=1,
		)
		return name

	def _bin(self, warehouse, actual_qty):
		_insert(
			"tabBin",
			name=f"_T-TSL-BIN-{frappe.generate_hash(length=10)}",
			item_code=self.item,
			warehouse=warehouse,
			actual_qty=actual_qty,
		)

	def _riv(self, status="Queued", docstatus=1, **fields):
		name = f"_T-TSL-RIV-{frappe.generate_hash(length=10)}"
		fields = fields or {
			"based_on": "Item and Warehouse",
			"item_code": self.item,
			"warehouse": self.tank_a,
		}
		_insert(
			"tabRepost Item Valuation",
			name=name,
			status=status,
			docstatus=docstatus,
			posting_date="2026-11-20",
			**fields,
		)
		return name

	def _ledger(self, warehouses=None, **kw):
		return tank_stock.ledger(warehouses or [self.tank_a, self.tank_b], self.item, **kw)

	def _state(self, result, warehouse):
		return next(w for w in result["warehouses"] if w["warehouse"] == warehouse)


class TestQuantities(TankStockCase):
	def test_quantities_are_strings_with_all_nine_decimals(self):
		qtys = ["1000.000000002", "-0.100000000", "12345678901.123456789", "0.000000001"]
		# What a float would do to them: the test values are ones a float cannot carry.
		self.assertNotEqual(str(float(qtys[2])), qtys[2])
		self.assertNotEqual(str(float(qtys[1])), qtys[1])
		for i, qty in enumerate(qtys):
			self._sle(self.tank_a, qty, qty, modified=f"2026-11-20 10:00:0{i}.000000")
		self._bin(self.tank_a, "12345679901.023456792")

		result = json.loads(frappe.as_json(self._ledger()))  # as the HTTP response carries it

		self.assertEqual([e["actual_qty"] for e in result["entries"]], qtys)
		self.assertEqual([e["qty_after_transaction"] for e in result["entries"]], qtys)
		for e in result["entries"]:
			self.assertRegex(e["actual_qty"], NINE_DP)
		self.assertEqual(self._state(result, self.tank_a)["bin_actual_qty"], "12345679901.023456792")

	def test_warehouse_without_a_bin_reads_none(self):
		self.assertIsNone(self._state(self._ledger(), self.tank_b)["bin_actual_qty"])

	def test_entry_fields_and_timestamps(self):
		name = self._sle(
			self.tank_a,
			"5.000000000",
			"5.000000000",
			voucher_no="_T-TSL-SE-1",
			modified="2026-11-20 10:00:00.000001",
			posting="2026-11-19 08:30:00.000000",
		)
		(entry,) = self._ledger()["entries"]
		self.assertEqual(entry["name"], name)
		self.assertEqual(entry["voucher_type"], "Stock Entry")
		self.assertEqual(entry["voucher_no"], "_T-TSL-SE-1")
		self.assertEqual(entry["voucher_detail_no"], f"{name}-row")
		self.assertEqual(entry["warehouse"], self.tank_a)
		self.assertEqual(entry["posting_datetime"], "2026-11-19 08:30:00.000000")
		self.assertEqual(entry["creation"], "2026-11-20 10:00:00.000001")
		self.assertEqual(entry["modified"], "2026-11-20 10:00:00.000001")
		self.assertEqual(entry["is_cancelled"], 0)

	def test_only_the_item_in_the_given_warehouses(self):
		other_item = self._item(f"_T-TSL-OTHER-{self.tag}")
		elsewhere = self._warehouse(f"_T-TSL Elsewhere {self.tag}")
		mine = self._sle(self.tank_a, "1.000000000")
		self._sle(self.tank_a, "2.000000000", item=other_item)
		self._sle(elsewhere, "3.000000000")
		self.assertEqual([e["name"] for e in self._ledger()["entries"]], [mine])


class TestCancelled(TankStockCase):
	"""Cancelled entries are returned, flagged by is_cancelled = 1, never left out."""

	def test_cancelled_entries_are_returned_and_flagged(self):
		live = self._sle(self.tank_a, "7.000000000", voucher_no="_T-TSL-LIVE")
		original = self._sle(self.tank_a, "4.500000000", voucher_no="_T-TSL-GONE", is_cancelled=1)
		reversal = self._sle(self.tank_a, "-4.500000000", voucher_no="_T-TSL-GONE", is_cancelled=1)
		flags = {e["name"]: e["is_cancelled"] for e in self._ledger()["entries"]}
		self.assertEqual(flags, {live: 0, original: 1, reversal: 1})

	def test_a_real_cancellation_comes_back_flagged_and_newly_modified(self):
		company = frappe.db.get_single_value("Global Defaults", "default_company")
		if not company:
			self.skipTest("needs an ERPNext company")
		item, tank = _real_item(self.tag), _real_warehouse(self.tag, company)
		receipt = frappe.get_doc(
			{
				"doctype": "Stock Entry",
				"stock_entry_type": "Material Receipt",
				"company": company,
				"items": [{"item_code": item, "qty": 10.5, "t_warehouse": tank, "basic_rate": 3.0}],
			}
		).insert(ignore_permissions=True)
		receipt.submit()
		(before,) = tank_stock.ledger([tank], item)["entries"]
		self.assertEqual((before["actual_qty"], before["is_cancelled"]), ("10.500000000", 0))

		receipt.cancel()

		after = tank_stock.ledger([tank], item, modified_since=before["modified"])["entries"]
		by_name = {e["name"]: e for e in after}
		self.assertEqual(len(after), 2, "the original and its reversal")
		self.assertEqual(by_name[before["name"]]["is_cancelled"], 1)
		self.assertGreater(by_name[before["name"]]["modified"], before["modified"])
		(reversal,) = [e for e in after if e["name"] != before["name"]]
		self.assertEqual((reversal["actual_qty"], reversal["is_cancelled"]), ("-10.500000000", 1))
		self.assertEqual(reversal["voucher_no"], receipt.name)


class TestModifiedSince(TankStockCase):
	def setUp(self):
		super().setUp()
		self.t1 = self._sle(self.tank_a, "1.000000000", modified="2026-11-20 10:00:00.000000")
		self.t2 = self._sle(self.tank_a, "2.000000000", modified="2026-11-20 10:00:00.000001")
		self.t3 = self._sle(self.tank_b, "3.000000000", modified="2026-11-21 00:00:00.000000")

	def _names(self, since):
		return {e["name"] for e in self._ledger(modified_since=since)["entries"]}

	def test_no_cursor_returns_everything(self):
		self.assertEqual(self._names(None), {self.t1, self.t2, self.t3})
		self.assertEqual(self._names(""), {self.t1, self.t2, self.t3})

	def test_cursor_is_inclusive_to_the_microsecond(self):
		self.assertEqual(self._names("2026-11-20 10:00:00.000001"), {self.t2, self.t3})

	def test_cursor_after_everything_returns_nothing(self):
		self.assertEqual(self._names("2026-11-21 00:00:00.000001"), set())

	def test_returned_modified_round_trips_as_the_cursor(self):
		entries = self._ledger()["entries"]
		cursor = max(e["modified"] for e in entries)
		self.assertEqual(self._names(cursor), {self.t3})

	def test_cursor_with_a_time_zone_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self._ledger(modified_since="2026-11-20T10:00:00+00:00")
		with self.assertRaises(frappe.ValidationError):
			self._ledger(modified_since="not a timestamp")


class TestVoucherFields(TankStockCase):
	def _voucher(self, doctype, name, **fields):
		row = {"name": name, "docstatus": 1}
		row.update({f: v for f, v in fields.items() if frappe.db.has_column(doctype, f)})
		_insert(f"tab{doctype}", **row)

	def _expect(self, doctype, field, value):
		return value if frappe.db.has_column(doctype, field) else None

	def test_each_voucher_brings_its_own_keys(self):
		key = self.tag.lower()
		self._voucher("Delivery Note", f"_T-TSL-DN-{key}", custom_invoiced_item_id=f"ii-{key}", is_return=0)
		self._voucher("Purchase Receipt", f"_T-TSL-PR-{key}", custom_app_op_key=f"erp-fr-{key}", is_return=1)
		self._voucher("Stock Entry", f"_T-TSL-SE-{key}", custom_app_op_key=f"erp-issue-{key}", is_return=0)
		self._sle(self.tank_a, "-1.000000000", voucher_type="Delivery Note", voucher_no=f"_T-TSL-DN-{key}")
		self._sle(self.tank_a, "2.000000000", voucher_type="Purchase Receipt", voucher_no=f"_T-TSL-PR-{key}")
		self._sle(self.tank_a, "3.000000000", voucher_type="Stock Entry", voucher_no=f"_T-TSL-SE-{key}")
		self._sle(
			self.tank_a, "0.000000000", voucher_type="Stock Reconciliation", voucher_no=f"_T-TSL-SR-{key}"
		)

		got = {
			e["voucher_type"]: (e["custom_invoiced_item_id"], e["custom_app_op_key"], e["is_return"])
			for e in self._ledger()["entries"]
		}

		dn, pr, se = "Delivery Note", "Purchase Receipt", "Stock Entry"
		self.assertEqual(got[dn], (self._expect(dn, "custom_invoiced_item_id", f"ii-{key}"), None, 0))
		self.assertEqual(got[pr], (None, self._expect(pr, "custom_app_op_key", f"erp-fr-{key}"), 1))
		self.assertEqual(got[se], (None, self._expect(se, "custom_app_op_key", f"erp-issue-{key}"), 0))
		self.assertEqual(got["Stock Reconciliation"], (None, None, None))

	def test_missing_key_columns_read_as_none(self):
		"""Before fuelbuddy_crm's op-key patch (or on a site without the desk Custom Field)."""
		self._voucher("Delivery Note", f"_T-TSL-DN-{self.tag}", is_return=1)
		self._sle(
			self.tank_a, "-1.000000000", voucher_type="Delivery Note", voucher_no=f"_T-TSL-DN-{self.tag}"
		)
		real = frappe.db.has_column

		def without_keys(doctype, column):
			return column not in ("custom_invoiced_item_id", "custom_app_op_key") and real(doctype, column)

		with patch.object(frappe.local.db, "has_column", side_effect=without_keys):
			(entry,) = self._ledger()["entries"]
		self.assertEqual((entry["custom_invoiced_item_id"], entry["custom_app_op_key"]), (None, None))
		self.assertEqual(entry["is_return"], 1)


class TestBusy(TankStockCase):
	def setUp(self):
		super().setUp()
		self.redis = shadow_bin._raw_redis()
		if self.redis.exists(shadow_bin.RUN_KEY):
			self.skipTest(f"{shadow_bin.RUN_KEY} is held by a real run on this site")

	def tearDown(self):
		shadow_bin.release_run_key(f"_t-tsl-{self.tag}")
		super().tearDown()

	def _busy(self):
		result = self._ledger()
		return {
			w["warehouse"]: (w["repost_pending"], w["drain_active"], w["busy"]) for w in result["warehouses"]
		}

	def test_idle(self):
		self.assertEqual(
			self._busy(), {self.tank_a: (False, False, False), self.tank_b: (False, False, False)}
		)

	def test_item_and_warehouse_repost_queued_or_in_progress(self):
		tank_c = self._warehouse(f"_T-TSL Tank C {self.tag}")
		queued = self._riv("Queued")
		in_progress = self._riv(
			"In Progress", based_on="Item and Warehouse", item_code=self.item, warehouse=tank_c
		)
		result = self._ledger([self.tank_a, self.tank_b, tank_c])
		self.assertEqual(self._state(result, self.tank_a)["pending_reposts"], [queued])
		self.assertEqual(self._state(result, tank_c)["pending_reposts"], [in_progress])
		busy = {
			w["warehouse"]: (w["repost_pending"], w["drain_active"], w["busy"]) for w in result["warehouses"]
		}
		self.assertEqual(
			busy,
			{
				self.tank_a: (True, False, True),
				self.tank_b: (False, False, False),
				tank_c: (True, False, True),
			},
		)

	def test_settled_draft_or_other_item_reposts_are_not_busy(self):
		for status in ("Completed", "Skipped", "Failed"):
			self._riv(status)
		self._riv("Queued", docstatus=0)
		other_item = self._item(f"_T-TSL-OTHER-{self.tag}")
		self._riv(based_on="Item and Warehouse", item_code=other_item, warehouse=self.tank_a)
		self.assertEqual(
			self._busy(), {self.tank_a: (False, False, False), self.tank_b: (False, False, False)}
		)

	def test_transaction_repost_marks_the_warehouses_its_voucher_touched(self):
		voucher = f"_T-TSL-DN-{self.tag}"
		# A cancelled voucher still owes its repost.
		self._sle(
			self.tank_b, "-2.000000000", voucher_type="Delivery Note", voucher_no=voucher, is_cancelled=1
		)
		riv = self._riv(based_on="Transaction", voucher_type="Delivery Note", voucher_no=voucher)
		result = self._ledger()
		self.assertEqual(self._state(result, self.tank_b)["pending_reposts"], [riv])
		self.assertEqual(self._busy(), {self.tank_a: (False, False, False), self.tank_b: (True, False, True)})

	def test_held_drain_key_marks_every_warehouse(self):
		self.assertTrue(shadow_bin.acquire_run_key(f"_t-tsl-{self.tag}", 60))
		self.assertEqual(self._busy(), {self.tank_a: (False, True, True), self.tank_b: (False, True, True)})
		shadow_bin.release_run_key(f"_t-tsl-{self.tag}")
		self.assertEqual(
			self._busy(), {self.tank_a: (False, False, False), self.tank_b: (False, False, False)}
		)


class TestReadOnlyAndAccess(TankStockCase):
	def test_reads_only(self):
		self._sle(self.tank_a, "1.000000000")
		self._bin(self.tank_a, "1.000000000")
		self._riv()
		queries, real_sql = [], frappe.db.sql

		def spy(query, *args, **kwargs):
			queries.append(str(query))
			return real_sql(query, *args, **kwargs)

		redis = MagicMock()
		redis.exists.return_value = 0
		writes_before = frappe.db.transaction_writes
		with (
			patch.object(frappe.local.db, "sql", side_effect=spy),
			patch.object(shadow_bin, "_raw_redis", return_value=redis),
		):
			self._ledger()

		self.assertTrue(queries)
		self.assertEqual([q for q in queries if WRITE.match(q)], [])
		self.assertEqual(frappe.db.transaction_writes, writes_before)
		self.assertEqual([c[0] for c in redis.method_calls], ["exists"])

	def test_system_manager_only(self):
		in_test = frappe.flags.in_test
		frappe.flags.in_test = False  # frappe.only_for lets every test through
		try:
			frappe.set_user("Guest")
			with self.assertRaises(frappe.PermissionError):
				self._ledger()
		finally:
			frappe.set_user("Administrator")
			frappe.flags.in_test = in_test

	def test_warehouses_as_json_or_a_single_name(self):
		self._sle(self.tank_a, "1.000000000")
		as_json = tank_stock.ledger(json.dumps([self.tank_a, self.tank_a]), self.item)
		self.assertEqual([w["warehouse"] for w in as_json["warehouses"]], [self.tank_a])
		single = tank_stock.ledger(self.tank_a, self.item)
		self.assertEqual(len(single["entries"]), 1)

	def test_refuses_what_it_cannot_read(self):
		group = self._warehouse(f"_T-TSL Group {self.tag}", is_group=1)
		for warehouses, item in (
			([f"_T-TSL Nowhere {self.tag}"], self.item),
			([group], self.item),
			([self.tank_a], f"_T-TSL-NO-ITEM-{self.tag}"),
			([], self.item),
		):
			with self.subTest(warehouses=warehouses, item=item):
				with self.assertRaises(frappe.ValidationError):
					tank_stock.ledger(warehouses, item)


# ---- real documents ------------------------------------------------------------------------------
def _real_item(tag):
	code = f"_T-TSL-REAL-{tag}"
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": code,
			"item_name": code,
			"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
			"stock_uom": "Litre",
			"is_stock_item": 1,
			"valuation_rate": 3.0,
		}
	).insert(ignore_permissions=True, ignore_mandatory=True)
	return code


def _real_warehouse(tag, company):
	return (
		frappe.get_doc({"doctype": "Warehouse", "warehouse_name": f"_T-TSL Real {tag}", "company": company})
		.insert(ignore_permissions=True)
		.name
	)
