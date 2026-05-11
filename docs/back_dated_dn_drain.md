# Back-dated Delivery Note Drain (v3.6)

A fast, identity-preserving drain pipeline for submitting large back-dated
Delivery Note backlogs. Designed for the Fuelbuddy UAE ERPNext instance
where a single hot key — `(item_code = "FB/FL/00001", warehouse = "Default
Warehouse - FFSL")` — receives essentially all stock movement.

---

## Why this exists

Submitting back-dated Delivery Notes through ERPNext's standard path is
serialised by row-level lock contention on a single hot `tabBin` row and
by per-submit `Repost Item Valuation` queueing. Observed production
throughput hovers around **~5 submits/minute** at scale.

`v3.6` defers four expensive per-submit operations during a batch, then
recovers them in one consolidated step at the end. Verified at **120 ms /
submit** on the same hardware — **~80× faster** for the submission phase,
**~8× faster** end-to-end including the final repost.

Identity vs. native ERPNext submit + native repost has been verified
byte-for-byte at 100-DN scale on a fair-baseline test (April 6 NFPC,
crossing 13 GRN events): **zero diffs** on Stock Ledger Entry, GL Entry,
and `tabBin` financial fields.

---

## What it does

For each batch:

| Stage | Action |
|-------|--------|
| 1 | Pick draft DNs in `(customer, date-range, item, warehouse)` window |
| 2 | Initialise a Redis-backed shadow of `tabBin` for the affected pairs |
| 3 | Skip any pre-existing queued `Repost Item Valuation` rows (defence in depth) |
| 4 | **Submit loop** with four `frappe.flags.fb_skip_*` engaged. Each DN: pre-flight against shadow → `doc.submit()` → atomic shadow decrement |
| 5 | **One consolidated `Repost Item Valuation`** from the earliest `posting_date` in the batch. Recomputes SLE valuation/qty, deletes-and-recreates stale GL Entries, refreshes `tabBin` |
| 6 | SQL recompute of `tabSales Order Item.delivered_qty` and `tabSales Order.per_delivered` from authoritative DN rows |
| 7 | Reconcile shadow vs. `tabBin` — `actual_qty` drift must be float-precision noise |
| 8 | Cleanup all `fb_drain:*` Redis keys |

### Why each patch is safe

| Patch | Operation skipped per-submit | Recovery mechanism |
|-------|------------------------------|---------------------|
| `fb_skip_repost` | `repost_future_sle_and_gle` (Repost Item Valuation insertion) | One consolidated RIV at end of batch covers all affected (item, warehouse) chains |
| `fb_skip_future_sle_update` | `update_qty_in_future_sle` (wide `UPDATE` on future SLE rows) | Consolidated RIV recomputes `qty_after_transaction` on every walked SLE |
| `fb_skip_bin_update` | `update_qty` on `tabBin` row | Consolidated RIV calls `update_bin_data` from final SLE; Redis shadow keeps a precise running balance for pre-flight checks |
| `fb_skip_billing_status` | `update_billing_status` on Delivery Note | SQL recompute step writes `delivered_qty` and `per_delivered` from authoritative DN aggregates |

Read each patch's docstring in `fuelbuddy_dubai/__init__.py` for the exact
semantics. The patches are **no-ops outside the drain code path** — they
return immediately unless the corresponding `frappe.flags.fb_skip_*` flag
is set to True.

---

## How to run

### Dry run (recommended first step)

Simulates the pre-flight and shadow update without touching the database.
Reports any drafts that would cause negative stock per the shadow.

```bash
bench --site erpnext.fuelbuddy.ae execute fuelbuddy_dubai.api.dn_drain.drain \
  --kwargs '{
    "from_date": "2026-04-01",
    "to_date":   "2026-04-30",
    "dry_run":   1
  }'
```

### Full month, all customers

```bash
bench --site erpnext.fuelbuddy.ae execute fuelbuddy_dubai.api.dn_drain.drain \
  --kwargs '{
    "from_date": "2026-04-01",
    "to_date":   "2026-04-30"
  }'
```

### Per-customer drain

```bash
bench --site erpnext.fuelbuddy.ae execute fuelbuddy_dubai.api.dn_drain.drain \
  --kwargs '{
    "customer":  "CUST-PRO-000000072",
    "from_date": "2026-04-06",
    "to_date":   "2026-04-12"
  }'
```

### Batched (for progressive checkpoints)

