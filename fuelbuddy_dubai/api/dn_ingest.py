# =============================================================================
# Delivery Note ingest — async draft creation off the gunicorn web workers
# =============================================================================
# erp-functions (the Hasura -> ERPNext integration) used to create DN drafts by
# calling POST /api/resource/Delivery Note, which runs `doc.insert()` inline on
# a gunicorn web worker: full validation + item pricing + taxes + the
# Document-Naming-Rule `FOR UPDATE` lock on the naming series. Under Hasura
# punch load that held workers for the whole insert and starved the ERPNext UI.
#
# This module exposes a whitelisted endpoint that simply ENQUEUES the insert
# onto an RQ background queue and returns immediately, so the gunicorn worker is
# freed in milliseconds. The background job performs the insert (draft,
# docstatus 0) and writes the resulting DN name + sync status back to Hasura
# itself (invoiced_item.delivery_note_erp_code / erp_sync_status).
#
# Submission (draft -> docstatus 1) is NOT done here — it stays out-of-band via
# the serialized, cascade-skipping drain. "Synced to ERP" == "draft created".
#
# Deploy: drop this file at fuelbuddy_dubai/api/dn_ingest.py (with an empty
# fuelbuddy_dubai/api/__init__.py) in the deployed app, `bench build`/restart so
# the RQ workers pick up the new code, and set the two site-config keys below.
#
# Required site config (site_config.json or common_site_config.json):
#   "hasura_endpoint":     "https://<hasura-host>/v1/graphql"
#   "hasura_admin_secret": "<x-hasura-admin-secret>"
# =============================================================================

import json

import frappe
from frappe import _
from frappe.utils.background_jobs import enqueue

DOCTYPE = "Delivery Note"

# RQ queue for the insert. 'long' keeps the heavy insert off the short/default
# queues and — because the long queue runs at low worker concurrency — naturally
# serializes the naming-series `FOR UPDATE` lock instead of letting dozens of
# concurrent web requests collide on it.
INGEST_QUEUE = "long"
INGEST_TIMEOUT = 600  # seconds


@frappe.whitelist()
def enqueue_delivery_note(dn_payload, invoiced_item_id):
	"""Whitelisted entrypoint called by erp-functions.

	Hands a fully-built Delivery Note payload to an RQ job and returns at once.

	:param dn_payload: the DN doc payload (JSON string or dict), incl. child
	    `items`/`taxes`. Same object erp-functions used to POST to the resource
	    API. `docstatus` is forced to 0 (draft) regardless of what is sent.
	:param invoiced_item_id: Hasura invoiced_item.id — used as the RQ job's
	    idempotency key (dedupes re-delivered Hasura events) and for writeback.
	"""
	if isinstance(dn_payload, str):
		dn_payload = json.loads(dn_payload)

	if not invoiced_item_id:
		frappe.throw(_("invoiced_item_id is required"))

	# Never let an enqueue request accidentally submit.
	dn_payload["docstatus"] = 0

	job = enqueue(
		"fuelbuddy_dubai.api.dn_ingest.create_delivery_note_draft",
		queue=INGEST_QUEUE,
		timeout=INGEST_TIMEOUT,
		# Idempotency: if the same invoiced_item is still queued/running, don't
		# enqueue a second insert (Hasura may re-deliver the event).
		job_id=_job_id(invoiced_item_id),
		deduplicate=True,
		dn_payload=dn_payload,
		invoiced_item_id=invoiced_item_id,
	)

	return {
		"queued": True,
		"job_id": getattr(job, "id", None),
		"invoiced_item_id": invoiced_item_id,
	}


def create_delivery_note_draft(dn_payload, invoiced_item_id):
	"""Unit of work: insert the DN as a draft, then sync the result to Hasura.

	Idempotent: if a DN already exists for this invoiced_item (re-delivery, or a
	prior run that inserted but failed before writeback) it is reused rather than
	duplicated.

	Orchestrator-agnostic: today this is invoked by frappe.enqueue (RQ). It is
	intentionally a plain function with idempotent semantics so it can later be
	driven by a Temporal activity instead — bringing Temporal in is a matter of
	changing the trigger, not this body.
	"""
	try:
		existing = _existing_dn(invoiced_item_id)
		if existing:
			_run_duplicate_check(invoiced_item_id, existing, dn_payload.get("amended_from"))
			_sync_hasura(invoiced_item_id, existing, "COMPLETED")
			return

		doc = frappe.get_doc({"doctype": DOCTYPE, **dn_payload})
		doc.docstatus = 0  # draft
		doc.insert(ignore_permissions=True)
		frappe.db.commit()

		_run_duplicate_check(invoiced_item_id, doc.name, dn_payload.get("amended_from"))
		_sync_hasura(invoiced_item_id, doc.name, "COMPLETED")

	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="DN Ingest — create failed",
			message="invoiced_item={0}\n{1}".format(invoiced_item_id, frappe.get_traceback()),
		)
		# Best-effort: mark the row FAILED so erp-functions' retry/monitoring can
		# pick it up. Re-raise so RQ records the failure for this job.
		_sync_hasura(invoiced_item_id, None, "FAILED")
		raise


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _job_id(invoiced_item_id):
	return "dn-ingest::{0}".format(invoiced_item_id)


