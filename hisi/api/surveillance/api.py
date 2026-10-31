import json
import time

import frappe
from frappe.rate_limiter import rate_limit

from hisi.api import patient
from hisi.api.patient import http_get, join_url, simulation_base_url
from hisi.api.response_handler import APIResponse, Forbidden, NotFound, api_endpoint
from hisi.api.surveillance.audit import log_event
from hisi.api.surveillance.scope import patient_in_scope, requested_identifier, requested_visit_id, visit_in_scope
from hisi.api.surveillance.session import require_session, scope_lines

STEP_EVENTS = {
	"patient": "patient_viewed",
	"eligibility": "eligibility_viewed",
	"consent": "consent_viewed",
	"records": "records_viewed",
}


def duration_ms(start):
	return int((time.time() - start) * 1000)


def as_list(raw):
	if raw in (None, ""):
		return None
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return None
	return raw if isinstance(raw, list) else None


def as_dict(raw):
	if raw in (None, ""):
		return None
	if isinstance(raw, str):
		try:
			raw = json.loads(raw)
		except ValueError:
			return None
	return raw if isinstance(raw, dict) else None


def append_surveiled_refs(session_name, refs):
	if not refs:
		return
	current = frappe.db.get_value("HISI Surveillance Session", session_name, "surveiled_refs")
	rows = json.loads(current or "[]")
	for ref in refs:
		rows.append(ref)
	frappe.db.set_value("HISI Surveillance Session", session_name, "surveiled_refs", json.dumps(rows))


def facility_view(facility_name):
	rows = frappe.get_all(
		"HISI Health Facility",
		filters={"name": facility_name},
		fields=["name", "facility_code", "county", "level", "operational_status", "license", "latitude", "longitude"],
		limit=1,
	)
	if not rows:
		return None
	row = rows[0]
	# radius / geofence internals deliberately excluded (spec §3.6)
	return {
		"name": row["name"],
		"facility_code": row["facility_code"],
		"county": row["county"],
		"level": row["level"],
		"operational_status": row["operational_status"],
		"license": row["license"],
		"latitude": row["latitude"],
		"longitude": row["longitude"],
	}


def practitioner_views(facility_name):
	rows = frappe.get_all(
		"HISI Professional Record",
		filters={"facility": facility_name},
		fields=["name", "cadre", "regulator", "registration_number", "facility"],
		order_by="name asc",
	)
	return [
		{
			"record_id": row["name"],
			"name": row["name"],
			"cadre": row["cadre"],
			"regulator": row["regulator"],
			"registration_number": row["registration_number"],
			"facility": row["facility"],
		}
		for row in rows
	]


def practitioner_registry_url():
	"""The DHA practitioner registry endpoint (HISI Settings), or None when
	fetch_practitioner is not configured."""
	doc = frappe.get_doc("HISI Settings")
	if doc.get("enable_uat_simulation"):
		base_url = simulation_base_url()
	else:
		base_url = (doc.get("base_url") or "").strip()
	path = (doc.get("fetch_practitioner") or "").strip()
	if not base_url or not path:
		return None
	return join_url(join_url(base_url, path), "Professional Record")