```bash
bench --site erpnext.fuelbuddy.ae execute fuelbuddy_dubai.api.dn_drain.drain \
  --kwargs '{
    "from_date":  "2026-04-01",
    "to_date":    "2026-04-30",
    "batch_size": 10000
  }'
```

---

## Parameters

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `customer` | str \| None | `None` | Filter to one customer; `None` drains all customers |
| `from_date` | str (`YYYY-MM-DD`) | `2026-04-01` | `posting_date` lower bound, inclusive |
| `to_date` | str (`YYYY-MM-DD`) | `2026-04-30` | `posting_date` upper bound, inclusive |
| `batch_size` | int | `0` (all) | Cap on number of drafts processed in this invocation |
| `dry_run` | int | `0` | If `1`, simulate without commits; shadow still tracks for pre-flight accuracy |
| `item_code` | str | `FB/FL/00001` | Hot item — drain assumes single-item, single-warehouse |
| `warehouse` | str | `Default Warehouse - FFSL` | Hot warehouse |

---

## Returned payload

```json
{
  "stage": "complete",
  "params": { /* echo of input params */ },
  "draft_count": 95397,
  "submitted_count": 95397,
  "skipped_negative_count": 0,
  "failed_count": 0,
  "failed": [ /* up to 50 {name, error} pairs */ ],
  "skipped_negative_sample": [ /* up to 20 draft names */ ],
  "submit_avg_ms": 119,
  "run_id": "fb-drain-a1b2c3d4",
  "consolidated_repost": "ri-12345",
  "shadow_reconciliation": {
    "shadow":          { "actual_qty": 8525754.7320, "stock_value": ... },
    "bin":             { "actual_qty": 8525754.7320, "stock_value": ... },
    "drift_actual_qty":   -3.7e-09,
    "drift_reserved_qty":  0.0,
    "drift_stock_value":  -568235.66
  },
  "reconciliation_passed": true,
  "timings": {
    "pick_s":                0.5,
    "shadow_init_s":         0.1,
    "submit_s":          11400.0,
    "repost_s":           4380.0,
    "billing_recompute_s":   5.0,
    "total_s":           15786.0
  }
}
```

`reconciliation_passed: true` means `|drift_actual_qty| < 0.001` and
`|drift_reserved_qty| < 0.001`. `drift_stock_value` is expected to be
non-zero whenever `valuation_rate` changes during the batch — the shadow
tracks a constant rate (snapshot at init), while the consolidated RIV
recomputes the rate from final FIFO state. The authoritative value is
`tabBin.stock_value` after the RIV completes.

---

## Identity guarantees

After a successful drain (`stage == "complete"` and
`reconciliation_passed == true`), the following invariants hold:

- `tabBin` for the affected `(item, warehouse)` matches the value that
  native submit + native repost would have produced on identical input.
- Every new `tabStock Ledger Entry` row has the correct `actual_qty`,
  `valuation_rate`, `stock_value_difference`, `stock_value`,
  `qty_after_transaction`, and `stock_queue`.
- Every new `tabGL Entry` row has the correct `debit` and `credit`
  values on the COGS and Inventory accounts.
- `tabSales Order Item.delivered_qty` and `tabSales Order.per_delivered`
  reflect the SUM of submitted DN qty.

The consolidated `Repost Item Valuation` is what enforces this. It walks
SLE rows forward from the earliest `posting_date`, recomputes all
valuation fields, and calls `repost_gle_for_stock_vouchers` which:

```python
# from erpnext/accounts/utils.py:repost_gle_for_stock_vouchers
for voucher_type, voucher_no in stock_vouchers_chunk:
    expected_gle = voucher_obj.get_gl_entries(warehouse_account)
    if not compare_existing_and_expected_gle(existing_gle, expected_gle, precision):
        _delete_accounting_ledger_entries(voucher_type, voucher_no)
        voucher_obj.make_gl_entries(gl_entries=expected_gle, from_repost=True)
```

Any per-submit GL Entry rows posted with a stale `valuation_rate` get
**deleted and replaced** with correctly-computed ones during the repost.

---

## Failure & recovery

| Scenario | Behaviour |
|----------|-----------|
| Submit fails on a specific DN (validation, etc.) | DN added to `failed[]` with traceback in `tabError Log`; batch continues |
| Reconciliation fails (drift exceeds threshold) | `reconciliation_passed: false` in return payload. **Stop. Do not re-enable live operations.** Inspect `fb_drain:bin_mutations:*` LIST in Redis for the per-DN audit log |
| Drain process is killed mid-batch | Shadow keys persist with 24h TTL. Submitted DNs are at `docstatus=1` with potentially stale per-submit fields (Bin, SLE, GL). Re-running `drain()` with the same window picks up remaining drafts; the consolidated repost at the end converges everything |
| RIV creation fails | Caught and logged. Other consequences depend on which patches were engaged at the time — manual investigation needed |
| Redis is unavailable | `shadow_bin.initialize()` will fail before submit loop begins; drain aborts with no DB changes |