def _existing_dn(invoiced_item_id):
	"""Return the name of a non-cancelled DN already created for this invoiced
	item, if any (docstatus 0 draft or 1 submitted)."""
	return frappe.db.get_value(
		DOCTYPE,
		{"custom_invoiced_item_id": invoiced_item_id, "docstatus": ["<", 2]},
		"name",
	)


def _run_duplicate_check(invoiced_item_id, dn_name, amended_from):
	"""Defense-in-depth: catch race-created duplicates (two jobs/events that both
	inserted for the same invoiced_item). Logs an Error Log entry and files an
	Issue so ops can cancel the redundant doc — mirrors the check erp-functions
	used to run inline, moved here because this is where the new DN name is known."""
	others = frappe.get_all(
		DOCTYPE,
		filters={
			"custom_invoiced_item_id": invoiced_item_id,
			"name": ["!=", dn_name],
			"docstatus": ["<", 2],
		},
		fields=["name", "docstatus"],
	)
	if not others:
		return

	dup_list = ", ".join("{0}(docstatus={1})".format(d.name, d.docstatus) for d in others)
	dup_msg = "DN {0} has {1} other DN(s) for invoiced_item {2} — {3}".format(
		dn_name, len(others), invoiced_item_id, dup_list
	)
	subject = "DUPLICATE_AMENDED_DN" if amended_from else "DUPLICATE_DN_FOR_INVOICED_ITEM"
	frappe.log_error(title="DN Ingest — {0}".format(subject), message=dup_msg)

	try:
		frappe.get_doc(
			{
				"doctype": "Issue",
				"subject": subject,
				"description": (
					"Amendment race: {0} (amended_from={1}). Ops: cancel the redundant amendment.".format(dup_msg, amended_from)
					if amended_from
					else dup_msg
				),
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		# Issue filing is best-effort; never fail the ingest because of it.
		frappe.log_error(
			title="DN Ingest — Issue filing failed",
			message=frappe.get_traceback(),
		)


def _sync_hasura(invoiced_item_id, dn_code, status):
	"""Write delivery_note_erp_code + erp_sync_status back to Hasura.

	`status` is one of COMPLETED / FAILED / PENDING (matches the erp-functions
	ERP_SYNC_STATUS enum). dn_code is set only when known.
	"""
	import requests

	endpoint = frappe.conf.get("hasura_endpoint")
	secret = frappe.conf.get("hasura_admin_secret")
	if not endpoint or not secret:
		frappe.log_error(
			title="DN Ingest — Hasura creds missing",
			message="Set hasura_endpoint + hasura_admin_secret in site config. "
			"invoiced_item={0} dn={1} status={2}".format(invoiced_item_id, dn_code, status),
		)
		return

	_set = {"erp_sync_status": status}
	if dn_code:
		_set["delivery_note_erp_code"] = dn_code

	query = (
		"mutation($id: uuid!, $_set: invoiced_item_set_input!) {"
		"  update_invoiced_item_by_pk(pk_columns: {id: $id}, _set: $_set) {"
		"    id delivery_note_erp_code erp_sync_status"
		"  }"
		"}"
	)

	try:
		resp = requests.post(
			endpoint,
			json={"query": query, "variables": {"id": invoiced_item_id, "_set": _set}},
			headers={
				"Content-Type": "application/json",
				"x-hasura-admin-secret": secret,
			},
			timeout=30,
		)
		resp.raise_for_status()
		data = resp.json()
		if data.get("errors"):
			frappe.log_error(
				title="DN Ingest — Hasura writeback errors",
				message="invoiced_item={0}\n{1}".format(invoiced_item_id, json.dumps(data["errors"])),
			)
	except Exception:
		frappe.log_error(
			title="DN Ingest — Hasura writeback failed",
			message="invoiced_item={0} dn={1} status={2}\n{3}".format(
				invoiced_item_id, dn_code, status, frappe.get_traceback()
			),
		)