def practitioner_registry_rows(facility_name, session=None, user=None):
	"""The facility's practitioner rows from the registry (exact facility match).

	Registry-confirmed data only: local 'HISI Professional Record' rows are
	created from these. Returns a list of {name, cadre, regulator,
	registration_number} dicts - empty when the registry is unconfigured or
	has no rows; an unreachable registry logs an error event and returns []
	(the local list, possibly empty, stands)."
	"""
	url = practitioner_registry_url()
	if url is None:
		return []
	try:
		payload = http_get(
			url,
			{
				"filters": json.dumps([["facility_name", "like", "%{}%".format(facility_name)]]),
				"fields": json.dumps(["*"]),
				"limit_page_length": 20,
				"limit_start": 0,
			},
		)
	except Exception:
		if session is not None:
			log_event(session, user, "error", "practitioner", facility_name, details={"reason": "practitioner registry unreachable"}, outcome="error")
		return []
	rows = payload if isinstance(payload, list) else None
	if rows is None and isinstance(payload, dict):
		rows = payload.get("data") if isinstance(payload.get("data"), list) else []
	if not isinstance(rows, list):
		return []
	target = str(facility_name).strip().lower()
	out = []
	for row in rows:
		if not isinstance(row, dict):
			continue
		fac = str(row.get("facility_name") or row.get("facility") or "").strip()
		if fac and fac.lower() != target:
			continue
		name = str(row.get("name") or row.get("practitioner_name") or row.get("professional_name") or "").strip()
		if not name:
			continue
		out.append(
			{
				"name": name,
				"cadre": str(row.get("cadre") or "").strip() or None,
				"regulator": str(row.get("regulator") or "").strip() or None,
				"registration_number": str(row.get("registration_number") or row.get("registration_no") or "").strip() or None,
			}
		)
	return out


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=60, seconds=60)
@api_endpoint
def surveil_patient(session_id, steps=None, filters=None):
	start = time.time()
	user = frappe.session.user
	ctx = require_session(session_id, touch=True)
	row = ctx["session"]
	mandate = ctx["mandate"]
	steps = as_list(steps)
	filters = as_dict(filters)
	lines = scope_lines(mandate.name)
	identifier = requested_identifier(filters)
	visit_id = requested_visit_id(filters)
	if identifier and not patient_in_scope(lines, identifier):
		log_event(row["name"], user, "scope_denied", "patient", identifier, details={"reason": "patient identifier outside mandate scope", "steps": steps}, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("The requested patient is outside the scope of this mandate.")
	if not visit_in_scope(lines, visit_id):
		log_event(row["name"], user, "scope_denied", "visit", visit_id, details={"reason": "visit outside mandate scope", "steps": steps}, outcome="denied", duration_ms=duration_ms(start))
		raise Forbidden("The requested visit is outside the scope of this mandate.")
	patient.patient_flow({"steps": steps, "filters": filters})
	response = frappe.local.response
	if response.get("status") != "success":
		# patient_flow's own wrapper already wrote the error envelope; record the
		# one-event-per-call invariant and leave the envelope untouched
		log_event(row["name"], user, "error", "patient", identifier, details={"code": response.get("code"), "message": response.get("message"), "steps": steps}, outcome="error", duration_ms=duration_ms(start))
		return None
	data = response.get("data") or {}
	now = frappe.utils.now()
	refs = []
	if identifier:
		refs.append({"type": "patient", "ref": identifier, "at": now})
	if visit_id:
		refs.append({"type": "visit", "ref": visit_id, "at": now})
	append_surveiled_refs(row["name"], refs)
	for step in data.get("steps") or []:
		event_type = STEP_EVENTS.get(step)
		if event_type:
			log_event(row["name"], user, event_type, "patient", identifier, details={"step": step, "steps": data.get("steps")}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(data=data)


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=300, seconds=60)
@api_endpoint
def get_facility(session_id):
	start = time.time()
	user = frappe.session.user
	ctx = require_session(session_id, touch=True)
	row = ctx["session"]
	mandate = ctx["mandate"]
	view = facility_view(mandate.facility)
	if view is None:
		log_event(row["name"], user, "error", "facility", mandate.facility, details={"reason": "facility row missing"}, outcome="error", duration_ms=duration_ms(start))
		raise NotFound("The facility record is missing.")
	log_event(row["name"], user, "facility_viewed", "facility", view["name"], outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(data=view)


@frappe.whitelist(methods=["GET"])
@rate_limit(limit=300, seconds=60)
@api_endpoint
def list_practitioners(session_id):
	start = time.time()
	user = frappe.session.user
	ctx = require_session(session_id, touch=True)
	row = ctx["session"]
	mandate = ctx["mandate"]
	practitioners = practitioner_views(mandate.facility)
	if not practitioners:
		# The facility has no local rows yet: confirm the practitioners from the
		# registry and register them (registry-confirmed data only; shared across
		# companies - the next company's session reuses the local rows).
		created = 0
		for spec in practitioner_registry_rows(mandate.facility, session=row["session_no"], user=user):
			if not frappe.db.exists("HISI Professional Record", spec["name"]):
				frappe.get_doc(
					{
						"doctype": "HISI Professional Record",
						"name": spec["name"],
						"cadre": spec["cadre"],
						"regulator": spec["regulator"],
						"registration_number": spec["registration_number"],
						"facility": mandate.facility,
						"registered_by": mandate.company,
					}
				).insert(ignore_permissions=True)
				created += 1
		if created:
			log_event(row["name"], user, "practitioner_registered", "practitioner", mandate.facility, details={"count": created}, outcome="ok")
		practitioners = practitioner_views(mandate.facility)
	now = frappe.utils.now()
	append_surveiled_refs(row["name"], [{"type": "practitioner", "ref": item["record_id"], "at": now} for item in practitioners])
	log_event(row["name"], user, "practitioner_viewed", "practitioner", mandate.facility, details={"count": len(practitioners)}, outcome="ok", duration_ms=duration_ms(start))
	return APIResponse.success(data={"facility": mandate.facility, "practitioners": practitioners})