---

## Pre-flight checklist (before running in production)

1. **Server Script `Sales Order updated with Draft qty of delivery note`
   is disabled.** It does its own aggregate UPDATE on every DN save and
   makes the drain ~3× slower without contributing financial value
   (the same field is updated by ERPNext's own `update_billing_status`,
   which we already defer + recompute via SQL).
2. **Live submissions paused.** The shadow assumes nothing else mutates
   `tabBin` for the hot key during the batch.
3. **DB backup taken** (`mariabackup --backup` or `bench backup`).
4. **Dry run executed** for the target window. Review
   `skipped_negative_count` and disposition any flagged drafts.
5. **`server_script_enabled: 1` set in `common_site_config.json`** — only
   matters if any DN doctype-event Server Scripts exist that we want to
   keep running. Production has these disabled, so this is informational.

---

## Post-drain validation (audit checklist)

After `drain()` returns `stage == "complete"`:

```sql
-- 1. Every submitted DN has at least one SLE row
SELECT dn.name
FROM `tabDelivery Note` dn
LEFT JOIN `tabStock Ledger Entry` sle
  ON sle.voucher_type = 'Delivery Note' AND sle.voucher_no = dn.name
WHERE dn.docstatus = 1 AND sle.name IS NULL;
-- expected: empty

-- 2. Every submitted DN has balanced GL Entries
SELECT voucher_no, SUM(debit) - SUM(credit) AS imbalance
FROM `tabGL Entry`
WHERE voucher_type = 'Delivery Note' AND is_cancelled = 0
GROUP BY voucher_no
HAVING ABS(imbalance) > 0.01;
-- expected: empty

-- 3. tabBin.actual_qty matches the latest SLE.qty_after_transaction
SELECT b.item_code, b.warehouse, b.actual_qty,
       (SELECT qty_after_transaction
        FROM `tabStock Ledger Entry`
        WHERE item_code = b.item_code AND warehouse = b.warehouse AND is_cancelled = 0
        ORDER BY posting_datetime DESC, creation DESC LIMIT 1) AS sle_latest
FROM `tabBin` b
WHERE b.item_code = 'FB/FL/00001' AND b.warehouse = 'Default Warehouse - FFSL'
HAVING ABS(actual_qty - sle_latest) > 0.001;
-- expected: empty

-- 4. SO.per_delivered matches the SO Item aggregate
SELECT so.name, so.per_delivered,
       (SELECT 100.0 * SUM(soi.delivered_qty) / NULLIF(SUM(soi.qty), 0)
        FROM `tabSales Order Item` soi WHERE soi.parent = so.name) AS computed
FROM `tabSales Order` so
WHERE so.docstatus = 1 AND so.per_delivered > 0
HAVING ABS(per_delivered - computed) > 0.1;
-- expected: empty
```

If any of these return rows, **do not re-enable live operations**.

---

## Performance projection

Measured rates on local dev hardware (Docker, M-series Mac, MariaDB 10.6):

| Phase | Native rate | v3.6 rate |
|-------|-------------|-----------|
| Submit (per DN) | ~9.4 sec | ~0.12 sec |
| Consolidated repost (per SLE walked) | ~0.6 sec | ~0.6 sec (same code path) |

Estimated runtime for **95,000-DN drain** on comparable production
hardware:

```
Submit phase :  95,000 × 0.12 s  =   3 h 10 min
Repost       : 120,000 × 0.6 s   =   1 h 20 min    (95k new + ~25k pre-existing SLEs)
SQL recompute:                       <    5 s
-----------------------------------------------------------------
Total                            ≈   4 h 30 min
```

Fits comfortably in a single off-hours maintenance window.

---

## Files

- `fuelbuddy_dubai/__init__.py` — the four guarded monkey-patches.
- `fuelbuddy_dubai/api/shadow_bin.py` — Redis-backed atomic shadow of
  `tabBin` for pre-flight checks and audit log.
- `fuelbuddy_dubai/api/dn_drain.py` — the orchestrator. Single public
  entry point: `drain()`.

The patches are loaded at app import time. They are no-ops unless the
drain code explicitly engages them. No other call site is affected.
